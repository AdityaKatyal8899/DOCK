"""Build a dry-run provisioning plan from a config and discovered system."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import ConfigError, load_config
from .docker_engine import DockerStatus
from .requirements import RequirementStatus, inspect_requirements
from .system import SystemProfile, discover_system


def _application_fingerprint(application: dict[str, Any]) -> str:
    encoded = json.dumps(application, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class PlanStep:
    action: str
    description: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProvisioningPlan:
    name: str
    steps: list[PlanStep] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)


def create_plan(
    config: dict[str, Any],
    system: SystemProfile,
    package_statuses: list[RequirementStatus] | None = None,
    docker_status: DockerStatus | None = None,
    containers: list[dict[str, str]] | None = None,
    image_available: bool | None = None,
    local_build_context: str | None = None,
    local_dockerfile: str | None = None,
    dockerfile_requested: bool = False,
) -> ProvisioningPlan:
    """Compare desired config with discovered state without changing anything."""
    plan = ProvisioningPlan(name=config["name"])
    packages = config.get("system", {}).get("packages", [])
    wants_docker = config.get("docker", {}).get("enabled", False)
    application = config.get("application")

    if package_statuses is None:
        package_statuses = inspect_requirements(packages, system.package_manager)
    package_by_name = {item.name: item for item in package_statuses}
    missing_packages = [name for name in packages if package_by_name.get(name) and package_by_name[name].installed is False]
    unknown_packages = [name for name in packages if package_by_name.get(name) is None or package_by_name[name].installed is None]
    if missing_packages:
        plan.steps.append(PlanStep(
            action="ensure_packages",
            description=f"Install missing packages using {system.package_manager or 'a supported package manager'}.",
            details={"packages": missing_packages, "package_manager": system.package_manager},
        ))
    installed_packages = [name for name in packages if package_by_name.get(name) and package_by_name[name].installed is True]
    if installed_packages:
        plan.notes.append("Already available: " + ", ".join(installed_packages))
    if unknown_packages:
        plan.blockers.append("Could not determine whether these requirements are installed: " + ", ".join(unknown_packages))

    if docker_status is None:
        docker_status = DockerStatus(
            installed=system.docker_installed,
            daemon_running=False,
            version=system.docker_version,
            detail="Daemon state was not checked.",
        )
    docker_required = wants_docker or application is not None
    if docker_required and not docker_status.installed:
        plan.steps.append(PlanStep(
            action="install_docker",
            description="Install and configure Docker for this operating system.",
            details={"os": system.os, "distribution": system.distribution, "package_manager": system.package_manager},
        ))
    elif docker_required and not docker_status.daemon_running:
        plan.steps.append(PlanStep(
            action="start_docker_service",
            description="Start the installed Docker service or desktop engine.",
            details={"os": system.os},
        ))

    if application is not None:
        source = application.get("source")
        app_docker = application.get("docker")
        configured_image = application.get("image")
        if not wants_docker:
            plan.blockers.append(
                "An application is configured, but 'docker.enabled: true' is not set. The current application flow requires Docker."
            )
        if source and (not app_docker or not app_docker.get("dockerfile")):
            plan.blockers.append("An application is configured without 'application.docker.dockerfile'.")

        current = containers or []
        matching = next((item for item in current if item.get("name") == config["name"]), None)
        fingerprint = _application_fingerprint(application)
        matches_config = bool(matching) and matching.get("labels", {}).get("dock.config-hash") == fingerprint

        if matching and matches_config and matching.get("status", "").lower().startswith("up"):
            plan.notes.append(f"Application container '{config['name']}' is already running and matches the current configuration.")
        elif matching and matches_config:
            plan.steps.append(PlanStep(
                action="start_project_containers",
                description="Start the existing stopped application container.",
                details={"container": config["name"], "ports": application.get("ports", [])},
            ))
            healthcheck = application.get("healthcheck")
            if healthcheck:
                plan.steps.append(PlanStep(
                    action="verify_application",
                    description="Check the application HTTP health endpoint.",
                    details=healthcheck,
                ))
        elif configured_image:
            should_build_locally = image_available is not True and bool(
                local_build_context and local_dockerfile and Path(local_dockerfile).is_file()
            )
            missing_requested_dockerfile = (
                image_available is not True and dockerfile_requested and not should_build_locally
            )
            if missing_requested_dockerfile:
                plan.blockers.append(
                    f"The configured application Dockerfile '{local_dockerfile}' is unavailable. "
                    "Create it or update 'application.docker.dockerfile', then run 'dock sync' again."
                )
            elif should_build_locally:
                plan.steps.append(PlanStep(
                    action="build_image",
                    description=f"Build Docker image '{configured_image}' from '{local_dockerfile}'.",
                    details={
                        "image": configured_image,
                        "context": local_build_context,
                        "dockerfile": local_dockerfile,
                        "reuse_existing": True,
                    },
                ))
            elif image_available is not True:
                plan.steps.append(PlanStep(
                    action="pull_image",
                    description=f"Pull the configured Docker image '{configured_image}'.",
                    details={"image": configured_image},
                ))
            if not missing_requested_dockerfile:
                plan.steps.append(PlanStep(
                    action="update_image_application" if matching else "run_application",
                    description=(
                        "Replace the application container with the configured Docker image."
                        if matching else (
                            "Create and start a container using the locally available Docker image."
                            if image_available is True else
                            "Create and start a container from the configured Docker image."
                        )
                    ),
                    details={
                        "container": config["name"],
                        "image": configured_image,
                        "image_available": image_available is True,
                        "ports": application.get("ports", []),
                        "config_hash": fingerprint,
                    },
                ))
                healthcheck = application.get("healthcheck")
                if healthcheck:
                    plan.steps.append(PlanStep(
                        action="verify_application",
                        description="Check the application HTTP health endpoint.",
                        details=healthcheck,
                    ))
        elif matching:
            plan.steps.append(PlanStep(
                action="update_application",
                description="Update the application container to match the current configuration.",
                details={
                    "container": config["name"],
                    "image": config["name"],
                    "source": source,
                    "dockerfile": (app_docker or {}).get("dockerfile"),
                    "ports": application.get("ports", []),
                    "config_hash": fingerprint,
                },
            ))
            healthcheck = application.get("healthcheck")
            if healthcheck:
                plan.steps.append(PlanStep(
                    action="verify_application",
                    description="Check the application HTTP health endpoint.",
                    details=healthcheck,
                ))
        elif source and app_docker and app_docker.get("dockerfile"):
            repository = source["repository"]
            branch = source.get("branch", "main")
            dockerfile = app_docker["dockerfile"]
            plan.steps.append(PlanStep(
                action="fetch_application",
                description="Fetch the configured application source.",
                details={"type": source["type"], "repository": repository, "branch": branch},
            ))
            plan.steps.append(PlanStep(
                action="build_image",
                description="Build a Docker image from the configured Dockerfile.",
                details={"dockerfile": dockerfile, "image": config["name"]},
            ))
            plan.steps.append(PlanStep(
                action="run_application",
                description="Create and start the application container.",
                details={
                    "container": config["name"],
                    "image": config["name"],
                    "ports": application.get("ports", []),
                    "config_hash": fingerprint,
                },
            ))
            healthcheck = application.get("healthcheck")
            if healthcheck:
                plan.steps.append(PlanStep(
                    action="verify_application",
                    description="Check the application HTTP health endpoint.",
                    details=healthcheck,
                ))

    return plan


def print_plan(plan: ProvisioningPlan) -> None:
    print(f"Dock plan: {plan.name}")
    if plan.steps:
        for index, step in enumerate(plan.steps, start=1):
            print(f"{index}. {step.description}")
            if step.details:
                print(f"   {json.dumps(step.details, sort_keys=True)}")
    else:
        print("No provisioning steps were produced.")
    for note in plan.notes:
        print(f"Note: {note}")
    for blocker in plan.blockers:
        print(f"Blocker: {blocker}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a dry-run Dock provisioning plan.")
    parser.add_argument("config", nargs="?", default="dock.yaml", help="Path to Dock YAML config")
    parser.add_argument("--json", action="store_true", help="Print plan as JSON")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(str(exc))
    plan = create_plan(config, discover_system())
    if args.json:
        print(json.dumps(asdict(plan), indent=2))
    else:
        print_plan(plan)
    return 1 if plan.blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
