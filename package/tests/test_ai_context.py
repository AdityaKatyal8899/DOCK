import tempfile
import unittest
from pathlib import Path

from docklib.ai.context import collect_project_context


class ProjectContextTests(unittest.TestCase):
    def test_detects_python_framework_docker_compose_and_entrypoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "requirements.txt").write_text("fastapi==0.1\nuvicorn\n", encoding="utf-8")
            (root / "main.py").write_text(
                "from fastapi import FastAPI\nimport os\napp = FastAPI()\nPORT = os.getenv('PORT')\n",
                encoding="utf-8",
            )
            (root / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
            (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

            context = collect_project_context(root)

        self.assertEqual(context.project_root, ".")
        self.assertIn("Python", context.signals.languages)
        self.assertIn("FastAPI", context.signals.frameworks)
        self.assertEqual(context.signals.package_manager, "pip")
        self.assertIn("requirements.txt", context.signals.dependency_files)
        self.assertIn("Dockerfile", context.signals.dockerfiles)
        self.assertIn("docker-compose.yml", context.signals.compose_files)
        self.assertIn("main.py", context.signals.entry_points)
        self.assertIn("PORT", context.signals.environment_variables)

    def test_detects_node_package_manager_and_framework_from_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text(
                '{"dependencies":{"next":"1","react":"1"},"scripts":{"dev":"next dev"}}',
                encoding="utf-8",
            )
            (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9'\n", encoding="utf-8")
            context = collect_project_context(root)
        self.assertIn("JavaScript", context.signals.languages)
        self.assertIn("Next.js", context.signals.frameworks)
        self.assertIn("React", context.signals.frameworks)
        self.assertEqual(context.signals.package_manager, "pnpm")
        self.assertIn("package.json:scripts", context.signals.entry_points)

    def test_excludes_secrets_and_generated_directories_and_redacts_assignments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text("API_KEY=do-not-send\n", encoding="utf-8")
            (root / "credentials.txt").write_text("password=also-do-not-send\n", encoding="utf-8")
            (root / "main.py").write_text(
                "API_KEY = 'visible-secret-value'\nTOKEN = 'bc_live_12345678901234567890'\n",
                encoding="utf-8",
            )
            (root / "node_modules").mkdir()
            (root / "node_modules" / "library.js").write_text("export default 1", encoding="utf-8")
            context = collect_project_context(root)
            serialized = "\n".join(item.content for item in context.files)
        self.assertNotIn("do-not-send", serialized)
        self.assertNotIn("also-do-not-send", serialized)
        self.assertNotIn("visible-secret-value", serialized)
        self.assertNotIn("bc_live_12345678901234567890", serialized)
        self.assertNotIn("node_modules", [item.path for item in context.files])
        self.assertIn("<REDACTED>", serialized)

    def test_environment_file_paths_are_exposed_without_reading_contents(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text("GEMINI_API_KEY=private-env-secret\n", encoding="utf-8")
            (root / "config").mkdir()
            (root / "config" / "production.env").write_text("DB_PASSWORD=private-db-secret\n", encoding="utf-8")
            (root / "main.py").write_text("import os\nos.getenv('GEMINI_API_KEY')\n", encoding="utf-8")
            context = collect_project_context(root)
            serialized = context.model_dump_json()

        self.assertEqual(context.signals.environment_files, [".env", "config/production.env"])
        self.assertIn("GEMINI_API_KEY", context.signals.environment_variables)
        self.assertIn(".env", serialized)
        self.assertIn("config/production.env", serialized)
        self.assertNotIn("private-env-secret", serialized)
        self.assertNotIn("private-db-secret", serialized)

    def test_enforces_total_context_budget_and_rejects_invalid_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "README.md").write_text("A" * 800, encoding="utf-8")
            (root / "main.py").write_text("B" * 800, encoding="utf-8")
            context = collect_project_context(root, max_total_chars=500)
            with self.assertRaises(ValueError):
                collect_project_context(root, max_files=0)
        self.assertLessEqual(sum(len(item.content) for item in context.files), 500)
        self.assertTrue(context.warnings)

    def test_caps_each_included_file_at_schema_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "README.md").write_text("x" * 30_000, encoding="utf-8")
            context = collect_project_context(root)
        self.assertLessEqual(max((len(item.content) for item in context.files), default=0), 24_000)
        self.assertTrue(context.warnings)

    def test_rejects_non_directory_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            file_path = Path(temp) / "file.txt"
            file_path.write_text("text", encoding="utf-8")
            with self.assertRaises(ValueError):
                collect_project_context(file_path)


if __name__ == "__main__":
    unittest.main()
