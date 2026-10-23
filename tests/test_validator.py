import unittest
from pathlib import Path

from dockhealer import scanner, validator

ROOT = Path(__file__).resolve().parent.parent
GOOD = """FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
USER 10001
EXPOSE 5000
CMD ["python", "app.py"]
"""


def rules(text, info=None):
    return {f.rule for f in validator.validate(text, info).findings}


class BaselineTests(unittest.TestCase):
    """The hand-written baselines must be clean: this validates the validator in the other direction
    (no false positives on a known-good file)."""

    def test_baselines_have_zero_findings(self):
        for app in ("flask_app", "fastapi_app", "express_app"):
            info = scanner.scan(ROOT / "examples" / app)
            text = (ROOT / "baselines" / f"{app}.Dockerfile").read_text()
            res = validator.validate(text, info)
            self.assertEqual(res.findings, [], f"{app}: {validator.format_findings(res)}")


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.info = scanner.scan(ROOT / "examples" / "flask_app")

    def test_good_file_is_ok(self):
        self.assertTrue(validator.validate(GOOD, self.info).ok)

    def test_no_from(self):
        self.assertIn("E001", rules("RUN echo hi\nCMD [\"x\"]\n"))

    def test_instruction_before_from(self):
        self.assertIn("E001", rules("RUN echo hi\nFROM python:3.12-slim\nCMD [\"x\"]\n"))

    def test_invalid_instruction(self):
        self.assertIn("E002", rules(GOOD + "INSTALL flask\n"))

    def test_prose_leaking_into_dockerfile_is_an_error(self):
        self.assertIn("E002", rules("Here is your Dockerfile:\n" + GOOD))

    def test_pipe_to_shell(self):
        self.assertIn("E004", rules(GOOD.replace("COPY . .", "RUN curl -fsSL https://x.sh | sh\nCOPY . .")))

    def test_literal_secret_in_env_and_arg(self):
        self.assertIn("E006", rules(GOOD + "ENV DB_PASSWORD=hunter2\n"))
        self.assertIn("E006", rules(GOOD + "ARG API_KEY=abc123\n"))

    def test_secret_reference_or_no_default_is_fine(self):
        self.assertNotIn("E006", rules(GOOD + "ARG API_KEY\nENV TOKEN_FILE=$SOME\n"))

    def test_copy_of_missing_file_is_error(self):
        self.assertIn("E007", rules(GOOD.replace("requirements.txt", "reqs.txt"), self.info))

    def test_copy_of_excluded_secret_file_is_error(self):
        self.assertIn("E007", rules(GOOD.replace("COPY . .", "COPY .env.example ."), self.info))

    def test_copy_from_stage_not_path_checked(self):
        text = "FROM python:3.12-slim AS build\nRUN echo hi\nFROM python:3.12-slim\nCOPY --from=build /out /out\nUSER 1000\nCMD [\"python\",\"app.py\"]\n"
        self.assertNotIn("E007", rules(text, self.info))

    def test_glob_sources(self):
        node = scanner.scan(ROOT / "examples" / "express_app")
        text = "FROM node:20-slim\nCOPY package*.json ./\nUSER node\nCMD [\"npm\",\"start\"]\n"
        self.assertNotIn("E007", rules(text, node))

    def test_apt_without_y(self):
        self.assertIn("E012", rules(GOOD.replace("COPY . .", "RUN apt-get install curl\nCOPY . .")))

    def test_apt_with_y_ok(self):
        r = rules(GOOD.replace("COPY . .", "RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*\nCOPY . ."))
        self.assertNotIn("E012", r)
        self.assertNotIn("W004", r)

    def test_missing_cmd(self):
        self.assertIn("E011", rules(GOOD.replace('CMD ["python", "app.py"]\n', "")))

    def test_unpinned_base_image(self):
        self.assertIn("W001", rules(GOOD.replace("python:3.12-slim", "python")))
        self.assertIn("W001", rules(GOOD.replace("python:3.12-slim", "python:latest")))
        self.assertNotIn("W001", rules(GOOD.replace("python:3.12-slim", "localhost:5000/team/python:3.12")))

    def test_cache_busting_copy_order(self):
        bad = "FROM python:3.12-slim\nCOPY . .\nRUN pip install --no-cache-dir -r requirements.txt\nUSER 1000\nCMD [\"python\",\"app.py\"]\n"
        self.assertIn("W002", rules(bad))
        self.assertNotIn("W002", rules(GOOD))

    def test_wrong_entrypoint_warns(self):
        self.assertIn("W003", rules(GOOD.replace("app.py", "main.py"), self.info))

    def test_runs_as_root(self):
        self.assertIn("W009", rules(GOOD.replace("USER 10001\n", "")))
        self.assertIn("W009", rules(GOOD.replace("USER 10001", "USER root")))

    def test_expose_mismatch(self):
        self.assertIn("W008", rules(GOOD.replace("EXPOSE 5000", "EXPOSE 8080"), self.info))

    def test_line_continuations_are_joined(self):
        text = GOOD.replace("RUN pip install --no-cache-dir -r requirements.txt",
                            "RUN pip install \\\n    --no-cache-dir \\\n    -r requirements.txt")
        self.assertNotIn("W006", rules(text))
        self.assertNotIn("E002", rules(text))

    def test_findings_carry_line_numbers(self):
        f = [x for x in validator.validate(GOOD + "ENV DB_PASSWORD=hunter2\n").findings if x.rule == "E006"][0]
        self.assertEqual(f.line, GOOD.count("\n") + 1)


if __name__ == "__main__":
    unittest.main()
