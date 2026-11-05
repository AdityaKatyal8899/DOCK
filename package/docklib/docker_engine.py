"""Small Docker CLI adapter used by Dock's future executor.

This module separates Docker command details from configuration and planning.
Nothing runs until a caller explicitly invokes one of the engine methods.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class DockerError(RuntimeError):
    """Raised when Docker is unavailable or a Docker operation fails."""


@dataclass
class DockerStatus:
    installed: bool
    daemon_running: bool
    version: str | None = None
    detail: str | None = None


class DockerEngine:
    """Interact with Docker using argument-safe subprocess calls."""

    def __init__(self, executable: str = "docker", timeout: int = 300) -> None:
        self.executable = shutil.which(executable) or (executable if Path(executable).is_file() else None)
        self.timeout = timeout

    def _run(self, arguments: Sequence[str], timeout: int | None = None) -> str:
        if not self.executable:
            raise DockerError("Docker CLI was not found on PATH.")
        try:
            result = subprocess.run(
                [self.executable, *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout or self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise DockerError(f"Docker command timed out: docker {' '.join(arguments)}") from exc
        except OSError as exc:
            raise DockerError(f"Could not run Docker CLI: {exc}") from exc

        output = (result.stdout or "").strip()
        if result.returncode != 0:
            detail = (result.stderr or output or "Docker returned an error without details.").strip()
            raise DockerError(detail)
        return output

    def check(self) -> DockerStatus:
        """Report whether the CLI exists and the Docker daemon responds."""
        if not self.executable:
            return DockerStatus(installed=False, daemon_running=False, detail="Docker CLI was not found on PATH.")
        try:
            version = self._run(["--version"], timeout=10)
        except DockerError as exc:
            return DockerStatus(installed=True, daemon_running=False, detail=str(exc))
        try:
            self._run(["info", "--format", "{{.ServerVersion}}"], timeout=15)
        except DockerError as exc:
            return DockerStatus(installed=True, daemon_running=False, version=version, detail=str(exc))
        return DockerStatus(installed=True, daemon_running=True, version=version)

    def pull(self, image: str) -> str:
        """Pull an image from its configured registry."""
        return self._run(["pull", image])

    def image_exists(self, image: str) -> bool:
        """Return whether an image reference is available in the active Docker engine."""
        try:
            return bool(self._run(["image", "inspect", "--format", "{{.Id}}", image]))
        except DockerError as exc:
            detail = str(exc).lower()
            if "no such image" in detail or "no such object" in detail:
                return False
            raise

    def build(self, image: str, context: str | Path, dockerfile: str = "Dockerfile") -> str:
        """Build and tag an image from a local build context."""
        context_path = Path(context)
        dockerfile_path = Path(dockerfile)
        if not context_path.is_dir():
            raise DockerError(f"Build context directory does not exist: {context_path}")
        if not dockerfile_path.is_absolute():
            dockerfile_path = context_path / dockerfile_path
        if not dockerfile_path.is_file():
            raise DockerError(f"Dockerfile does not exist: {dockerfile_path}")
        return self._run([
            "build", "--file", str(dockerfile_path), "--tag", image, str(context_path)
        ])

    def create(
        self,
        name: str,
        image: str,
        ports: Sequence[dict[str, int]] = (),
        labels: dict[str, str] | None = None,
    ) -> str:
        """Create (but do not start) a container with the requested port mappings."""
        arguments = ["create", "--name", name]
        for key, value in (labels or {}).items():
            arguments.extend(["--label", f"{key}={value}"])
        for mapping in ports:
            host = mapping.get("host")
            container = mapping.get("container")
            if not isinstance(host, int) or not 1 <= host <= 65535:
                raise DockerError("Host port must be an integer from 1 to 65535.")
            if not isinstance(container, int) or not 1 <= container <= 65535:
                raise DockerError("Container port must be an integer from 1 to 65535.")
            arguments.extend(["--publish", f"{host}:{container}"])
        arguments.append(image)
        return self._run(arguments)

    def start(self, container: str) -> str:
        """Start an existing container by name or ID."""
        return self._run(["start", container])

    def stop(self, container: str, timeout: int = 10) -> str:
        """Stop a running container and wait up to timeout seconds."""
        if timeout < 0:
            raise ValueError("timeout must be zero or greater")
        return self._run(["stop", "--time", str(timeout), container])

    def remove(self, container: str, force: bool = False) -> str:
        """Remove a container. Force removal is opt-in."""
        arguments = ["rm"]
        if force:
            arguments.append("--force")
        arguments.append(container)
        return self._run(arguments)

    def status(self, container: str) -> str:
        """Return Docker's status text for a named container or ID."""
        return self._run(["inspect", "--format", "{{.State.Status}}", container])

    def list_project_containers(self, project: str) -> list[dict[str, str]]:
        """List all containers carrying Dock's project label."""
        output = self._run([
            "ps", "--all", "--filter", f"label=dock.project={project}", "--format", "{{json .}}"
        ])
        containers: list[dict[str, str]] = []
        for line in output.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DockerError(f"Could not parse Docker container listing: {exc}") from exc
            containers.append({
                "id": str(item.get("ID", "")),
                "name": str(item.get("Names", "")),
                "image": str(item.get("Image", "")),
                "status": str(item.get("Status", "unknown")),
                "command": str(item.get("Command", "")),
                "created": str(item.get("CreatedAt", "")),
                "ports": str(item.get("Ports", "")),
                "labels": _parse_labels(str(item.get("Labels", ""))),
            })
        return containers

    def logs(self, container: str, tail: int = 100) -> str:
        """Return the most recent logs from a container."""
        if tail < 1:
            raise ValueError("tail must be greater than zero")
        return self._run(["logs", "--tail", str(tail), container])


def _parse_labels(raw: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for item in raw.split(","):
        key, separator, value = item.partition("=")
        if separator:
            labels[key] = value
    return labels
