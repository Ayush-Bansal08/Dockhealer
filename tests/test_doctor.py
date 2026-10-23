import os
import tempfile
import unittest
from unittest.mock import MagicMock

from dockhealer import doctor
from dockhealer.config import Settings
from dockhealer.llm import LLMUnavailable


class RecommendModelTests(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(doctor.recommend_model(4), "qwen2.5-coder:1.5b")
        self.assertEqual(doctor.recommend_model(8), "qwen2.5-coder:3b")
        self.assertEqual(doctor.recommend_model(16), "qwen2.5-coder:7b")


class MeminfoTests(unittest.TestCase):
    def _write(self, text):
        f = tempfile.NamedTemporaryFile("w", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_parse_kb_to_gib(self):
        path = self._write("MemTotal:       8388608 kB\nMemAvailable:   4194304 kB\nOther: 1 kB\n")
        mem = doctor.read_meminfo(path)
        self.assertAlmostEqual(mem["MemTotal"], 8.0)
        self.assertAlmostEqual(mem["MemAvailable"], 4.0)

    def test_check_ram_warns_when_unreadable(self):
        self.assertEqual(doctor.check_ram("/nonexistent/meminfo").status, doctor.WARN)

    def test_check_ram_low_memory_warns_and_mentions_wslconfig(self):
        path = self._write("MemTotal: 4194304 kB\nMemAvailable: 2097152 kB\n")
        r = doctor.check_ram(path)
        self.assertEqual(r.status, doctor.WARN)
        self.assertIn(".wslconfig", r.hint)


class ModelMatchTests(unittest.TestCase):
    def test_exact_and_implicit_latest(self):
        self.assertTrue(doctor.model_matches("a:3b", ["a:3b"]))
        self.assertTrue(doctor.model_matches("a", ["a:latest"]))
        self.assertFalse(doctor.model_matches("a:3b", ["a:1b"]))


class OllamaCheckTests(unittest.TestCase):
    def test_server_down_is_fail_with_install_hint(self):
        client = MagicMock()
        client.settings = Settings()
        client.version.side_effect = LLMUnavailable("down")
        r = doctor.check_ollama(client)
        self.assertEqual(r.status, doctor.FAIL)
        self.assertIn("ollama serve", r.hint)

    def test_model_missing_is_fail_with_pull_hint(self):
        client = MagicMock()
        client.settings = Settings(model="qwen2.5-coder:3b")
        client.list_models.return_value = ["llama3.2:1b"]
        r = doctor.check_model(client)
        self.assertEqual(r.status, doctor.FAIL)
        self.assertIn("ollama pull qwen2.5-coder:3b", r.hint)

    def test_model_check_skipped_when_server_down(self):
        client = MagicMock()
        client.settings = Settings()
        client.list_models.side_effect = LLMUnavailable("down")
        self.assertEqual(doctor.check_model(client).status, doctor.WARN)


if __name__ == "__main__":
    unittest.main()
