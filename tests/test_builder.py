import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from dockhealer import builder, scanner

ROOT = Path(__file__).resolve().parent.parent
DF = "FROM python:3.12-slim\nCMD [\"python\", \"app.py\"]\n"


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["docker"], returncode, stdout, stderr)


class StagedContextTests(unittest.TestCase):
    def _project(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        root = Path(d.name)
        (root / "requirements.txt").write_text("flask\n")
        (root / "app.py").write_text("x=1\n")
        (root / ".env").write_text("SECRET=abcd1234\n")
        (root / "sub").mkdir()
        (root / "sub" / "mod.py").write_text("y=2\n")
        return root

    def test_only_approved_files_are_copied_and_original_is_untouched(self):
        root = self._project()
        before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
        info = scanner.scan(root)
        with builder.staged_context(info) as ctx:
            copied = sorted(p.relative_to(ctx).as_posix() for p in ctx.rglob("*") if p.is_file())
            self.assertEqual(copied, ["app.py", "requirements.txt", "sub/mod.py"])
            self.assertFalse((ctx / ".env").exists())
            self.assertEqual((ctx / "app.py").read_text(), "x=1\n")
            staged_path = ctx
        self.assertFalse(staged_path.exists(), "staging dir must be deleted afterwards")
        self.assertEqual(sorted(p.relative_to(root).as_posix() for p in root.rglob("*")), before)

    def test_file_swapped_for_symlink_after_scan_is_skipped(self):
        root = self._project()
        info = scanner.scan(root)
        outside = tempfile.NamedTemporaryFile("w", delete=False)
        outside.write("stolen")
        outside.close()
        self.addCleanup(os.unlink, outside.name)
        (root / "app.py").unlink()
        os.symlink(outside.name, root / "app.py")
        with builder.staged_context(info) as ctx:
            self.assertFalse((ctx / "app.py").exists())

    def test_context_size_limit(self):
        root = self._project()
        info = scanner.scan(root)
        old = builder.MAX_CONTEXT_BYTES
        builder.MAX_CONTEXT_BYTES = 5
        self.addCleanup(setattr, builder, "MAX_CONTEXT_BYTES", old)
        with self.assertRaises(builder.ContextTooLarge):
            with builder.staged_context(info):
                pass


class RunBuildTests(unittest.TestCase):
    def setUp(self):
        self.info = scanner.scan(ROOT / "examples" / "flask_app")

    def test_command_is_a_list_with_expected_flags_and_a_timeout(self):
        runner = MagicMock(side_effect=[completed(0, stderr="#1 DONE"), completed(0, stdout="123456\n")])
        res = builder.run_build(self.info, DF, tag="t:1", timeout_s=42, runner=runner)
        cmd = runner.call_args_list[0].args[0]
        kwargs = runner.call_args_list[0].kwargs
        self.assertIsInstance(cmd, list)
        self.assertEqual(cmd[:2], ["docker", "build"])
        self.assertIn("--progress=plain", cmd)
        self.assertEqual(cmd[cmd.index("--tag") + 1], "t:1")
        self.assertEqual(kwargs["timeout"], 42)
        self.assertNotIn("shell", kwargs)
        self.assertTrue(res.success)
        self.assertEqual(res.image_size_bytes, 123456)

    def test_dockerfile_is_written_outside_the_context_and_the_project(self):
        seen = {}

        def runner(cmd, **kw):
            df = Path(cmd[cmd.index("-f") + 1])
            ctx = Path(cmd[-1])
            seen["text"] = df.read_text()
            seen["inside"] = ctx in df.parents
            seen["in_project"] = Path(self.info.root) in df.parents
            return completed(1, stderr="boom")

        builder.run_build(self.info, DF, runner=runner)
        self.assertEqual(seen["text"], DF)
        self.assertFalse(seen["inside"])
        self.assertFalse(seen["in_project"])

    def test_failure_returns_result_not_exception(self):
        runner = MagicMock(return_value=completed(1, stderr="ERROR: failed to solve"))
        res = builder.run_build(self.info, DF, runner=runner)
        self.assertFalse(res.success)
        self.assertEqual(res.returncode, 1)
        self.assertIn("failed to solve", res.log)
        self.assertEqual(runner.call_count, 1, "no image inspection after a failed build")

    def test_timeout_is_reported_even_though_stdout_is_bytes(self):
        exc = subprocess.TimeoutExpired(["docker"], 5, output=b"partial out", stderr=b"partial err")
        res = builder.run_build(self.info, DF, runner=MagicMock(side_effect=exc))
        self.assertTrue(res.timed_out)
        self.assertIsNone(res.returncode)
        self.assertIn("partial err", res.log)

    def test_missing_docker_cli_raises_dockerunavailable(self):
        with self.assertRaises(builder.DockerUnavailable):
            builder.run_build(self.info, DF, runner=MagicMock(side_effect=FileNotFoundError()))

    def test_huge_log_is_truncated_keeping_the_end(self):
        big = "x" * 100_000 + "THE-REAL-ERROR"
        res = builder.run_build(self.info, DF, runner=MagicMock(return_value=completed(1, stderr=big)))
        self.assertLessEqual(len(res.log), builder.LOG_TAIL_CHARS + 50)
        self.assertTrue(res.log.endswith("THE-REAL-ERROR"))

    def test_clock_injection_gives_deterministic_duration(self):
        ticks = iter([100.0, 107.5])
        res = builder.run_build(self.info, DF, runner=MagicMock(return_value=completed(1)), clock=lambda: next(ticks))
        self.assertEqual(res.duration_s, 7.5)

    def test_tag_is_valid_docker_reference(self):
        tag = builder.make_tag(self.info)
        self.assertRegex(tag, r"^[a-z0-9][a-z0-9_.-]*:\d+$")


class HelperTests(unittest.TestCase):
    def test_image_size_handles_garbage_and_failure(self):
        self.assertIsNone(builder.image_size("t", runner=MagicMock(return_value=completed(1))))
        self.assertIsNone(builder.image_size("t", runner=MagicMock(return_value=completed(0, stdout="abc"))))

    def test_remove_image(self):
        self.assertTrue(builder.remove_image("t", runner=MagicMock(return_value=completed(0))))
        self.assertFalse(builder.remove_image("t", runner=MagicMock(side_effect=OSError())))


if __name__ == "__main__":
    unittest.main()
