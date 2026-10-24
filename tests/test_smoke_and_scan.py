"""Tests for smoke.py and vulnscan.py. Docker and Trivy are replaced by scripted fakes; the HTTP probe is
also tested against a REAL local HTTP server. NOT proven here: the behaviour of a real Docker engine or a
real Trivy run (the Trivy JSON below is hand-written from its documented schema)."""

import json
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from dockhealer import smoke, scanner, vulnscan

ROOT = Path(__file__).resolve().parent.parent
INFO = scanner.scan(ROOT / "examples" / "flask_app")        # port 5000


def cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class FakeDocker:
    def __init__(self, run_rc=0, states=(("true", "0"),), logs="", run_err=""):
        self.calls, self.states, self.logs, self.run_rc, self.run_err = [], list(states), logs, run_rc, run_err

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        sub = cmd[1]
        if sub == "run":
            return cp(self.run_rc, "id\n" if self.run_rc == 0 else "", self.run_err)
        if sub == "inspect":
            st = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return cp(0, f"{st[0]} {st[1]}\n")
        if sub == "logs":
            return cp(0, self.logs)
        return cp()

    def subcommands(self):
        return [c[1] for c in self.calls]


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def run(docker, probe, timeout=5.0, info=INFO):
    clk = Clock()
    return smoke.run_smoke("img:1", info, startup_timeout_s=timeout, runner=docker, probe=probe,
                           sleep=clk.sleep, clock=clk.now, port_fn=lambda: 40123)


class SmokePass(unittest.TestCase):
    def test_answers_on_health(self):
        d = FakeDocker()
        r = run(d, lambda url: 200)
        self.assertTrue(r.ok)
        self.assertEqual((r.status, r.path), (200, "/health"))
        self.assertEqual(d.subcommands()[-1], "rm", "container must always be removed")

    def test_container_is_run_with_restrictions_and_no_host_access(self):
        d = FakeDocker()
        run(d, lambda url: 200)
        cmd = next(c for c in d.calls if c[1] == "run")
        joined = " ".join(cmd)
        self.assertIn("127.0.0.1:40123:5000", joined)             # published on loopback only
        self.assertIn("--cap-drop ALL", joined)
        self.assertIn("no-new-privileges", joined)
        for forbidden in ("--privileged", "-v ", "--volume", "--mount", "--network host", "--pid host"):
            self.assertNotIn(forbidden, joined)
        self.assertIn("--memory", joined)
        self.assertIn("--pids-limit", joined)

    def test_waits_until_the_app_is_up(self):
        answers = iter([None] * 4 + [200] * 2)        # two probes per round: rounds 1-2 silent, round 3 answers
        r = run(FakeDocker(), lambda url: next(answers))
        self.assertTrue(r.ok)
        self.assertGreaterEqual(r.duration_s, 2.0, 'it must have waited, not answered instantly')

    def test_falls_back_to_root_path(self):
        r = run(FakeDocker(), lambda url: 404 if url.endswith("/health") else 200)
        self.assertEqual((r.ok, r.path, r.status), (True, "/", 200))

    def test_any_non_5xx_answer_counts_as_reachable(self):
        r = run(FakeDocker(), lambda url: 404)
        self.assertTrue(r.ok)

    def test_no_port_is_skipped_not_passed(self):
        import dataclasses
        r = run(FakeDocker(), lambda url: 200, info=dataclasses.replace(INFO, port=None))
        self.assertTrue(r.skipped)
        self.assertFalse(r.ok)


class SmokeFailures(unittest.TestCase):
    def diag(self, r):
        return smoke.diagnose_smoke(r, INFO)

    def test_container_exits_with_missing_module(self):
        r = run(FakeDocker(states=[("false", "1")], logs="Traceback...\nModuleNotFoundError: No module named 'flaskk'\n"), lambda u: None)
        self.assertTrue(r.container_exited and not r.ok)
        self.assertEqual(r.exit_code, 1)
        d = self.diag(r)
        self.assertEqual((d.category, d.action), ("RUNTIME_MODULE_MISSING", smoke.REPAIR))
        self.assertIn("No module named 'flaskk'", d.evidence[-1])

    def test_never_answers_means_unreachable_with_bind_hint(self):
        logs = " * Serving Flask app 'app'\n * Running on http://127.0.0.1:5000\n"
        r = run(FakeDocker(logs=logs), lambda u: None, timeout=3)
        self.assertFalse(r.ok or r.container_exited)
        d = self.diag(r)
        self.assertEqual((d.category, d.action), ("UNREACHABLE", smoke.REPAIR))
        self.assertIn("0.0.0.0", d.hint)
        self.assertTrue(any("127.0.0.1" in e for e in d.evidence))

    def test_only_5xx_is_an_app_error_not_a_container_recipe_problem(self):
        r = run(FakeDocker(), lambda u: 500, timeout=3)
        d = self.diag(r)
        self.assertEqual((d.category, d.action), ("HTTP_5XX", smoke.STOP))

    def test_docker_run_itself_fails(self):
        r = run(FakeDocker(run_rc=127, run_err="OCI runtime create failed: executable file not found in $PATH"), lambda u: 200)
        self.assertEqual(self.diag(r).category, "CMD_NOT_FOUND")

    def test_exit_zero_means_nothing_kept_running(self):
        r = run(FakeDocker(states=[("false", "0")], logs="done\n"), lambda u: None)
        self.assertEqual(self.diag(r).category, "EXITED_IMMEDIATELY")

    def test_permission_and_port_conflicts(self):
        r = run(FakeDocker(states=[("false", "1")], logs="PermissionError: [Errno 13] Permission denied: '/app/x'\n"), lambda u: None)
        self.assertEqual(self.diag(r).category, "RUNTIME_PERMISSION")
        r = run(FakeDocker(states=[("false", "1")], logs="OSError: [Errno 98] Address already in use\n"), lambda u: None)
        d = self.diag(r)
        self.assertEqual((d.category, d.action), ("PORT_IN_USE", smoke.STOP))

    def test_unknown_crash_is_still_diagnosed(self):
        r = run(FakeDocker(states=[("false", "3")], logs="something odd\n"), lambda u: None)
        self.assertEqual(self.diag(r).category, "STARTUP_CRASH")

    def test_container_removed_even_when_the_probe_blows_up(self):
        d = FakeDocker()

        def boom(url):
            raise RuntimeError("probe crashed")
        with self.assertRaises(RuntimeError):
            run(d, boom)
        self.assertEqual(d.subcommands()[-1], "rm")

    def test_secrets_in_container_logs_are_redacted(self):
        r = run(FakeDocker(states=[("false", "1")], logs="PASSWORD=hunter2hunter2\nboom\n"), lambda u: None)
        self.assertNotIn("hunter2", r.logs)

    def test_passing_or_skipped_results_have_no_diagnosis(self):
        self.assertIsNone(self.diag(smoke.SmokeResult(ok=True)))
        self.assertIsNone(self.diag(smoke.SmokeResult(ok=False, skipped=True)))


class RealHttpProbe(unittest.TestCase):
    """http_status against a real socket: this part is not mocked."""

    @classmethod
    def setUpClass(cls):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                code = {"/ok": 200, "/missing": 404, "/boom": 500}.get(self.path, 200)
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass
        cls.srv = HTTPServer(("127.0.0.1", 0), H)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_statuses(self):
        base = f"http://127.0.0.1:{self.port}"
        self.assertEqual(smoke.http_status(base + "/ok"), 200)
        self.assertEqual(smoke.http_status(base + "/missing"), 404)
        self.assertEqual(smoke.http_status(base + "/boom"), 500)

    def test_nothing_listening_is_none(self):
        self.assertIsNone(smoke.http_status(f"http://127.0.0.1:{smoke.free_port()}/", timeout=1))

    def test_free_port_is_usable(self):
        self.assertTrue(1024 < smoke.free_port() < 65536)


TRIVY_JSON = json.dumps({"Results": [
    {"Target": "python:3.12-slim (debian 12)", "Vulnerabilities": [
        {"VulnerabilityID": "CVE-1", "PkgName": "libc6", "InstalledVersion": "2.36", "FixedVersion": "2.37", "Severity": "HIGH"},
        {"VulnerabilityID": "CVE-2", "PkgName": "zlib", "InstalledVersion": "1.2", "Severity": "CRITICAL"},
        {"VulnerabilityID": "CVE-3", "PkgName": "openssl", "InstalledVersion": "3.0", "FixedVersion": "3.1", "Severity": "HIGH"}]},
    {"Target": "app/requirements.txt", "Vulnerabilities": None}]})


class VulnScanTests(unittest.TestCase):
    def test_parse(self):
        r = vulnscan.parse_trivy_json(TRIVY_JSON, ["HIGH", "CRITICAL"])
        self.assertEqual((r.total, r.fixable), (3, 2))
        self.assertEqual(r.counts, {"CRITICAL": 1, "HIGH": 2})
        self.assertEqual(r.top[0]["id"], "CVE-2", "most severe first")
        self.assertEqual(r.top[0]["fixed"], "")

    def test_empty_results(self):
        r = vulnscan.parse_trivy_json(json.dumps({"Results": None}))
        self.assertEqual((r.ok, r.total), (True, 0))
        self.assertIn("does not show the image is secure", vulnscan.summarize(r))

    def test_trivy_not_installed_is_a_clean_skip(self):
        r = vulnscan.scan_image("img:1", which=lambda name: None)
        self.assertEqual((r.available, r.ok), (False, False))
        self.assertIn("skipped", vulnscan.summarize(r))

    def test_command_is_a_list_with_vuln_only_and_the_requested_severities(self):
        seen = {}

        def runner(cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw
            return cp(0, TRIVY_JSON)
        r = vulnscan.scan_image("img:1", severities=("CRITICAL",), runner=runner, which=lambda n: "/usr/bin/trivy")
        self.assertTrue(r.ok)
        self.assertIsInstance(seen["cmd"], list)
        self.assertEqual(seen["cmd"][-1], "img:1")
        self.assertIn("vuln", seen["cmd"])
        self.assertIn("CRITICAL", seen["cmd"])
        self.assertNotIn("shell", seen["kw"])

    def test_failures_become_results_not_exceptions(self):
        which = lambda n: "/usr/bin/trivy"
        r = vulnscan.scan_image("i", runner=lambda c, **k: cp(1, "", "FATAL failed to download vulnerability DB"), which=which)
        self.assertEqual((r.available, r.ok), (True, False))
        self.assertIn("network", r.detail)

        def hang(c, **k):
            raise subprocess.TimeoutExpired(c, 1)
        self.assertFalse(vulnscan.scan_image("i", runner=hang, which=which).ok)
        self.assertFalse(vulnscan.scan_image("i", runner=lambda c, **k: cp(0, "not json"), which=which).ok)


if __name__ == "__main__":
    unittest.main()
