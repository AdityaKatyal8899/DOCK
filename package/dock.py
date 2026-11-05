"""Local command-line interface for the Dock provisioning prototype."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

__version__ = "0.2.1"


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("config_path", nargs="?", help="Local YAML config path")
    parser.add_argument("-c", "--config", dest="config_option", help="Local YAML config path (default: dock.yaml)")


def _config_path(args: argparse.Namespace) -> str:
    return args.config_option or args.config_path or "dock.yaml"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dock",
        description="Inspect, compare, and provision a local project from dock.yaml.",
    )
    parser.add_argument("-v", "--version", action="version", version=f"Dock {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    discover = commands.add_parser("discover", help="Show the current machine profile")
    discover.add_argument("--json", action="store_true", help="Print the profile as JSON")

    validate = commands.add_parser("validate", help="Validate a local YAML configuration")
    _add_config_argument(validate)

    status = commands.add_parser("status", help="Show current system and container status")
    _add_config_argument(status)
    container_filter = status.add_mutually_exclusive_group()
    container_filter.add_argument(
        "--running", action="store_true", help="Show only running Dock-managed containers"
    )
    container_filter.add_argument(
        "--stopped", action="store_true", help="Show only non-running Dock-managed containers"
    )

    sync = commands.add_parser("sync", help="Compare the config with current state; make no changes")
    _add_config_argument(sync)

    init = commands.add_parser("init", aliases=["apply"], help="Apply the configuration locally")
    _add_config_argument(init)

    plan = commands.add_parser("plan", help="Show the installations and provisioning actions needed")
    _add_config_argument(plan)
    plan.add_argument("--json", action="store_true", help="Print the plan as JSON")

    for name, help_text in (
        ("start", "Start existing containers managed by this Dock project"),
        ("stop", "Stop containers managed by this Dock project without removing them"),
        ("logs", "Show recent logs from this project's managed containers"),
    ):
        command = commands.add_parser(name, help=help_text)
        _add_config_argument(command)

    ai = commands.add_parser("ai", help="Analyze a project with an optional AI provider")
    ai.add_argument("request", help="Natural-language project or container request")
    ai.add_argument("--project", default=".", help="Project workspace (default: current directory)")
    ai.add_argument("-c", "--config", default="dock.yaml", help="Dock YAML path for diagnosis or approved apply")
    ai.add_argument("--container", help="Diagnose this Dock-managed container using recent logs")
    ai.add_argument("--apply", action="store_true", help="Offer to apply the current Dock plan after preview")

    return parser


def _print_status(snapshot, container_filter: str | None = None) -> None:
    profile = snapshot.system
    distribution = f" ({profile.distribution} {profile.version})" if profile.distribution else f" ({profile.version})"
    print("Dock System Status")
    print("\nSystem")
    print(f"  OS:             {profile.os}{distribution}")
    print(f"  Architecture:   {profile.architecture}")
    print(f"  Package Mgr:    {profile.package_manager or 'Not detected'}")

    print("\nDocker")
    print(f"  Docker Engine:  {'Installed' if snapshot.docker.installed else 'Missing'}")
    print(f"  Docker Version: {snapshot.docker.version or 'Unknown'}")
    service_state = "Running" if snapshot.docker.daemon_running else "Not running / unavailable"
    print(f"  Docker Service: {service_state}")
    if snapshot.docker.detail:
        print(f"  Detail:         {snapshot.docker.detail}")

    print("\nRequired Tools")
    if snapshot.requirements:
        for requirement in snapshot.requirements:
            state = "Installed" if requirement.installed is True else "Missing" if requirement.installed is False else "Unknown"
            print(f"  {requirement.name + ':':<16}{state}")
    else:
        print("  No OS package requirements are configured.")

    print("\nContainers")
    if snapshot.inspection_error:
        print(f"  Could not inspect: {snapshot.inspection_error}")
    else:
        containers = snapshot.containers
        if container_filter == "running":
            containers = [item for item in containers if item["status"].lower().startswith("up")]
        elif container_filter == "stopped":
            containers = [item for item in containers if not item["status"].lower().startswith("up")]
        if containers:
            for container in containers:
                print(f"  {container['name'] + ':':<16}{container['status']}")
        elif container_filter:
            print(f"  No {container_filter} Dock-managed containers found for this project.")
        else:
            print("  No Dock-managed containers found for this project.")

    missing = sum(item.installed is False for item in snapshot.requirements)
    unknown = sum(item.installed is None for item in snapshot.requirements)
    docker_required = snapshot.config.get("docker", {}).get("enabled", False) or snapshot.config.get("application") is not None
    if docker_required and not snapshot.docker.installed:
        missing += 1
    elif docker_required and not snapshot.docker.daemon_running:
        missing += 1
    if snapshot.config.get("application") is not None:
        running_app = any(
            item["name"] == snapshot.plan.name and item["status"].lower().startswith("up")
            for item in snapshot.containers
        )
        if not running_app:
            missing += 1
    print("\nStatus")
    summary = f"{missing} requirement(s) missing"
    if unknown:
        summary += f"; {unknown} could not be checked"
    print(f"  {summary}")


def _print_sync(snapshot, title: str = "Dock Sync", show_next_step: bool = True) -> None:
    plan = snapshot.plan
    print(title)
    print(f"\nConfiguration: {snapshot.config_path}")
    print("\nSystem Requirements")
    if snapshot.requirements:
        for requirement in snapshot.requirements:
            current = "Installed" if requirement.installed is True else "Missing" if requirement.installed is False else "Unknown"
            action = "None" if requirement.installed is True else "Install" if requirement.installed is False else "Resolve detection"
            print(f"\n  {requirement.name}")
            print(f"    Current: {current}")
            print(f"    Action:  {action}")
    else:
        print("  No OS package requirements configured.")

    docker_required = snapshot.config.get("docker", {}).get("enabled", False) or snapshot.config.get("application") is not None
    if docker_required:
        docker_current = "Running" if snapshot.docker.daemon_running else "Installed, service stopped" if snapshot.docker.installed else "Missing"
        docker_actions = [step for step in plan.steps if step.action in {"install_docker", "start_docker_service"}]
        docker_action = docker_actions[0].description if docker_actions else "None"
        print("\n  Docker")
        print(f"    Current: {docker_current}")
        print(f"    Action:  {docker_action}")

    if snapshot.config.get("application") is not None:
        current_container = next((item for item in snapshot.containers if item["name"] == plan.name), None)
        app_current = current_container["status"] if current_container else "Not initialized"
        app_steps = [step for step in plan.steps if step.action in {
            "fetch_application", "build_image", "pull_image", "run_application", "update_application",
            "update_image_application", "start_project_containers"
        }]
        app_action = "; ".join(step.description for step in app_steps) if app_steps else "None"
        print("\n  Application")
        print(f"    Current: {app_current}")
        print(f"    Action:  {app_action}")

    print("\nRequired Actions")
    if plan.steps:
        for step in plan.steps:
            if step.action == "ensure_packages":
                print("  + Install " + ", ".join(step.details["packages"]))
            else:
                print(f"  + {step.description}")
    else:
        print("  No changes required.")
    for blocker in plan.blockers:
        print(f"  ! {blocker}")
    if plan.notes:
        print("\nNotes")
        for note in plan.notes:
            print(f"  {note}")
    if show_next_step:
        print("\nRun `dock init` to apply supported changes.")


def _print_plan(snapshot) -> None:
    """Show only pending prerequisite and application actions, without current-state detail."""
    steps = snapshot.plan.steps
    system_actions = [
        step for step in steps
        if step.action in {"ensure_packages", "install_docker", "start_docker_service"}
    ]
    application_actions = [
        step for step in steps
        if step.action not in {"ensure_packages", "install_docker", "start_docker_service"}
    ]

    print("Dock Plan")
    print(f"\nConfiguration: {snapshot.config_path}")
    print("\nInstallations and prerequisites")
    if system_actions:
        for step in system_actions:
            if step.action == "ensure_packages":
                print("  + Install " + ", ".join(step.details["packages"]))
            else:
                print(f"  + {step.description}")
    else:
        print("  No system installations or prerequisite changes needed.")

    if snapshot.config.get("application") is not None:
        print("\nApplication actions")
        if application_actions:
            for step in application_actions:
                print(f"  + {step.description}")
        else:
            state = "planned" if snapshot.plan.blockers else "needed"
            print(f"  No application changes {state}.")

    for blocker in snapshot.plan.blockers:
        print(f"\n  ! {blocker}")
    if steps and not snapshot.plan.blockers:
        print("\nRun `dock init` to apply these actions.")


def _print_runtime_summary(snapshot, executor) -> None:
    """Show the resulting application container state after init."""
    from docklib.docker_engine import DockerError

    application = snapshot.config.get("application")
    if application is None:
        return

    print("\nApplication now")
    try:
        containers = executor.docker.list_project_containers(snapshot.plan.name)
    except DockerError as exc:
        print(f"  Could not inspect the resulting container: {exc}")
        return

    container = next((item for item in containers if item["name"] == snapshot.plan.name), None)
    if container is None:
        print("  No Dock-managed application container was found.")
        return

    print(f"  Container: {container['name']}")
    print(f"  Image:     {container['image']}")
    print(f"  Status:    {container['status']}")
    ports = application.get("ports", [])
    if ports:
        print("  Ports:")
        for mapping in ports:
            print(f"    localhost:{mapping['host']} -> container:{mapping['container']}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "ai":
        from docklib.ai.cli import run_ai_command

        return run_ai_command(args)

    # Lazy imports keep `dock -v` and `dock --help` independent of dependencies.
    from docklib.config import ConfigError, load_config
    from docklib.docker_engine import DockerEngine, DockerError
    from docklib.executor import ExecutionError, Executor
    from docklib.project import inspect_project
    from docklib.system import discover_system, print_report

    try:
        if args.command == "discover":
            profile = discover_system()
            if args.json:
                print(json.dumps(asdict(profile), indent=2))
            else:
                print_report(profile)
            return 0

        if args.command == "validate":
            path = _config_path(args)
            load_config(path)
            print(f"Configuration '{path}' is valid.")
            print("The current schema is an initial prototype and may evolve.")
            return 0

        if args.command in {"status", "sync", "plan", "init", "apply", "start", "stop", "logs"}:
            path = _config_path(args)
            snapshot = inspect_project(path)

        if args.command == "status":
            container_filter = "running" if args.running else "stopped" if args.stopped else None
            _print_status(snapshot, container_filter)
            return 0

        if args.command in {"sync", "plan"}:
            if args.command == "plan" and args.json:
                print(json.dumps(asdict(snapshot.plan), indent=2))
            elif args.command == "plan":
                _print_plan(snapshot)
            else:
                _print_sync(snapshot)
            return 1 if snapshot.plan.blockers else 0

        if args.command in {"init", "apply"}:
            print("Dock Init")
            print(f"Configuration: {path}")
            _print_sync(snapshot, title="Planned changes", show_next_step=False)
            try:
                executor = Executor()
                results = executor.execute(snapshot.plan, os_name=snapshot.system.os)
            except ExecutionError as exc:
                print(f"\nInitialization stopped: {exc}", file=sys.stderr)
                return 1
            print("\nInitialization results")
            for result in results:
                print(f"  [OK] {result}")
            _print_runtime_summary(snapshot, executor)
            return 0

        if args.command in {"start", "stop", "logs"}:
            if not snapshot.docker.daemon_running:
                detail = snapshot.docker.detail or "Docker daemon is not responding."
                raise DockerError(f"Docker is not ready: {detail}")
            executor = Executor()
            if args.command == "start":
                print("Dock Start\n\nStarting containers...")
                results = executor.start_project_containers(snapshot.plan.name)
                for result in results:
                    print(f"  {result}")
                if not results or results[0] != "No Dock-managed containers found.":
                    print("\nAll Dock containers are running.")
            elif args.command == "stop":
                print("Dock Stop\n\nStopping containers...")
                results = executor.stop_project_containers(snapshot.plan.name)
                for result in results:
                    print(f"  {result}")
                if not results or results[0] != "No Dock-managed containers found.":
                    print("\nAll Dock containers are stopped.")
            else:
                print("Dock Logs")
                logs = executor.project_logs(snapshot.plan.name)
                if not logs:
                    print("No Dock-managed containers found for this project.")
                for container_name, output in logs:
                    print(f"\n[{container_name}]")
                    print(output or "(no recent logs)")
            return 0

    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
    except (DockerError, ExecutionError) as exc:
        print(f"Dock error: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
