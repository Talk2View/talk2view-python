"""Judge one runtime-guard run. Standard library only.

    python guard_judge.py --out <dir> --label linux --expect-chromium 83 [--summary <file>]

Reads <dir>/guard-results.json (written by slicer_guard_run.py's host) and <dir>/ci-run.json.
Prints a Markdown report, appends it to --summary (pass "$GITHUB_STEP_SUMMARY" in CI) and
exits 0 (pass) or 1 (fail). The run fails on any probe that FAILs, any scripted step that is
not PASS or out of order, any uncaught error, CSP violation or seq gap (before the scripted
reload and after it), a reply the page and the server disagree on, a host call the page did
not make, a set of results that did not come home the way the page really sends them, or a
userAgent that is no longer Chrome/<expect>: a Slicer release on a newer Qt retires the
Chromium 83 constraint on purpose, by failing here.

Everything in a results file came from a web page, so none of it is trusted: whatever it
holds, the judge answers with exit 0 or 1 and a report, never a traceback, and every piece
of it that reaches the report goes through `clean()` first (the report is printed to a CI
log, where a line starting `::` is a workflow command, and appended to a job summary, which
renders Markdown and HTML).

Imported by slicer_guard_run.py for the in-process verdict, so it runs on Slicer's Python
3.12 and on Python 3.10+.
"""

import argparse
import collections
import json
import os
import re
import sys
import unicodedata

REQUIRED_PROBES = (
    "storage",
    "font",
    "css:custom-properties",
    "css:flex-margins",
    "css:shimmer",
    "css:composer-row",
    "css:focus-ring",
    "policy:always-allow",
    "sse:fetch-stream",
    "sse:eventsource",
)
MIN_API_PROBES = 12  # distinct api: ids
#: The scripted run's steps, in the order src/guard/autorun.ts runs them. Order is judged:
#: the run drives one control after another through states each step leaves for the next,
#: so a report whose steps arrived in another order did not run the scenario at all.
REQUIRED_AUTO_STEPS = (
    "auto:connected",
    "auto:send",
    "auto:sticky",
    "auto:stream",
    "auto:markdown-dom",
    "auto:link",
    "auto:copy",
    "auto:approval-card",
    "auto:allow-once",
    "auto:deny",
    "auto:edit-arguments",
    "auto:always-allow",
    "auto:cancel-stream",
    "auto:cancel-approval",
    "auto:error-retry",
    "auto:views",
    "auto:new-chat",
    "auto:reload",
    "auto:guest-limit",
)
#: The only statuses the page emits (probes.ts ProbeStatus, autorun.ts AutoStatus).
#: Anything else means the page and this judge no longer agree on what a result is.
PROBE_STATUSES = ("PASS", "FAIL", "INFO")
STEP_STATUSES = ("PASS", "FAIL")
# Counters that must be the integer 0, before the reload (page.auto.beforeReload) and after
# it (page.live, page.live.stats).
COUNTERS = (
    ("errorCount", "uncaught errors"),
    ("cspViolations", "CSP violations"),
    ("gaps", "seq gaps"),
)
#: What the scripted run's link and Copy steps must have asked the host for.
LINK = "https://slicer.readthedocs.io/en/latest/"
COPIED_PREFIX = "## Segmentation plan"
#: The payload shape src/guard/index.ts sends (GuardResults.schema).
SCHEMA = 1
#: How the results reached the driver (server.resultsVia). Only the first is a pass: the
#: page's way home is the `copy` command through the host, and that path is part of what
#: the guard tests. The other two are the driver digging the payload out of a page that
#: could not send it, which is evidence about a failure, never a way to pass.
VIA_HOST = "clipboard_set"
VIA_EVAL = "evaljs"
VIA_CONSOLE = "console"
#: `gap` is banned in the page's CSS because Chromium 83 does not support it. The probe is
#: informational, but on 83 its answer is a fact about the browser: if it ever reads
#: "supported" there, either the ban's reason is gone or the probe stopped measuring.
GAP_PROBE = "css:flex-gap"
NO_GAP = "NOT supported"
#: `:focus` matches only inside a focused window, so the focus-ring probe records whether
#: it had one. Without it the probe proves nothing and the driver failed to raise the
#: window.
FOCUS_PROBE = "css:focus-ring"
FOCUSED = "document.hasFocus true"
#: A step that had to clean up after an earlier failure says so here. It is still a PASS.
RECOVERED = "[recovered:"
#: One `Chrome/<major>.` token, not the tail of another product name ("HeadlessChrome/"),
#: and a major of at most six digits (a longer run is not a version, and int() would
#: refuse a long enough one). Every QtWebEngine userAgent seen in real Slicer has exactly one:
#: "... QtWebEngine/5.15.2 Chrome/83.0.4103.122 Safari/537.36".
CHROME_TOKEN = re.compile(r"(?<![A-Za-z])Chrome/(\d{1,6})\.")
HEADLESS = "HeadlessChrome/"

# ── Printing untrusted text ──
#: A cell shows at most this many characters of what the page sent; a list line (a
#: problem or a note) at most LINE_LIMIT; the whole report at most REPORT_LIMIT, well under
#: the 1 MiB GitHub accepts for a step summary.
CELL_LIMIT = 300
LINE_LIMIT = 600
REPORT_LIMIT = 100000
#: A message is built from at most this much of any one value before it is cleaned.
TEXT_LIMIT = 2000
TRUNCATED = chr(0x2026) + "(truncated)"
BACKSLASH = chr(92)
#: Markdown and HTML that must print as text: a pipe would end a table cell, a backslash
#: would un-escape the pipe after it, a backtick opens code, brackets open a link or an
#: image, and <, > and & open HTML (an <img> in a job summary loads from anywhere).
ESCAPES = {
    BACKSLASH: BACKSLASH * 2,
    "|": BACKSLASH + "|",
    "`": BACKSLASH + "`",
    "[": BACKSLASH + "[",
    "]": BACKSLASH + "]",
    "<": "&lt;",
    ">": "&gt;",
    "&": "&amp;",
}
#: Control, format, surrogate and line/paragraph-separator characters. Every one of them
#: prints as a visible escape: a CR or U+2028 would start a new log line (and a line that
#: starts `::` is a workflow command), a bidi override would reorder what a reader sees,
#: and a lone surrogate cannot be written as UTF-8 at all.
INVISIBLE = ("Cc", "Cf", "Cs", "Zl", "Zp")
ESCAPE_FORMAT = BACKSLASH + "u%04x"
#: How the Actions runner spots a command: a line whose start, once leading whitespace is
#: trimmed, is `::`, or `##[` anywhere in a line. It splits lines on CR as well as LF.
COMMAND_START = "::"
COMMAND_ANYWHERE = "##["


def txt(value, limit=TEXT_LIMIT):
    """str(value), whatever the value is. A value nested past the recursion limit cannot
    even be printed, and saying so is the honest answer, never an exception."""
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:  # RecursionError, or an int too long to convert
        text = "<unprintable %s>" % type(value).__name__
    return text if len(text) <= limit else text[:limit] + TRUNCATED


def brief(value, limit=80):
    """`value` as JSON, cut to `limit` characters. Types show: null, "1" and 1 differ."""
    try:
        text = json.dumps(value)
    except Exception:
        text = "<unprintable %s>" % type(value).__name__
    return text if len(text) <= limit else text[: limit - 3] + "..."


def clean(value, limit=CELL_LIMIT):
    """Untrusted text, made safe to print in one line of Markdown. The ONE place every
    piece of page text passes through on its way to stdout or a job summary: cut to
    `limit` characters, every invisible character shown as an escape, tabs and line
    breaks as spaces, and Markdown and HTML escaped."""
    text = txt(value, limit)
    out = []
    for ch in text:
        if ch in "\t\n\r":
            out.append(" ")
        elif unicodedata.category(ch) in INVISIBLE:
            out.append(ESCAPE_FORMAT % ord(ch))
        else:
            out.append(ESCAPES.get(ch, ch))
    return "".join(out)


def no_workflow_commands(text):
    """The last line of defence for stdout: no line can read as a runner command. `clean()`
    already keeps page text from forming one, so this guards against a future change to
    the report itself. It matches the runner, not a simpler model of it: a CR ends a line
    too, a line whose trimmed start is `::` gets a visible `> ` in front (a space would be
    trimmed away), and `##[` anywhere loses its bracket (`##\\[`)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    for line in lines:
        if line.lstrip().startswith(COMMAND_START):
            line = "> " + line
        out.append(line.replace(COMMAND_ANYWHERE, "##" + BACKSLASH + "["))
    return "\n".join(out)


def load_json(path):
    """(value, problem). The problem is None when the file parsed, and otherwise says why
    it did not: missing, unreadable, not UTF-8, not JSON, or nested past what the parser
    can follow. A byte-order mark is allowed (a file saved by a Windows editor has one)."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, "%s is missing" % os.path.basename(path)
    except (OSError, ValueError, RecursionError) as exc:  # ValueError covers decode errors
        return None, "%s is not readable JSON (%s: %s)" % (
            os.path.basename(path),
            type(exc).__name__,
            txt(exc, 200),
        )


def chromium_majors(user_agent):
    """Every `Chrome/<major>.` token in the userAgent, as ints."""
    if not isinstance(user_agent, str):
        return []
    return [int(m) for m in CHROME_TOKEN.findall(user_agent)]


def chromium_major(user_agent):
    """The Chromium major, or None unless the userAgent names exactly one."""
    majors = chromium_majors(user_agent)
    return majors[0] if len(majors) == 1 else None


def is_exactly(value, want):
    """The integer `want` only: not True/False, a float, a string or null."""
    return type(value) is int and value == want


def is_zero(value):
    return is_exactly(value, 0)


def shown(obj, key):
    return brief(obj[key]) if key in obj else "missing"


def errors_of(obj):
    errors = obj.get("errors")
    if isinstance(errors, list):
        errors = " | ".join(txt(e) for e in errors)
    return " - %s" % txt(errors) if errors else ""


def child(parent, key, path, problems):
    """parent[key] when it is an object, else {}. Absent or null is left to the checks on
    its contents; any other type is a problem of its own."""
    value = parent.get(key)
    if isinstance(value, dict):
        return value
    if value is not None:
        problems.append("%s is not an object: %s" % (path, brief(value)))
    return {}


def entries(parent, key, path, what, problems):
    """The object entries of the list parent[key]; everything else is a problem."""
    value = parent.get(key)
    if value is None:
        problems.append("%s is missing" % path)
        return []
    if not isinstance(value, list):
        problems.append("%s is not a list: %s" % (path, brief(value)))
        return []
    found = []
    for item in value:
        if isinstance(item, dict):
            found.append(item)
        else:
            problems.append("%s entry is not an object: %s" % (what, brief(item)))
    return found


def no_errors(parent, path, label, problems, rows):
    """`parent["errors"]` must be an empty list. The counter beside it is judged on its
    own; an empty count with a message in the list means the two disagree, and the list is
    the one that says what happened."""
    errors = parent.get("errors")
    if isinstance(errors, list) and not errors:
        rows.append(("PASS", label, "none"))
        return
    rows.append(("FAIL", label, shown(parent, "errors")))
    if not isinstance(errors, list):
        problems.append("%s.errors is not a list: %s" % (path, shown(parent, "errors")))
    else:
        problems.append("%s recorded: %s" % (label, " | ".join(txt(e) for e in errors)))


def judge_user_agent(page, expect_major, problems, rows):
    """The one browser this run happened in, from its userAgent. Returns its major, or
    None when the userAgent does not name exactly one Chromium."""
    user_agent = page.get("userAgent")
    majors = chromium_majors(user_agent)
    major = majors[0] if len(majors) == 1 else None
    rows.append(
        (
            "PASS" if major == expect_major else "FAIL",
            "Chromium major version",
            "%s (expected %s)" % (major, expect_major),
        )
    )
    if isinstance(user_agent, str) and HEADLESS in user_agent:
        problems.append(
            "userAgent is a headless Chromium, not a QtWebEngine web view: %s" % brief(user_agent)
        )
    elif not majors:
        problems.append("userAgent has no Chrome/<major>: %s" % brief(user_agent))
    elif len(majors) > 1:
        problems.append(
            "userAgent names %d Chromium versions (%s), so it cannot be read as one "
            "browser: %s" % (len(majors), ", ".join(str(m) for m in majors), brief(user_agent))
        )
    elif major != expect_major:
        problems.append(
            "userAgent is Chrome/%s, expected Chrome/%s: a different QtWebEngine "
            "than the one this guard is about" % (major, expect_major)
        )
    # The page reads the same string itself. The two disagreeing means the payload was
    # assembled from something other than this page load. (A userAgent that names no one
    # Chromium has already failed above, so there is nothing to agree with.)
    claimed = page.get("chromiumMajor")
    if major is not None and not is_exactly(claimed, major):
        problems.append(
            "page.chromiumMajor is %s but its userAgent says %s: the results do not "
            "describe one page load" % (brief(claimed), major)
        )
    return major


def judge_probes(page, major, problems, rows):
    probes = entries(page, "probes", "page.probes", "probe", problems)
    ids = [p.get("id") for p in probes if isinstance(p.get("id"), str)]
    by_id = {}  # the first report of each id; a second one is a failure of its own
    for probe_id, probe in ((p.get("id"), p) for p in probes):
        if isinstance(probe_id, str):
            by_id.setdefault(probe_id, probe)
    for probe in probes:
        probe_id, status = probe.get("id"), probe.get("status")
        label, detail = txt(probe.get("label")), txt(probe.get("detail"))
        if not isinstance(probe_id, str):
            problems.append("probe with no string id: %s" % brief(probe_id))
        if status == "FAIL":
            problems.append("probe FAIL: %s - %s" % (label, detail))
        elif probe_id in REQUIRED_PROBES and status != "PASS":
            problems.append(
                "required probe is not PASS: %s is %s - %s" % (txt(probe_id), brief(status), detail)
            )
        elif status not in PROBE_STATUSES:
            problems.append(
                "probe status is not PASS, FAIL or INFO: %s is %s - %s"
                % (brief(probe_id), brief(status), detail)
            )
        rows.append((txt(status), label, detail))
    for probe_id, count in sorted(collections.Counter(ids).items()):
        if count > 1:
            problems.append(
                "probe reported %d times: %s - the page sends each probe once, so a "
                "second answer could hide the first" % (count, probe_id)
            )
    for probe_id in REQUIRED_PROBES:
        if probe_id not in ids:
            problems.append("probe missing from the results: %s" % probe_id)
    api_count = len(set(i for i in ids if i.startswith("api:")))
    if api_count < MIN_API_PROBES:
        problems.append(
            "only %d api: probes ran (want >= %d distinct ids)" % (api_count, MIN_API_PROBES)
        )

    # The focus ring is only meaningful in a focused window: without one the probe would
    # PASS or FAIL on nothing, and the driver failed at its own job.
    focus_detail = (by_id.get(FOCUS_PROBE) or {}).get("detail")
    focused = isinstance(focus_detail, str) and FOCUSED in focus_detail
    rows.append(("PASS" if focused else "FAIL", "the window had focus", brief(focus_detail)))
    if not focused:
        problems.append(
            "%s does not say %r: the guard window never took focus, so the ring was "
            "measured on nothing - %s" % (FOCUS_PROBE, FOCUSED, brief(focus_detail))
        )

    # Only on the Chromium the ban is about. `gap` works from 84 on, so on anything newer
    # "supported" is the right answer and says nothing about 83.
    if major == 83:
        gap_detail = (by_id.get(GAP_PROBE) or {}).get("detail")
        gap_ok = isinstance(gap_detail, str) and NO_GAP in gap_detail
        rows.append(
            ("PASS" if gap_ok else "FAIL", "flexbox gap unsupported on 83", brief(gap_detail))
        )
        if not gap_ok:
            problems.append(
                "%s reads %s on Chromium 83, expected %r: either this is not the browser "
                "the CSS ban is about, or the probe stopped measuring"
                % (GAP_PROBE, brief(gap_detail), NO_GAP)
            )


def judge_steps(auto, problems, notes, rows):
    steps = entries(auto, "steps", "page.auto.steps", "scripted step", problems)
    seen = set(s.get("id") for s in steps if isinstance(s.get("id"), str))
    for step in steps:
        step_id, status, detail = step.get("id"), step.get("status"), step.get("detail")
        rows.append((txt(status), txt(step_id), "%s (%s ms)" % (txt(detail), txt(step.get("ms")))))
        if status == "FAIL":
            problems.append("scripted step FAIL: %s - %s" % (txt(step_id), txt(detail)))
        elif step_id in REQUIRED_AUTO_STEPS and status != "PASS":
            problems.append(
                "scripted step is not PASS: %s is %s - %s" % (step_id, brief(status), txt(detail))
            )
        elif status not in STEP_STATUSES:
            problems.append(
                "scripted step status is not PASS or FAIL: %s is %s"
                % (brief(step_id), brief(status))
            )
        elif step_id not in REQUIRED_AUTO_STEPS:
            problems.append(
                "scripted step this judge does not know: %s - the page's script and the "
                "judge's list of steps no longer agree" % brief(step_id)
            )
        if isinstance(detail, str) and detail.startswith(RECOVERED):
            notes.append(
                "%s had to clean up after an earlier step: %s" % (txt(step_id), txt(detail))
            )
    for step_id in REQUIRED_AUTO_STEPS:
        if step_id not in seen:
            problems.append("scripted step never ran: %s" % step_id)
    ran = [s.get("id") for s in steps if s.get("id") in REQUIRED_AUTO_STEPS]
    order_ok = ran == list(REQUIRED_AUTO_STEPS)
    rows.append(("PASS" if order_ok else "FAIL", "the 18 steps, in order", "%d ran" % len(ran)))
    if seen >= set(REQUIRED_AUTO_STEPS) and not order_ok:
        problems.append("scripted steps ran out of order or twice: %s" % brief(ran, limit=200))


def judge_auto(auto, live, problems, notes, rows):
    if not isinstance(auto, dict):
        problems.append(
            "page.auto is missing or not an object: the scripted run did not happen%s"
            % errors_of(live)
        )
        return
    judge_steps(auto, problems, notes, rows)
    # One reload, and an end: a run cut short parks its report and never comes back.
    reloads_ok = is_exactly(auto.get("reloads"), 1)
    rows.append(("PASS" if reloads_ok else "FAIL", "scripted reloads", shown(auto, "reloads")))
    if not reloads_ok:
        problems.append(
            "page.auto.reloads is %s, not 1: the mid-stream reload is one of the "
            "steps" % shown(auto, "reloads")
        )
    finished = auto.get("finishedAt")
    done = isinstance(finished, str) and bool(finished)
    rows.append(("PASS" if done else "FAIL", "the run reached its end", shown(auto, "finishedAt")))
    if not done:
        problems.append(
            "page.auto.finishedAt is %s: the scripted run never reached its end, so "
            "what arrived is a partial report" % shown(auto, "finishedAt")
        )
    # A reload resets the page's counters, so the first load's are judged here.
    before = auto.get("beforeReload")
    if not isinstance(before, dict) or not before:
        problems.append(
            "page.auto.beforeReload is missing or empty: the first page load's "
            "counters were not recorded (%s)" % brief(before)
        )
        return
    for key, label in COUNTERS:
        ok = is_zero(before.get(key))
        rows.append(("PASS" if ok else "FAIL", label + " before the reload", shown(before, key)))
        if not ok:
            problems.append(
                "%s before the reload: %s%s"
                % (label, shown(before, key), errors_of(before) if key == "errorCount" else "")
            )
    no_errors(before, "page.auto.beforeReload", "errors before the reload", problems, rows)


def judge_live(live, stats, problems, rows):
    for obj, (key, label) in zip((live, live, stats), COUNTERS):
        ok = is_zero(obj.get(key))
        rows.append(("PASS" if ok else "FAIL", label, shown(obj, key)))
        if not ok:
            problems.append(
                "%s: %s%s"
                % (label, shown(obj, key), errors_of(live) if key == "errorCount" else "")
            )
    no_errors(live, "page.live", "errors", problems, rows)

    # The page's own transport, live and never fallen back: EventSource cannot send the
    # bearer header, so a run that finished on it did not exercise what ships.
    conn = child(live, "conn", "page.live.conn", problems)
    mode, fell_back = conn.get("mode"), conn.get("fellBack")
    rows.append(
        (
            "PASS" if mode == "fetch-stream" and fell_back is False else "FAIL",
            "transport",
            "%s, fellBack=%s, reconnects=%s"
            % (brief(mode), brief(fell_back), brief(conn.get("reconnects"))),
        )
    )
    if mode != "fetch-stream":
        problems.append(
            "the live transport is %s, not fetch-stream: the page finished on the "
            "EventSource fallback" % txt(mode, 80)
        )
    if fell_back is not False:
        problems.append(
            "the live transport fell back (fellBack=%s): fetch + ReadableStream did not "
            "hold for the whole run" % brief(fell_back)
        )


def judge_server(server, problems, rows):
    matches = server.get("contentMatches")
    rows.append(("PASS" if matches is True else "FAIL", "server.contentMatches", brief(matches)))
    if matches is not True:
        problems.append("server.contentMatches is not true: the page lost or duplicated text")

    calls = server.get("hostCalls")
    calls = [c for c in calls if isinstance(c, dict)] if isinstance(calls, list) else []
    opened = any(c.get("host") == "open_external" and c.get("value") == LINK for c in calls)
    copied = any(
        c.get("host") == "clipboard_set"
        and isinstance(c.get("value"), str)
        and c["value"].startswith(COPIED_PREFIX)
        for c in calls
    )
    rows.append(("PASS" if opened else "FAIL", "host opened the reply's link", str(opened)))
    rows.append(("PASS" if copied else "FAIL", "host clipboard got the reply", str(copied)))
    if not opened:
        problems.append("the host never opened %s: the link click did not reach openLink" % LINK)
    if not copied:
        problems.append("the host clipboard never got the reply: Copy did not reach the host")

    # One report, sent the way the page really sends one. A second report means an
    # abandoned run reloaded and finished behind the first; a payload the driver had to
    # dig out of the window or the console means no command ever got home.
    count_ok = is_exactly(server.get("resultsMessages"), 1)
    rows.append(
        ("PASS" if count_ok else "FAIL", "results messages", shown(server, "resultsMessages"))
    )
    if not count_ok:
        problems.append(
            "the host recorded %s results messages, not exactly 1: the judge read the "
            "first" % shown(server, "resultsMessages")
        )
    via = server.get("resultsVia")
    rows.append(("PASS" if via == VIA_HOST else "FAIL", "how the results came home", brief(via)))
    if via != VIA_HOST:
        problems.append(
            "the results never reached the host: the driver recovered them via %s, and "
            "the page's way home (the copy command) is part of what the guard tests" % brief(via)
        )


def judge(record, expect_major):
    """Return (problems, notes, rows). rows = [(status, label, detail)].

    Anything of an unexpected type is a problem, never an exception: the report a broken
    page sends is exactly the one the judge must be able to read.
    """
    problems, notes, rows = [], [], []
    if not isinstance(record, dict) or not isinstance(record.get("page"), dict):
        return ["guard-results.json is missing or has no `page` object"], notes, rows
    page = record["page"]
    server = child(record, "server", "server", problems)
    live = child(page, "live", "page.live", problems)
    stats = child(live, "stats", "page.live.stats", problems)

    schema_ok = is_exactly(page.get("schema"), SCHEMA)
    rows.append(("PASS" if schema_ok else "FAIL", "results schema", shown(page, "schema")))
    if not schema_ok:
        problems.append(
            "page.schema is not %d: %s - this judge reads one payload shape only"
            % (SCHEMA, shown(page, "schema"))
        )
    major = judge_user_agent(page, expect_major, problems, rows)
    judge_probes(page, major, problems, rows)
    judge_auto(page.get("auto"), live, problems, notes, rows)
    judge_live(live, stats, problems, rows)
    judge_server(server, problems, rows)

    rows.append(
        ("INFO", "jank while streaming (software rendering: not judged)", brief(live.get("jank")))
    )
    rows.append(("INFO", "JS heap", brief(live.get("heap"))))
    rows.append(
        (
            "INFO",
            "Slicer main-thread stalls (not judged in CI)",
            brief(server.get("mainThreadStalls")),
        )
    )
    return problems, notes, rows


def cell(value):
    return clean(value, CELL_LIMIT)


def render(label, expect_major, record, run, problems, notes, rows):
    page = record.get("page") if isinstance(record, dict) else None
    page = page if isinstance(page, dict) else {}
    out = [
        "## Runtime guard (Chromium %s) - %s: %s"
        % (cell(expect_major), cell(label), "FAIL" if problems else "PASS"),
        "",
    ]
    out.append("- userAgent: %s" % cell(page.get("userAgent")))
    if isinstance(run, dict):
        out.append(
            "- Slicer %s r%s on %s, Qt %s, Python %s"
            % tuple(
                cell(run.get(key))
                for key in ("slicerVersion", "slicerRevision", "os", "qtVersion", "python")
            )
        )
        out.append("- driver: %s after %s s" % (cell(run.get("reason")), cell(run.get("elapsedS"))))
    else:
        out.append(
            "- ci-run.json is missing or unreadable: Slicer never reached the driver, or was killed"
        )
    if problems:
        out += ["", "### Problems", ""] + ["- " + clean(p, LINE_LIMIT) for p in problems]
    if notes:
        out += ["", "### Notes", ""] + ["- " + clean(n, LINE_LIMIT) for n in notes]
    if rows:
        out += ["", "| Status | Check | Detail |", "|---|---|---|"]
        out += ["| %s | %s | %s |" % (cell(s), cell(lbl), cell(d)) for s, lbl, d in rows]
    text = "\n".join(out) + "\n"
    if len(text) > REPORT_LIMIT:
        cut = text.rfind("\n", 0, REPORT_LIMIT) + 1
        text = text[:cut] + "\n- (report truncated: %d of %d characters shown)\n" % (
            cut,
            len(text),
        )
    return no_workflow_commands(text)


def run_judge(out_dir, label, expect_major):
    """Return (exit_code, markdown). Never raises: whatever the files hold, the answer is a
    verdict and a report."""
    record, unreadable = load_json(os.path.join(out_dir, "guard-results.json"))
    run, _run_unreadable = load_json(os.path.join(out_dir, "ci-run.json"))
    if unreadable:
        problems, notes, rows = [unreadable], [], []
    else:
        try:
            problems, notes, rows = judge(record, expect_major)
        except Exception as exc:  # a last resort: the tests keep every known shape out of here
            problems, notes, rows = ["the judge raised on this record: %s" % txt(repr(exc))], [], []
    try:
        markdown = render(label, expect_major, record, run, problems, notes, rows)
    except Exception as exc:
        problems = problems or ["the report could not be rendered"]
        markdown = "## Runtime guard (Chromium %s) - %s: FAIL\n\n- %s: %s\n" % (
            cell(expect_major),
            cell(label),
            "the report could not be rendered",
            cell(repr(exc)),
        )
    return (1 if problems else 0), markdown


def main(argv=None):
    ap = argparse.ArgumentParser(description="Judge a runtime-guard run")
    ap.add_argument("--out", required=True, help="directory holding guard-results.json")
    ap.add_argument("--label", default="run")
    ap.add_argument("--expect-chromium", type=int, default=83)
    ap.add_argument("--summary", default="", help="file to append the Markdown report to")
    args = ap.parse_args(argv)
    code, markdown = run_judge(args.out, args.label, args.expect_chromium)
    try:  # a Windows console defaults to cp1252; a probe detail may not fit in it
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.stdout.write(markdown)
    if args.summary:
        # Best effort: the verdict is the exit code, and a summary that cannot be written
        # must not turn a pass into a traceback and a fail.
        try:
            with open(args.summary, "a", encoding="utf-8") as fh:
                fh.write(markdown)
        except OSError as exc:
            sys.stderr.write(
                "guard_judge: the report was not appended to the summary file (%s)\n"
                % clean(exc, 200)
            )
    return code


if __name__ == "__main__":
    sys.exit(main())
