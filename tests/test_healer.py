"""Behaviour tests for the bounded repair loop, driven by scripted fakes for the model and for Docker.

What these prove: the LOGIC of the loop (when it stops, what it feeds the model, what it records).
What they do not prove: that a real model writes good repairs or that real Docker logs classify as
assumed. That is measured later, in the evaluation, with real runs.
"""

import json
import tempfile
import unittest
from pathlib import Path

from dockhealer import builder, healer, scanner
from dockhealer.builder import BuildResult
from dockhealer.llm import ChatResult, LLMUnavailable

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"
INFO = scanner.scan(ROOT / "examples" / "flask_app")
GOOD = (ROOT / "baselines" / "flask_app.Dockerfile").read_text()


def py(version: str) -> str:
    return GOOD.replace("python:3.12-slim", f"python:{version}-slim")


def fence(df: str) -> str:
    return f"```dockerfile\n{df}```"


def fixture_log(name: str) -> str:
    return (FIX / name).read_text().partition("\n")[2]


def apt_fail(i: int) -> str:
    return (f'#7 RUN apt-get install -y pkg{i}\n#7 E: Unable to locate package pkg{i}\n'
            f'#7 ERROR: process "/bin/sh -c apt-get install -y pkg{i}" did not complete successfully: exit code: 100\n')


OK = BuildResult(True, 0, 3.0, "", "t:1", image_size_bytes=100)


def failed(log: str) -> BuildResult:
    return BuildResult(False, 1, 2.0, log, "t:1")


class FakeClient:
    def __init__(self, replies):
        self.replies, self.prompts, self.systems = list(replies), [], []

    def chat(self, prompt, system=None, model=None):
        self.prompts.append(prompt)
        self.systems.append(system)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return ChatResult(reply, "fake-model", 1.0, 10)


class FakeBuild:
    def __init__(self, results):
        self.results, self.calls = list(results), 0

    def __call__(self, info, dockerfile, *, timeout_s=0, **kw):
        self.calls += 1
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def run(replies, builds, **kw):
    client, build_fn, removed = FakeClient(replies), FakeBuild(builds), []
    report = healer.heal(INFO, client, build_fn=build_fn, remove_fn=removed.append, **kw)
    return report, client, build_fn, removed


class SuccessPaths(unittest.TestCase):
    def test_first_try(self):
        r, c, b, removed = run([fence(GOOD)], [OK])
        self.assertTrue(r.success)
        self.assertEqual((r.stop_reason, r.llm_calls, r.builds), (healer.BUILT, 1, 1))
        self.assertEqual(removed, ["t:1"])
        self.assertEqual(r.final_dockerfile, GOOD)

    def test_keep_image_skips_cleanup(self):
        _, _, _, removed = run([fence(GOOD)], [OK], keep_image=True)
        self.assertEqual(removed, [])

    def test_invalid_candidate_is_never_built_and_is_repaired(self):
        bad = GOOD.replace("requirements.txt", "reqs.txt")           # validator error E007
        r, c, b, _ = run([fence(bad), fence(GOOD)], [OK])
        self.assertTrue(r.success)
        self.assertEqual([a.outcome for a in r.attempts], ["invalid", "built"])
        self.assertEqual(b.calls, 1, "the invalid Dockerfile must not cost a docker build")
        self.assertIn("E007", c.prompts[1])

    def test_build_failure_is_diagnosed_and_repair_prompt_carries_the_evidence(self):
        r, c, _, _ = run([fence(py("3.99")), fence(GOOD)], [failed(fixture_log("base_image_not_found.log")), OK])
        self.assertTrue(r.success)
        self.assertEqual([a.kind for a in r.attempts], ["generate", "repair"])
        p = c.prompts[1]
        self.assertIn("BASE_IMAGE_NOT_FOUND", p)
        self.assertIn("<build_log>", p)
        self.assertIn("python:3.99-slim", p)                           # the current (broken) Dockerfile is shown
        self.assertIn("untrusted", c.systems[1])                       # system prompt frames the log as data

    def test_network_failure_retries_same_dockerfile_without_asking_the_model(self):
        r, c, b, _ = run([fence(GOOD)], [failed(fixture_log("pip_network.log")), OK])
        self.assertTrue(r.success)
        self.assertEqual((r.llm_calls, r.builds), (1, 2))
        self.assertEqual([a.kind for a in r.attempts], ["generate", "retry"])
        self.assertEqual(r.attempts[0].dockerfile, r.attempts[1].dockerfile)

    def test_repair_that_adds_a_new_warning_is_recorded(self):
        no_user = GOOD.replace("USER appuser\n", "")                    # introduces W009 (runs as root)
        r, *_ = run([fence(GOOD), fence(no_user)], [failed(apt_fail(0)), OK])
        self.assertTrue(r.success)
        self.assertEqual(r.attempts[1].new_warnings, ["W009"])


class StopConditions(unittest.TestCase):
    def test_human_only_diagnosis_stops_immediately(self):
        r, c, b, _ = run([fence(GOOD)], [failed(fixture_log("pip_dependency_project.log"))])
        self.assertFalse(r.success)
        self.assertEqual(r.stop_reason, healer.HUMAN_REQUIRED)
        self.assertEqual((len(c.prompts), b.calls), (1, 1), "no repair call may be made for a STOP diagnosis")
        self.assertIn("DEPENDENCY_IN_PROJECT", r.detail)

    def test_retry_budget_is_respected(self):
        net = failed(fixture_log("pip_network.log"))
        r, c, b, _ = run([fence(GOOD)], [net, net], max_retries=1)
        self.assertEqual((r.stop_reason, r.llm_calls, r.builds), (healer.HUMAN_REQUIRED, 1, 2))

    def test_zero_retries_means_no_retry(self):
        r, _, b, _ = run([fence(GOOD)], [failed(fixture_log("pip_network.log"))], max_retries=0)
        self.assertEqual((r.stop_reason, b.calls), (healer.HUMAN_REQUIRED, 1))

    def test_model_budget_exhausted(self):
        r, c, b, _ = run([fence(py("3.9")), fence(py("3.10")), fence(py("3.11"))],
                         [failed(apt_fail(0)), failed(apt_fail(1)), failed(apt_fail(2))], max_attempts=3)
        self.assertEqual((r.stop_reason, r.llm_calls, r.builds), (healer.BUDGET_EXHAUSTED, 3, 3))
        self.assertFalse(r.success)

    def test_model_returns_same_dockerfile_means_no_progress(self):
        r, _, b, _ = run([fence(py("3.9")), fence(py("3.9"))], [failed(apt_fail(0))])
        self.assertEqual(r.stop_reason, healer.NO_PROGRESS)
        self.assertEqual(b.calls, 1)

    def test_whitespace_only_change_still_counts_as_no_progress(self):
        r, *_ = run([fence(py("3.9")), fence(py("3.9").replace("WORKDIR /app", "WORKDIR /app   "))],
                    [failed(apt_fail(0))])
        self.assertEqual(r.stop_reason, healer.NO_PROGRESS)

    def test_same_failure_after_a_repair_is_a_repeated_failure(self):
        r, _, b, _ = run([fence(py("3.9")), fence(py("3.10"))], [failed(apt_fail(0)), failed(apt_fail(0))])
        self.assertEqual((r.stop_reason, b.calls), (healer.REPEATED_FAILURE, 2))

    def test_same_validation_errors_after_a_repair_is_a_repeated_failure(self):
        bad1, bad2 = GOOD.replace("requirements.txt", "a.txt"), GOOD.replace("requirements.txt", "b.txt")
        r, _, b, _ = run([fence(bad1), fence(bad2)], [])
        self.assertEqual((r.stop_reason, b.calls), (healer.REPEATED_FAILURE, 0))

    def test_llm_error_stops_cleanly(self):
        r, _, b, _ = run([LLMUnavailable("down")], [])
        self.assertEqual((r.stop_reason, r.llm_calls, b.calls), (healer.LLM_ERROR, 1, 0))

    def test_docker_missing_is_an_environment_stop(self):
        r, *_ = run([fence(GOOD)], [builder.DockerUnavailable("no docker")])
        self.assertEqual(r.stop_reason, healer.ENVIRONMENT)

    def test_reply_without_a_dockerfile_consumes_an_attempt(self):
        r, *_ = run(["I cannot do that.", fence(GOOD)], [OK])
        self.assertTrue(r.success)
        self.assertEqual((r.attempts[0].outcome, r.llm_calls), ("no_dockerfile", 2))

    def test_endless_garbage_still_terminates(self):
        r, _, b, _ = run(["x", "x", "x"], [], max_attempts=3)
        self.assertEqual((r.stop_reason, r.llm_calls, b.calls), (healer.BUDGET_EXHAUSTED, 3, 0))

    def test_timeout_is_not_retried_or_repaired(self):
        timed_out = BuildResult(False, None, 600.0, "", "t:1", timed_out=True)
        r, c, *_ = run([fence(GOOD)], [timed_out])
        self.assertEqual((r.stop_reason, len(c.prompts)), (healer.HUMAN_REQUIRED, 1))


class HardBound(unittest.TestCase):
    def test_loop_terminates_within_budget_for_every_setting(self):
        """Worst case: the model always returns something new and every build fails differently."""
        for max_attempts in range(1, 7):
            for max_retries in range(0, 3):
                with self.subTest(max_attempts=max_attempts, max_retries=max_retries):
                    counter = {"n": 0}

                    class Endless:
                        def chat(self, prompt, system=None, model=None):
                            counter["n"] += 1
                            return ChatResult(fence(py(f"3.{20 + counter['n']}")), "m", 0.1, 1)

                    builds = {"n": 0}

                    def build_fn(info, df, *, timeout_s=0, **kw):
                        builds["n"] += 1
                        if builds["n"] > 100:
                            raise AssertionError("runaway loop")
                        return failed(apt_fail(builds["n"]))

                    r = healer.heal(INFO, Endless(), max_attempts=max_attempts, max_retries=max_retries,
                                    build_fn=build_fn, remove_fn=lambda t: None)
                    self.assertFalse(r.success)
                    self.assertLessEqual(r.llm_calls, max_attempts)
                    self.assertLessEqual(r.builds, max_attempts + max_retries)
                    self.assertEqual(r.stop_reason, healer.BUDGET_EXHAUSTED)


class Reporting(unittest.TestCase):
    def test_secrets_in_build_logs_never_reach_the_model_or_the_report(self):
        log = ('#6 ERROR: failed to resolve source metadata for docker.io/library/python:3.99-slim: not found\n'
               '#6 PASSWORD=hunter2hunter2\n')
        r, c, *_ = run([fence(py("3.99")), fence(GOOD)], [failed(log), OK])
        self.assertNotIn("hunter2", c.prompts[1])
        self.assertNotIn("hunter2", json.dumps(healer.report_to_dict(r)))

    def test_report_is_written_as_json_with_one_dockerfile_per_attempt(self):
        r, *_ = run([fence(py("3.99")), fence(GOOD)], [failed(fixture_log("base_image_not_found.log")), OK])
        with tempfile.TemporaryDirectory() as d:
            path = healer.write_report(r, Path(d) / "run1")
            data = json.loads(path.read_text())
            self.assertEqual(data["stop_reason"], "built")
            self.assertEqual([a["kind"] for a in data["attempts"]], ["generate", "repair"])
            self.assertEqual(data["attempts"][0]["diagnosis"]["category"], "BASE_IMAGE_NOT_FOUND")
            self.assertEqual((Path(d) / "run1" / "Dockerfile.attempt1").read_text(), py("3.99"))
            self.assertTrue((Path(d) / "run1" / "Dockerfile.attempt2").exists())

    def test_progress_callback_receives_messages(self):
        msgs = []
        client, build_fn = FakeClient([fence(GOOD)]), FakeBuild([OK])
        healer.heal(INFO, client, build_fn=build_fn, remove_fn=lambda t: None, say=msgs.append)
        self.assertTrue(any("BUILD OK" in m for m in msgs))


if __name__ == "__main__":
    unittest.main()
