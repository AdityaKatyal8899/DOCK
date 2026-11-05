"""Read-only checks for configured OS package requirements."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class RequirementStatus:
    name: str
    installed: bool | None
    detected_by: str | None = None


def _query_package(manager: str | None, name: str) -> bool | None:
    if not manager:
        return None
    if manager in {"apt", "apt-get"}:
        command = ["dpkg-query", "-W", "-f=${db:Status-Status}", name]
    elif manager in {"dnf", "yum", "zypper"}:
        command = ["rpm", "-q", name]
    elif manager == "pacman":
        command = ["pacman", "-Q", name]
    elif manager == "apk":
        command = ["apk", "info", "-e", name]
    elif manager == "brew":
        command = ["brew", "list", "--versions", name]
    else:
        return None
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def inspect_requirements(names: list[str], package_manager: str | None) -> list[RequirementStatus]:
    """Check configured package names without changing the machine.

    A matching executable is considered installed first. Otherwise the native
    package database is queried where Dock has a known read-only query.
    """
    statuses: list[RequirementStatus] = []
    for name in dict.fromkeys(names):
        executable = shutil.which(name)
        if executable:
            statuses.append(RequirementStatus(name, True, f"executable: {executable}"))
            continue
        installed = _query_package(package_manager, name)
        detector = f"{package_manager} package database" if installed is not None else None
        statuses.append(RequirementStatus(name, installed, detector))
    return statuses
