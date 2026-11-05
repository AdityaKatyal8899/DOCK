"""Discover basic system information for Dock.

This module only inspects the current machine. It does not install software or
make system changes. It uses the Python standard library so it can run before
Dock has installed any project dependencies.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

@dataclass
class SystemProfile:
    os: str
    distribution: str | None
    version: str
    architecture: str
    package_manager: str | None
    docker_installed: bool
    docker_version: str | None
    cpu: dict[str, Any]
    memory: dict[str, int | None]
    disks: list[dict[str, Any]]
    gpu: dict[str, Any]

def _read_os_release() -> dict[str, str]:
    """Parse Linux's os-release file without requiring a third-party package."""
    values: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.startswith("#"):
                continue
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def _detect_os() -> tuple[str, str | None, str]:
    system = platform.system().lower()
    if system == "linux":
        release = _read_os_release()
        return "linux", release.get("ID", "unknown"), release.get("VERSION_ID", platform.release())
    if system == "windows":
        return "windows", None, platform.version()
    if system == "darwin":
        return "macos", None, platform.mac_ver()[0] or platform.release()
    return system or "unknown", None, platform.release() or "unknown"


def _detect_package_manager(os_name: str, distribution: str | None) -> str | None:
    candidates: list[str]
    if os_name == "linux":
        distro = (distribution or "").lower()
        if distro in {"ubuntu", "debian", "linuxmint", "pop"}:
            candidates = ["apt-get", "apt"]
        elif distro in {"fedora", "rhel", "centos", "rocky", "almalinux"}:
            candidates = ["dnf", "yum"]
        elif distro in {"arch", "manjaro", "endeavouros"}:
            candidates = ["pacman"]
        elif distro in {"opensuse", "opensuse-leap", "opensuse-tumbleweed"}:
            candidates = ["zypper"]
        else:
            candidates = ["apt-get", "dnf", "yum", "pacman", "zypper", "apk"]
    elif os_name == "macos":
        candidates = ["brew"]
    elif os_name == "windows":
        candidates = ["winget", "choco"]
    else:
        candidates = []
    return next((name for name in candidates if shutil.which(name)), None)


def _run(command: list[str], timeout: float = 3.0) -> str | None:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or result.stderr).strip() or None


def _memory_info() -> dict[str, int | None]:
    """Return memory totals in bytes where the OS exposes them simply."""
    total: int | None = None
    if os.name == "nt":
        # GlobalMemoryStatusEx is available via the standard Windows API.
        try:
            import ctypes
            from ctypes import wintypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                total = int(status.ullTotalPhys)
        except (AttributeError, OSError):
            pass
    elif platform.system() == "Linux":
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024
                    break
        except (OSError, ValueError, IndexError):
            pass
    elif platform.system() == "Darwin":
        raw = _run(["sysctl", "-n", "hw.memsize"])
        if raw and raw.isdigit():
            total = int(raw)
    return {"total_bytes": total}


def _disk_info() -> list[dict[str, Any]]:
    root = Path(os.environ.get("SystemDrive", "C:") + "\\") if os.name == "nt" else Path("/")
    try:
        usage = shutil.disk_usage(root)
        return [{
            "path": str(root),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        }]
    except OSError:
        return []


def _gpu_info() -> dict[str, Any]:
    # nvidia-smi gives useful details when an NVIDIA GPU and its driver are present.
    if shutil.which("nvidia-smi"):
        result = _run([
            "nvidia-smi", "--query-gpu=name", "--format=csv,noheader"
        ])
        if result:
            return {"detected": True, "vendor": "NVIDIA", "devices": result.splitlines()}

    # Linux exposes PCI display/3D controllers here, even when no vendor tool exists.
    if platform.system() == "Linux":
        devices: list[str] = []
        for path in Path("/sys/bus/pci/devices").glob("*/class"):
            try:
                device_class = path.read_text(encoding="ascii").strip().lower()
                if device_class.startswith(("0x0300", "0x0302")):
                    devices.append(path.parent.name)
            except OSError:
                continue
        if devices:
            return {"detected": True, "vendor": None, "devices": devices}
    return {"detected": False, "vendor": None, "devices": []}


def discover_system() -> SystemProfile:
    """Inspect this machine and return a structured profile."""
    os_name, distribution, version = _detect_os()
    docker_path = shutil.which("docker")
    docker_version = _run([docker_path, "--version"]) if docker_path else None
    logical_cpus = os.cpu_count()

    return SystemProfile(
        os=os_name,
        distribution=distribution,
        version=version,
        architecture=platform.machine() or "unknown",
        package_manager=_detect_package_manager(os_name, distribution),
        docker_installed=docker_path is not None,
        docker_version=docker_version,
        cpu={"logical_cores": logical_cpus, "processor": platform.processor() or None},
        memory=_memory_info(),
        disks=_disk_info(),
        gpu=_gpu_info(),
    )


def print_report(profile: SystemProfile) -> None:
    """Print a compact, human-readable system summary."""
    data = asdict(profile)
    print("Dock System Discovery")
    print(f"OS:            {data['os']}")
    print(f"Distribution:  {data['distribution'] or 'not applicable / unknown'}")
    print(f"Version:       {data['version']}")
    print(f"Architecture:  {data['architecture']}")
    print(f"Package Mgr:   {data['package_manager'] or 'not detected'}")
    print(f"Docker:        {data['docker_version'] or ('installed (version unavailable)' if data['docker_installed'] else 'not installed')}")
    print(f"CPU cores:     {data['cpu']['logical_cores'] or 'unknown'}")
    memory = data["memory"]["total_bytes"]
    print(f"RAM:           {memory / (1024 ** 3):.1f} GiB" if memory else "RAM:           unknown")
    if data["disks"]:
        for disk in data["disks"]:
            print(f"Disk ({disk['path']}): {disk['free_bytes'] / (1024 ** 3):.1f} GiB free / {disk['total_bytes'] / (1024 ** 3):.1f} GiB")
    else:
        print("Disk:          unknown")
    gpu = data["gpu"]
    print(f"GPU:           {', '.join(gpu['devices']) if gpu['detected'] else 'not detected'}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Discover the current machine for Dock.")
    parser.add_argument("--json", action="store_true", help="Print the complete profile as JSON.")
    args = parser.parse_args()
    system_profile = discover_system()
    if args.json:
        print(json.dumps(asdict(system_profile), indent=2))
    else:
        print_report(system_profile)
