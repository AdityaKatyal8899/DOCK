"""Interactive ``dock ai`` command; all mutations require separate approval."""

from __future__ import annotations

import sys
import os
from pathlib import Path
from typing import Any


_RESET = "\033[0m"
_BOLD = "\033[1m"
_COLORS = {
    "brand": "\033[38;5;208m",
    "response": "\033[38;5;82m",
    "signals": "\033[38;5;39m",
    "plan": "\033[38;5;51m",
    "muted": "\033[38;5;245m",
    "warning": "\033[38;5;220m",
    "error": "\033[38;5;203m",
}


def _color_enabled(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR") is not None or os.environ.get("TERM", "").lower() == "dumb":
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


def _paint(text: str, tone: str, *, bold: bool = False, stream=None) -> str:
    if not _color_enabled(stream):
        return text
    prefix = _COLORS[tone] + (_BOLD if bold else "")
    return prefix + text + _RESET


def _section(title: str, tone: str) -> None:
    print("\n" + _paint(title.upper(), tone, bold=True))
    print(_paint("─" * max(len(title), 18), "muted"))


def _approved(question: str) -> bool:
    try:
        answer = input(question + " [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nApproval not received; no changes were made.")
        return False
    return answer in {"y", "yes"}


def _print_context(context: Any) -> None:
    signals = context.signals
    _section("Project signals", "signals")
    print("  Languages:       " + (", ".join(signals.languages) or "Not detected"))
    print("  Frameworks:      " + (", ".join(signals.frameworks) or "Not detected"))
    print("  Package manager: " + (signals.package_manager or "Not detected"))
    print("  Dependencies:    " + (", ".join(signals.dependency_files) or "None found"))
    print("  Dockerfiles:     " + (", ".join(signals.dockerfiles) or "None found"))
    print("  Compose files:   " + (", ".join(signals.compose_files) or "None found"))
    print("  Entry points:    " + (", ".join(signals.entry_points) or "Not detected"))
    print("  Env var names:   " + (", ".join(signals.environment_variables) or "None detected"))
    print("  Env files:       " + (", ".join(signals.environment_files) or "None detected"))
    if signals.environment_files:
        print("  Env file values are never read or sent to the AI.")
    for warning in dict.fromkeys(context.warnings):
        print("  " + _paint("Note:", "warning", bold=True) + " " + warning)


def _print_plan(plan: Any) -> None:
    _section("Deployment plan", "plan")
    print("  " + plan.summary)
    for service in plan.services:
        print(f"\n  Service: {service.name}")
        if service.purpose:
            print("    Purpose: " + service.purpose)
        if service.image:
            print("    Image:   " + service.image)
        if service.build_context:
            print("    Build:   " + service.build_context)
        for port in service.ports:
            print(f"    Port:    localhost:{port.host} -> container:{port.container}")
        if service.volumes:
            print("    Volumes: " + ", ".join(service.volumes))
        if service.environment_variables:
            print("    Env:     " + ", ".join(service.environment_variables))
        if service.depends_on:
            print("    Depends: " + ", ".join(service.depends_on))
    for line in plan.host_prerequisites:
        print("  Prerequisite: " + line)
    for line in plan.assumptions:
        print("  " + _paint("Assumption:", "muted", bold=True) + " " + line)
    for line in plan.warnings:
        print("  " + _paint("Warning:", "warning", bold=True) + " " + line)


def _config_path(project_root: Path, configured_path: str) -> Path:
    path = Path(configured_path).expanduser()
    return path if path.is_absolute() else project_root / path


def run_ai_command(args, *, ai_client=None, service_factory=None) -> int:
    """Run analysis, generation, diagnosis, and explicitly approved actions."""
    try:
        from .actions import AIActionError, execute_approved_plan, preview_artifact, validate_generated_artifact, write_approved_artifact
        from .service import AIService, AIServiceError
        from .client import AIClient, AIClientError
    except ImportError as exc:
        print(
            "Dock AI dependencies are not installed. Install the optional extra with "
            "`python -m pip install 'dock-api[ai]'`.",
            file=sys.stderr,
        )
        return 1

    try:
        project_root = Path(args.project).expanduser().resolve(strict=True)
        if not project_root.is_dir():
            raise AIServiceError(f"Project directory does not exist: {project_root}")
        client = ai_client or AIClient.from_environment()
        service = (service_factory or AIService)(client)
        config_path = _config_path(project_root, args.config)

        if args.container:
            return _diagnose(service, args.request, config_path, args.container)

        context, result = service.handle_request(args.request, project_root)
        print(_paint(f"Dock AI · {client.provider}", "brand", bold=True))
        _section("AI response", "response")
        print("  " + result.summary.replace("\n", "\n  "))
        _print_context(context)
        if result.follow_up_questions:
            _section("Follow-up questions", "muted")
            for question in result.follow_up_questions:
                print("  ? " + question)
        if result.warnings:
            _section("AI warnings", "warning")
            for warning in result.warnings:
                print("  ! " + warning)

        if result.deployment_plan is not None:
            _print_plan(result.deployment_plan)

        if result.intent == "operate":
            return _handle_operation(service, result.operation, config_path)

        if result.intent == "diagnose":
            container_name = _default_container_name(config_path)
            return _diagnose(service, args.request, config_path, container_name)

        artifacts = sorted(result.artifacts, key=lambda item: item.kind != "dockerignore")
        requires_build_context_protection = any(
            item.kind in {"dockerfile", "compose"} for item in artifacts
        )
        dockerignore_ready = not requires_build_context_protection
        for artifact in artifacts:
            _section(f"Artifact preview · {artifact.path} ({artifact.kind})", "plan")
            for message in validate_generated_artifact(artifact):
                print("  Validation: " + message)
            diff = preview_artifact(artifact, project_root)
            if not diff:
                print("  No file changes proposed.")
                if artifact.kind == "dockerignore":
                    dockerignore_ready = True
                continue
            print(diff, end="" if diff.endswith("\n") else "\n")
            if artifact.kind in {"dockerfile", "compose"} and not dockerignore_ready:
                print(
                    "  Not written: approve the accompanying .dockerignore first to keep env files and secrets out of the build context."
                )
                continue
            if not _approved(f"Write {artifact.path}?"):
                print("  Skipped; no file was written.")
                continue
            target = project_root / artifact.path
            overwrite = target.exists()
            if overwrite and not _approved(f"Replace existing file {artifact.path}?"):
                print("  Skipped; existing file was left unchanged.")
                continue
            written = write_approved_artifact(
                artifact,
                project_root,
                approved=True,
                overwrite_approved=overwrite,
            )
            print("  Wrote " + str(written))
            if artifact.kind == "dockerignore":
                dockerignore_ready = True

        if args.apply:
            return _apply_config(service, config_path)
        return 0
    except (AIClientError, AIServiceError, AIActionError, ValueError, OSError) as exc:
        print(_paint(f"Dock AI error: {exc}", "error", bold=True, stream=sys.stderr), file=sys.stderr)
        return 1


def _print_project_status(snapshot: Any) -> None:
    _section("Live Dock status", "signals")
    print(f"  Project:        {snapshot.config['name']}")
    print(f"  Docker engine:  {'Running' if snapshot.docker.daemon_running else 'Unavailable'}")
    if snapshot.docker.version:
        print(f"  Docker version: {snapshot.docker.version}")
    if snapshot.docker.detail and not snapshot.docker.daemon_running:
        print("  Detail:         " + snapshot.docker.detail)

    application = snapshot.config.get("application") or {}
    image = application.get("image")
    if image:
        actions = {step.action for step in snapshot.plan.steps}
        if not snapshot.docker.daemon_running:
            image_state = "Unknown (Docker unavailable)"
        elif "build_image" in actions:
            image_state = "Build required"
        elif "pull_image" in actions:
            image_state = "Not present locally; pull required"
        else:
            image_state = "Available locally"
        print(f"  Image:          {image} ({image_state})")

    print("  Dock-managed containers (all states):")
    if snapshot.inspection_error:
        print("    Inspection failed: " + snapshot.inspection_error)
    elif snapshot.containers:
        columns = (
            ("CONTAINER ID", "id", 12),
            ("IMAGE", "image", 22),
            ("COMMAND", "command", 24),
            ("CREATED", "created", 22),
            ("STATUS", "status", 28),
            ("PORTS", "ports", 24),
            ("NAMES", "name", 20),
        )
        widths = [len(header) for header, _, _ in columns]
        for index, (_, key, limit) in enumerate(columns):
            widths[index] = min(limit, max(widths[index], *(len(str(c.get(key, ""))) for c in snapshot.containers)))
        header = "  " + "  ".join(name.ljust(widths[i]) for i, (name, _, _) in enumerate(columns))
        print(header)
        print("  " + "  ".join("─" * width for width in widths))
        for container in snapshot.containers:
            cells = []
            for index, (_, key, _) in enumerate(columns):
                value = str(container.get(key, "")) or "—"
                width = widths[index]
                if len(value) > width:
                    value = value[:width - 1] + "…"
                cells.append(value.ljust(width))
            print("  " + "  ".join(cells).rstrip())
    else:
        print("    None found for this project.")

    if snapshot.plan.blockers:
        print("  Blockers:")
        for blocker in snapshot.plan.blockers:
            print("    ! " + blocker)


def _handle_operation(service: Any, operation: str | None, config_path: Path) -> int:
    from ..docker_engine import DockerEngine
    from ..executor import Executor
    from ..project import inspect_project
    from .actions import execute_approved_build, execute_approved_lifecycle

    if operation not in {"status", "start", "stop", "build", "provision"}:
        raise ValueError("The model did not select a supported Dock operation.")
    _section(f"Dock action · {operation}", "plan")
    engine = service.docker or DockerEngine()
    snapshot = inspect_project(str(config_path), docker=engine)

    if operation == "status":
        _print_project_status(snapshot)
        return 0 if not snapshot.inspection_error else 1

    if operation == "provision":
        return _apply_config(service, config_path)

    if snapshot.inspection_error:
        raise ValueError("Cannot safely change containers after an incomplete Docker inspection: " + snapshot.inspection_error)
    if snapshot.plan.blockers:
        for blocker in snapshot.plan.blockers:
            print("  ! " + blocker)
        print("Resolve the Dock plan blockers before performing this operation.")
        return 1
    if not snapshot.docker.daemon_running:
        detail = snapshot.docker.detail or "Docker daemon is not responding."
        print("Docker is not ready: " + detail)
        return 1

    executor = Executor(docker=engine)
    if operation in {"start", "stop"}:
        if not snapshot.containers:
            _print_project_status(snapshot)
            print("No Dock-managed containers are available to " + operation + ".")
            return 0
        print(f"\nDock will {operation} these project containers:")
        for container in snapshot.containers:
            print(f"  - {container['name']} ({container['status']})")
        if not _approved(f"Approve {operation} for project '{snapshot.plan.name}'?"):
            print("No container changes were made.")
            return 0
        results = execute_approved_lifecycle(
            snapshot, operation, approved=True, executor=executor
        )
        for result in results:
            print("  " + result)
        refreshed = inspect_project(str(config_path), docker=engine)
        _print_project_status(refreshed)
        return 0

    build_steps = [step for step in snapshot.plan.steps if step.action in {"fetch_application", "build_image"}]
    if not build_steps:
        if any(step.action == "pull_image" for step in snapshot.plan.steps):
            print("This project uses a registry image; there is no local build step. Ask Dock to provision the project to pull and start it.")
        else:
            print("The Dock plan has no image build step to run.")
        return 0
    print("\nDock image build plan")
    for step in build_steps:
        print("  + " + step.description)
    if not _approved(f"Build the configured image for project '{snapshot.plan.name}'?"):
        print("No image was built.")
        return 0
    results = execute_approved_build(snapshot, approved=True, executor=executor)
    for result in results:
        print("  " + result)
    refreshed = inspect_project(str(config_path), docker=engine)
    _print_project_status(refreshed)
    return 0


def _diagnose(service, request: str, config_path: Path, container_name: str) -> int:
    from ..project import inspect_project

    from .service import AIServiceError

    snapshot = inspect_project(config_path, docker=service.docker)
    diagnosis = service.diagnose_container(snapshot, container_name, request)
    print(f"Container diagnosis: {diagnosis.container_name}\n")
    print(diagnosis.summary)
    print("Confidence: " + diagnosis.confidence)
    for cause in diagnosis.likely_causes:
        print("  Likely cause: " + cause)
    for check in diagnosis.recommended_checks:
        print("  Check: " + check)
    for warning in diagnosis.warnings:
        print("  Warning: " + warning)
    return 0


def _default_container_name(config_path: Path) -> str:
    from ..config import load_config

    config = load_config(config_path)
    if config.get("application") is None:
        from .service import AIServiceError

        raise AIServiceError("No application is configured. Use --container with a Dock project config.")
    return config["name"]


def _apply_config(service, config_path: Path) -> int:
    from ..docker_engine import DockerEngine
    from ..executor import Executor
    from ..project import inspect_project

    from .actions import execute_approved_plan

    engine = service.docker or DockerEngine()
    snapshot = inspect_project(config_path, docker=engine)
    print("\nDock plan to apply")
    for step in snapshot.plan.steps:
        print("  + " + step.description)
    for blocker in snapshot.plan.blockers:
        print("  ! " + blocker)
    if snapshot.inspection_error:
        print("  ! Container inspection failed: " + snapshot.inspection_error)
        print("Cannot safely apply a plan without complete container inspection.")
        return 1
    if snapshot.plan.blockers:
        print("Cannot apply this plan until its blockers are resolved.")
        return 1
    if not snapshot.plan.steps:
        print("  No provisioning changes are needed.")
    if not _approved("Apply this Dock plan?"):
        print("No infrastructure changes were made.")
        return 0
    results = execute_approved_plan(snapshot, approved=True, executor=Executor(docker=engine))
    for item in results:
        print("  " + item)
    refreshed = inspect_project(config_path, docker=engine)
    _print_project_status(refreshed)
    return 0

