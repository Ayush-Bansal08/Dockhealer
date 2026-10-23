import os
import tempfile
import unittest
from pathlib import Path

from dockhealer import scanner

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


class TempProject(unittest.TestCase):
    def mkproject(self, files: dict[str, str]) -> Path:
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        root = Path(d.name)
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return root


class SampleAppTests(unittest.TestCase):
    def test_flask(self):
        i = scanner.scan(EXAMPLES / "flask_app")
        self.assertEqual((i.language, i.framework, i.entrypoint), ("python", "flask", "app.py"))
        self.assertEqual((i.port, i.port_source), (5000, "detected"))
        self.assertEqual(i.start_command, "python app.py")
        self.assertEqual(i.warnings, [])
        self.assertIn((".env.example", "secret-like filename"), i.excluded)

    def test_fastapi(self):
        i = scanner.scan(EXAMPLES / "fastapi_app")
        self.assertEqual((i.language, i.framework, i.entrypoint), ("python", "fastapi", "main.py"))
        self.assertEqual((i.port, i.port_source), (8000, "default"))
        self.assertEqual(i.start_command, "uvicorn main:app --host 0.0.0.0 --port 8000")

    def test_express(self):
        i = scanner.scan(EXAMPLES / "express_app")
        self.assertEqual((i.language, i.framework, i.entrypoint), ("node", "express", "server.js"))
        self.assertEqual((i.port, i.port_source), (3000, "detected"))
        self.assertEqual(i.start_command, "npm start")
        self.assertEqual(i.runtime_version, ">=18")


class ExclusionTests(TempProject):
    def test_secret_files_and_dirs_are_excluded_and_never_in_context(self):
        root = self.mkproject({
            "requirements.txt": "flask\n",
            "app.py": "print('hi')\n",
            ".env": "DB_PASSWORD=hunter2hunter2\n",
            "deploy/key.pem": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
            "terraform.tfstate": "{}",
            "node_modules/x/index.js": "x",
            ".git/config": "x",
        })
        i = scanner.scan(root)
        for secret in (".env", "deploy/key.pem", "terraform.tfstate"):
            self.assertNotIn(secret, i.files)
        self.assertNotIn("node_modules/x/index.js", i.files)
        ctx, _ = scanner.build_context(i)
        self.assertNotIn("hunter2", ctx)
        self.assertNotIn(".env", ctx.split("FILE TREE:")[1])

    def test_symlinks_are_not_followed(self):
        root = self.mkproject({"requirements.txt": "flask\n", "app.py": "x=1\n"})
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (Path(outside.name) / "stolen.txt").write_text("top secret")
        os.symlink(outside.name, root / "linked_dir")
        os.symlink(Path(outside.name) / "stolen.txt", root / "linked_file.txt")
        i = scanner.scan(root)
        self.assertNotIn("linked_file.txt", i.files)
        self.assertFalse(any(f.startswith("linked_dir") for f in i.files))
        self.assertEqual(sorted(r for _, r in i.excluded if "symlink" in r), ["symlink (not followed)"] * 2)

    def test_file_limit_is_enforced(self):
        files = {"requirements.txt": "flask\n"}
        files.update({f"f{n}.txt": "x" for n in range(scanner.MAX_FILES + 20)})
        i = scanner.scan(self.mkproject(files))
        self.assertEqual(len(i.files), scanner.MAX_FILES)
        self.assertTrue(any("file limit" in r for _, r in i.excluded))


class RedactionTests(unittest.TestCase):
    def test_patterns(self):
        cases = {
            "API_KEY = 'abcd1234efgh'": "abcd1234efgh",
            "password: hunter2hunter2": "hunter2hunter2",
            "aws=AKIAABCDEFGHIJKLMNOP": "AKIAABCDEFGHIJKLMNOP",
            "t=ghp_" + "a" * 36: "a" * 36,
        }
        for text, leaked in cases.items():
            clean, n = scanner.redact(text)
            self.assertNotIn(leaked, clean, text)
            self.assertGreaterEqual(n, 1, text)

    def test_innocent_text_untouched(self):
        clean, n = scanner.redact("app.run(host='0.0.0.0', port=5000)")
        self.assertEqual(n, 0)

    def test_secret_inside_included_file_is_redacted_in_context(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        (Path(d.name) / "requirements.txt").write_text("flask\n")
        (Path(d.name) / "app.py").write_text("SECRET_KEY = 'supersecretvalue123'\n")
        ctx, n = scanner.build_context(scanner.scan(d.name))
        self.assertNotIn("supersecretvalue123", ctx)
        self.assertEqual(n, 1)


class WarningTests(TempProject):
    def test_flask_localhost_bind_warns(self):
        root = self.mkproject({"requirements.txt": "flask\n",
                               "app.py": "from flask import Flask\napp=Flask(__name__)\napp.run(port=5000)\n"})
        i = scanner.scan(root)
        self.assertTrue(any("0.0.0.0" in w for w in i.warnings))

    def test_unsupported_project(self):
        i = scanner.scan(self.mkproject({"main.go": "package main"}))
        self.assertFalse(i.supported)
        self.assertTrue(i.warnings)

    def test_malformed_package_json_does_not_crash(self):
        i = scanner.scan(self.mkproject({"package.json": "{not json"}))
        self.assertFalse(i.supported)

    def test_not_a_directory(self):
        with self.assertRaises(NotADirectoryError):
            scanner.scan("/definitely/not/here")


if __name__ == "__main__":
    unittest.main()
