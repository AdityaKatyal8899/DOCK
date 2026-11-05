"""Bounded, deterministic project inventory for Dock AI.

The collector reads selected text files only. It skips common secret files,
generated/vendor directories, files outside the workspace, and oversized
inputs. It never executes project code.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from .schemas import ProjectContext, ProjectFile, ProjectSignals


_IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", ".next",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".dock",
    "dist", "build", "target", "vendor", "coverage", ".idea", ".gradle",
}
_SECRET_NAMES = {
    ".env", ".env.local", ".env.development", ".env.production", ".env.test",
    ".envrc",
    "credentials", "credentials.json", "secrets.yml", "secrets.yaml",
    "id_rsa", "id_ed25519",
}
_TEXT_SUFFIXES = {
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue",
    ".svelte", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".c",
    ".h", ".cpp", ".hpp", ".sh", ".bash", ".toml", ".yaml", ".yml", ".json",
    ".xml", ".gradle", ".properties", ".txt", ".md", ".ini", ".cfg", ".conf",
    ".sql",
}
_MANIFEST_NAMES = {
    "pyproject.toml", "requirements.txt", "pipfile", "pipfile.lock", "poetry.lock",
    "uv.lock", "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "go.mod", "cargo.toml", "cargo.lock", "pom.xml", "build.gradle",
    "build.gradle.kts", "gemfile", "gemfile.lock", "composer.json",
}
_ENV_PATTERNS = (
    re.compile(r"\bos\s*\.\s*(?:getenv|environ\s*\.\s*get)\s*\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"),
    re.compile(r"\bos\s*\.\s*environ\s*\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]"),
    re.compile(r"\bprocess\s*\.\s*env\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"\bprocess\s*\.\s*env\s*\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]"),
    re.compile(r"\bENV\s*\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]"),
    re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}"),
)
_ASSIGNED_SECRET = re.compile(
    r'''(?i)(\b(?:api[_-]?key|secret|password|passwd|token|access[_-]?key|private[_-]?key|client[_-]?secret|credentials?|connection[_-]?string|database[_-]?url|authorization|bearer)\b\s*[:=]\s*)("[^"\r\n]*"|'[^'\r\n]*'|[^\s,;]+)'''
)
_KNOWN_TOKEN = re.compile(r"\b(?:bc_live|ghp|github_pat|sk-[A-Za-z0-9_-]{8,})[A-Za-z0-9_-]{8,}\b")

_PRIORITY_NAMES = {
    "dock.yaml": 0, "dock.yml": 0, "pyproject.toml": 1, "package.json": 1,
    "requirements.txt": 1, "go.mod": 1, "cargo.toml": 1, "pom.xml": 1,
    "compose.yaml": 2, "compose.yml": 2, "docker-compose.yaml": 2,
    "docker-compose.yml": 2, "dockerfile": 2, ".dockerignore": 2, "readme.md": 4,
    "main.py": 5, "app.py": 5, "manage.py": 5, "server.js": 5,
    "index.js": 5, "main.go": 5,
}


def _is_environment_file(path: Path) -> bool:
    name = path.name.lower()
    return name == ".env" or name.startswith(".env.") or name.endswith(".env") or name == ".envrc"


def _is_secret_file(path: Path) -> bool:
    name = path.name.lower()
    return (
        name in _SECRET_NAMES
        or _is_environment_file(path)
        or any(marker in name for marker in ("secret", "credential", "private_key", "id_rsa"))
        or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".keystore"}
        or any(part in {"secrets", "credentials"} for part in (p.lower() for p in path.parts))
    )


def _category(path: Path) -> str | None:
    name = path.name.lower()
    if name == ".dockerignore" or name == "dockerfile" or name.startswith("dockerfile.") or "compose" in name and name.endswith((".yml", ".yaml")):
        return "docker"
    if name in _MANIFEST_NAMES or name.endswith((".csproj", ".sln")):
        return "manifest"
    if name.startswith("readme") or path.suffix.lower() in {".md", ".rst"}:
        return "documentation"
    if path.suffix.lower() in _TEXT_SUFFIXES:
        return "source" if path.suffix.lower() in {
            ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue",
            ".svelte", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".c",
            ".h", ".cpp", ".hpp", ".sh", ".bash", ".sql",
        } else "config"
    return None


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _candidate_files(root: Path, max_files: int, max_file_bytes: int) -> list[Path]:
    candidates: list[Path] = []
    for path in root.rglob("*"):
        if any(part.lower() in _IGNORED_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if not path.is_file() or _is_secret_file(path) or _category(path) is None:
            continue
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
            if path.stat().st_size > max_file_bytes:
                continue
        except (OSError, ValueError):
            continue
        candidates.append(path)

    candidates.sort(key=lambda p: (
        _PRIORITY_NAMES.get(p.name.lower(), 3 if _category(p) in {"manifest", "docker"} else 6),
        len(p.parts),
        _relative(p, root).lower(),
    ))
    return candidates[:max_files]


def _environment_file_paths(root: Path, *, limit: int = 50) -> list[str]:
    """List environment-file paths without opening or reading their contents."""
    paths: list[str] = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file() or not _is_environment_file(path):
            continue
        relative_parts = path.relative_to(root).parts
        if any(part.lower() in _IGNORED_DIRS for part in relative_parts[:-1]):
            continue
        try:
            path.resolve(strict=True).relative_to(root)
        except (OSError, ValueError):
            continue
        paths.append(_relative(path, root))
    return sorted(paths, key=str.lower)[:limit]


def _read_text(path: Path, max_file_bytes: int) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > max_file_bytes or b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def sanitize_untrusted_text(text: str) -> str:
    """Redact common inline credentials before project/log text is sent out."""
    text = _ASSIGNED_SECRET.sub(r"\1<REDACTED>", text)
    return _KNOWN_TOKEN.sub("<REDACTED_TOKEN>", text)


def _environment_references(texts: Iterable[str]) -> list[str]:
    names: set[str] = set()
    for text in texts:
        for pattern in _ENV_PATTERNS:
            names.update(pattern.findall(text))
    return sorted(names)[:100]


def _detect_language(paths: list[Path]) -> list[str]:
    suffixes = {path.suffix.lower() for path in paths}
    names = {path.name.lower() for path in paths}
    result: list[str] = []
    checks = (
        ("Python", {".py", ".pyi"}, {"pyproject.toml", "requirements.txt", "pipfile", "uv.lock", "poetry.lock"}),
        ("JavaScript", {".js", ".mjs", ".cjs", ".jsx"}, {"package.json"}),
        ("TypeScript", {".ts", ".tsx"}, {"tsconfig.json"}),
        ("Go", {".go"}, {"go.mod"}),
        ("Rust", {".rs"}, {"cargo.toml"}),
        ("Java", {".java", ".kt"}, {"pom.xml", "build.gradle", "build.gradle.kts"}),
        ("Ruby", {".rb"}, {"gemfile", "gemfile.lock"}),
        ("PHP", {".php"}, {"composer.json"}),
        ("C#", {".cs"}, set()),
        ("C/C++", {".c", ".h", ".cpp", ".hpp"}, set()),
    )
    for language, language_suffixes, manifest_names in checks:
        if suffixes & language_suffixes or names & manifest_names:
            result.append(language)
    return result


def _detect_package_manager(names: set[str]) -> str | None:
    if "pnpm-lock.yaml" in names:
        return "pnpm"
    if "yarn.lock" in names:
        return "yarn"
    if "package-lock.json" in names or "package.json" in names:
        return "npm"
    if "uv.lock" in names:
        return "uv"
    if "poetry.lock" in names:
        return "Poetry"
    if "pipfile" in names or "pipfile.lock" in names:
        return "Pipenv"
    if "requirements.txt" in names or "pyproject.toml" in names:
        return "pip"
    for manifest, manager in (("go.mod", "Go modules"), ("cargo.toml", "Cargo"), ("pom.xml", "Maven"),
                             ("build.gradle", "Gradle"), ("build.gradle.kts", "Gradle"),
                             ("gemfile", "Bundler"), ("composer.json", "Composer")):
        if manifest in names:
            return manager
    return None


def _detect_frameworks(texts: Iterable[str]) -> list[str]:
    combined = "\n".join(text.lower() for text in texts)
    patterns = (
        ("FastAPI", r"\bfastapi\b"), ("Django", r"\bdjango\b"),
        ("Flask", r"\bflask\b"), ("Streamlit", r"\bstreamlit\b"),
        ("React", r"\breact\b"), ("Next.js", r"\bnext\b"),
        ("Vue", r"\bvue\b"), ("Nuxt", r"\bnuxt\b"),
        ("Angular", r"\b@angular/core\b"), ("Express", r"\bexpress\b"),
        ("NestJS", r"\b@nestjs/"), ("Spring Boot", r"spring-boot|spring\.boot"),
        ("Rails", r"\brails\b"), ("Laravel", r"\blaravel/framework\b"),
    )
    return [name for name, pattern in patterns if re.search(pattern, combined)]


def collect_project_context(
    project_root: str | Path = ".",
    *,
    max_files: int = 40,
    max_file_bytes: int = 256_000,
    max_total_chars: int = 60_000,
) -> ProjectContext:
    """Inspect a project without executing code or reading known secret files.

    The returned ``project_root`` is deliberately ``."``; machine-specific
    absolute paths remain local and are not part of serialized model context.
    """
    if max_files < 1 or max_files > 100 or max_file_bytes < 1 or max_total_chars < 1:
        raise ValueError("context limits must be positive; max_files cannot exceed 100")
    root = Path(project_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Project root is not a directory: {root}")

    candidates = _candidate_files(root, max_files, max_file_bytes)
    raw_files: list[tuple[Path, str, str]] = []
    warnings: list[str] = []
    for path in candidates:
        text = _read_text(path, max_file_bytes)
        if text is None:
            continue
        raw_files.append((path, _category(path) or "config", text))

    env_names = _environment_references(text for _, _, text in raw_files)
    sanitized: list[ProjectFile] = []
    total = 0
    for path, category, text in raw_files:
        excerpt = sanitize_untrusted_text(text)
        remaining = min(max_total_chars - total, 24_000)
        if remaining <= 0:
            warnings.append("Project text was truncated to the configured context size limit.")
            break
        if len(excerpt) > remaining:
            excerpt = excerpt[:remaining]
            warnings.append("Project text was truncated to the configured context size limit.")
        sanitized.append(ProjectFile(path=_relative(path, root), category=category, content=excerpt))
        total += len(excerpt)

    names = {path.name.lower() for path, _, _ in raw_files}
    relative_paths = {_relative(path, root): path for path, _, _ in raw_files}
    dependency_names = _MANIFEST_NAMES | {"tsconfig.json"}
    dependency_files = sorted(rel for rel in relative_paths if Path(rel).name.lower() in dependency_names or Path(rel).suffix.lower() == ".csproj")
    dockerfiles = sorted(rel for rel in relative_paths if Path(rel).name.lower() == "dockerfile" or Path(rel).name.lower().startswith("dockerfile."))
    compose_files = sorted(rel for rel in relative_paths if "compose" in Path(rel).name.lower() and Path(rel).suffix.lower() in {".yml", ".yaml"})

    entry_candidates = {
        "app.py", "main.py", "manage.py", "server.py", "server.js", "index.js",
        "index.ts", "main.go", "main.rs", "app.js", "app.ts",
    }
    entry_points = sorted(rel for rel in relative_paths if Path(rel).name.lower() in entry_candidates)
    package_json = next((text for path, _, text in raw_files if path.name.lower() == "package.json"), None)
    if package_json:
        try:
            scripts = json.loads(package_json).get("scripts", {})
            if isinstance(scripts, dict) and any(name in scripts for name in ("start", "dev")):
                entry_points.append("package.json:scripts")
        except (json.JSONDecodeError, AttributeError):
            warnings.append("Could not parse package.json scripts for entry-point hints.")

    signals = ProjectSignals(
        languages=_detect_language([path for path, _, _ in raw_files]),
        frameworks=_detect_frameworks(text for _, _, text in raw_files),
        package_manager=_detect_package_manager(names),
        dependency_files=dependency_files[:30],
        dockerfiles=dockerfiles[:20],
        compose_files=compose_files[:20],
        entry_points=sorted(set(entry_points))[:30],
        environment_variables=env_names,
        environment_files=_environment_file_paths(root),
    )
    return ProjectContext(project_root=".", signals=signals, files=sanitized, warnings=warnings[:50])

