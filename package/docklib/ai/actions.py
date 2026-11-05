"""Workspace-bounded artifact handling and explicit Dock action approval."""

from __future__ import annotations

import difflib
import re
from pathlib import Path, PureWindowsPath
from typing import Any

import yaml

from ..planner import PlanStep, ProvisioningPlan
from ..project import ProjectSnapshot
from .schemas import GeneratedArtifact


class AIActionError(RuntimeError):
    """Raised when a proposed AI action is invalid or not approved."""


_DOCKERIGNORE_MARKER = "# Dock safety rules: keep secrets out of the Docker build context"
_DOCKERIGNORE_RULES = (
    ".env", ".env.*", "**/.env", "**/.env.*", "*.env", "**/*.env",
    ".envrc", "**/.envrc", "*.pem", "**/*.pem", "*.key", "**/*.key",
    "*.p12", "**/*.p12", "*.pfx", "**/*.pfx", "*.keystore", "**/*.keystore",
    "secrets/", "**/secrets/", "credentials/", "**/credentials/",
)
_DOCKERIGNORE_FOOTER = _DOCKERIGNORE_MARKER + "\n" + "\n".join(_DOCKERIGNORE_RULES)


def secure_dockerignore(existing: str = "") -> str:
    """Preserve custom ignore rules, then enforce Dock's secret exclusions."""
    base = existing.rstrip()
    marker_index = base.rfind(_DOCKERIGNORE_MARKER)
    if marker_index >= 0:
        base = base[:marker_index].rstrip()
    return (base + "\n\n" if base else "") + _DOCKERIGNORE_FOOTER + "\n"


def _validated_artifact(artifact: GeneratedArtifact) -> GeneratedArtifact:
    try:
        return GeneratedArtifact.model_validate(artifact.model_dump())
    except Exception as exc:
        raise AIActionError(f"Generated artifact failed schema validation: {exc}") from exc


def _workspace_target(workspace: str | Path, relative_path: str) -> tuple[Path, Path]:
    root = Path(workspace).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise AIActionError("The selected project workspace is not a directory.")
    if Path(relative_path).is_absolute() or PureWindowsPath(relative_path).is_absolute():
        raise AIActionError("Artifact paths must be relative to the selected project workspace.")
    if any(part == ".." for part in relative_path.replace("\\", "/").split("/")):
        raise AIActionError("Artifact paths cannot escape the selected project workspace.")

    target = root / relative_path
    try:
        resolved = target.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise AIActionError("Artifact path resolves outside the selected project workspace.") from exc

    current = root
    for part in Path(relative_path.replace("\\", "/")).parts:
        current = current / part
        if current.is_symlink():
            raise AIActionError("Artifact paths cannot pass through symbolic links.")
    return root, resolved


def validate_generated_artifact(artifact: GeneratedArtifact) -> list[str]:
    """Perform local static checks without writing files or running tools."""
    artifact = _validated_artifact(artifact)
    warnings: list[str] = []
    if "\0" in artifact.content:
        raise AIActionError("Generated artifact contains a null byte.")

    if artifact.kind == "dockerfile":
        instructions = [
            line for line in artifact.content.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not any(re.match(r"(?i)^\s*FROM\s+", line) for line in instructions):
            raise AIActionError("Dockerfile preview must contain a FROM instruction.")
        warnings.append("Dockerfile syntax was checked for a FROM instruction; no image build was run.")
    elif artifact.kind == "compose":
        try:
            data = yaml.safe_load(artifact.content)
        except yaml.YAMLError as exc:
            raise AIActionError(f"Compose preview is not valid YAML: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("services"), dict) or not data["services"]:
            raise AIActionError("Compose preview must define a non-empty services mapping.")
        if any(not isinstance(name, str) or not isinstance(value, dict) for name, value in data["services"].items()):
            raise AIActionError("Every Compose service must have a name and a mapping of settings.")
        warnings.append("Compose structure was checked locally; no Compose command was executed.")
    elif artifact.kind == "dockerignore":
        if not artifact.content.rstrip().endswith(_DOCKERIGNORE_FOOTER):
            raise AIActionError(
                ".dockerignore must end with Dock's required rules for env files, keys, and credentials."
            )
        warnings.append("Docker build-context exclusions include Dock's required secret-file patterns.")
    elif artifact.kind == "dock_yaml":
        from ..config import ConfigError, validate_config

        try:
            data = yaml.safe_load(artifact.content)
            validate_config(data)
        except (yaml.YAMLError, ConfigError) as exc:
            raise AIActionError(f"Dock configuration preview is invalid: {exc}") from exc
        warnings.append("Dock configuration passed the current Dock schema validation.")
    return warnings


def preview_artifact(artifact: GeneratedArtifact, workspace: str | Path) -> str:
    """Return a unified diff for review; this function never writes."""
    artifact = _validated_artifact(artifact)
    validate_generated_artifact(artifact)
    _, target = _workspace_target(workspace, artifact.path)
    if target.exists():
        if not target.is_file():
            raise AIActionError("The artifact target exists and is not a regular file.")
        try:
            current = target.read_text(encoding="utf-8").splitlines(keepends=True)
        except (OSError, UnicodeError) as exc:
            raise AIActionError(f"Could not read existing artifact target: {exc}") from exc
        from_label = artifact.path
    else:
        current = []
        from_label = "/dev/null"
    proposed = artifact.content.splitlines(keepends=True)
    return "".join(difflib.unified_diff(
        current, proposed, fromfile=from_label, tofile=artifact.path + " (proposed)"
    ))


def write_approved_artifact(
    artifact: GeneratedArtifact,
    workspace: str | Path,
    *,
    approved: bool,
    overwrite_approved: bool = False,
) -> Path:
    """Write one validated artifact only after explicit user approval."""
    if approved is not True:
        raise AIActionError("File modification was not approved.")
    artifact = _validated_artifact(artifact)
    validate_generated_artifact(artifact)
    root, target = _workspace_target(workspace, artifact.path)
    if target.exists():
        if target.is_symlink() or not target.is_file():
            raise AIActionError("Refusing to replace a non-regular artifact target.")
        if not overwrite_approved:
            raise AIActionError("Replacing an existing file requires separate explicit approval.")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Re-resolve after creating parents so a symlinked directory cannot
        # redirect the write outside the workspace.
        _, checked_target = _workspace_target(root, artifact.path)
        if checked_target != target:
            raise AIActionError("Artifact path changed while preparing the write.")
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise AIActionError("Refusing to replace a non-regular artifact target.")
        if target.exists() and not overwrite_approved:
            raise AIActionError("Replacing an existing file requires separate explicit approval.")
        # Exclusive creation prevents silently overwriting a file that appears
        # after the approval check. Existing-file replacement is only allowed
        # with the separate overwrite approval above.
        mode = "w" if overwrite_approved else "x"
        with target.open(mode, encoding="utf-8", newline="\n") as stream:
            stream.write(artifact.content)
    except AIActionError:
        raise
    except FileExistsError as exc:
        raise AIActionError("The artifact target appeared during the write; review and approve again.") from exc
    except OSError as exc:
        raise AIActionError(f"Could not write approved artifact: {exc}") from exc
    return target


def _execution_snapshot(snapshot: Any) -> tuple[ProvisioningPlan, Any]:
    if not isinstance(snapshot, ProjectSnapshot):
        raise AIActionError("Execution requires a Dock project snapshot from inspect_project().")
    plan = getattr(snapshot, "plan", None)
    system = getattr(snapshot, "system", None)
    if not isinstance(plan, ProvisioningPlan) or not all(isinstance(step, PlanStep) for step in plan.steps) or system is None:
        raise AIActionError("Execution requires a Dock project snapshot from inspect_project().")
    if snapshot.inspection_error:
        raise AIActionError("Dock could not fully inspect the project containers: " + snapshot.inspection_error)
    if plan.blockers:
        raise AIActionError("Dock plan has blockers: " + " ".join(plan.blockers))
    return plan, system


def execute_approved_plan(snapshot: Any, *, approved: bool, executor: Any | None = None) -> list[str]:
    """Apply the inspected Dock plan after explicit user approval."""
    if approved is not True:
        raise AIActionError("Infrastructure execution was not approved.")
    plan, system = _execution_snapshot(snapshot)
    if executor is None:
        from ..executor import Executor

        executor = Executor()
    try:
        return executor.execute(plan, os_name=system.os)
    except Exception as exc:
        raise AIActionError(str(exc)) from exc


def execute_approved_build(snapshot: Any, *, approved: bool, executor: Any | None = None) -> list[str]:
    """Execute only fetch/build steps from the inspected Dock plan."""
    if approved is not True:
        raise AIActionError("Image build was not approved.")
    plan, system = _execution_snapshot(snapshot)
    build_steps = [step for step in plan.steps if step.action in {"fetch_application", "build_image"}]
    if not build_steps:
        raise AIActionError("The Dock plan has no local image build step to execute.")
    build_plan = ProvisioningPlan(name=plan.name, steps=build_steps, blockers=plan.blockers)
    if executor is None:
        from ..executor import Executor

        executor = Executor()
    try:
        return executor.execute(build_plan, os_name=system.os)
    except Exception as exc:
        raise AIActionError(str(exc)) from exc


def execute_approved_lifecycle(
    snapshot: Any,
    action: str,
    *,
    approved: bool,
    executor: Any | None = None,
) -> list[str]:
    """Start or stop only the Dock-managed containers in an inspected project."""
    if approved is not True:
        raise AIActionError("Container lifecycle change was not approved.")
    if action not in {"start", "stop"}:
        raise AIActionError("Lifecycle action must be 'start' or 'stop'.")
    plan, _ = _execution_snapshot(snapshot)
    if not snapshot.docker.daemon_running:
        detail = snapshot.docker.detail or "Docker daemon is not responding."
        raise AIActionError(f"Docker is not ready: {detail}")
    if executor is None:
        from ..executor import Executor

        executor = Executor()
    try:
        if action == "start":
            return executor.start_project_containers(plan.name)
        return executor.stop_project_containers(plan.name)
    except Exception as exc:
        raise AIActionError(str(exc)) from exc

