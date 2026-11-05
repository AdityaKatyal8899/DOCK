"""Validated data contracts shared by Dock AI modules.

These models describe proposals and collected context. They do not execute
commands or modify files.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectFile(StrictModel):
    """A bounded, sanitized text excerpt from inside a project workspace."""

    path: str = Field(min_length=1, max_length=500)
    category: Literal["manifest", "source", "docker", "documentation", "config"]
    content: str = Field(max_length=24_000)

    @field_validator("path")
    @classmethod
    def path_must_be_workspace_relative(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if not normalized.strip() or normalized in {".", "./"}:
            raise ValueError("project file path must name a file")
        if normalized.startswith("/") or PureWindowsPath(value).is_absolute():
            raise ValueError("project file paths must be relative to the workspace")
        if any(part == ".." for part in Path(normalized).parts):
            raise ValueError("project file paths cannot escape the workspace")
        return normalized


class ProjectSignals(StrictModel):
    languages: list[str] = Field(default_factory=list, max_length=12)
    frameworks: list[str] = Field(default_factory=list, max_length=20)
    package_manager: str | None = Field(default=None, max_length=80)
    dependency_files: list[str] = Field(default_factory=list, max_length=30)
    dockerfiles: list[str] = Field(default_factory=list, max_length=20)
    compose_files: list[str] = Field(default_factory=list, max_length=20)
    entry_points: list[str] = Field(default_factory=list, max_length=30)
    environment_variables: list[str] = Field(default_factory=list, max_length=100)
    environment_files: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("environment_variables")
    @classmethod
    def environment_entries_are_names(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item) for item in values):
            raise ValueError("environment_variables must contain variable names only")
        return values

    @field_validator("environment_files")
    @classmethod
    def environment_paths_are_relative(cls, values: list[str]) -> list[str]:
        for value in values:
            normalized = value.replace("\\", "/")
            if not normalized or normalized.startswith("/") or PureWindowsPath(value).is_absolute():
                raise ValueError("environment_files must contain workspace-relative paths")
            if any(part in {"", ".", ".."} for part in normalized.split("/")):
                raise ValueError("environment_files must not escape the workspace")
        return [value.replace("\\", "/") for value in values]


class ProjectContext(StrictModel):
    """Deterministic inventory and selected text sent to an AI workflow."""

    project_root: str = Field(min_length=1, max_length=1000)
    signals: ProjectSignals
    files: list[ProjectFile] = Field(default_factory=list, max_length=100)
    warnings: list[str] = Field(default_factory=list, max_length=50)


class PortMapping(StrictModel):
    host: int = Field(ge=1, le=65535)
    container: int = Field(ge=1, le=65535)


class ServiceProposal(StrictModel):
    """A declarative service suggestion; never an executable command."""

    name: str = Field(min_length=1, max_length=80)
    purpose: str = Field(default="", max_length=500)
    image: str | None = Field(default=None, max_length=300)
    build_context: str | None = Field(default=None, max_length=500)
    ports: list[PortMapping] = Field(default_factory=list, max_length=32)
    volumes: list[str] = Field(default_factory=list, max_length=32)
    environment_variables: list[str] = Field(default_factory=list, max_length=100)
    depends_on: list[str] = Field(default_factory=list, max_length=32)
    cpu_recommendation: str | None = Field(default=None, max_length=100)
    memory_recommendation: str | None = Field(default=None, max_length=100)

    @field_validator("environment_variables")
    @classmethod
    def environment_entries_are_names(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item) for item in values):
            raise ValueError("environment_variables must contain variable names only")
        return values


class DeploymentPlan(StrictModel):
    summary: str = Field(min_length=1, max_length=3000)
    services: list[ServiceProposal] = Field(default_factory=list, max_length=32)
    host_prerequisites: list[str] = Field(default_factory=list, max_length=50)
    assumptions: list[str] = Field(default_factory=list, max_length=50)
    warnings: list[str] = Field(default_factory=list, max_length=50)


class GeneratedArtifact(StrictModel):
    kind: Literal["dockerfile", "dockerignore", "compose", "dock_yaml"]
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=100_000)
    validation_messages: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("path")
    @classmethod
    def artifact_path_must_be_relative(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if not normalized.strip() or normalized in {".", "./"}:
            raise ValueError("artifact path must name a file")
        if normalized.startswith("/") or PureWindowsPath(value).is_absolute():
            raise ValueError("artifact paths must be relative to the workspace")
        if any(part == ".." for part in Path(normalized).parts):
            raise ValueError("artifact paths cannot escape the workspace")
        return normalized

    @model_validator(mode="after")
    def path_must_match_artifact_kind(self) -> "GeneratedArtifact":
        basename = PurePosixPath(self.path).name.lower()
        expected = {
            "dockerfile": lambda name: name == "dockerfile" or name.startswith("dockerfile."),
            "dockerignore": lambda name: name == ".dockerignore",
            "compose": lambda name: name in {"compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"},
            "dock_yaml": lambda name: name in {"dock.yaml", "dock.yml"},
        }
        if not expected[self.kind](basename):
            raise ValueError(f"{self.kind} artifacts must use their standard configuration filename")
        return self


class AIResult(StrictModel):
    intent: Literal["analyze", "plan", "generate", "diagnose", "operate"]
    operation: Literal["status", "start", "stop", "build", "provision"] | None = None
    summary: str = Field(min_length=1, max_length=5000)
    deployment_plan: DeploymentPlan | None = None
    artifacts: list[GeneratedArtifact] = Field(default_factory=list, max_length=20)
    follow_up_questions: list[str] = Field(default_factory=list, max_length=20)
    warnings: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def operation_matches_intent(self) -> "AIResult":
        if self.intent == "operate" and self.operation is None:
            raise ValueError("operate intent requires an allowlisted operation")
        if self.intent != "operate" and self.operation is not None:
            raise ValueError("operation is only allowed with operate intent")
        return self


class ContainerDiagnosis(StrictModel):
    container_name: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=3000)
    likely_causes: list[str] = Field(default_factory=list, max_length=20)
    recommended_checks: list[str] = Field(default_factory=list, max_length=30)
    confidence: Literal["low", "medium", "high"] = "low"
    warnings: list[str] = Field(default_factory=list, max_length=30)

