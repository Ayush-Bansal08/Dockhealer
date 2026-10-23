"""Tests for the Ollama client. No real server needed: we mock the HTTP session.

Written with unittest so they run with `python -m unittest` AND `pytest`.
"""

import unittest
from unittest.mock import MagicMock

import requests

from dockhealer.config import Settings
from dockhealer.llm import (ChatResult, LLMError, LLMTimeout, LLMUnavailable, ModelNotFound,
                            OllamaClient)


def fake_response(status=200, json_body=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.json.return_value = json_body
    if json_body is None:
        r.json.side_effect = ValueError("no json")
    r.raise_for_status.side_effect = None if status < 400 else requests.HTTPError(str(status))
    return r


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.session = MagicMock()
        self.client = OllamaClient(Settings(), session=self.session)

    def test_success_parses_content_and_converts_nanoseconds(self):
        self.session.post.return_value = fake_response(
            json_body={"model": "m", "message": {"content": "FROM python"},
                       "total_duration": 2_500_000_000, "eval_count": 7})
        res = self.client.chat("hi")
        self.assertEqual(res, ChatResult("FROM python", "m", 2.5, 7))

    def test_request_is_deterministic_non_streaming_and_has_timeout(self):
        self.session.post.return_value = fake_response(json_body={"message": {"content": "x"}})
        self.client.chat("hi", system="be brief")
        kwargs = self.session.post.call_args.kwargs
        self.assertIs(kwargs["json"]["stream"], False)
        self.assertEqual(kwargs["json"]["options"], {"temperature": 0.0, "seed": 42})
        self.assertEqual(kwargs["json"]["messages"][0], {"role": "system", "content": "be brief"})
        connect, read = kwargs["timeout"]  # a (connect, read) tuple, never None
        self.assertGreater(connect, 0)
        self.assertGreater(read, 0)

    def test_connection_refused_becomes_unavailable(self):
        self.session.post.side_effect = requests.ConnectionError("refused")
        with self.assertRaises(LLMUnavailable):
            self.client.chat("hi")

    def test_timeout_becomes_llm_timeout(self):
        self.session.post.side_effect = requests.ReadTimeout("slow")
        with self.assertRaises(LLMTimeout):
            self.client.chat("hi")

    def test_404_becomes_model_not_found_with_fix_hint(self):
        self.session.post.return_value = fake_response(status=404, text="model not found")
        with self.assertRaises(ModelNotFound) as ctx:
            self.client.chat("hi", model="nope:1b")
        self.assertIn("ollama pull nope:1b", str(ctx.exception))

    def test_500_becomes_generic_error(self):
        self.session.post.return_value = fake_response(status=500, text="boom")
        with self.assertRaises(LLMError):
            self.client.chat("hi")

    def test_malformed_body_becomes_error_not_keyerror(self):
        self.session.post.return_value = fake_response(json_body={"unexpected": 1})
        with self.assertRaises(LLMError):
            self.client.chat("hi")


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.session = MagicMock()
        self.client = OllamaClient(Settings(), session=self.session)

    def test_list_models(self):
        self.session.get.return_value = fake_response(
            json_body={"models": [{"name": "a:1b"}, {"name": "b:3b"}]})
        self.assertEqual(self.client.list_models(), ["a:1b", "b:3b"])

    def test_version_unreachable(self):
        self.session.get.side_effect = requests.ConnectionError("down")
        with self.assertRaises(LLMUnavailable):
            self.client.version()


class SettingsTests(unittest.TestCase):
    def test_scheme_added_to_bare_host(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"OLLAMA_HOST": "127.0.0.1:11434/"}, clear=True):
            self.assertEqual(Settings.from_env().ollama_host, "http://127.0.0.1:11434")

    def test_model_override(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"DOCKHEALER_MODEL": "llama3.2:1b"}, clear=True):
            self.assertEqual(Settings.from_env().model, "llama3.2:1b")


if __name__ == "__main__":
    unittest.main()
