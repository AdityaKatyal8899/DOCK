"""Apply a provisioning plan through OS and Docker execution adapters."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from .docker_engine import DockerEngine, DockerError
from .os_executor import OSExecutionError, OSExecutor
from .planner import PlanStep, ProvisioningPlan


class ExecutionError(RuntimeError):
    """Raised when a plan cannot be applied safely or an action fails."""


class Executor:
    def __init__(
        self,
        docker: DockerEngine | None = None,
        os_executor: OSExecutor | None = None,
        app_root: str | Path = ".dock/apps",
        healthcheck_timeout: int = 45,
    ) -> None:
        self.docker = docker or DockerEngine()
        self.os_executor = os_executor or OSExecutor()
        self.app_root = Path(app_root)
        self.healthcheck_timeout = healthcheck_timeout

    def _preflight(self, plan: ProvisioningPlan, os_name: str) -> Path | None:
        if plan.blockers:
            raise ExecutionError("Plan has blockers: " + " ".join(plan.blockers))

        supported = {
            "ensure_packages", "install_docker", "start_docker_service",
            "fetch_application", "build_image", "pull_image", "run_application", "update_application",
            "update_image_application",
            "start_project_containers", "verify_application",
        }
        unsupported = [step.action for step in plan.steps if step.action not in supported]
        if unsupported:
            raise ExecutionError("Unsupported plan action(s): " + ", ".join(dict.fromkeys(unsupported)))

        os_steps = [step for step in plan.steps if step.action in {
            "ensure_packages", "install_docker", "start_docker_service"
        }]
        try:
            self.os_executor.preflight(os_steps)
        except OSExecutionError as exc:
            raise ExecutionError(str(exc)) from exc

        fetch = next((step for step in plan.steps if step.action in {"fetch_application", "update_application"}), None)
        target: Path | None = None
        if fetch:
            if not shutil.which("git"):
                raise ExecutionError("Git is required to fetch the application but was not found on PATH.")
            target = self.app_root / plan.name
            if target.exists():
                self._validate_existing_checkout(target, fetch)

        has_docker_prerequisite = any(step.action in {"install_docker", "start_docker_service"} for step in plan.steps)
        if plan.steps and not has_docker_prerequisite:
            docker_status = self.docker.check()
            docker_needed = any(step.action in {
                "fetch_application", "build_image", "pull_image", "run_application", "update_application",
                "update_image_application",
                "start_project_containers", "verify_application"
            } for step in plan.steps)
            if docker_needed and not docker_status.daemon_running:
                detail = docker_status.detail or "Docker daemon is not responding."
                raise ExecutionError(f"Docker is not ready: {detail}")
        return target

    def _validate_existing_checkout(self, target: Path, step: PlanStep) -> None:
        if not (target / ".git").is_dir():
            raise ExecutionError(f"'{target}' already exists and is not a Git checkout; refusing to overwrite it.")
        source = step.details.get("source", step.details)
        expected = f"https://github.com/{source['repository']}.git"
        try:
            result = subprocess.run(
                ["git", "-C", str(target), "remote", "get-url", "origin"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutionError(f"Could not inspect existing checkout '{target}': {exc}") from exc
        if result.returncode != 0 or result.stdout.strip().rstrip("/") != expected.rstrip("/"):
            raise ExecutionError(f"'{target}' is not a checkout of the configured repository; refusing to reuse it.")

    def execute(self, plan: ProvisioningPlan, os_name: str = "linux") -> list[str]:
        """Apply steps after validating all supported actions and prerequisites."""
        checkout = self._preflight(plan, os_name)
        results: list[str] = []

        # Install/start OS prerequisites before Docker-dependent work.
        for step in plan.steps:
            if step.action in {"ensure_packages", "install_docker", "start_docker_service"}:
                try:
                    results.append(self.os_executor.execute_step(step, os_name))
                except OSExecutionError as exc:
                    raise ExecutionError(str(exc)) from exc

        docker_required = any(step.action in {
            "fetch_application", "build_image", "pull_image", "run_application", "update_application",
            "update_image_application",
            "start_project_containers", "verify_application"
        } for step in plan.steps)
        if docker_required:
            status = self.docker.check()
            if not status.daemon_running:
                detail = status.detail or "Docker daemon is not responding."
                raise ExecutionError(f"Docker is not ready after prerequisite setup: {detail}")

        context: Path | None = checkout
        for step in plan.steps:
            if step.action in {"ensure_packages", "install_docker", "start_docker_service"}:
                continue
            if step.action == "fetch_application":
                context = self._fetch_application(step, checkout)
                results.append(f"Application source ready at {context}")
            elif step.action == "build_image":
                build_context = context or (
                    Path(step.details["context"]) if step.details.get("context") else None
                )
                if build_context is None:
                    raise ExecutionError("Build step has no application directory.")
                image = step.details["image"]
                if step.details.get("reuse_existing") and self.docker.image_exists(image):
                    results.append(f"Using locally available image {image}; skipped build")
                else:
                    self.docker.build(image, build_context, step.details["dockerfile"])
                    results.append(f"Built image {image}")
            elif step.action == "pull_image":
                image = step.details["image"]
                if self.docker.image_exists(image):
                    results.append(f"Using locally available image {image}")
                else:
                    self.docker.pull(image)
                    results.append(f"Pulled image {image}")
            elif step.action == "run_application":
                results.extend(self._create_or_start_project_container(plan, step))
            elif step.action == "update_application":
                results.extend(self._update_application(plan, step, checkout))
            elif step.action == "update_image_application":
                results.extend(self._update_image_application(plan, step))
            elif step.action == "start_project_containers":
                results.extend(self.start_project_containers(plan.name))
            elif step.action == "verify_application":
                self._verify_http(step.details, plan)
                results.append("Application health check passed")

        if not results:
            results.append("System already matches the requested configuration; no changes were needed.")
        return results

    def _fetch_application(self, step: PlanStep, target: Path | None) -> Path:
        if target is None:
            raise ExecutionError("Application checkout path was not prepared during preflight.")
        if target.exists():
            return target
        details: dict[str, Any] = step.details
        if details.get("type") != "github":
            raise ExecutionError(f"Unsupported source type: {details.get('type')!r}")
        repository = details["repository"]
        url = f"https://github.com/{repository}.git"
        target.parent.mkdir(parents=True, exist_ok=True)
        command = ["git", "clone", "--branch", details.get("branch", "main"), "--", url, str(target)]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=300
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutionError(f"Could not fetch application source: {exc}") from exc
        if result.returncode != 0:
            raise ExecutionError((result.stderr or result.stdout or "Git clone failed.").strip())
        return target

    def _update_application(self, plan: ProvisioningPlan, step: PlanStep, target: Path | None) -> list[str]:
        if target is None:
            raise ExecutionError("Application update has no checkout path.")
        source = step.details["source"]
        fetch_step = PlanStep("fetch_application", "Fetch application", {
            "type": source["type"],
            "repository": source["repository"],
            "branch": source.get("branch", "main"),
        })
        if not target.exists():
            self._fetch_application(fetch_step, target)
        else:
            self._pull_checkout(target, source.get("branch", "main"))

        image = step.details["image"]
        self.docker.build(image, target, step.details["dockerfile"])
        containers = self.docker.list_project_containers(plan.name)
        old = next((item for item in containers if item["name"] == step.details["container"]), None)
        if old:
            if old["status"].lower().startswith("up"):
                self.docker.stop(old["id"])
            self.docker.remove(old["id"])
        self.docker.create(
            name=step.details["container"],
            image=image,
            ports=step.details.get("ports", []),
            labels={
                "dock.project": plan.name,
                "dock.managed": "true",
                "dock.config-hash": step.details["config_hash"],
            },
        )
        self.docker.start(step.details["container"])
        return [f"Updated and started container {step.details['container']}"]

    def _update_image_application(self, plan: ProvisioningPlan, step: PlanStep) -> list[str]:
        """Replace a Dock-managed container with the newly configured registry image."""
        name = step.details["container"]
        existing = self.docker.list_project_containers(plan.name)
        old = next((item for item in existing if item["name"] == name), None)
        if old:
            if old["status"].lower().startswith("up"):
                self.docker.stop(old["id"])
            self.docker.remove(old["id"])
        self.docker.create(
            name=name,
            image=step.details["image"],
            ports=step.details.get("ports", []),
            labels={
                "dock.project": plan.name,
                "dock.managed": "true",
                "dock.config-hash": step.details["config_hash"],
            },
        )
        self.docker.start(name)
        return [f"Updated and started container {name} from {step.details['image']}"]

    @staticmethod
    def _pull_checkout(target: Path, branch: str) -> None:
        try:
            state = subprocess.run(
                ["git", "-C", str(target), "status", "--porcelain"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False,
            )
            if state.returncode != 0:
                raise ExecutionError(f"Could not inspect application checkout '{target}'.")
            if state.stdout.strip():
                raise ExecutionError(
                    f"Application checkout '{target}' has local changes; commit or stash them before running 'dock init'."
                )
            fetch = subprocess.run(
                ["git", "-C", str(target), "fetch", "origin", branch],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, check=False,
            )
            if fetch.returncode != 0:
                raise ExecutionError((fetch.stderr or "Could not fetch the configured branch.").strip())
            switch = subprocess.run(
                ["git", "-C", str(target), "switch", branch],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
            )
            if switch.returncode != 0:
                switch = subprocess.run(
                    ["git", "-C", str(target), "switch", "--track", "-c", branch, f"origin/{branch}"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
                )
            if switch.returncode != 0:
                raise ExecutionError((switch.stderr or "Could not switch to the configured branch.").strip())
            pull = subprocess.run(
                ["git", "-C", str(target), "pull", "--ff-only", "origin", branch],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutionError(f"Could not update application checkout: {exc}") from exc
        if pull.returncode != 0:
            raise ExecutionError((pull.stderr or "Could not fast-forward the application branch.").strip())

    def _create_or_start_project_container(self, plan: ProvisioningPlan, step: PlanStep) -> list[str]:
        existing = self.docker.list_project_containers(plan.name)
        name = step.details["container"]
        project_container = next((item for item in existing if item["name"] == name), None)
        if project_container:
            if not project_container["status"].lower().startswith("up"):
                self.docker.start(project_container["id"])
                return [f"Started existing container {name}"]
            return [f"Container {name} is already running"]
        self.docker.create(
            name=name,
            image=step.details.get("image", name),
            ports=step.details.get("ports", []),
            labels={
                "dock.project": plan.name,
                "dock.managed": "true",
                "dock.config-hash": step.details.get("config_hash", ""),
            },
        )
        self.docker.start(name)
        return [f"Created and started container {name}"]

    def start_project_containers(self, project: str) -> list[str]:
        containers = self.docker.list_project_containers(project)
        if not containers:
            return ["No Dock-managed containers found."]
        results: list[str] = []
        for container in containers:
            if container["status"].lower().startswith("up"):
                results.append(f"{container['name']} is already running")
            else:
                self.docker.start(container["id"])
                results.append(f"Started {container['name']}")
        return results

    def stop_project_containers(self, project: str) -> list[str]:
        containers = self.docker.list_project_containers(project)
        if not containers:
            return ["No Dock-managed containers found."]
        results: list[str] = []
        for container in containers:
            if not container["status"].lower().startswith("up"):
                results.append(f"{container['name']} is already stopped")
            else:
                self.docker.stop(container["id"])
                results.append(f"Stopped {container['name']}")
        return results

    def project_logs(self, project: str, tail: int = 100) -> list[tuple[str, str]]:
        containers = self.docker.list_project_containers(project)
        return [(container["name"], self.docker.logs(container["id"], tail)) for container in containers]

    def _verify_http(self, healthcheck: dict[str, Any], plan: ProvisioningPlan) -> None:
        container_port = healthcheck["port"]
        ports = next((
            step.details.get("ports", [])
            for step in plan.steps
            if step.action in {
                "run_application", "update_application", "update_image_application",
                "start_project_containers",
            }
        ), [])
        host_port = next(
            (mapping["host"] for mapping in ports if mapping["container"] == container_port),
            container_port,
        )
        url = f"http://127.0.0.1:{host_port}{healthcheck['path']}"
        deadline = time.monotonic() + self.healthcheck_timeout
        last_error = "No response received."
        while time.monotonic() < deadline:
            try:
                with urlopen(url, timeout=3) as response:
                    if 200 <= response.status < 300:
                        return
                    last_error = f"HTTP status {response.status}"
            except (OSError, URLError) as exc:
                last_error = str(exc)
            time.sleep(1)
        raise ExecutionError(f"Health check did not pass for {url}: {last_error}")
