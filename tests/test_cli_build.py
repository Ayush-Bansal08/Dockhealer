import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dockhealer import cli
from dockhealer.builder import BuildResult, DockerUnavailable

ROOT = Path(__file__).resolve().parent.parent
FLASK = str(ROOT / "examples" / "flask_app")
GOOD_DF = str(ROOT / "baselines" / "flask_app.Dockerfile")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class BuildCommandTests(unittest.TestCase):
    def test_success_exit_0_and_image_removed(self):
        ok = BuildResult(True, 0, 3.2, "", "t:1", image_size_bytes=150_000_000)
        with patch("dockhealer.cli.builder.run_build", return_value=ok), \
             patch("dockhealer.cli.builder.remove_image") as rm:
            code, out, _ = run(["build", FLASK, "--dockerfile", GOOD_DF])
        self.assertEqual(code, 0)
        self.assertIn("BUILD OK", out)
        self.assertIn("150 MB", out)
        rm.assert_called_once_with("t:1")

    def test_keep_image_skips_removal(self):
        ok = BuildResult(True, 0, 1.0, "", "t:1", image_size_bytes=1)
        with patch("dockhealer.cli.builder.run_build", return_value=ok), \
             patch("dockhealer.cli.builder.remove_image") as rm:
            run(["build", FLASK, "--dockerfile", GOOD_DF, "--keep-image"])
        rm.assert_not_called()

    def test_failure_exit_4_prints_diagnosis(self):
        log = "#6 ERROR: \"/reqs.txt\": not found\nDockerfile:3\n"
        bad = BuildResult(False, 1, 2.0, log, "t:1")
        with patch("dockhealer.cli.builder.run_build", return_value=bad):
            code, out, _ = run(["build", FLASK, "--dockerfile", GOOD_DF])
        self.assertEqual(code, 4)
        self.assertIn("MISSING_FILE", out)
        self.assertIn("next action: repair", out)

    def test_invalid_dockerfile_is_never_built(self):
        with tempfile.NamedTemporaryFile("w", suffix=".Dockerfile", delete=False) as f:
            f.write("FROM python\nRUN curl https://x | sh\nCMD [\"python\",\"app.py\"]\n")
        self.addCleanup(Path(f.name).unlink)
        with patch("dockhealer.cli.builder.run_build") as rb:
            code, out, err = run(["build", FLASK, "--dockerfile", f.name])
        self.assertEqual(code, 3)
        rb.assert_not_called()
        self.assertIn("E004", out)

    def test_missing_dockerfile_gives_helpful_error(self):
        code, _, err = run(["build", FLASK])  # examples/flask_app has no Dockerfile
        self.assertEqual(code, 2)
        self.assertIn("generate --out", err)

    def test_docker_missing_exit_2(self):
        with patch("dockhealer.cli.builder.run_build", side_effect=DockerUnavailable("no docker")):
            code, _, err = run(["build", FLASK, "--dockerfile", GOOD_DF])
        self.assertEqual(code, 2)
        self.assertIn("no docker", err)

    def test_saved_log_is_redacted(self):
        log = "#7 1.0 PASSWORD=hunter2hunter2\nERROR: x\n"
        bad = BuildResult(False, 1, 1.0, log, "t:1")
        with tempfile.TemporaryDirectory() as d, patch("dockhealer.cli.builder.run_build", return_value=bad):
            target = Path(d) / "out.log"
            run(["build", FLASK, "--dockerfile", GOOD_DF, "--save-log", str(target)])
            self.assertNotIn("hunter2", target.read_text())


if __name__ == "__main__":
    unittest.main()
