"""The repair loop with the container smoke test and the vulnerability scan switched on."""

import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch

from dockhealer import cli, healer, scanner, smoke, vulnscan
from test_healer import FakeBuild, FakeClient, OK, fence, py, GOOD

ROOT = Path(__file__).resolve().parent.parent
INFO = scanner.scan(ROOT / "examples" / "flask_app")


def smoke_script(*results):
    queue, tags = list(results), []

    def fn(tag, info):
        tags.append(tag)
        return queue.pop(0)
    fn.tags = tags
    return fn


PASS = smoke.SmokeResult(True, status=200, path="/health", duration_s=1.0)
UNREACHABLE = smoke.SmokeResult(False, duration_s=30.0, statuses={"/health": None, "/": None},
                                logs=" * Running on http://127.0.0.1:5000\n", detail="no answer")
ERRORS_5XX = smoke.SmokeResult(False, statuses={"/health": 500, "/": 500}, logs="boom\n")


def run(replies, builds, smoke_fn=None, scan_fn=None, **kw):
    client, build_fn, removed = FakeClient(replies), FakeBuild(builds), []
    rep = healer.heal(INFO, client, build_fn=build_fn, remove_fn=removed.append, smoke_fn=smoke_fn, scan_fn=scan_fn, **kw)
    return rep, client, build_fn, removed


class SmokeInLoop(unittest.TestCase):
    def test_smoke_pass_completes_and_is_recorded(self):
        sm = smoke_script(PASS)
        r, *_ , removed = run([fence(GOOD)], [OK], smoke_fn=sm)
        self.assertTrue(r.success)
        self.assertTrue(r.attempts[-1].smoke["ok"])
        self.assertEqual(sm.tags, ["t:1"])
        self.assertEqual(removed, ["t:1"])

    def test_image_builds_but_container_unreachable_is_repaired(self):
        sm = smoke_script(UNREACHABLE, PASS)
        r, c, b, removed = run([fence(GOOD), fence(py("3.11"))], [OK, OK], smoke_fn=sm)
        self.assertTrue(r.success)
        self.assertEqual([a.outcome for a in r.attempts], ["smoke_failed", "built"])
        self.assertEqual(r.attempts[0].diagnosis["category"], "UNREACHABLE")
        self.assertIn("<container_log>", c.prompts[1])
        self.assertIn("0.0.0.0", c.prompts[1])
        self.assertEqual(removed, ["t:1", "t:1"], "every built image is cleaned up, including the failed one")

    def test_app_level_5xx_stops_for_a_human(self):
        r, c, *_ = run([fence(GOOD)], [OK], smoke_fn=smoke_script(ERRORS_5XX))
        self.assertEqual((r.stop_reason, len(c.prompts)), (healer.HUMAN_REQUIRED, 1))

    def test_same_smoke_failure_after_repair_is_a_repeated_failure(self):
        r, *_ = run([fence(GOOD), fence(py("3.11"))], [OK, OK], smoke_fn=smoke_script(UNREACHABLE, UNREACHABLE))
        self.assertEqual(r.stop_reason, healer.REPEATED_FAILURE)

    def test_skipped_smoke_does_not_fail_the_run(self):
        r, *_ = run([fence(GOOD)], [OK], smoke_fn=smoke_script(smoke.SmokeResult(False, skipped=True, detail="no port")))
        self.assertTrue(r.success)

    def test_budget_still_bounds_smoke_repairs(self):
        results = [smoke.SmokeResult(False, statuses={"/": None}, logs=f"line {i} 127.0.0.1\n") for i in range(5)]
        r, c, b, _ = run([fence(py(f"3.{10 + i}")) for i in range(5)], [OK] * 5, smoke_fn=smoke_script(*results), max_attempts=3)
        self.assertLessEqual(r.llm_calls, 3)
        self.assertFalse(r.success)


class ScanInLoop(unittest.TestCase):
    def test_scan_runs_only_on_an_image_that_passed(self):
        scanned = []

        def scan_fn(tag):
            scanned.append(tag)
            return {"available": True, "ok": True, "total": 4}
        r, *_ = run([fence(GOOD), fence(py("3.11"))], [OK, OK], smoke_fn=smoke_script(UNREACHABLE, PASS), scan_fn=scan_fn)
        self.assertEqual(len(scanned), 1, "the image that failed its smoke test must not be scanned")
        self.assertIsNone(r.attempts[0].scan)
        self.assertEqual(r.attempts[1].scan["total"], 4)

    def test_scanner_crash_never_fails_the_heal(self):
        def scan_fn(tag):
            raise RuntimeError("trivy exploded")
        r, *_ = run([fence(GOOD)], [OK], smoke_fn=smoke_script(PASS), scan_fn=scan_fn)
        self.assertTrue(r.success)
        self.assertFalse(r.attempts[-1].scan["ok"])
        self.assertIn("trivy exploded", r.attempts[-1].scan["detail"])

    def test_image_is_scanned_before_it_is_removed(self):
        order = []
        client, build_fn = FakeClient([fence(GOOD)]), FakeBuild([OK])
        healer.heal(INFO, client, build_fn=build_fn, remove_fn=lambda t: order.append("remove"),
                    smoke_fn=smoke_script(PASS), scan_fn=lambda t: order.append("scan") or {"ok": True})
        self.assertEqual(order, ["scan", "remove"])


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    FLASK = str(ROOT / "examples" / "flask_app")

    def test_smoke_command_codes(self):
        with patch("dockhealer.cli.smoke.run_smoke", return_value=PASS):
            self.assertEqual(run_cli(["smoke", "img:1", "--project", self.FLASK])[0], 0)
        with patch("dockhealer.cli.smoke.run_smoke", return_value=UNREACHABLE):
            code, out, _ = run_cli(["smoke", "img:1", "--project", self.FLASK])
        self.assertEqual(code, 4)
        self.assertIn("UNREACHABLE", out)
        with patch("dockhealer.cli.smoke.run_smoke", return_value=smoke.SmokeResult(False, skipped=True, detail="x")):
            self.assertEqual(run_cli(["smoke", "img:1", "--project", self.FLASK])[0], 5)

    def test_scan_command(self):
        res = vulnscan.parse_trivy_json(__import__("json").dumps({"Results": [{"Target": "t", "Vulnerabilities": [
            {"VulnerabilityID": "CVE-9", "PkgName": "p", "InstalledVersion": "1", "FixedVersion": "2", "Severity": "HIGH"}]}]}), ["HIGH", "CRITICAL"])
        with patch("dockhealer.cli.vulnscan.scan_image", return_value=res):
            code, out, _ = run_cli(["scan", "img:1"])
            self.assertEqual(code, 0)
            self.assertIn("CVE-9", out)
            self.assertIn("fixed in 2", out)
            self.assertEqual(run_cli(["scan", "img:1", "--fail-on", "HIGH"])[0], 4)
            self.assertEqual(run_cli(["scan", "img:1", "--fail-on", "CRITICAL"])[0], 0)

    def test_scan_unavailable_exit_2(self):
        res = vulnscan.ScanResult(available=False, ok=False, detail="trivy is not installed: scan skipped")
        with patch("dockhealer.cli.vulnscan.scan_image", return_value=res):
            code, out, _ = run_cli(["scan", "img:1"])
        self.assertEqual(code, 2)
        self.assertIn("not installed", out)

    def test_heal_flags_control_smoke_and_scan(self):
        rep = healer.HealReport(project="p", success=True, stop_reason="built", attempts=[], final_dockerfile="x",
                                llm_calls=1, builds=1, duration_s=1.0)
        with patch("dockhealer.cli.healer.heal", return_value=rep) as h, patch("dockhealer.cli.healer.write_report"):
            run_cli(["heal", self.FLASK])
            self.assertIsNotNone(h.call_args.kwargs["smoke_fn"])
            self.assertIsNone(h.call_args.kwargs["scan_fn"])
            run_cli(["heal", self.FLASK, "--no-smoke", "--scan"])
            self.assertIsNone(h.call_args.kwargs["smoke_fn"])
            self.assertIsNotNone(h.call_args.kwargs["scan_fn"])


if __name__ == "__main__":
    unittest.main()
