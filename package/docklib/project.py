"""Shared read-only inspection of a Dock project and its host."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import load_config
from .docker_engine import DockerEngine, DockerError, DockerStatus
from .planner import ProvisioningPlan, create_plan
from .requirements import RequirementStatus, inspect_requirements
from .system import SystemProfile, discover_system


@dataclass
class ProjectSnapshot:
    config_path: str
    config: dict
    system: SystemProfile
    requirements: list[RequirementStatus]
    docker: DockerStatus
    containers: list[dict[str, str]]
    plan: ProvisioningPlan
    inspection_error: str | None = None


def inspect_project(config_path: str = "dock.yaml", docker: DockerEngine | None = None) -> ProjectSnapshot:
    """Load one local project and collect the state needed by status/sync/init."""
    engine = docker or DockerEngine()
    config = load_config(config_path)
    system = discover_system()
    package_names = config.get("system", {}).get("packages", [])
    requirements = inspect_requirements(package_names, system.package_manager)
    docker_status = engine.check()
    containers: list[dict[str, str]] = []
    inspection_error: str | None = None
    if docker_status.daemon_running:
        try:
            containers = engine.list_project_containers(config["name"])
        except DockerError as exc:
            inspection_error = str(exc)
    image_available = None
    local_build_context: str | None = None
    local_dockerfile: str | None = None
    dockerfile_requested = False
    application = config.get("application") or {}
    if docker_status.daemon_running and application.get("image"):
        image_available = engine.image_exists(application["image"])
    if application.get("image"):
        project_dir = Path(config_path).expanduser().resolve().parent
        docker_settings = application.get("docker") or {}
        configured_dockerfile = docker_settings.get("dockerfile")
        dockerfile_requested = bool(configured_dockerfile)
        candidate = Path(configured_dockerfile) if configured_dockerfile else Path("Dockerfile")
        if not candidate.is_absolute():
            candidate = project_dir / candidate
        if candidate.is_file():
            local_build_context = str(project_dir)
            local_dockerfile = str(candidate.resolve())
        elif configured_dockerfile:
            local_build_context = str(project_dir)
            local_dockerfile = str(candidate)
    plan = create_plan(
        config, system, requirements, docker_status, containers, image_available,
        local_build_context, local_dockerfile, dockerfile_requested,
    )
    return ProjectSnapshot(
        config_path=str(config_path),
        config=config,
        system=system,
        requirements=requirements,
        docker=docker_status,
        containers=containers,
        plan=plan,
        inspection_error=inspection_error,
    )
