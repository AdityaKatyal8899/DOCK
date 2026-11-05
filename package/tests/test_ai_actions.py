import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from docklib.ai.actions import (
    AIActionError,
    execute_approved_build,
    execute_approved_lifecycle,
    execute_approved_plan,
    preview_artifact,
    validate_generated_artifact,
    write_approved_artifact,
    secure_dockerignore,
)
from docklib.ai.schemas import GeneratedArtifact
from docklib.docker_engine import DockerStatus
from docklib.planner import PlanStep, ProvisioningPlan
from docklib.project import ProjectSnapshot
from docklib.system import SystemProfile


def snapshot(blockers=None):
    return ProjectSnapshot(
        config_path="dock.yaml",
        config={"name": "demo"},
        system=SystemProfile(
            os="windows", distribution=None, version="10", architecture="AMD64",
            package_manager=None, docker_installed=True, docker_version="Docker test",
            cpu={}, memory={"total_bytes": None}, disks=[], gpu={},
        ),
        requirements=[],
        docker=DockerStatus(installed=True, daemon_running=True),
        containers=[],
        plan=ProvisioningPlan(
            name="demo",
            steps=[PlanStep(action="run_application", description="Start the app", details={})],
            blockers=blockers or [],
        ),
    )


class AIActionTests(unittest.TestCase):
    def test_compose_yaml_preview_is_validated_without_execution(self):
        artifact = GeneratedArtifact(
            kind="compose", path="compose.yaml",
            content="services:\n  web:\n    image: nginx:alpine\n",
        )
        messages = validate_generated_artifact(artifact)
        self.assertTrue(any("no Compose command was executed" in item for item in messages))
        with tempfile.TemporaryDirectory() as temp:
            preview = preview_artifact(artifact, temp)
            self.assertIn("+services:", preview)
            self.assertFalse((Path(temp) / "compose.yaml").exists())

    def test_invalid_compose_and_dock_config_are_rejected(self):
        invalid_compose = GeneratedArtifact(kind="compose", path="compose.yaml", content="services: []\n")
        with self.assertRaises(AIActionError):
            validate_generated_artifact(invalid_compose)
        invalid_dock = GeneratedArtifact(kind="dock_yaml", path="dock.yaml", content="application: {}\n")
        with self.assertRaises(AIActionError):
            validate_generated_artifact(invalid_dock)

    def test_dockerignore_must_end_with_secret_exclusion_rules(self):
        unsafe = GeneratedArtifact(kind="dockerignore", path=".dockerignore", content="node_modules\n")
        with self.assertRaisesRegex(AIActionError, "required rules"):
            validate_generated_artifact(unsafe)
        safe = GeneratedArtifact(
            kind="dockerignore", path=".dockerignore",
            content=secure_dockerignore("node_modules\ncustom-cache\n"),
        )
        self.assertIn("secret-file patterns", " ".join(validate_generated_artifact(safe)))
        self.assertTrue(safe.content.startswith("node_modules\ncustom-cache\n"))
        self.assertTrue(safe.content.rstrip().endswith("**/credentials/"))

    def test_file_write_requires_approval_and_separate_overwrite_approval(self):
        artifact = GeneratedArtifact(kind="dockerfile", path="Dockerfile", content="FROM python:3.12\n")
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "Dockerfile"
            with self.assertRaises(AIActionError):
                write_approved_artifact(artifact, temp, approved=False)
            written = write_approved_artifact(artifact, temp, approved=True)
            self.assertEqual(written.read_text(encoding="utf-8"), artifact.content)
            replacement = artifact.model_copy(update={"content": "FROM python:3.13\n"})
            with self.assertRaises(AIActionError):
                write_approved_artifact(replacement, temp, approved=True)
            write_approved_artifact(replacement, temp, approved=True, overwrite_approved=True)
            self.assertEqual(target.read_text(encoding="utf-8"), "FROM python:3.13\n")

    def test_artifact_paths_cannot_escape_workspace(self):
        artifact = GeneratedArtifact(kind="dockerfile", path="sub/Dockerfile", content="FROM alpine\n")
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(AIActionError):
                write_approved_artifact(artifact.model_copy(update={"path": "../outside"}), temp, approved=True)
            with self.assertRaises(AIActionError):
                write_approved_artifact(artifact.model_copy(update={"path": "C:/outside"}), temp, approved=True)

    def test_apply_requires_approval_and_uses_only_snapshot_plan(self):
        project = snapshot()
        plan = project.plan
        executor = Mock()
        executor.execute.return_value = ["applied"]
        with self.assertRaises(AIActionError):
            execute_approved_plan(project, approved=False, executor=executor)
        executor.execute.assert_not_called()
        result = execute_approved_plan(project, approved=True, executor=executor)
        self.assertEqual(result, ["applied"])
        executor.execute.assert_called_once_with(plan, os_name="windows")

    def test_apply_rejects_blocked_or_non_dock_snapshots(self):
        executor = Mock()
        blocked = snapshot(["missing prerequisite"])
        with self.assertRaises(AIActionError):
            execute_approved_plan(blocked, approved=True, executor=executor)
        with self.assertRaises(AIActionError):
            execute_approved_plan(SimpleNamespace(), approved=True, executor=executor)
        executor.execute.assert_not_called()

    def test_lifecycle_requires_approval_and_targets_only_project_executor_methods(self):
        project = snapshot()
        executor = Mock()
        executor.start_project_containers.return_value = ["Started demo"]
        with self.assertRaises(AIActionError):
            execute_approved_lifecycle(project, "start", approved=False, executor=executor)
        executor.start_project_containers.assert_not_called()
        result = execute_approved_lifecycle(project, "start", approved=True, executor=executor)
        self.assertEqual(result, ["Started demo"])
        executor.start_project_containers.assert_called_once_with(project.plan.name)
        with self.assertRaises(AIActionError):
            execute_approved_lifecycle(project, "remove", approved=True, executor=executor)

    def test_build_executes_only_planner_build_steps_after_approval(self):
        project = snapshot()
        project.plan = ProvisioningPlan(
            name="demo",
            steps=[
                PlanStep("build_image", "Build demo image", {"image": "demo"}),
                PlanStep("run_application", "Start demo", {}),
            ],
        )
        executor = Mock()
        executor.execute.return_value = ["Built image demo"]
        with self.assertRaises(AIActionError):
            execute_approved_build(project, approved=False, executor=executor)
        executor.execute.assert_not_called()
        result = execute_approved_build(project, approved=True, executor=executor)
        self.assertEqual(result, ["Built image demo"])
        build_plan = executor.execute.call_args.args[0]
        self.assertEqual([step.action for step in build_plan.steps], ["build_image"])


if __name__ == "__main__":
    unittest.main()
