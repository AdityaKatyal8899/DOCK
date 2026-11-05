"""Operating-system package and Docker service operations for ``dock init``."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from .docker_engine import DockerEngine
from .planner import PlanStep


class OSExecutionError(RuntimeError):
    """Raised when a requested OS-level operation is unsupported or fails."""


class OSExecutor:
    """Install missing packages and make Docker available on supported hosts.

    Package installation supports common package managers. Automatic Docker
    installation is currently limited to Debian/Ubuntu's ``docker.io`` package;
    other systems get an actionable message instead of a guessed install recipe.
    """

    _PACKAGE_MANAGERS = {"apt-get", "apt", "dnf", "yum", "pacman", "zypper", "apk", "brew"}

    def preflight(self, steps: list[PlanStep]) -> None:
        for step in steps:
            if step.action == "ensure_packages":
                manager = step.details.get("package_manager")
                if manager not in self._PACKAGE_MANAGERS:
                    raise OSExecutionError(f"Package installation is not supported for package manager {manager!r}.")
                if not shutil.which(manager):
                    raise OSExecutionError(f"Package manager '{manager}' is not available on PATH.")
            elif step.action == "install_docker":
                details = step.details
                if details.get("os") != "linux" or details.get("package_manager") not in {"apt", "apt-get"}:
                    raise OSExecutionError(
                        "Automatic Docker installation currently supports Debian/Ubuntu with apt only. "
                        "Install Docker for this system, then run 'dock sync' again."
                    )
                if not shutil.which("systemctl"):
                    raise OSExecutionError("Automatic Docker installation requires systemctl on Debian/Ubuntu.")
            elif step.action == "start_docker_service":
                os_name = step.details.get("os")
                if os_name == "linux" and not shutil.which("systemctl"):
                    raise OSExecutionError("Docker is installed but systemctl is unavailable to start its service.")
                if os_name not in {"linux", "windows"}:
                    raise OSExecutionError(f"Automatic Docker service startup is not supported on {os_name}.")

    @staticmethod
    def _privileged(command: list[str]) -> list[str]:
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
            return command
        if os.name != "nt" and shutil.which("sudo"):
            return ["sudo", *command]
        return command

    @staticmethod
    def _run(command: list[str], *, privileged: bool = False, timeout: int = 1800) -> None:
        actual = OSExecutor._privileged(command) if privileged else command
        try:
            result = subprocess.run(actual, check=False, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OSExecutionError(f"Could not run {' '.join(actual)}: {exc}") from exc
        if result.returncode != 0:
            raise OSExecutionError(f"Command failed with exit code {result.returncode}: {' '.join(actual)}")

    def install_packages(self, manager: str, packages: list[str]) -> None:
        if not packages:
            return
        if manager in {"apt", "apt-get"}:
            self._run(["apt-get", "update"], privileged=True)
            command = ["apt-get", "install", "-y", *packages]
        elif manager in {"dnf", "yum"}:
            command = [manager, "install", "-y", *packages]
        elif manager == "pacman":
            command = ["pacman", "-S", "--needed", "--noconfirm", *packages]
        elif manager == "zypper":
            command = ["zypper", "--non-interactive", "install", *packages]
        elif manager == "apk":
            command = ["apk", "add", *packages]
        elif manager == "brew":
            command = ["brew", "install", *packages]
        else:
            raise OSExecutionError(f"Package installation is not supported for package manager {manager!r}.")
        self._run(command, privileged=manager != "brew")

    def install_docker(self, package_manager: str) -> None:
        if package_manager not in {"apt", "apt-get"}:
            raise OSExecutionError("Automatic Docker installation currently supports Debian/Ubuntu with apt only.")
        self._run(["apt-get", "update"], privileged=True)
        self._run(["apt-get", "install", "-y", "docker.io"], privileged=True)
        self._run(["systemctl", "enable", "--now", "docker"], privileged=True)

    def start_docker(self, os_name: str, docker: DockerEngine) -> None:
        if os_name == "linux":
            self._run(["systemctl", "start", "docker"], privileged=True)
        elif os_name == "windows":
            program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            executable = program_files / "Docker" / "Docker" / "Docker Desktop.exe"
            if not executable.is_file():
                raise OSExecutionError("Docker Desktop was not found. Start Docker Desktop manually, then retry.")
            try:
                subprocess.Popen([str(executable)], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            except OSError as exc:
                raise OSExecutionError(f"Could not start Docker Desktop: {exc}") from exc
        else:
            raise OSExecutionError(f"Automatic Docker service startup is not supported on {os_name}.")

        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status = docker.check()
            if status.daemon_running:
                return
            time.sleep(2)
        raise OSExecutionError("Docker service did not become ready within 90 seconds.")

    def execute_step(self, step: PlanStep, os_name: str) -> str:
        if step.action == "ensure_packages":
            packages = step.details["packages"]
            self.install_packages(step.details["package_manager"], packages)
            return "Installed packages: " + ", ".join(packages)
        if step.action == "install_docker":
            self.install_docker(step.details["package_manager"])
            return "Installed and started Docker"
        if step.action == "start_docker_service":
            self.start_docker(os_name, DockerEngine())
            return "Started Docker service"
        raise OSExecutionError(f"Unsupported OS action: {step.action}")
