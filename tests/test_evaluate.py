"""Tests for the evaluation harness and for heal(initial_dockerfile=...).

These prove the HARNESS arithmetic and wiring (what is counted, when a run is 'healthy', that a must-stop
scenario really makes no model call). They do NOT prove anything about real Docker/Ollama behaviour: the
numbers a real evaluation produces come only from `dockhealer eval` on a machine that has both.
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from dockhealer import cli, evaluate, healer, scanner, validator
from dockhealer.builder import BuildResult
from test_healer import FakeClient, GOOD, OK, failed, fence, fixture_log, py

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "evaluation" / "scenarios.json"


class Manifest(unittest.TestCase):
    def test_every_path_exists_and_scenarios_load(self):
        sc = evaluate.load_scenarios(MANIFEST, ROOT)
        self.assertGreaterEqual(len(sc), 9)
        self.assertEqual({s.kind for s in sc}, {"generate", "inject"})
        self.assertEqual(len({s.id for s in sc}), len(sc))

    def test_references_are_clean_and_injected_faults_are_what_the_manifest_claims(self):
        for s in evaluate.load_scenarios(MANIFEST, ROOT):
            info = scanner.scan(ROOT / s.project)
            if s.reference:
                self.assertTrue(validator.validate((ROOT / s.reference).read_text(), info).ok, s.id)
            if s.kind == "inject":
                errors = validator.validate((ROOT / s.dockerfile).read_text(), info).errors
                if s.expect_category == "VALIDATION":
                    self.assertTrue(errors, f"{s.id} is expected to be caught by the validator")
                else:
                    self.assertEqual(errors, [], f"{s.id} must pass the validator so that it fails at build/run time")

    def test_bad_manifests_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.json"
            for bad in ({"scenarios": [{"id": "a", "kind": "nope", "project": ".", "expect_outcome": "healed"}]},
                        {"scenarios": [{"id": "a", "kind": "inject", "project": ".", "expect_outcome": "healed"}]},
                        {"scenarios": [{"id": "a", "kind": "generate", "project": "missing_dir", "expect_outcome": "healed"}]}):
                p.write_text(json.dumps(bad))
                with self.assertRaises((ValueError, FileNotFoundError)):
                    evaluate.load_scenarios(p, d)


class Wilson(unittest.TestCase):
    def test_known_values_and_small_sample_width(self):
        lo, hi = evaluate.wilson(3, 3)
        self.assertAlmostEqual(lo, 0.438, places=3)
        self.assertEqual(hi, 1.0)
        lo, hi = evaluate.wilson(5, 10)
        self.assertAlmostEqual(lo, 0.237, places=3)
        self.assertAlmostEqual(hi, 0.763, places=3)
        self.assertEqual(evaluate.wilson(0, 0), (0.0, 1.0))

    def test_zero_successes_is_not_a_zero_width_interval(self):
        lo, hi = evaluate.wilson(0, 3)
        self.assertEqual(lo, 0.0)
        self.assertGreater(hi, 0.5)


def build_fn(info, dockerfile, *, timeout_s=0, **kw):
    """Fake docker: fails exactly the way the injected faults would, otherwise succeeds."""
    if info.root.endswith("flask_bad_pin"):
        return failed(fixture_log("pip_dependency_project.log"))
    if "python:3.99" in dockerfile:
        return failed(fixture_log("base_image_not_found.log"))
    return BuildResult(True, 0, 4.0, "", "t:1", image_size_bytes=150_000_000)


def make_heal_fn(client):
    return lambda info, *, initial_dockerfile, max_attempts: healer.heal(
        info, client, max_attempts=max_attempts, build_fn=build_fn, remove_fn=lambda t: None,
        initial_dockerfile=initial_dockerfile)


class RunEval(unittest.TestCase):
    def setUp(self):
        by_id = {s.id: s for s in evaluate.load_scenarios(MANIFEST, ROOT)}
        self.scenarios = [by_id[i] for i in ("gen-flask", "inj-bad-base-image", "stop-bad-pin")]
        # replies consumed in order: gen-flask attempt 1 (broken), attempt 2 (repair); inj-bad-base-image repair
        self.client = FakeClient([fence(py("3.99")), fence(GOOD), fence(GOOD)])
        self.rows = evaluate.run_eval(self.scenarios, trials=1, heal_fn=make_heal_fn(self.client), root=ROOT)

    def row(self, sid, cond="dockhealer"):
        return next(r for r in self.rows if r["scenario"] == sid and r["condition"] == cond)

    def test_reference_row_uses_no_model_and_no_repair(self):
        r = self.row("gen-flask", "reference")
        self.assertEqual((r["success"], r["llm_calls"]), (True, 0))

    def test_generation_is_repaired_and_one_shot_is_recorded_separately(self):
        r = self.row("gen-flask")
        self.assertTrue(r["success"])
        self.assertFalse(r["first_attempt_healthy"], "attempt 1 used the broken base image")
        self.assertEqual((r["first_failure_category"], r["llm_calls"]), ("BASE_IMAGE_NOT_FOUND", 2))

    def test_injected_fault_is_detected_classified_and_repaired(self):
        r = self.row("inj-bad-base-image")
        self.assertFalse(r["first_attempt_healthy"])
        self.assertEqual(r["first_failure_category"], "BASE_IMAGE_NOT_FOUND")
        self.assertEqual((r["success"], r["llm_calls"]), (True, 1))

    def test_must_stop_scenario_stops_without_calling_the_model(self):
        r = self.row("stop-bad-pin")
        self.assertEqual((r["success"], r["stop_reason"], r["llm_calls"]), (False, healer.HUMAN_REQUIRED, 0))
        self.assertEqual(r["first_failure_category"], "DEPENDENCY_IN_PROJECT")

    def test_aggregate_counts(self):
        a = evaluate.aggregate(self.rows)
        self.assertEqual((a["generation"]["one_shot_success"]["k"], a["generation"]["one_shot_success"]["n"]), (0, 1))
        self.assertEqual(a["generation"]["success_with_repair"]["k"], 1)
        self.assertEqual(a["generation"]["repair_rescued"], 1)
        self.assertEqual((a["injection"]["repaired"]["k"], a["injection"]["repaired"]["n"]), (1, 1))
        self.assertEqual((a["injection"]["stopped_correctly"]["k"], a["injection"]["stopped_correctly"]["n"]), (1, 1))
        self.assertEqual((a["injection"]["classified_as_expected"]["k"], a["injection"]["classified_as_expected"]["n"]), (2, 2))
        self.assertEqual(a["reference"]["healthy"]["k"], 1)

    def test_report_states_provenance_and_limits_and_roundtrips_from_disk(self):
        meta = {"model": "fake", "trials": 1, "max_attempts": 3, "smoke": "off", "scan": "off", "date": "x",
                "provenance": "SIMULATED: fakes, not a measurement"}
        with tempfile.TemporaryDirectory() as d:
            jl, md = evaluate.write_results(self.rows, meta, d)
            text = md.read_text()
            self.assertIn("SIMULATED", text)
            self.assertIn("Not measured / limits", text)
            self.assertEqual(len(jl.read_text().splitlines()), len(self.rows))
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(cli.main(["eval", "--report-only", d]), 0)
            self.assertIn("SIMULATED", md.read_text())

    def test_a_stop_that_cost_a_model_call_is_not_counted_as_a_correct_stop(self):
        row = dict(self.row("stop-bad-pin"), llm_calls=1)
        a = evaluate.aggregate([row])
        self.assertEqual((a["injection"]["stopped_correctly"]["k"], a["injection"]["stopped_correctly"]["n"]), (0, 1))

    def test_built_but_smoke_failed_is_not_healthy(self):
        from dockhealer import smoke
        info = scanner.scan(ROOT / "examples" / "flask_app")
        s = next(x for x in evaluate.load_scenarios(MANIFEST, ROOT) if x.id == "gen-flask")
        results = iter([smoke.SmokeResult(False, container_exited=True, exit_code=1, logs="ModuleNotFoundError: No module named 'x'"),
                        smoke.SmokeResult(True, status=200, path="/health")])
        rep = healer.heal(info, FakeClient([fence(GOOD), fence(GOOD.replace("COPY . .", "COPY . ./"))]), build_fn=build_fn,
                          remove_fn=lambda t: None, smoke_fn=lambda tag, i: next(results))
        row = evaluate.summarize_report(s, 1, rep, "dockhealer")
        self.assertTrue(rep.attempts[0].outcome == "smoke_failed" and rep.success)
        self.assertFalse(row["first_attempt_healthy"], "a build that cannot answer must not count as one-shot success")
        self.assertTrue(row["success"])

    def test_only_filter(self):
        rows = evaluate.run_eval(self.scenarios, trials=1, heal_fn=make_heal_fn(FakeClient([])), root=ROOT, only={"stop-bad-pin"})
        self.assertEqual([r["scenario"] for r in rows], ["stop-bad-pin"])

    def test_default_provenance_is_never_silently_blank(self):
        self.assertIn("UNSPECIFIED", evaluate.render_markdown(self.rows, {}))


class ProvidedDockerfile(unittest.TestCase):
    INFO = scanner.scan(ROOT / "examples" / "flask_app")

    def go(self, replies, results, **kw):
        client = FakeClient(replies)
        seq = list(results)
        rep = healer.heal(self.INFO, client, build_fn=lambda i, d, *, timeout_s=0, **k: seq.pop(0),
                          remove_fn=lambda t: None, **kw)
        return rep, client

    def test_healthy_provided_dockerfile_never_calls_the_model(self):
        rep, client = self.go([], [OK], initial_dockerfile=GOOD, max_attempts=0)
        self.assertEqual((rep.success, rep.llm_calls, rep.attempts[0].kind), (True, 0, "provided"))
        self.assertEqual(client.prompts, [])

    def test_failing_provided_dockerfile_is_repaired_by_the_model(self):
        rep, client = self.go([fence(GOOD)], [failed(fixture_log("base_image_not_found.log")), OK],
                              initial_dockerfile=py("3.99"), max_attempts=3)
        self.assertEqual([a.kind for a in rep.attempts], ["provided", "repair"])
        self.assertTrue(rep.success)
        self.assertIn("python:3.99-slim", client.prompts[0], "the repair prompt shows the supplied (broken) Dockerfile")

    def test_zero_budget_means_no_repair_at_all(self):
        rep, client = self.go([], [failed(fixture_log("base_image_not_found.log"))], initial_dockerfile=py("3.99"), max_attempts=0)
        self.assertEqual((rep.success, rep.stop_reason, rep.llm_calls), (False, healer.BUDGET_EXHAUSTED, 0))


if __name__ == "__main__":
    unittest.main()
