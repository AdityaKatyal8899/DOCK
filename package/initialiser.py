"""Compatibility entry point for Dock's initialisation pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from docklib.config import ConfigError, load_config
from docklib.docker_engine import DockerEngine, DockerError, DockerStatus
from docklib.executor import ExecutionError, Executor
from docklib.planner import ProvisioningPlan, create_plan, print_plan
from docklib.system import SystemProfile, discover_system, print_report


def initialise(
    config_path: str = "dock.yaml",
) -> tuple[SystemProfile, DockerStatus, ProvisioningPlan]:
    """Run Dock's current initialisation pipeline without changing the machine."""
    profile = discover_system()
    config = load_config(config_path)
    docker_status = DockerEngine().check()
    plan = create_plan(config, profile)
    return profile, docker_status, plan


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Discover this machine, validate Dock config, and create or apply a plan."
    )
    parser.add_argument("config", nargs="?", default="dock.yaml", help="Path to Dock YAML config")
    parser.add_argument("--json", action="store_true", help="Print system profile and plan as JSON")
    parser.add_argument("--apply", action="store_true", help="Execute supported actions in the plan")
    args = parser.parse_args()

    try:
        profile, docker_status, plan = initialise(args.config)
    except ConfigError as exc:
        parser.error(str(exc))

    if args.json:
        print(json.dumps({
            "system": asdict(profile),
            "docker_engine": asdict(docker_status),
            "plan": asdict(plan),
        }, indent=2))
    else:
        print_report(profile)
        daemon = "running" if docker_status.daemon_running else "not responding"
        version = f" ({docker_status.version})" if docker_status.version else ""
        print(f"Docker daemon: {daemon}{version}")
        if docker_status.detail:
            print(f"Docker detail: {docker_status.detail}")
        print()
        print_plan(plan)
        if args.apply:
            print("\nApplying supported actions from the plan.")
        else:
            print("\nThis run only inspected the system and produced a plan; no changes were made.")
            print("OS package and Docker installation actions still need an OS executor.")

    if args.apply:
        try:
            results = Executor().execute(plan, os_name=profile.os)
        except (ExecutionError, DockerError) as exc:
            print(f"Execution stopped: {exc}", file=sys.stderr)
            return 1
        for result in results:
            print(result)
        return 0

    return 1 if plan.blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
