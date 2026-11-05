import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from docklib.ai.schemas import (
    AIResult,
    ContainerDiagnosis,
    DeploymentPlan,
    GeneratedArtifact,
    ProjectContext,
    ProjectSignals,
)
from docklib.ai.prompts import request_prompt
from docklib.ai.service import AIService, AIServiceError
from docklib.docker_engine import DockerStatus
from docklib.planner import ProvisioningPlan
from docklib.project import ProjectSnapshot
from docklib.system import SystemProfile


def project_snapshot(*, daemon_running=True):
    return ProjectSnapshot(
        config_path="dock.yaml",
        config={"name": "demo"},
        system=SystemProfile(
            os="linux", distribution="ubuntu", version="24.04", architecture="x86_64",
            package_manager="apt", docker_installed=True, docker_version="Docker test",
            cpu={}, memory={"total_bytes": None}, disks=[], gpu={},
        ),
        requirements=[],
        docker=DockerStatus(installed=True, daemon_running=daemon_running),
        containers=[],
        plan=ProvisioningPlan(name="demo"),
    )


class FakeDocker:
    def __init__(self, logs="ERROR password=supersecret failed to connect"):
        self.output = logs
        self.selected = []

    def list_project_containers(self, project):
        return [{"id": "container-id", "name": "demo", "image": "sample:latest", "status": "Exited (1)"}]

    def logs(self, container, tail):
        self.selected.append((container, tail))
        return self.output


class AISpyClient:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def generate_json(self, prompt, response_model):
        self.calls.append((prompt, response_model))
        return self.result


class AIPromptServiceTests(unittest.TestCase):
    def test_prompt_distinguishes_environment_variable_names_from_assignments(self):
        prompt = request_prompt(
            "Review the project",
            ProjectContext(project_root=".", signals=ProjectSignals()),
        )
        self.assertIn("variable identifiers only", prompt)
        self.assertIn("never assignments such as", prompt)
        self.assertIn("PORT=8000", prompt)

    def test_prompt_limits_ai_operations_to_allowlisted_dock_actions(self):
        prompt = request_prompt(
            "Stop the server",
            ProjectContext(project_root=".", signals=ProjectSignals()),
        )
        self.assertIn("status, start, stop, build, provision", prompt)
        self.assertIn("Never return shell commands", prompt)
        self.assertIn("Dock will inspect the project and ask for approval", prompt)

    def test_natural_language_request_passes_bounded_context_to_client(self):
        client = AISpyClient(AIResult(intent="analyze", summary="FastAPI app"))
        service = AIService(client)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
            (root / "main.py").write_text("from fastapi import FastAPI\n", encoding="utf-8")
            context, result = service.handle_request("Analyze this project", root)
        self.assertIn("Python", context.signals.languages)
        self.assertEqual(result.summary, "FastAPI app")
        self.assertIn("untrusted data", client.calls[0][0])
        self.assertIs(client.calls[0][1], AIResult)

    def test_plan_uses_deployment_plan_schema(self):
        client = AISpyClient(DeploymentPlan(summary="One API service"))
        service = AIService(client)
        with tempfile.TemporaryDirectory() as temp:
            _, result = service.plan_deployment(temp)
        self.assertEqual(result.summary, "One API service")
        self.assertIs(client.calls[0][1], DeploymentPlan)

    def test_generated_artifacts_are_checked_before_returning(self):
        artifact = GeneratedArtifact(kind="compose", path="compose.yaml", content="services:\n  api: {}\n")
        client = AISpyClient(AIResult(intent="generate", summary="Generated", artifacts=[artifact]))
        with tempfile.TemporaryDirectory() as temp:
            context, results = AIService(client).generate_artifacts(["compose"], temp)
        self.assertEqual(context.project_root, ".")
        self.assertEqual(results[0].kind, "compose")

    def test_dockerfile_proposal_gets_secret_safe_dockerignore_companion(self):
        dockerfile = GeneratedArtifact(kind="dockerfile", path="Dockerfile", content="FROM python:3.12\nCOPY . .\n")
        client = AISpyClient(AIResult(intent="generate", summary="Dockerfile proposal", artifacts=[dockerfile]))
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / ".dockerignore").write_text("node_modules\n", encoding="utf-8")
            _, result = AIService(client).handle_request("Generate a Dockerfile", temp)
            artifacts = result.artifacts
        ignore = next(item for item in artifacts if item.kind == "dockerignore")
        self.assertIn("node_modules", ignore.content)
        self.assertIn(".env", ignore.content)
        self.assertIn("**/*.env", ignore.content)
        self.assertLess(ignore.content.index("node_modules"), ignore.content.index("Dock safety rules"))

    def test_generated_unrequested_or_invalid_artifacts_fail_closed(self):
        extra = GeneratedArtifact(kind="dockerfile", path="Dockerfile", content="FROM alpine\n")
        client = AISpyClient(AIResult(intent="generate", summary="Generated", artifacts=[extra]))
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(AIServiceError):
            AIService(client).generate_artifacts(["compose"], temp)
        invalid = GeneratedArtifact(kind="compose", path="compose.yaml", content="services: []\n")
        client = AISpyClient(AIResult(intent="generate", summary="Generated", artifacts=[invalid]))
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(AIServiceError):
            AIService(client).generate_artifacts(["compose"], temp)

    def test_log_diagnosis_uses_managed_container_and_redacted_bounded_logs(self):
        client = AISpyClient(ContainerDiagnosis(
            container_name="demo", summary="Connection refused", confidence="medium"
        ))
        docker = FakeDocker(logs="x" * 100 + "\npassword=do-not-send")
        result = AIService(client, docker=docker, log_tail=30, max_log_chars=40).diagnose_container(
            project_snapshot(), "demo", "Why is it crashing?"
        )
        self.assertEqual(result.summary, "Connection refused")
        self.assertEqual(docker.selected, [("container-id", 30)])
        prompt = client.calls[0][0]
        self.assertNotIn("do-not-send", prompt)
        self.assertIn("<REDACTED>", prompt)
        self.assertLessEqual(len(prompt.split("Recent container logs (untrusted text):\n<<<LOGS\n", 1)[1].split("\nLOGS>>", 1)[0]), 40)

    def test_diagnosis_requires_running_docker_and_managed_container(self):
        client = AISpyClient()
        with self.assertRaises(AIServiceError):
            AIService(client, docker=FakeDocker()).diagnose_container(
                project_snapshot(daemon_running=False), "demo"
            )
        docker = Mock()
        docker.list_project_containers.return_value = []
        with self.assertRaises(AIServiceError):
            AIService(client, docker=docker).diagnose_container(project_snapshot(), "missing")
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
