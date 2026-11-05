import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from docklib.ai.cli import run_ai_command
from docklib.ai.actions import secure_dockerignore
from docklib.ai.schemas import AIResult, GeneratedArtifact, ProjectContext, ProjectSignals


def args(project, request="Analyze this project", *, apply=False, container=None):
    return SimpleNamespace(
        request=request,
        project=str(project),
        config="dock.yaml",
        apply=apply,
        container=container,
    )


class AICommandTests(unittest.TestCase):
    def test_ai_response_and_project_signals_are_distinct_and_ordered(self):
        context = ProjectContext(
            project_root=".",
            signals=ProjectSignals(languages=["Python"], frameworks=["FastAPI"]),
        )
        result = AIResult(intent="analyze", summary="This is the AI's main response.")
        service = Mock()
        service.handle_request.return_value = (context, result)
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(output):
            code = run_ai_command(args(temp), ai_client=SimpleNamespace(provider="gemini"), service_factory=lambda _: service)
        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertLess(rendered.index("AI RESPONSE"), rendered.index("This is the AI's main response."))
        self.assertLess(rendered.index("This is the AI's main response."), rendered.index("PROJECT SIGNALS"))
        self.assertIn("Python", rendered)
        self.assertNotIn("\033[", rendered)

    def test_color_output_honors_force_color_and_no_color(self):
        from docklib.ai.cli import _paint

        with patch.dict("os.environ", {"FORCE_COLOR": "1"}, clear=True), patch("sys.stdout.isatty", return_value=False):
            self.assertIn("\033[", _paint("header", "signals"))
        with patch.dict("os.environ", {"FORCE_COLOR": "1", "NO_COLOR": "1"}, clear=True):
            self.assertEqual(_paint("header", "signals"), "header")

    def test_status_operation_reports_live_snapshot_without_confirmation(self):
        context = ProjectContext(project_root=".", signals=ProjectSignals())
        result = AIResult(intent="operate", operation="status", summary="Checking the project status.")
        service = Mock()
        service.handle_request.return_value = (context, result)
        snapshot = SimpleNamespace(
            config={"name": "demo", "application": {"image": "demo:latest"}},
            docker=SimpleNamespace(daemon_running=True, version="Docker test", detail=None),
            plan=SimpleNamespace(name="demo", steps=[], blockers=[]),
            containers=[{"name": "demo", "status": "Up 2 minutes", "image": "demo:latest"}],
            inspection_error=None,
        )
        with tempfile.TemporaryDirectory() as temp, \
             patch("docklib.project.inspect_project", return_value=snapshot), \
             patch("builtins.input", side_effect=AssertionError("status must not ask for approval")), \
             redirect_stdout(io.StringIO()) as output:
            code = run_ai_command(args(temp), ai_client=SimpleNamespace(provider="gemini", docker=Mock()), service_factory=lambda _: service)
        self.assertEqual(code, 0)
        self.assertIn("Docker engine:  Running", output.getvalue())
        self.assertIn("demo: Up 2 minutes", output.getvalue())

    def test_missing_gemini_key_fails_cleanly_without_printing_credentials(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {}, clear=True):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = run_ai_command(args(temp))
        self.assertEqual(result, 1)
        self.assertIn("GEMINI_API_KEY is required", stderr.getvalue())

    def test_artifact_is_written_only_after_cli_approval(self):
        artifact = GeneratedArtifact(kind="dockerfile", path="Dockerfile", content="FROM python:3.12\n")
        ignore = GeneratedArtifact(kind="dockerignore", path=".dockerignore", content=secure_dockerignore())
        context = ProjectContext(project_root=".", signals=ProjectSignals())
        result = AIResult(intent="generate", summary="Generated a starter Dockerfile.", artifacts=[artifact, ignore])
        service = Mock()
        service.handle_request.return_value = (context, result)
        client = SimpleNamespace(provider="gemini")
        with tempfile.TemporaryDirectory() as temp:
            output = io.StringIO()
            with patch("builtins.input", return_value="yes"), redirect_stdout(output):
                code = run_ai_command(args(temp), ai_client=client, service_factory=lambda _: service)
            target = Path(temp) / "Dockerfile"
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), artifact.content)
        self.assertEqual(code, 0)
        self.assertIn("Wrote", output.getvalue())

    def test_declining_artifact_write_leaves_workspace_unchanged(self):
        artifact = GeneratedArtifact(kind="dockerfile", path="Dockerfile", content="FROM python:3.12\n")
        ignore = GeneratedArtifact(kind="dockerignore", path=".dockerignore", content=secure_dockerignore())
        context = ProjectContext(project_root=".", signals=ProjectSignals())
        result = AIResult(intent="generate", summary="Generated a starter Dockerfile.", artifacts=[artifact, ignore])
        service = Mock()
        service.handle_request.return_value = (context, result)
        with tempfile.TemporaryDirectory() as temp:
            with patch("builtins.input", return_value="no"), redirect_stdout(io.StringIO()):
                code = run_ai_command(args(temp), ai_client=SimpleNamespace(provider="gemini"), service_factory=lambda _: service)
            self.assertFalse((Path(temp) / "Dockerfile").exists())
        self.assertEqual(code, 0)

    def test_dockerfile_is_not_written_if_dockerignore_is_declined(self):
        artifact = GeneratedArtifact(kind="dockerfile", path="Dockerfile", content="FROM python:3.12\n")
        ignore = GeneratedArtifact(kind="dockerignore", path=".dockerignore", content=secure_dockerignore())
        context = ProjectContext(project_root=".", signals=ProjectSignals())
        result = AIResult(intent="generate", summary="Generated Docker artifacts.", artifacts=[artifact, ignore])
        service = Mock()
        service.handle_request.return_value = (context, result)
        with tempfile.TemporaryDirectory() as temp:
            output = io.StringIO()
            with patch("builtins.input", return_value="no"), redirect_stdout(output):
                code = run_ai_command(args(temp), ai_client=SimpleNamespace(provider="gemini"), service_factory=lambda _: service)
            self.assertFalse((Path(temp) / ".dockerignore").exists())
            self.assertFalse((Path(temp) / "Dockerfile").exists())
        self.assertEqual(code, 0)
        self.assertIn("approve the accompanying .dockerignore first", output.getvalue())

    def test_natural_language_diagnosis_requires_and_uses_container_logs(self):
        context = ProjectContext(project_root=".", signals=ProjectSignals())
        result = AIResult(intent="diagnose", summary="This request needs log context.")
        service = Mock()
        service.handle_request.return_value = (context, result)
        service.diagnose_container.return_value = SimpleNamespace(
            container_name="demo", summary="Connection refused", confidence="medium",
            likely_causes=["App exited before binding"], recommended_checks=["Check startup command"], warnings=[],
        )
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "dock.yaml").write_text("name: demo\napplication:\n  image: demo:latest\n", encoding="utf-8")
            fake_snapshot = SimpleNamespace()
            with patch("docklib.project.inspect_project", return_value=fake_snapshot), \
                 redirect_stdout(io.StringIO()) as output:
                code = run_ai_command(
                    args(temp, "Why is it failing?"), ai_client=SimpleNamespace(provider="gemini"),
                    service_factory=lambda _: service,
                )
        self.assertEqual(code, 0)
        service.diagnose_container.assert_called_once()
        self.assertIn("Connection refused", output.getvalue())


if __name__ == "__main__":
    unittest.main()
