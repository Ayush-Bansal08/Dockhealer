"""Data-driven: every tests/fixtures/*.log declares its expected category on line 1 (`# expect: X`).
To add a REAL captured log, save it there with that header and the test picks it up automatically."""

import unittest
from pathlib import Path

from dockhealer import diagnose
from dockhealer.builder import BuildResult

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXPECTED_ACTION = {
    diagnose.NETWORK: diagnose.RETRY,
    diagnose.DOCKER_UNAVAILABLE: diagnose.STOP,
    diagnose.DISK_FULL: diagnose.STOP,
    diagnose.DEPENDENCY_IN_PROJECT: diagnose.STOP,
}


def load(path: Path) -> tuple[str, str]:
    first, _, rest = path.read_text().partition("\n")
    assert first.startswith("# expect:"), f"{path.name} lacks an '# expect:' header"
    return first.split(":", 1)[1].strip(), rest


class FixtureTests(unittest.TestCase):
    def test_every_fixture_classifies_as_declared(self):
        paths = sorted(FIXTURES.glob("*.log"))
        self.assertGreaterEqual(len(paths), 10)
        for p in paths:
            with self.subTest(fixture=p.name):
                expected, log = load(p)
                d = diagnose.classify_log(log)
                self.assertEqual(d.category, expected)
                self.assertEqual(d.action, EXPECTED_ACTION.get(expected, diagnose.REPAIR))
                self.assertTrue(d.evidence, "diagnosis must cite evidence from the log")

    def test_every_category_is_covered_by_a_fixture(self):
        declared = {load(p)[0] for p in FIXTURES.glob("*.log")}
        categories = {r.category for r in diagnose.RULES} | {diagnose.UNKNOWN, diagnose.DEPENDENCY_IN_PROJECT}
        self.assertEqual(categories - declared, set())


class DetailTests(unittest.TestCase):
    def test_failed_step_and_dockerfile_line_are_extracted(self):
        _, log = load(FIXTURES / "pip_dependency_project.log")
        d = diagnose.classify_log(log)
        self.assertEqual(d.failed_step, "pip install --no-cache-dir -r requirements.txt")
        self.assertEqual(d.failed_line, 5)

    def test_network_beats_dependency_when_pip_has_no_connectivity(self):
        """pip says 'No matching distribution' when offline; blaming the package would be a wrong diagnosis."""
        _, log = load(FIXTURES / "pip_network.log")
        self.assertIn("No matching distribution found", log)
        self.assertEqual(diagnose.classify_log(log).category, diagnose.NETWORK)

    def test_docker_socket_permission_is_not_a_dockerfile_permission_problem(self):
        _, log = load(FIXTURES / "daemon_socket_permission.log")
        self.assertEqual(diagnose.classify_log(log).category, diagnose.DOCKER_UNAVAILABLE)

    def test_base_image_registry_lookup_failure_is_network_not_bad_tag(self):
        _, log = load(FIXTURES / "base_image_network.log")
        self.assertEqual(diagnose.classify_log(log).category, diagnose.NETWORK)

    def test_success_has_no_diagnosis(self):
        self.assertIsNone(diagnose.diagnose(BuildResult(True, 0, 1.0, "", "t")))

    def test_timeout_is_stop(self):
        d = diagnose.diagnose(BuildResult(False, None, 600.0, "", "t", timed_out=True))
        self.assertEqual((d.category, d.action), (diagnose.TIMEOUT, diagnose.STOP))

    def test_log_for_model_strips_prefixes_redacts_secrets_and_is_bounded(self):
        noisy = "\n".join(f"#7 1.{i} line {i}" for i in range(100)) + "\n#7 2.0 PASSWORD=hunter2hunter2"
        out = diagnose.log_for_model(noisy, max_lines=10)
        self.assertEqual(len(out.splitlines()), 10)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("#7", out)


if __name__ == "__main__":
    unittest.main()
