"""Provider-independent Dock AI workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .actions import AIActionError, secure_dockerignore, validate_generated_artifact
from .context import collect_project_context, sanitize_untrusted_text
from .prompts import artifact_prompt, diagnosis_prompt, deployment_prompt, request_prompt
from .schemas import AIResult, ContainerDiagnosis, DeploymentPlan, GeneratedArtifact, ProjectContext


class AIServiceError(RuntimeError):
    """Raised when a Dock AI workflow cannot safely produce a result."""


@dataclass
class AIService:
    client: Any
    docker: Any | None = None
    log_tail: int = 150
    max_log_chars: int = 12_000

    def __post_init__(self) -> None:
        if self.log_tail < 1 or self.max_log_chars < 1:
            raise ValueError("log_tail and max_log_chars must be positive")

    def _context(self, project_root: str | Path) -> ProjectContext:
        return collect_project_context(project_root)

    def handle_request(self, request: str, project_root: str | Path = ".") -> tuple[ProjectContext, AIResult]:
        """Interpret one natural-language request without applying changes."""
        if not request.strip():
            raise ValueError("request cannot be empty")
        context = self._context(project_root)
        result = self.client.generate_json(request_prompt(request, context), AIResult)
        if result.intent == "plan" and result.deployment_plan is None:
            raise AIServiceError("The model selected deployment planning but returned no deployment plan.")
        if result.intent == "generate":
            artifacts = self._add_dockerignore_if_needed(result.artifacts, context)
            result = result.model_copy(update={"artifacts": artifacts})
            self._validate_artifacts(artifacts)
        return context, result

    def plan_deployment(self, project_root: str | Path = ".") -> tuple[ProjectContext, DeploymentPlan]:
        context = self._context(project_root)
        plan = self.client.generate_json(deployment_prompt(context), DeploymentPlan)
        return context, plan

    def generate_artifacts(
        self,
        kinds: Iterable[str],
        project_root: str | Path = ".",
        request: str = "Prepare this project for local container deployment.",
    ) -> tuple[ProjectContext, list[GeneratedArtifact]]:
        allowed = set(kinds)
        unsupported = allowed - {"dockerfile", "dockerignore", "compose", "dock_yaml"}
        if unsupported:
            raise ValueError("Unsupported artifact kind(s): " + ", ".join(sorted(unsupported)))
        if not allowed:
            raise ValueError("At least one artifact kind is required")
        context = self._context(project_root)
        result = self.client.generate_json(artifact_prompt(context, allowed, request), AIResult)
        if result.intent != "generate":
            raise AIServiceError(f"The model returned intent '{result.intent}' instead of 'generate'.")
        returned_kinds = {item.kind for item in result.artifacts}
        unexpected = returned_kinds - allowed
        if unexpected:
            raise AIServiceError("The model returned unrequested artifact kind(s): " + ", ".join(sorted(unexpected)))
        artifacts = self._add_dockerignore_if_needed(result.artifacts, context)
        self._validate_artifacts(artifacts)
        return context, artifacts

    @staticmethod
    def _add_dockerignore_if_needed(
        artifacts: list[GeneratedArtifact], context: ProjectContext
    ) -> list[GeneratedArtifact]:
        """Pair image/build proposals with deterministic secret exclusions."""
        needs_build_context_protection = any(
            item.kind in {"dockerfile", "compose", "dockerignore"} for item in artifacts
        )
        if not needs_build_context_protection:
            return artifacts
        existing = next(
            (item.content for item in context.files if item.path.lower() == ".dockerignore"),
            "",
        )
        proposed = next((item.content for item in artifacts if item.kind == "dockerignore"), existing)
        secure = secure_dockerignore(proposed)
        companion = GeneratedArtifact(kind="dockerignore", path=".dockerignore", content=secure)
        return [item for item in artifacts if item.kind != "dockerignore"] + [companion]

    def diagnose_container(
        self,
        project_snapshot: Any,
        container_name: str,
        request: str = "Explain why this container may be failing.",
    ) -> ContainerDiagnosis:
        """Summarize bounded, redacted logs for a Dock-managed container."""
        from ..project import ProjectSnapshot

        from ..docker_engine import DockerEngine, DockerError

        if not isinstance(project_snapshot, ProjectSnapshot):
            raise AIServiceError("Container diagnosis requires a Dock project snapshot.")
        if not project_snapshot.docker.daemon_running:
            raise AIServiceError("Docker must be running to diagnose container logs.")
        engine = self.docker or DockerEngine()
        try:
            containers = engine.list_project_containers(project_snapshot.plan.name)
            container = next((item for item in containers if item.get("name") == container_name), None)
            if container is None:
                raise AIServiceError(
                    f"No Dock-managed container named '{container_name}' was found for this project."
                )
            logs = engine.logs(container["id"], tail=self.log_tail)
        except DockerError as exc:
            raise AIServiceError(f"Could not collect container logs: {exc}") from exc
        safe_logs = sanitize_untrusted_text(logs)[-self.max_log_chars:]
        prompt = diagnosis_prompt(container, safe_logs, request)
        diagnosis = self.client.generate_json(prompt, ContainerDiagnosis)
        if diagnosis.container_name != container_name:
            raise AIServiceError("The model response did not match the selected container.")
        return diagnosis

    @staticmethod
    def _validate_artifacts(artifacts: list[GeneratedArtifact]) -> None:
        for artifact in artifacts:
            try:
                validate_generated_artifact(artifact)
            except AIActionError as exc:
                raise AIServiceError(f"Artifact '{artifact.path}' failed local validation: {exc}") from exc

