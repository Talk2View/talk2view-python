"""Run: python3 -m unittest test_guard_judge -v   (standard library only, from this directory)

Every test here is one rule the judge owes the runtime guard. A rule with no test is a
rule CI can lose silently, so each of the twelve checks below has at
least one test that fails without it.
"""

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import guard_judge

#: The userAgents recorded in real Slicer 5.12.4 runs: the judge must take each at face value.
UA83 = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "QtWebEngine/5.15.2 Chrome/83.0.4103.122 Safari/537.36"
)
UA83_WINDOWS = (
    "Mozilla/5.0 (Windows NT 6.2; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "QtWebEngine/5.15.2 Chrome/83.0.4103.122 Safari/537.36"
)
UA87_MACOS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 26_6_2) AppleWebKit/537.36 (KHTML, like Gecko) "
    "QtWebEngine/5.15.19 Chrome/87.0.4280.144 Safari/537.36"
)
#: Characters a page could put in any string it sends, built with chr() so this file holds
#: none of them literally: each would start a new log line, reorder text, or not encode.
LF, CR, NEL, LSEP, PSEP = chr(10), chr(13), chr(0x85), chr(0x2028), chr(0x2029)
ESC, DEL, BIDI, SURROGATE = chr(0x1B), chr(0x7F), chr(0x202E), chr(0xD800)
INVISIBLES = (CR, NEL, LSEP, PSEP, ESC, DEL, BIDI, SURROGATE)
#: What the real page writes for these two, and what the judge reads them for: the focus
#: ring is only meaningful in a focused window, and `gap` is banned because 83 lacks it.
FOCUS_DETAIL = (
    "solid 2px rgb(38, 200, 184) (the page draws solid 2px rgb(38, 200, 184)); "
    "document.hasFocus true after 0 ms"
)
NO_GAP = "NOT supported"


def good_record():
    details = {"css:focus-ring": FOCUS_DETAIL}
    probes = [
        {"id": pid, "label": pid, "status": "PASS", "detail": details.get(pid, "ok")}
        for pid in guard_judge.REQUIRED_PROBES
    ]
    probes += [
        {"id": "api:%d" % i, "label": "api %d" % i, "status": "PASS", "detail": "polyfilled"}
        for i in range(guard_judge.MIN_API_PROBES)
    ]
    probes.append(
        {"id": "css:flex-gap", "label": "flexbox gap", "status": "INFO", "detail": NO_GAP}
    )
    steps = [
        {"id": sid, "status": "PASS", "detail": "ok", "ms": 1}
        for sid in guard_judge.REQUIRED_AUTO_STEPS
    ]
    return {
        "server": {
            "contentMatches": True,
            "mainThreadStalls": None,
            "resultsMessages": 1,
            "resultsVia": guard_judge.VIA_HOST,
            "hostCalls": [
                {"host": "open_external", "value": guard_judge.LINK},
                {"host": "clipboard_set", "value": "## Segmentation plan\n\nHere is how"},
            ],
        },
        "page": {
            "schema": 1,
            "userAgent": UA83,
            "chromiumMajor": 83,
            "probes": probes,
            "auto": {
                "steps": steps,
                "reloads": 1,
                "finishedAt": "2026-09-22T00:00:00.000Z",
                "beforeReload": {
                    "errorCount": 0,
                    "errors": [],
                    "cspViolations": 0,
                    "gaps": 0,
                    "messageCount": 2,
                },
            },
            "live": {
                "errorCount": 0,
                "errors": [],
                "cspViolations": 0,
                "stats": {"gaps": 0},
                "conn": {"mode": "fetch-stream", "fellBack": False, "reconnects": 0},
                "jank": {},
                "heap": {},
            },
        },
    }


def watchdog_record():
    """What the page sends when it never drew a thread: no probes, no scripted run, and
    the reason in `live.errors`. A FAIL, never a crash."""
    record = good_record()
    record["page"]["probes"] = []
    record["page"]["auto"] = None
    record["page"]["live"]["errorCount"] = 1
    record["page"]["live"]["errors"] = ["guard: the page never drew a thread within 30000 ms"]
    return record


def problems_of(record, expect=83):
    return guard_judge.judge(record, expect)[0]


def probe(record, probe_id):
    return [p for p in record["page"]["probes"] if p["id"] == probe_id][0]


def step(record, step_id):
    return [s for s in record["page"]["auto"]["steps"] if s["id"] == step_id][0]


def one_problem(test, record, needle, expect=83):
    found = problems_of(record, expect)
    test.assertEqual(len(found), 1, found)
    test.assertIn(needle, found[0])


def write_record(out, record):
    with open(os.path.join(out, "guard-results.json"), "w", encoding="utf-8") as fh:
        json.dump(record, fh)


def quiet_main(argv):
    """guard_judge.main() with its report kept off the test run's own output."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = guard_judge.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def unescaped_pipes(line):
    """The pipes Markdown reads as cell boundaries: those after an even run of backslashes."""
    count = 0
    for i, ch in enumerate(line):
        if ch == "|":
            run = 0
            while i - run - 1 >= 0 and line[i - run - 1] == "\\":
                run += 1
            count += run % 2 == 0
    return count


class JudgeTest(unittest.TestCase):
    def test_a_clean_run_has_no_problems(self):
        problems, notes, rows = guard_judge.judge(good_record(), 83)
        self.assertEqual(problems, [])
        self.assertEqual(notes, [])
        self.assertTrue(rows)

    def test_a_silent_qt_upgrade_is_a_failure(self):
        record = good_record()
        record["page"]["userAgent"] = UA83.replace("Chrome/83.", "Chrome/87.")
        record["page"]["chromiumMajor"] = 87
        found = problems_of(record)
        self.assertTrue(found)
        self.assertIn("Chrome/87", found[0])
        # On 87 the same record is clean, except that `gap` works there, so the page would
        # not have written "NOT supported": check 10 only binds the two together on 83.
        record["page"]["probes"] = [
            p for p in record["page"]["probes"] if p["id"] != "css:flex-gap"
        ]
        record["page"]["probes"].append(
            {"id": "css:flex-gap", "label": "flexbox gap", "status": "INFO", "detail": "supported"}
        )
        self.assertEqual(problems_of(record, expect=87), [])

    def test_any_failed_or_missing_probe_is_a_failure(self):
        for probe_id in ("font", "sse:fetch-stream", "sse:eventsource"):
            record = good_record()
            probe(record, probe_id)["status"] = "FAIL"
            one_problem(self, record, "probe FAIL: " + probe_id)
        record = good_record()
        record["page"]["probes"] = [
            p for p in record["page"]["probes"] if p["id"] != "css:composer-row"
        ]
        one_problem(self, record, "probe missing from the results: css:composer-row")
        record = good_record()
        record["page"]["probes"] = [
            p for p in record["page"]["probes"] if not p["id"].startswith("api:")
        ]
        one_problem(self, record, "only 0 api: probes ran")

    def test_the_scripted_run_must_be_complete_and_green(self):
        record = good_record()
        record["page"]["auto"] = None
        one_problem(self, record, "page.auto is missing")
        record = good_record()
        step(record, "auto:markdown-dom")["status"] = "FAIL"
        one_problem(self, record, "scripted step FAIL: auto:markdown-dom")
        record = good_record()
        record["page"]["auto"]["steps"] = [
            s for s in record["page"]["auto"]["steps"] if s["id"] != "auto:guest-limit"
        ]
        one_problem(self, record, "scripted step never ran: auto:guest-limit")

    def test_counters_content_and_host_calls(self):
        for path, value, needle in (
            (("page", "live", "errorCount"), 2, "uncaught errors: 2"),
            (("page", "live", "cspViolations"), 1, "CSP violations: 1"),
            (("page", "live", "stats", "gaps"), 1, "seq gaps: 1"),
            (("server", "contentMatches"), False, "contentMatches"),
            (("page", "auto", "beforeReload", "errorCount"), 1, "before the reload"),
            (
                ("server", "hostCalls"),
                [{"host": "open_external", "value": guard_judge.LINK}],
                "clipboard",
            ),
            (
                ("server", "hostCalls"),
                [{"host": "clipboard_set", "value": "## Segmentation plan"}],
                "never opened",
            ),
            (("server", "hostCalls"), "junk", "never opened"),
        ):
            record = good_record()
            node = record
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            found = problems_of(record)
            self.assertTrue(found, path)
            self.assertIn(needle, found[0])

    def test_garbage_in_is_a_failure_not_a_crash(self):
        for record in (None, {}, {"page": None}, {"page": {}}, [], "text", {"page": []}):
            self.assertTrue(problems_of(copy.deepcopy(record)))

    def test_a_user_agent_without_a_chrome_major_is_a_failure(self):
        for user_agent in (
            "Mozilla/5.0 (X11; Linux x86_64; rv:115.0) Gecko Firefox/115.0",
            "",
            None,
            83,
        ):
            record = good_record()
            record["page"]["userAgent"] = user_agent
            record["page"]["chromiumMajor"] = None
            one_problem(self, record, "userAgent has no Chrome/<major>")

    def test_a_required_id_must_pass_not_just_be_present(self):
        for status in ("INFO", "SKIP", None):
            record = good_record()
            probe(record, "css:composer-row")["status"] = status
            one_problem(self, record, "required probe is not PASS: css:composer-row")
            record = good_record()
            step(record, "auto:deny")["status"] = status
            one_problem(self, record, "scripted step is not PASS: auto:deny")

    def test_the_counters_before_the_reload_must_be_real_integer_zeros(self):
        for before in ("absent", None, {}, [], "0"):
            record = good_record()
            if before == "absent":
                del record["page"]["auto"]["beforeReload"]
            else:
                record["page"]["auto"]["beforeReload"] = before
            one_problem(self, record, "page.auto.beforeReload is missing")
        for key, value, needle in (
            ("gaps", "absent", "seq gaps before the reload: missing"),
            ("errorCount", False, "uncaught errors before the reload: false"),
            ("cspViolations", 0.0, "CSP violations before the reload: 0.0"),
        ):
            record = good_record()
            before = record["page"]["auto"]["beforeReload"]
            if value == "absent":
                del before[key]
            else:
                before[key] = value
            one_problem(self, record, needle)


class ReviewerChecklistTest(unittest.TestCase):
    """The twelve checks the judge owes, one test each where the judge above
    does not already cover them."""

    # 1. Exactly one results message, and it came home the way the page really sends it.
    def test_more_than_one_results_message_is_a_failure(self):
        for count, needle in ((2, "2 results messages"), (0, "0 results messages"), ("1", "1")):
            record = good_record()
            record["server"]["resultsMessages"] = count
            one_problem(self, record, needle)
        record = good_record()
        del record["server"]["resultsMessages"]
        one_problem(self, record, "results messages")

    def test_results_the_driver_had_to_dig_out_are_a_failure(self):
        for via in (guard_judge.VIA_EVAL, guard_judge.VIA_CONSOLE, None, "something-new"):
            record = good_record()
            record["server"]["resultsVia"] = via
            found = problems_of(record)
            self.assertTrue(found, via)
            self.assertIn("never reached the host", found[0])
            self.assertIn(guard_judge.brief(via), found[0])  # it names how they did arrive

    # 2. schema === 1.
    def test_a_results_payload_of_another_schema_is_a_failure(self):
        for schema in (2, "1", None, 1.0):
            record = good_record()
            record["page"]["schema"] = schema
            one_problem(self, record, "page.schema is not 1")
        record = good_record()
        del record["page"]["schema"]
        one_problem(self, record, "page.schema is not 1")

    # 3. The 18 step ids, in order; `[recovered: …]` is allowed.
    def test_the_scripted_steps_must_run_in_order(self):
        record = good_record()
        steps = record["page"]["auto"]["steps"]
        steps[3], steps[4] = steps[4], steps[3]
        one_problem(self, record, "scripted steps ran out of order")
        record = good_record()
        record["page"]["auto"]["steps"].append(dict(step(record, "auto:send")))
        one_problem(self, record, "scripted steps ran out of order")

    def test_a_recovered_step_still_passes_and_is_reported(self):
        record = good_record()
        step(record, "auto:deny")["detail"] = "[recovered: clicked Stop] step denied"
        problems, notes, _rows = guard_judge.judge(record, 83)
        self.assertEqual(problems, [])
        self.assertEqual(len(notes), 1)
        self.assertIn("auto:deny", notes[0])
        self.assertIn("clicked Stop", notes[0])

    # 5. live.errors empty, not just the count zero.
    def test_a_recorded_error_message_is_a_failure_even_with_the_count_at_zero(self):
        record = good_record()
        record["page"]["live"]["errors"] = ["error: boom @ index.js:1"]
        one_problem(self, record, "boom")
        record = good_record()
        record["page"]["live"]["errors"] = "not a list"
        one_problem(self, record, "page.live.errors")

    # 6. beforeReload.errors empty too.
    def test_an_error_recorded_before_the_reload_is_a_failure(self):
        record = good_record()
        record["page"]["auto"]["beforeReload"]["errors"] = ["csp: script-src blob:"]
        one_problem(self, record, "script-src")

    # 7. reloads === 1 and finishedAt set.
    def test_the_scripted_reload_must_have_happened_exactly_once(self):
        for reloads, needle in (
            (0, "auto.reloads is 0"),
            (2, "auto.reloads is 2"),
            ("1", "auto.reloads is"),
        ):
            record = good_record()
            record["page"]["auto"]["reloads"] = reloads
            one_problem(self, record, needle)

    def test_a_run_that_never_finished_is_a_failure(self):
        for value in (None, "", 0):
            record = good_record()
            record["page"]["auto"]["finishedAt"] = value
            one_problem(self, record, "auto.finishedAt")
        record = good_record()
        del record["page"]["auto"]["finishedAt"]
        one_problem(self, record, "auto.finishedAt")

    # 10. The Chromium major, from both the UA and the page's own reading; `gap` on 83.
    def test_the_pages_own_chromium_major_must_agree_with_the_user_agent(self):
        record = good_record()
        record["page"]["chromiumMajor"] = 87
        one_problem(self, record, "page.chromiumMajor is 87")

    def test_flexbox_gap_must_be_unsupported_on_83(self):
        record = good_record()
        probe(record, "css:flex-gap")["detail"] = "supported"
        one_problem(self, record, "css:flex-gap")
        record = good_record()
        record["page"]["probes"] = [
            p for p in record["page"]["probes"] if p["id"] != "css:flex-gap"
        ]
        one_problem(self, record, "css:flex-gap")

    # 11. The live transport is the page's own, not the fallback.
    def test_the_live_transport_must_be_the_fetch_stream(self):
        for key, value, needle in (
            ("mode", "eventsource", "live transport is eventsource"),
            ("fellBack", True, "fell back"),
            # Falsy is not False: only the page's own `false` says it never fell back.
            ("fellBack", None, "fell back"),
            ("fellBack", 0, "fell back"),
            ("mode", None, "live transport is None"),
        ):
            record = good_record()
            record["page"]["live"]["conn"][key] = value
            one_problem(self, record, needle)
        record = good_record()
        record["page"]["live"]["conn"] = None
        found = problems_of(record)
        self.assertTrue(found)

    # 12. The focus ring was measured in a focused window.
    def test_a_focus_ring_measured_without_focus_is_a_failure(self):
        record = good_record()
        probe(record, "css:focus-ring")["detail"] = FOCUS_DETAIL.replace(
            "hasFocus true", "hasFocus false"
        )
        one_problem(self, record, "document.hasFocus")
        record = good_record()
        probe(record, "css:focus-ring")["detail"] = "ok"
        one_problem(self, record, "document.hasFocus")


class WatchdogRecordTest(unittest.TestCase):
    def test_a_page_that_never_drew_a_thread_fails_with_its_reason(self):
        problems = problems_of(watchdog_record())
        # The reason belongs ON the "no scripted run" problem, not only further down
        # beside the error count: that line is the first one a reader looks at.
        missing = [p for p in problems if p.startswith("page.auto is missing")]
        self.assertEqual(len(missing), 1, problems)
        self.assertIn("never drew a thread", missing[0])

    def test_the_watchdog_report_never_raises(self):
        record = watchdog_record()
        record["page"]["live"]["errors"] = None  # a truncated payload on top of it
        guard_judge.judge(record, 83)


class HostCallTest(unittest.TestCase):
    """Checks 8 and 9 look at the right host AND the right value: a call that has only one
    of the two is not the page's link or the page's copy."""

    def calls(self, *calls):
        record = good_record()
        record["server"]["hostCalls"] = [{"host": h, "value": v} for h, v in calls]
        return record

    def test_the_link_must_reach_open_external(self):
        record = self.calls(
            ("clipboard_set", guard_judge.LINK), ("clipboard_set", "## Segmentation plan\n\nx")
        )
        one_problem(self, record, "never opened")

    def test_the_link_must_be_the_docs_url_exactly(self):
        for wrong in (guard_judge.LINK[:-1], guard_judge.LINK + "x", "https://example.com/"):
            record = self.calls(
                ("open_external", wrong), ("clipboard_set", "## Segmentation plan\n\nx")
            )
            one_problem(self, record, "never opened")

    def test_the_copy_must_reach_clipboard_set(self):
        record = self.calls(
            ("open_external", guard_judge.LINK), ("open_external", "## Segmentation plan\n\nx")
        )
        one_problem(self, record, "clipboard never got the reply")

    def test_the_copy_must_be_the_reply(self):
        for wrong in ("Here is how", " ## Segmentation plan", "", None):
            record = self.calls(("open_external", guard_judge.LINK), ("clipboard_set", wrong))
            one_problem(self, record, "clipboard never got the reply")

    def test_the_two_calls_swapped_fail_both(self):
        record = self.calls(
            ("clipboard_set", guard_judge.LINK), ("open_external", "## Segmentation plan\n\nx")
        )
        found = problems_of(record)
        self.assertEqual(len(found), 2, found)

    def test_content_matches_must_be_the_boolean_true(self):
        for value in ("true", 1, None):
            record = good_record()
            record["server"]["contentMatches"] = value
            one_problem(self, record, "contentMatches is not true")


class StrictnessTest(unittest.TestCase):
    """Shapes the real page never sends. Each would otherwise pass, so each fails."""

    def test_every_userAgent_plan_0_recorded_is_read_as_one_chromium(self):
        for user_agent, major in ((UA83, 83), (UA83_WINDOWS, 83), (UA87_MACOS, 87)):
            record = good_record()
            record["page"]["userAgent"] = user_agent
            record["page"]["chromiumMajor"] = major
            if major != 83:
                probe(record, "css:flex-gap")["detail"] = "supported"
            self.assertEqual(problems_of(record, expect=major), [], user_agent)

    def test_a_longer_major_is_not_83(self):
        record = good_record()
        record["page"]["userAgent"] = UA83.replace("Chrome/83.", "Chrome/830.")
        record["page"]["chromiumMajor"] = 830
        one_problem(self, record, "userAgent is Chrome/830")

    def test_a_product_name_ending_in_chrome_is_not_a_chrome_token(self):
        record = good_record()
        record["page"]["userAgent"] = UA83.replace("Safari/", "CrChrome/99.0 Safari/")
        self.assertEqual(problems_of(record), [])

    def test_a_major_too_long_to_be_a_version_fails_without_raising(self):
        record = good_record()
        record["page"]["userAgent"] = "Mozilla/5.0 Chrome/" + "9" * 5000 + ".0"
        one_problem(self, record, "userAgent has no Chrome/<major>")

    def test_a_headless_chromium_is_not_a_web_view(self):
        record = good_record()
        record["page"]["userAgent"] = UA83.replace("Chrome/83.", "HeadlessChrome/83.")
        one_problem(self, record, "headless")

    def test_a_userAgent_naming_two_chromiums_is_a_failure(self):
        for extra in (" Chrome/87.0.4280.144", " Chrome/83.0.4103.122"):
            record = good_record()
            record["page"]["userAgent"] = UA83 + extra
            one_problem(self, record, "names 2 Chromium versions")

    def test_the_pages_own_major_must_be_an_integer(self):
        for claimed in (83.0, "83", None, True):
            record = good_record()
            record["page"]["chromiumMajor"] = claimed
            one_problem(self, record, "page.chromiumMajor is")
        record = good_record()
        del record["page"]["chromiumMajor"]
        one_problem(self, record, "page.chromiumMajor is")

    def test_a_probe_reported_twice_is_a_failure_whatever_the_order(self):
        for probe_id, bad in (
            ("css:focus-ring", "document.hasFocus false"),
            ("css:flex-gap", "supported"),
        ):
            for position in ("first", "last"):
                record = good_record()
                twin = dict(probe(record, probe_id), detail=bad)
                if position == "first":
                    record["page"]["probes"].insert(0, twin)
                else:
                    record["page"]["probes"].append(twin)
                found = problems_of(record)
                self.assertTrue(any("probe reported 2 times: " + probe_id in p for p in found))

    def test_a_probe_status_outside_pass_fail_info_is_a_failure(self):
        for status in ("WARN", "fail", "PASS ", None, 1):
            record = good_record()
            record["page"]["probes"].append(
                {"id": "extra", "label": "x", "status": status, "detail": ""}
            )
            one_problem(self, record, "probe status is not PASS, FAIL or INFO")
        record = good_record()
        probe(record, "api:0")["status"] = None  # only their count used to be judged
        one_problem(self, record, "probe status is not PASS, FAIL or INFO")

    def test_a_step_the_judge_does_not_know_is_a_failure(self):
        for status, needle in (
            ("PASS", "scripted step this judge does not know"),
            ("INFO", "scripted step status is not PASS or FAIL"),
            ("fail", "scripted step status is not PASS or FAIL"),
        ):
            record = good_record()
            record["page"]["auto"]["steps"].append(
                {"id": "auto:extra", "status": status, "detail": "", "ms": 1}
            )
            one_problem(self, record, needle)


def all_paths(obj, prefix=()):
    """Every key and index path inside a JSON value."""
    if isinstance(obj, dict):
        items = obj.items()
    elif isinstance(obj, list):
        items = enumerate(obj)
    else:
        return
    for key, value in items:
        yield prefix + (key,)
        yield from all_paths(value, prefix + (key,))


class CrashProofTest(unittest.TestCase):
    """Whatever a results file holds, the answer is exit 0 or 1 and a report."""

    def assert_a_report(self, record):
        problems, notes, rows = guard_judge.judge(record, 83)
        markdown = guard_judge.render("x", 83, record, {"os": None}, problems, notes, rows)
        self.assertTrue(markdown.startswith("## Runtime guard"))
        return problems

    def test_every_field_deleted_or_replaced_by_every_other_type(self):
        paths = list(all_paths(good_record()))
        self.assertGreater(len(paths), 200)
        for replacement in ("delete", None, [], "x", {}, True, [[1]], {"a": {}}):
            for path in paths:
                record = good_record()
                node = record
                for key in path[:-1]:
                    node = node[key]
                if replacement == "delete":
                    del node[path[-1]]
                else:
                    node[path[-1]] = copy.deepcopy(replacement)
                with self.subTest(path=path, replacement=replacement):
                    self.assert_a_report(record)

    def test_ids_that_are_lists_or_objects_fail_without_raising(self):
        for bad_id in ([1], {}, {"a": 1}, None, 7):
            record = good_record()
            record["page"]["probes"].append({"id": bad_id, "status": "PASS"})
            self.assertTrue(any("no string id" in p for p in self.assert_a_report(record)))
            record = good_record()
            record["page"]["auto"]["steps"].append({"id": bad_id, "status": "PASS", "ms": 1})
            self.assertTrue(self.assert_a_report(record))

    def test_a_value_nested_past_the_recursion_limit_is_reported_not_raised(self):
        deep = []
        for _ in range(100000):
            deep = [deep]
        for place in ("detail", "label", "status"):
            record = good_record()
            step(record, "auto:deny")[place] = deep
            probe(record, "font")[place] = deep
            self.assert_a_report(record)

    def test_every_truncation_of_a_good_file_fails_with_a_report(self):
        data = json.dumps(good_record()).encode("utf-8")
        with tempfile.TemporaryDirectory() as out:
            for cut in range(0, len(data), 97):
                with open(os.path.join(out, "guard-results.json"), "wb") as fh:
                    fh.write(data[:cut])
                code, markdown = guard_judge.run_judge(out, "x", 83)
                self.assertEqual(code, 1, cut)
                self.assertTrue(markdown.startswith("## Runtime guard"), cut)

    def test_files_that_are_not_a_record_fail_with_a_report(self):
        good = json.dumps(good_record()).encode("utf-8")
        cases = {
            "nested past the parser's depth": b"[" * 200000,
            "UTF-16": json.dumps(good_record()).encode("utf-16"),
            "not UTF-8": b"\xff\xfe{",
            "an integer too long to read": good.replace(b'"gaps": 0', b'"gaps": 1' + b"0" * 4999),
            "empty": b"",
            "a list": b"[]",
            "null": b"null",
            "NaN for a counter": good.replace(b'"errorCount": 0', b'"errorCount": NaN'),
        }
        with tempfile.TemporaryDirectory() as out:
            for name, data in cases.items():
                with open(os.path.join(out, "guard-results.json"), "wb") as fh:
                    fh.write(data)
                code, text, err = quiet_main(["--out", out])
                self.assertEqual(code, 1, name)
                self.assertTrue(text.startswith("## Runtime guard"), name)
                self.assertEqual(err, "", name)

    def test_a_judge_that_raises_still_yields_exit_1_and_a_report(self):
        with tempfile.TemporaryDirectory() as out:
            write_record(out, good_record())
            with mock.patch.object(guard_judge, "judge", side_effect=RuntimeError("bug")):
                code, markdown = guard_judge.run_judge(out, "x", 83)
        self.assertEqual(code, 1)
        self.assertIn("the judge raised on this record", markdown)

    def test_a_render_that_raises_still_yields_a_failing_report(self):
        with tempfile.TemporaryDirectory() as out:
            write_record(out, good_record())
            with mock.patch.object(guard_judge, "render", side_effect=RuntimeError("bug")):
                code, markdown = guard_judge.run_judge(out, "x", 83)
        self.assertEqual(code, 1)
        self.assertTrue(markdown.startswith("## Runtime guard (Chromium 83) - x: FAIL"))

    def test_a_byte_order_mark_is_allowed(self):
        with tempfile.TemporaryDirectory() as out:
            with open(os.path.join(out, "guard-results.json"), "wb") as fh:
                fh.write(b"\xef\xbb\xbf" + json.dumps(good_record()).encode("utf-8"))
            code, _markdown = guard_judge.run_judge(out, "x", 83)
        self.assertEqual(code, 0)

    def test_a_summary_that_cannot_be_written_keeps_the_verdict(self):
        with tempfile.TemporaryDirectory() as out:
            write_record(out, good_record())
            summary = os.path.join(out, "no-such-directory", "summary.md")
            code, text, err = quiet_main(["--out", out, "--summary", summary])
        self.assertEqual(code, 0)
        self.assertIn(": PASS", text)
        self.assertIn("was not appended to the summary file", err)


class InjectionTest(unittest.TestCase):
    """Page text reaches a CI log (where `::` starts a workflow command) and a job summary
    (which renders Markdown and HTML). It must arrive there as inert text."""

    def hostile_record(self):
        attack = (
            LF
            + "::error::injected"
            + CR
            + "::add-mask::x"
            + LSEP
            + "::stop-commands::t"
            + PSEP
            + "::warning::p"
        )
        record = good_record()
        record["page"]["userAgent"] = UA83 + attack
        step(record, "auto:deny")["detail"] = (
            "a"
            + BIDI
            + "b"
            + NEL
            + "c"
            + ESC
            + "[31m"
            + DEL
            + SURROGATE
            + attack
            + " <img src=https://example.com/x.png> [l](https://example.com) `code` a\\| b"
            + " a &lt; b"
        )
        probe(record, "font")["label"] = "label" + attack
        record["page"]["live"]["errors"] = ["boom" + attack]
        record["page"]["live"]["errorCount"] = 1
        return record

    def outputs(self, record, run=None):
        with tempfile.TemporaryDirectory() as out:
            write_record(out, record)
            if run is not None:
                with open(os.path.join(out, "ci-run.json"), "w", encoding="utf-8") as fh:
                    json.dump(run, fh)
            summary = os.path.join(out, "summary.md")
            code, text, err = quiet_main(["--out", out, "--label", "linux", "--summary", summary])
            with open(summary, encoding="utf-8") as fh:
                written = fh.read()
        self.assertEqual(err, "")
        return code, text, written

    def test_no_line_of_output_can_be_a_workflow_command(self):
        run = {"os": "linux" + LF + "::error::from-ci-run", "reason": CR + "::warning::r"}
        code, text, written = self.outputs(self.hostile_record(), run)
        self.assertEqual(code, 1)
        for output in (text, written):
            for line in output.splitlines():  # splits on CR, NEL, U+2028 and the rest too
                self.assertFalse(line.startswith(("::", "##[")), line)
            # ...because the text stayed on its own line, not because the last line of
            # defence caught a line the cleaning let through.
            lines = output.splitlines()
            self.assertIn("from-ci-run", [ln for ln in lines if ln.startswith("- Slicer")][0])
            self.assertIn("::warning::r", [ln for ln in lines if ln.startswith("- driver:")][0])
            self.assertIn("::warning::p", [ln for ln in lines if ln.startswith("- userAgent:")][0])

    def test_invisible_characters_arrive_as_visible_escapes(self):
        _code, text, written = self.outputs(self.hostile_record())
        for output in (text, written):
            for ch in INVISIBLES:
                self.assertNotIn(ch, output, hex(ord(ch)))
        self.assertIn(chr(92) + "u202e", text)  # the bidi override, shown, not obeyed

    def test_html_and_markdown_arrive_as_text(self):
        _code, text, _written = self.outputs(self.hostile_record())
        self.assertNotIn("<img", text)
        self.assertIn("&lt;img", text)
        self.assertNotIn(".png>", text)
        self.assertIn("a &amp;lt; b", text)  # an entity the page typed stays as typed
        self.assertNotIn(" [l](", text)  # no link can form: both brackets are escaped
        self.assertIn(chr(92) + "[l" + chr(92) + "](", text)
        self.assertNotIn(" `code`", text)

    def test_a_backslash_before_a_pipe_cannot_open_a_cell(self):
        for detail in ("a\\| b", "a\\\\| b", "a | b", "\\", "a\\"):
            record = good_record()
            step(record, "auto:deny")["detail"] = detail
            _p, _n, rows = guard_judge.judge(record, 83)
            markdown = guard_judge.render("x", 83, record, None, [], [], rows)
            line = [ln for ln in markdown.splitlines() if ln.startswith("| PASS | auto:deny |")][0]
            self.assertEqual(unescaped_pipes(line), 4, (detail, line))

    def test_a_huge_detail_is_cut_and_says_so(self):
        record = good_record()
        step(record, "auto:deny")["detail"] = "A" * 1000000
        _code, text, _written = self.outputs(record)
        line = [ln for ln in text.splitlines() if ln.startswith("| PASS | auto:deny |")][0]
        # Pinned numbers, not the module's constant: raising the cap must fail here.
        self.assertLess(len(line), 400)
        self.assertIn(guard_judge.TRUNCATED, line)

    def test_the_whole_report_is_capped(self):
        record = good_record()
        record["page"]["probes"] += [
            {"id": "extra:%d" % i, "label": "L" * 400, "status": "INFO", "detail": "D" * 400}
            for i in range(3000)
        ]
        _code, text, written = self.outputs(record)
        for output in (text, written):
            self.assertLess(len(output), 100200)  # pinned
            self.assertIn("report truncated", output)

    def test_the_caps_are_pinned(self):
        # A changed cap changes what a CI log or job summary can hold; make it a
        # decision someone sees in review, not a constant the tests follow silently.
        caps = (
            guard_judge.CELL_LIMIT,
            guard_judge.LINE_LIMIT,
            guard_judge.TEXT_LIMIT,
            guard_judge.REPORT_LIMIT,
        )
        self.assertEqual(caps, (300, 600, 2000, 100000))

    def test_the_last_line_of_defence_leaves_no_line_the_runner_would_obey(self):
        # The runner trims leading whitespace before looking for `::`, splits lines
        # on CR, and obeys `##[` anywhere in a line; a guard that only indents does nothing.
        text = "ok\n::error::a\n   ::error::b\nc\r::error::d\n- e ##[error]f\n- fine"
        guarded = guard_judge.no_workflow_commands(text)
        self.assertNotIn("\r", guarded)
        for line in guarded.split("\n"):
            self.assertFalse(line.lstrip().startswith("::"), line)
            self.assertNotIn("##[", line)
        expected = [
            "ok",
            "> ::error::a",
            ">    ::error::b",
            "c",
            "> ::error::d",
            "- e ##\\[error]f",
            "- fine",
        ]
        self.assertEqual(guarded.split("\n"), expected)


class RunJudgeTest(unittest.TestCase):
    def test_missing_results_file_fails_and_says_so(self):
        with tempfile.TemporaryDirectory() as out:
            code, markdown = guard_judge.run_judge(out, "linux", 83)
        self.assertEqual(code, 1)
        self.assertIn("linux: FAIL", markdown)
        self.assertIn("guard-results.json is missing", markdown)
        self.assertIn("ci-run.json is missing", markdown)

    def test_main_appends_the_report_to_the_summary_file(self):
        with tempfile.TemporaryDirectory() as out:
            with open(os.path.join(out, "guard-results.json"), "w", encoding="utf-8") as fh:
                json.dump(good_record(), fh)
            with open(os.path.join(out, "ci-run.json"), "w", encoding="utf-8") as fh:
                json.dump(
                    {"reason": "results written", "elapsedS": 99.0, "qtVersion": "5.15.2"}, fh
                )
            summary = os.path.join(out, "summary.md")
            with open(summary, "w", encoding="utf-8") as fh:
                fh.write("earlier step\n")
            code, _text, _err = quiet_main(["--out", out, "--label", "linux", "--summary", summary])
            with open(summary, encoding="utf-8") as fh:
                text = fh.read()
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith("earlier step\n"))
        self.assertIn("## Runtime guard (Chromium 83) - linux: PASS", text)
        self.assertIn("| PASS | auto:reload | ok (1 ms) |", text)
        self.assertIn("Qt 5.15.2", text)

    def test_a_failing_run_exits_one_and_lists_its_problems(self):
        record = good_record()
        record["page"]["live"]["errorCount"] = 3
        with tempfile.TemporaryDirectory() as out:
            with open(os.path.join(out, "guard-results.json"), "w", encoding="utf-8") as fh:
                json.dump(record, fh)
            code, markdown = guard_judge.run_judge(out, "windows", 83)
        self.assertEqual(code, 1)
        self.assertIn("windows: FAIL", markdown)
        self.assertIn("### Problems", markdown)
        self.assertIn("uncaught errors: 3", markdown)

    def test_a_pipe_in_a_detail_cannot_break_the_table(self):
        record = good_record()
        probe(record, "font")["detail"] = "a | b\nand a newline"
        _problems, _notes, rows = guard_judge.judge(record, 83)
        markdown = guard_judge.render("linux", 83, record, None, [], [], rows)
        for line in markdown.splitlines():
            if line.startswith("| ") and "a \\| b" in line:
                # Three cells: every pipe but the escaped one is a cell boundary, and the
                # newline is gone, so the row did not spill into one of its own.
                self.assertEqual(unescaped_pipes(line), 4)
                self.assertIn("b and a newline", line)
                break
        else:
            self.fail("the escaped detail is not in the table")


if __name__ == "__main__":
    unittest.main()
