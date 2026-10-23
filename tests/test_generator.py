import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from dockhealer import generator, scanner
from dockhealer.llm import ChatResult

ROOT = Path(__file__).resolve().parent.parent
DF = "FROM python:3.12-slim\nCMD [\"python\", \"app.py\"]\n"


class ExtractTests(unittest.TestCase):
    def test_fenced_block_with_prose_around_it(self):
        reply = f"Sure! Here you go:\n```dockerfile\n{DF}```\nHope that helps."
        self.assertEqual(generator.extract_dockerfile(reply), DF)

    def test_fence_without_language(self):
        self.assertEqual(generator.extract_dockerfile(f"```\n{DF}```"), DF)

    def test_picks_the_block_that_contains_from(self):
        reply = f"```bash\npip install x\n```\n```dockerfile\n{DF}```"
        self.assertEqual(generator.extract_dockerfile(reply), DF)

    def test_unfenced_starts_at_from(self):
        self.assertEqual(generator.extract_dockerfile("Here:\n" + DF), DF)

    def test_no_dockerfile_returns_none(self):
        self.assertIsNone(generator.extract_dockerfile("I cannot help with that."))

    def test_lowercase_from_accepted(self):
        self.assertIsNotNone(generator.extract_dockerfile("```\nfrom python:3.12\n```"))


class GenerateTests(unittest.TestCase):
    def _client(self, content):
        c = MagicMock()
        c.chat.return_value = ChatResult(content, "m", 1.0, 5)
        return c

    def test_generate_passes_facts_and_system_prompt(self):
        info = scanner.scan(ROOT / "examples" / "flask_app")
        client = self._client(f"```dockerfile\n{DF}```")
        gen = generator.generate(client, info)
        self.assertEqual(gen.dockerfile, DF)
        args, kwargs = client.chat.call_args
        self.assertIn("start command: python app.py", args[0])
        self.assertIn("port: 5000", args[0])
        self.assertEqual(kwargs["system"], generator.SYSTEM_PROMPT)

    def test_prompt_never_contains_excluded_files(self):
        info = scanner.scan(ROOT / "examples" / "flask_app")
        prompt, _ = generator.build_prompt(info)
        self.assertNotIn(".env.example", prompt)

    def test_reply_without_dockerfile_raises(self):
        info = scanner.scan(ROOT / "examples" / "flask_app")
        with self.assertRaises(generator.GenerationError):
            generator.generate(self._client("sorry"), info)


class WriteTests(unittest.TestCase):
    def test_refuses_to_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "Dockerfile").write_text("original")
            with self.assertRaises(FileExistsError):
                generator.write_dockerfile(DF, d)
            self.assertEqual((Path(d) / "Dockerfile").read_text(), "original")

    def test_force_overwrites_and_new_file_is_written(self):
        with tempfile.TemporaryDirectory() as d:
            p = generator.write_dockerfile(DF, d)
            self.assertEqual(p.read_text(), DF)
            generator.write_dockerfile("FROM x\n", d, force=True)
            self.assertEqual(p.read_text(), "FROM x\n")


if __name__ == "__main__":
    unittest.main()
