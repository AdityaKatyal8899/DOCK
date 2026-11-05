"""Load and validate Dock's initial YAML configuration."""

from __future__ import annotations
from pathlib import Path
from typing import Any
import yaml

class ConfigError(ValueError):
    """Raised when a Dock configuration cannot be loaded or validated."""


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a mapping")
    return value


def _check_string_list(value: Any, where: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ConfigError(f"{where} must be a list of non-empty strings")


def validate_config(data: Any) -> dict[str, Any]:
    """Validate the first Dock config shape and return it as a plain dict."""
    config = _mapping(data, "Configuration")

    name = config.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("'name' is required and must be a non-empty string")

    if "system" in config:
        system = _mapping(config["system"], "'system'")
        if "packages" in system:
            _check_string_list(system["packages"], "'system.packages'")

    if "docker" in config:
        docker = _mapping(config["docker"], "'docker'")
        if "enabled" in docker and not isinstance(docker["enabled"], bool):
            raise ConfigError("'docker.enabled' must be true or false")

    application = config.get("application")
    if application is not None:
        application = _mapping(application, "'application'")
        image = application.get("image")
        if image is not None and (
            not isinstance(image, str) or not image.strip() or any(char.isspace() for char in image)
        ):
            raise ConfigError("'application.image' must be a non-empty Docker image reference without whitespace")

        source = application.get("source")
        if image is not None and source is not None:
            raise ConfigError("Set either 'application.image' or 'application.source', not both")
        if image is None and source is None:
            raise ConfigError("Application must define either 'application.image' or 'application.source'")

        if source is not None:
            source = _mapping(source, "'application.source'")
            if source.get("type") not in {"github"}:
                raise ConfigError("'application.source.type' must currently be 'github'")
            repository = source.get("repository")
            if not isinstance(repository, str) or not repository.strip() or "/" not in repository:
                raise ConfigError("'application.source.repository' must look like 'owner/repository'")
            if "branch" in source and (not isinstance(source["branch"], str) or not source["branch"].strip()):
                raise ConfigError("'application.source.branch' must be a non-empty string")

        app_docker = application.get("docker")
        if app_docker is not None:
            app_docker = _mapping(app_docker, "'application.docker'")
            if "dockerfile" in app_docker and (
                not isinstance(app_docker["dockerfile"], str) or not app_docker["dockerfile"].strip()
            ):
                raise ConfigError("'application.docker.dockerfile' must be a non-empty string")
        if source is not None and (not app_docker or not app_docker.get("dockerfile")):
            raise ConfigError("'application.source' requires 'application.docker.dockerfile'")

        if "ports" in application:
            ports = application["ports"]
            if not isinstance(ports, list):
                raise ConfigError("'application.ports' must be a list")
            for index, port in enumerate(ports):
                port = _mapping(port, f"'application.ports[{index}]'")
                for key in ("host", "container"):
                    value = port.get(key)
                    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
                        raise ConfigError(f"'application.ports[{index}].{key}' must be an integer from 1 to 65535")

        healthcheck = application.get("healthcheck")
        if healthcheck is not None:
            healthcheck = _mapping(healthcheck, "'application.healthcheck'")
            if healthcheck.get("type") != "http":
                raise ConfigError("'application.healthcheck.type' must currently be 'http'")
            if not isinstance(healthcheck.get("path"), str) or not healthcheck["path"].startswith("/"):
                raise ConfigError("'application.healthcheck.path' must be an absolute URL path, such as '/health'")
            port = healthcheck.get("port")
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                raise ConfigError("'application.healthcheck.port' must be an integer from 1 to 65535")

    return config


def load_config(path: str | Path) -> dict[str, Any]:
    """Read a YAML file and validate it, with actionable errors for users."""
    config_path = Path(path)
    try:
        with config_path.open("r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except OSError as exc:
        raise ConfigError(f"Could not read '{config_path}': {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in '{config_path}': {exc}") from exc
    return validate_config(data)


if __name__ == "__main__":
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="Validate a Dock YAML configuration.")
    parser.add_argument("config", nargs="?", default="dock.yaml", help="Path to the YAML config (default: dock.yaml)")
    args = parser.parse_args()
    try:
        loaded = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("Configuration is valid.")
    print(json.dumps(loaded, indent=2))
