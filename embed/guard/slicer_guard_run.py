"""The runtime guard's driver. Stdlib, Slicer and the talk2view wheel only.

It runs the built guard page (``npm run build:guard``) inside official 3D Slicer, served by
the real ``UIServer`` over ``talk2view.ui.testing.FakeController``, and judges the results
the page hands back. Run by Slicer, never by a plain python::

    Slicer --testing --no-splash --python-script slicer_guard_run.py

Configuration comes from the environment (arguments would have to survive the Slicer
launcher's own parsing):

    T2V_GUARD_OUT        every output file (default: ./guard-out next to this file)
    T2V_GUARD_DIST       the built guard page (default: ../dist-guard)
    T2V_GUARD_SITE       where the talk2view wheel is installed (``pip install --target``),
                         put first on sys.path (default: none: Slicer's own site-packages)
    T2V_GUARD_TIMEOUT_S  hard limit for the whole run (default: 300)
    T2V_GUARD_EXPECT     the Chromium major the judge insists on (default: 83)
    T2V_GUARD_LABEL      the name used in the report (default: slicer)

Outputs in T2V_GUARD_OUT: guard-results.json (what the page sent, plus the server's side),
driver.log, ci-run.json, ci-summary.md, widget.png, screen.png, slicer.log; and
hang-traceback.txt (every thread's Python stack) only when the run hung.

Exit codes: 0 pass, 1 judged fail, 3 timed out waiting for the results, 4 the driver or the
judge raised (including finish() itself, and a talk2view imported from anywhere but
T2V_GUARD_SITE), 5 the Qt event loop was wedged (watchdog thread).

Harness rules (what focus-only reruns in real Slicer taught): Chromium matches :focus
only in a focused window. Under Xvfb (Linux) there is no window manager, so the driver
activates the guard window itself; on Windows it leaves focus to the system, because every
explicit activation broke page focus there; and it never calls QApplication.setActiveWindow.
"""

import contextlib
import faulthandler
import json
import os
import queue
import shutil
import sys
import threading
import time
import traceback

import qt
import slicer

HERE = os.path.dirname(os.path.abspath(globals().get("__file__") or sys.argv[0]))
sys.path.insert(0, HERE)  # guard_judge, before anything can raise and want it
OUT = os.path.abspath(os.environ.get("T2V_GUARD_OUT") or os.path.join(HERE, "guard-out"))
DIST = os.path.abspath(os.environ.get("T2V_GUARD_DIST") or os.path.join(HERE, "..", "dist-guard"))
SITE = os.environ.get("T2V_GUARD_SITE") or ""
#: The page gives its own self-test 100 s per load and reports what it has when that runs
#: out, so the driver's deadline sits far enough above that the PAGE's watchdog is what
#: reports a slow run. A slow CI runner makes the page slow, never fast: healthy runs
#: measured 13-39 s at 16-20x CPU throttle, and a pathological load-then-reload is bounded
#: at roughly 230 s. Below that the driver would report its own timeout with nothing in it.
TIMEOUT_S = float(os.environ.get("T2V_GUARD_TIMEOUT_S", "300"))
EXPECT = int(os.environ.get("T2V_GUARD_EXPECT", "83"))
LABEL = os.environ.get("T2V_GUARD_LABEL", "slicer")
RESULTS = os.path.join(OUT, "guard-results.json")
OUTPUTS = (
    "guard-results.json",
    "ci-run.json",
    "ci-summary.md",
    "driver.log",
    "widget.png",
    "screen.png",
    "slicer.log",
    "hang-traceback.txt",
)
POLL_MS = 500
#: After the first report the driver keeps listening before it judges, so a SECOND report
#: is counted rather than missed (the judge fails on more than one). The longest honest
#: gap is the page's own retry: deliver() sends the payload again when the first copy's
#: result has not come back within 15 s (src/guard/index.ts DELIVER_MS), 15.5 s later.
#: This also gives the page far more than the time it needs to paint for the screenshot.
LISTEN_MS = 20000
EVAL_MS = 3000  # the web view answers evalJS asynchronously: the answer's own window
HANG_DUMP_S = TIMEOUT_S + 55.0  # 5 s before the watchdog's hard exit
FOCUS_KEEP_S = 15.0  # the page's focus-ring probe waits up to 5 s for a focused window
#: What the page's guard prefixes its results with (src/guard/index.ts RESULTS_MARKER).
RESULTS_MARKER = "t2v-guard-results:"
#: Where the page also leaves the payload, for a driver no command could reach.
WINDOW_KEY = "__t2vGuardResults"
#: How the results got here, for the judge (guard_judge.VIA_*). Only the first is a pass.
VIA_HOST = "clipboard_set"
VIA_EVAL = "evaljs"
VIA_CONSOLE = "console"

STATE = {
    "started": time.monotonic(),
    "server": None,
    "controller": None,
    "host": None,
    "widget": None,
    "stalls": None,
    "poll": None,
    "focus": None,
    "focus_until": 0.0,
    "focus_errors": set(),
    "hang": None,
    "finishing": False,
}


def log(text):
    """Log to stdout and driver.log. Two rules, because the page's text reaches here (its
    userAgent, an exception's message) and the CI step prints this output, where a line
    that starts `::` is a workflow command: EVERY physical line carries the driver's own
    prefix (splitlines() breaks on CR, U+2028 and the rest, a superset of what the runner
    breaks on), and logging never raises, since it runs inside every except handler."""
    try:
        stamp = "%s [guard] " % time.strftime("%H:%M:%S")
        # backslashreplace: a lone surrogate from the page would not encode as UTF-8.
        text = str(text).encode("utf-8", "backslashreplace").decode("utf-8")
        # The runner obeys `##[` anywhere in a line, not only at its start.
        text = text.replace("##[", "##\\[")
        lines = [stamp + part for part in (text.splitlines() or [""])]
    except Exception:
        lines = ["[guard] (a log line that could not be printed)"]
    for line in lines:
        with contextlib.suppress(Exception):  # a console that cannot print must not stop a run
            print(line)
    with contextlib.suppress(Exception):  # nor an OUT that cannot be written
        with open(os.path.join(OUT, "driver.log"), "a", encoding="utf-8") as fh:
            fh.write("".join(line + "\n" for line in lines))


def write_out(name, text):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as fh:
        fh.write(text)


def read_results():
    """The parsed results file, or None while it is absent or half-written."""
    try:
        with open(RESULTS, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def parse_payload(text):
    """The GuardResults object inside `text`, wherever the marker sits in it, or None.

    One rule for all three ways the payload can reach the driver: the `copy` command, the
    window, and a console line in Slicer's log, which carries a timestamp in front and
    whatever the logger appended behind. `raw_decode` reads the first JSON value and
    ignores the rest, and the FIRST marker wins, so a second report never displaces it.
    """
    if not isinstance(text, str):
        return None
    start = text.find(RESULTS_MARKER)
    if start == -1:
        return None
    try:
        value, _end = json.JSONDecoder().raw_decode(text, start + len(RESULTS_MARKER))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def write_record(page, via, count):
    """Write guard-results.json: what the page sent, and the server's own side of it."""
    controller, host = STATE["controller"], STATE["host"]
    server_reply = ""
    if controller is not None:
        try:
            # A report arriving during teardown must still land on disk: a reply the
            # driver could not read makes contentMatches false, which is honest, where
            # losing the whole record would leave nothing to read at all.
            snapshot = controller.snapshot()
            replies = [m for m in snapshot["messages"] if m["role"] == "assistant"]
            server_reply = replies[-1]["content"] if replies else ""
        except Exception:
            log("the controller could not be read for contentMatches:\n" + traceback.format_exc())
    record = {
        "page": page,
        "server": {
            "hostCalls": list(host.calls) if host is not None else [],
            "contentMatches": page.get("lastReplyText") == server_reply,
            "serverReplyLength": len(server_reply),
            # How many reports arrived, and how this one got here: both are judged. The
            # page's way home is part of what the guard tests, so a payload the driver had
            # to dig out is evidence about a failure, never a way to pass.
            "resultsMessages": count,
            "resultsVia": via,
            "mainThreadStalls": STATE["stalls"].report() if STATE["stalls"] else None,
        },
    }
    tmp = RESULTS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
    os.replace(tmp, RESULTS)
    log(
        "results written via %s (message %d, contentMatches=%s)"
        % (via, count, record["server"]["contentMatches"])
    )


class StallMeter:
    """A 50 ms main-thread QTimer. A late tick means Slicer's UI thread stalled (reported only)."""

    def __init__(self, interval_ms=50):
        self.interval_ms = interval_ms
        self.max_gap_ms = 0.0
        self.over_250ms = 0
        self.ticks = 0
        self._last = time.monotonic()
        self._timer = qt.QTimer()
        self._timer.setInterval(interval_ms)
        self._timer.connect("timeout()", self._tick)
        self._timer.start()

    def _tick(self):
        now = time.monotonic()
        gap = (now - self._last) * 1000.0
        self._last = now
        self.ticks += 1
        self.max_gap_ms = max(self.max_gap_ms, gap)
        if gap > 250.0:
            self.over_250ms += 1

    def report(self):
        return {
            "intervalMs": self.interval_ms,
            "ticks": self.ticks,
            "maxGapMs": round(self.max_gap_ms, 1),
            "gapsOver250ms": self.over_250ms,
        }

    def stop(self):
        self._timer.stop()


class GuardHost:
    """The talk2view.ui HostAdapter for the guard: a queue drained by a main-thread QTimer
    (a QTimer.singleShot started from another thread never fires), the guard window as the
    web view, and a clipboard that receives the page's results."""

    name = "slicer-guard"
    supports_clipboard = True

    def __init__(self, Theme):
        self._theme = Theme(mode="light")
        self._main = threading.get_ident()
        self._queue = queue.Queue()
        self.calls = []  # [{"host": "open_external" | "clipboard_set", "value": str}]
        #: How many messages carried the results marker, and the first one's payload. A
        #: self-test whose budget ran out sends what it has and the run it gave up on can
        #: still reload and finish behind it, so a second report is possible: the FIRST is
        #: the one judged, and the count itself is a failure the judge reports.
        self.results_messages = 0
        self.first_page = None
        self._timer = qt.QTimer()
        self._timer.setInterval(20)
        self._timer.connect("timeout()", self._drain)
        self._timer.start()

    def _drain(self):
        while True:
            try:
                fn = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception:
                log("a posted call raised:\n" + traceback.format_exc())

    def stop(self):
        self._timer.stop()

    def post(self, fn):
        self._queue.put(fn)

    def is_main_thread(self):
        return threading.get_ident() == self._main

    def show(self, url):
        STATE["widget"].setUrl(url + "&auto=1")  # QString slot; the page strips the hash

    def open_external(self, url):
        self.calls.append({"host": "open_external", "value": url})

    def clipboard_set(self, text):
        if not text.startswith(RESULTS_MARKER):
            self.calls.append({"host": "clipboard_set", "value": text})
            return
        self.results_messages += 1
        if self.results_messages > 1:
            log(
                "a SECOND results message arrived (%d so far): the FIRST is the one judged"
                % self.results_messages
            )
        if self.first_page is None:
            page = parse_payload(text)
            if page is None:
                log("a results message did not parse as JSON; recording it as a host call")
                self.calls.append({"host": "clipboard_set", "value": text[:400]})
                return
            self.first_page = page
        write_record(self.first_page, VIA_HOST, self.results_messages)

    def theme(self):
        return self._theme

    def storage(self):
        return None

    def audio(self):
        return None


def grab(widget, name):
    """QWidget.grab() -> QPixmap.save(). Returns the file name, or a reason."""
    try:
        ok = widget.grab().save(os.path.join(OUT, name))
        return name if ok else "save() returned False"
    except Exception as exc:
        return "failed: %r" % (exc,)


def focus_guard_window():
    """Raise and activate the guard window and focus its web view, as a desktop window
    manager does when a window opens (Xvfb has none). Not on Windows: see the module doc."""
    widget = STATE["widget"]
    if widget is None or sys.platform == "win32":
        return

    def attempt(what, call):
        try:  # each step on its own: one that fails must not skip the others
            call()
        except Exception as exc:
            if what not in STATE["focus_errors"]:
                STATE["focus_errors"].add(what)
                log("focus step %s failed: %r" % (what, exc))

    attempt("raise", widget.raise_)
    attempt("activateWindow", widget.activateWindow)
    attempt("webView.setFocus", lambda: widget.webView().setFocus())


def keep_focus():
    if STATE["finishing"] or time.monotonic() > STATE["focus_until"]:
        timer, STATE["focus"] = STATE["focus"], None
        if timer is not None:
            timer.stop()
        return
    focus_guard_window()


def on_eval_result(_js, result):
    """The web view's answer to the evalJS below. Best effort: a page that never got its
    report home is already a failed run, and this only makes the failure legible."""
    try:
        if read_results() is not None:
            return  # the host got them after all; that report is the one that counts
        page = parse_payload(str(result or ""))
        if page is None:
            log("window.%s held nothing the driver could parse" % WINDOW_KEY)
            return
        write_record(page, VIA_EVAL, 1)
    except Exception:
        log(traceback.format_exc())


def ask_the_page_for_its_results():
    """Last resort 1: read window.__t2vGuardResults out of the web view.

    The page sets it before it tries to send anything, so a run whose `copy` command never
    reached the host still has its report in there. Returns True when the request went
    out, so the caller knows to wait for the answer. Every part of this is best effort:
    ``evalJS`` is asynchronous and its signal differs between Slicer builds, and losing it
    costs evidence about a run that has already failed, never a verdict.
    """
    widget = STATE["widget"]
    if widget is None:
        return False
    try:
        widget.connect("evalResult(QString,QString)", on_eval_result)
        widget.evalJS("String(window.%s || '')" % WINDOW_KEY)
        return True
    except Exception:
        log("could not ask the page for its results:\n" + traceback.format_exc())
        return False


def recover_from_the_console():
    """Last resort 2: the page writes the payload to console.error when no command could
    carry it, and the widget logs every console message into Slicer's own log."""
    try:
        path = slicer.app.errorLogModel().filePath
        if not path or not os.path.isfile(path):
            return
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception:
        log(traceback.format_exc())
        return
    page = parse_payload(text)
    if page is None:
        log("no results marker in the Slicer log either")
        return
    write_record(page, VIA_CONSOLE, 1)


def disarm_hang_dump():
    faulthandler.cancel_dump_traceback_later()
    hang, STATE["hang"] = STATE["hang"], None
    if hang is not None:
        hang.close()
        if os.path.getsize(hang.name) == 0:
            os.remove(hang.name)  # nothing hung: leave no empty file behind


def finish(code, reason):
    if STATE["finishing"]:
        return
    STATE["finishing"] = True
    for key in ("poll", "focus"):
        if STATE[key] is not None:
            STATE[key].stop()
    log("finishing: %s (provisional exit code %d)" % (reason, code))
    if STATE["host"] is not None:
        # A report the server accepted but the 20 ms timer has not run yet is still a
        # report: count it before the record is read.
        STATE["host"]._drain()
    record = read_results()
    run = {
        "label": LABEL,
        "reason": reason,
        "exitCode": code,
        "elapsedS": round(time.monotonic() - STATE["started"], 1),
        "qtVersion": qt.qVersion(),
        "slicerVersion": slicer.app.applicationVersion,
        "slicerRevision": slicer.app.revision,
        "os": "%s %s" % (slicer.app.os, slicer.app.arch),
        "python": sys.version.split()[0],
        "userAgent": ((record or {}).get("page") or {}).get("userAgent"),
        "stalls": STATE["stalls"].report() if STATE["stalls"] else None,
        "screenshots": {},
        "slicerLog": None,
    }
    try:
        log_path = slicer.app.errorLogModel().filePath
        if log_path and os.path.isfile(log_path):
            shutil.copyfile(log_path, os.path.join(OUT, "slicer.log"))
            run["slicerLog"] = "slicer.log"
    except Exception as exc:
        run["slicerLog"] = "failed: %r" % (exc,)
    verdict = None
    if record is not None:
        # In-process verdict, on disk BEFORE the screenshots. The exit code only ever goes
        # UP here — a judged PASS never rescues a run that timed out — and the workflow
        # runs the judge again on its own, so a lost exit code cannot turn a failure green.
        try:
            import guard_judge

            verdict = guard_judge.judge(record, EXPECT)
            if verdict[0] and code == 0:
                code = 1
            write_out("ci-summary.md", guard_judge.render(LABEL, EXPECT, record, run, *verdict))
        except Exception as exc:
            log(traceback.format_exc())
            verdict, code = None, 4
            run["reason"] = "guard_judge raised %r after: %s" % (exc, reason)
    run["exitCode"] = code
    write_out("ci-run.json", json.dumps(run, indent=2))
    if STATE["widget"] is not None:
        run["screenshots"]["widget"] = grab(STATE["widget"], "widget.png")
    main_window = slicer.util.mainWindow()
    if main_window is not None:
        run["screenshots"]["mainWindow"] = grab(main_window, "screen.png")
    write_out("ci-run.json", json.dumps(run, indent=2))
    if verdict is not None:
        try:
            write_out("ci-summary.md", guard_judge.render(LABEL, EXPECT, record, run, *verdict))
        except Exception:
            log(traceback.format_exc())
    log("userAgent=%s qt=%s exit=%d" % (run["userAgent"], run["qtVersion"], code))
    for key, method in (
        ("server", "stop"),
        ("controller", "close"),
        ("host", "stop"),
        ("stalls", "stop"),
        ("widget", "close"),
    ):
        if STATE[key] is not None:
            try:
                getattr(STATE[key], method)()
            except Exception:
                log(traceback.format_exc())
    disarm_hang_dump()
    slicer.util.exit(code)


def end_run(code, reason):
    """finish(), and when finish itself raises, end the run anyway: the watchdog would, but
    only after its full timeout and with nothing on disk to say why. Exit 4, the code for
    "the driver raised", whatever the provisional code was."""
    try:
        finish(code, reason)
    except Exception as exc:
        log("finish() raised while ending the run (%s):\n%s" % (reason, traceback.format_exc()))
        with contextlib.suppress(Exception):
            record_raised_finish(reason, exc)
        try:
            disarm_hang_dump()
            slicer.util.exit(4)
        except Exception:
            os._exit(4)


def record_raised_finish(reason, exc):
    """ci-run.json for a run whose finish() raised. The workflow and anyone reading
    the artifacts look there first, so there is always one, and it never claims an exit
    code this run does not end with: whatever finish() wrote is kept, with exit code 4 and
    the reason why."""
    run = {}
    with contextlib.suppress(Exception):
        with open(os.path.join(OUT, "ci-run.json"), encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            run = loaded
    run["label"] = LABEL
    run["exitCode"] = 4
    run["reason"] = "finish() raised %r while ending the run (%s)" % (exc, reason)
    with contextlib.suppress(Exception):
        run.setdefault("elapsedS", round(time.monotonic() - STATE["started"], 1))
    write_out("ci-run.json", json.dumps(run, indent=2))


def give_up():
    """No results through the host, and the last resorts have had their turn."""
    if read_results() is None:
        recover_from_the_console()
    end_run(3, "timed out after %.0f s without results" % TIMEOUT_S)


def on_timed_out():
    """The page never got its report home. Before writing the run off, dig for it: a
    recovered payload says WHAT broke, where a bare timeout says only that something did.
    The run fails either way — the judge sees how the results arrived."""
    STATE["poll"].stop()
    log("no results after %.0f s; asking the page for window.%s" % (TIMEOUT_S, WINDOW_KEY))
    if ask_the_page_for_its_results():
        qt.QTimer.singleShot(EVAL_MS, give_up)  # main thread: this one does fire
    else:
        give_up()


def tick():
    try:
        if read_results() is not None:
            STATE["poll"].stop()
            log("results are in; listening %d ms for a second report" % LISTEN_MS)
            qt.QTimer.singleShot(LISTEN_MS, lambda: end_run(0, "results written"))
        elif time.monotonic() - STATE["started"] > TIMEOUT_S:
            on_timed_out()
    except Exception:
        log(traceback.format_exc())
        end_run(4, "driver raised in tick()")


def is_inside(path, directory):
    """True when `path` is inside `directory`, after resolving links; case-insensitive on
    Windows. Paths on two different drives are not inside one another."""
    try:
        path = os.path.normcase(os.path.realpath(path))
        directory = os.path.normcase(os.path.realpath(directory))
        return os.path.commonpath([path, directory]) == directory
    except ValueError:
        return False


def watchdog():
    # A plain thread: fires even when a modal dialog or a hung page has wedged the Qt event
    # loop, where no QTimer would ever tick again.
    time.sleep(TIMEOUT_S + 60.0)
    try:
        log("WATCHDOG: the Qt event loop never finished the run; hard exit 5")
    finally:
        os._exit(5)


def main():
    # First, before anything can raise and before --testing is told not to quit: from here
    # on, whatever happens (an exception, a failing except handler, a wedged event loop),
    # something ends the run.
    threading.Thread(target=watchdog, name="t2v-guard-watchdog", daemon=True).start()
    os.makedirs(OUT, exist_ok=True)
    for stale in OUTPUTS:
        if os.path.exists(os.path.join(OUT, stale)):
            os.remove(os.path.join(OUT, stale))
    STATE["hang"] = open(os.path.join(OUT, "hang-traceback.txt"), "w", encoding="utf-8")
    faulthandler.dump_traceback_later(HANG_DUMP_S, repeat=False, file=STATE["hang"], exit=False)
    # --testing quits the moment this file returns; the run is asynchronous.
    slicer.app.commandOptions().runPythonAndExit = False
    log(
        "Slicer %s r%s, Qt %s, Python %s, out=%s"
        % (
            slicer.app.applicationVersion,
            slicer.app.revision,
            qt.qVersion(),
            sys.version.split()[0],
            OUT,
        )
    )
    if not os.path.isfile(os.path.join(DIST, "index.html")):
        raise RuntimeError("no index.html in %s: run `npm run build:guard`" % DIST)
    if slicer.util.mainWindow() is None:
        raise RuntimeError("no main window: do not pass --no-main-window")

    if SITE:
        sys.path.insert(0, os.path.abspath(SITE))
    import talk2view
    from talk2view.ui import Theme, UIServer
    from talk2view.ui.options import ChatOptions, ChatWelcome, ToolPresentation
    from talk2view.ui.testing import DEFAULT_REPLY, FakeController

    log(
        "talk2view %s from %s"
        % (getattr(talk2view, "__version__", "?"), os.path.dirname(talk2view.__file__))
    )
    if SITE and not is_inside(talk2view.__file__, SITE):
        # A stale copy in Slicer's own site-packages must never be what passed.
        raise RuntimeError(
            "talk2view was imported from %s, not from T2V_GUARD_SITE (%s): this run "
            "would test another copy than the wheel it was given"
            % (os.path.dirname(talk2view.__file__), os.path.abspath(SITE))
        )

    # The reply embed/e2e/backend.py serves too: every Markdown element the
    # page's markdown-dom step checks, streamed twice for a longer stream.
    reply = DEFAULT_REPLY + (
        "\n> Check the window/level preset before you threshold.\n\n"
        "More in [the Slicer docs](https://slicer.readthedocs.io/en/latest/).\n"
    )
    host = GuardHost(Theme)
    STATE["host"] = host
    controller = FakeController(reply=reply, repeat=2, chunk_delay_s=0.02)
    STATE["controller"] = controller
    options = ChatOptions(
        welcome=ChatWelcome(heading="Ask about your scene", suggestions=["Load MRHead"]),
        footer="Answers can be wrong. Check them before you act.",
        tools={
            # Not destructive: the scripted run clicks "Always allow", which the page never
            # offers for a destructive tool. The rule itself is the policy:always-allow
            # probe, and the card's destructive shape is an e2e spec.
            "load_sample_data": ToolPresentation(activity="Loading sample data")
        },
    )
    server = UIServer(controller, host, static_dir=DIST, options=options)
    server.start()
    STATE["server"] = server

    widget = slicer.qSlicerWebWidget()
    widget.setWindowTitle("Talk2View runtime guard (CI)")
    widget.resize(480, 800)
    widget.javaScriptConsoleMessageLoggingEnabled = True  # page console -> slicer.log
    STATE["widget"] = widget
    server.show()  # posts host.show(url): the page loads on the next drain
    widget.show()
    STATE["stalls"] = StallMeter()
    focus_guard_window()
    STATE["focus_until"] = time.monotonic() + FOCUS_KEEP_S
    focus = qt.QTimer()
    focus.setInterval(POLL_MS)
    focus.connect("timeout()", keep_focus)
    focus.start()
    STATE["focus"] = focus  # keep a reference or PythonQt collects the timer
    log("guard page on port %d, auto-run requested" % server.port)

    poll = qt.QTimer()
    poll.setInterval(POLL_MS)
    poll.connect("timeout()", tick)
    poll.start()
    STATE["poll"] = poll


try:
    main()
except Exception as exc:
    # log() copes with an OUT it cannot write, and end_run() with a finish() that fails.
    with contextlib.suppress(Exception):
        os.makedirs(OUT, exist_ok=True)
    log(traceback.format_exc())
    end_run(4, "driver raised in main(): %s" % (exc,))
