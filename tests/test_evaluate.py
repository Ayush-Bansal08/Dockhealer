import tempfile
import unittest
from pathlib import Path

from dockhealer import evaluate

ROOT = Path(__file__).resolve().parent.parent


class EvaluationTests(unittest.TestCase):
    def test_corpus_passes_without_docker(self):
        report = evaluate.evaluate(ROOT)
        self.assertTrue(report.success)
        self.assertTrue(all(case.validation_ok for case in report.baselines))
        self.assertEqual(len(report.failures), 5)
        self.assertTrue(any(not case.validation_ok for case in report.failures))
        self.assertTrue(any(case.validation_ok for case in report.failures))

    def test_successful_injected_build_is_a_regression(self):
        class Built:
            success = True
            tag = "test:image"

        calls = []

        def build_fn(info, text, timeout_s):
            calls.append(info.root)
            return Built()

        report = evaluate.evaluate(ROOT, build=True, build_fn=build_fn, remove_fn=lambda tag: None)
        self.assertFalse(report.success)
        self.assertEqual(len(calls), 7)

    def test_report_is_json(self):
        report = evaluate.evaluate(ROOT)
        with tempfile.TemporaryDirectory() as directory:
            path = evaluate.write_report(report, Path(directory) / "evaluation.json")
            self.assertIn('"success": true', path.read_text())


if __name__ == "__main__":
    unittest.main()
