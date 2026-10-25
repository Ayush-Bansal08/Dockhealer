import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dockhealer import cli, diagnose, healer, repair, scanner
from dockhealer.builder import BuildResult
from dockhealer.llm import ChatResult

ROOT = Path(__file__).resolve().parent.parent
FLASK = str(ROOT / "examples" / "flask_app")
INFO = scanner.scan(FLASK)
GOOD = (ROOT / "baselines" / "flask_app.Dockerfile").read_text()


class RepairPromptTests(unittest.TestCase):
    def test_prompt_contains_facts_current_dockerfile_and_problem(self):
        p = repair.build_repair_prompt(INFO, GOOD, "something broke")
        self.assertIn("start command: python app.py", p)
        self.assertIn("FROM python:3.12-slim", p)
        self.assertIn("something broke", p)

    def test_problem_is_truncated(self):
        p = repair.build_repair_prompt(INFO, GOOD, "x" * 50_000)
        self.assertLess(len(p), 20_000)

    def test_system_prompt_forbids_editing_source_and_marks_logs_untrusted(self):
        self.assertIn("untrusted", repair.REPAIR_SYSTEM)
        self.assertIn("Never ask to edit application source", repair.REPAIR_SYSTEM)

    def test_generation_guidance_matches_base_image_users(self):
        self.assertIn("Python images do not", repair.SYSTEM_PROMPT)
        self.assertIn("create an app user", repair.SYSTEM_PROMPT)
        self.assertIn("do not write `exec` as an item", repair.SYSTEM_PROMPT)

    def test_build_failure_description(self):
        log = '#7 ERROR: process "/bin/sh -c apt-get install -y x" did not complete successfully: exit code: 100\n#7 E: Unable to locate package x\nDockerfile:3\n'
        res = BuildResult(False, 1, 1.0, log, "t:1")
        text = repair.describe_build_failure(res, diagnose.diagnose(res))
        self.assertIn("DEPENDENCY_IN_DOCKERFILE", text)
        self.assertIn("apt-get install -y x", text)
        self.assertIn("Dockerfile line 3", text)

    def test_reply_without_dockerfile_raises(self):
        class C:
            def chat(self, *a, **k):
                return ChatResult("no idea", "m", 0.1, 1)
        with self.assertRaises(repair.RepairError):
            repair.repair_dockerfile(C(), INFO, GOOD, "p")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


def fake_report(success, reason=healer.BUILT):
    return healer.HealReport(project=FLASK, success=success, stop_reason=reason, attempts=[
        healer.Attempt(1, "generate", "built" if success else "build_failed", dockerfile=GOOD,
                       diagnosis=None if success else {"category": "DEPENDENCY_IN_PROJECT"})],
        final_dockerfile=GOOD, llm_calls=1, builds=1, duration_s=1.0, model="m")


class HealCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)

    def test_success_writes_dockerfile_and_report(self):
        out = self.tmp_path / "out"
        out.mkdir()
        with patch("dockhealer.cli.healer.heal", return_value=fake_report(True)):
            code, stdout, _ = run(["heal", FLASK, "--out", str(out), "--report-dir", str(self.tmp_path / "rep")])
        self.assertEqual(code, 0)
        self.assertIn("RESULT: HEALED", stdout)
        self.assertEqual((out / "Dockerfile").read_text(), GOOD)
        self.assertTrue((self.tmp_path / "rep" / "report.json").exists())

    def test_failure_exit_4_and_nothing_written(self):
        out = self.tmp_path / "out"
        out.mkdir()
        with patch("dockhealer.cli.healer.heal", return_value=fake_report(False, healer.HUMAN_REQUIRED)):
            code, stdout, _ = run(["heal", FLASK, "--out", str(out), "--report-dir", str(self.tmp_path / "rep")])
        self.assertEqual(code, 4)
        self.assertIn("NOT HEALED", stdout)
        self.assertFalse((out / "Dockerfile").exists())

    def test_existing_dockerfile_is_not_overwritten_without_force(self):
        out = self.tmp_path / "out"
        out.mkdir()
        (out / "Dockerfile").write_text("mine")
        with patch("dockhealer.cli.healer.heal", return_value=fake_report(True)):
            code, _, err = run(["heal", FLASK, "--out", str(out), "--report-dir", str(self.tmp_path / "rep")])
        self.assertEqual(code, 2)
        self.assertEqual((out / "Dockerfile").read_text(), "mine")

    def test_budgets_are_capped(self):
        for flag, value in (("--max-attempts", "99"), ("--max-attempts", "0"), ("--max-retries", "50")):
            with self.subTest(flag=flag, value=value):
                code, _, err = run(["heal", FLASK, flag, value])
                self.assertEqual(code, 2)
                self.assertIn("must be between", err)

    def test_unsupported_project_exit_1(self):
        with tempfile.TemporaryDirectory() as d:
            code, _, _ = run(["heal", d])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
