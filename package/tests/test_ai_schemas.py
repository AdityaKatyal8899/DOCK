import unittest

from pydantic import ValidationError

from docklib.ai.schemas import (
    AIResult,
    ContainerDiagnosis,
    GeneratedArtifact,
    ProjectContext,
    ProjectFile,
    ProjectSignals,
    ServiceProposal,
)


class AISchemaTests(unittest.TestCase):
    def test_project_file_accepts_relative_path_and_normalizes_separators(self):
        item = ProjectFile(path="src\\main.py", category="source", content="print('hi')")
        self.assertEqual(item.path, "src/main.py")

    def test_project_file_rejects_absolute_and_parent_paths(self):
        for path in ("C:\\secrets.txt", "../outside.txt", "src/../../outside.txt"):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                ProjectFile(path=path, category="source", content="not read")

    def test_models_reject_unexpected_fields_and_invalid_ports(self):
        with self.assertRaises(ValidationError):
            ProjectSignals(untrusted_extra="value")
        with self.assertRaises(ValidationError):
            ServiceProposal(name="web", ports=[{"host": 70000, "container": 80}])
        with self.assertRaises(ValidationError):
            ServiceProposal(name="web", environment_variables=["API_KEY=secret-value"])

    def test_artifact_is_a_bounded_relative_proposal(self):
        artifact = GeneratedArtifact(kind="compose", path="compose.yaml", content="services: {}")
        self.assertEqual(artifact.path, "compose.yaml")
        with self.assertRaises(ValidationError):
            GeneratedArtifact(kind="compose", path="../../compose.yaml", content="services: {}")
        with self.assertRaises(ValidationError):
            GeneratedArtifact(kind="compose", path="compose.yaml", content="", command="docker compose up")
        with self.assertRaises(ValidationError):
            GeneratedArtifact(kind="dockerfile", path="src/main.py", content="FROM alpine")

    def test_ai_result_and_diagnosis_validate_known_shapes_only(self):
        result = AIResult(intent="generate", summary="Review these files", artifacts=[])
        diagnosis = ContainerDiagnosis(
            container_name="web", summary="Likely missing environment configuration", confidence="medium"
        )
        self.assertEqual(result.intent, "generate")
        self.assertEqual(diagnosis.confidence, "medium")
        with self.assertRaises(ValidationError):
            AIResult(intent="run_shell", summary="anything")

    def test_operate_intent_requires_an_allowlisted_operation(self):
        result = AIResult(intent="operate", operation="status", summary="Read project state")
        self.assertEqual(result.operation, "status")
        with self.assertRaises(ValidationError):
            AIResult(intent="operate", summary="Start something")
        with self.assertRaises(ValidationError):
            AIResult(intent="operate", operation="shell", summary="Run a command")

    def test_project_context_has_typed_signals_and_files(self):
        context = ProjectContext(
            project_root="C:/workspace/app",
            signals=ProjectSignals(languages=["Python"], frameworks=["FastAPI"]),
            files=[ProjectFile(path="pyproject.toml", category="manifest", content="[project]")],
        )
        self.assertEqual(context.signals.frameworks, ["FastAPI"])
        self.assertEqual(context.files[0].category, "manifest")


if __name__ == "__main__":
    unittest.main()
