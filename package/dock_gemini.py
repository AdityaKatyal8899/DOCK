"""Interactive Gemini launcher for Dock AI.

The API key is read from GEMINI_API_KEY or requested with hidden input. A
prompted key exists only in this process and is never saved to disk.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from dock import main as dock_main


DEFAULT_MODEL = "gemini-3.1-flash-lite"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Dock AI with Gemini, prompting for the API key if needed."
    )
    parser.add_argument(
        "query",
        nargs="*",
        help="Question for Dock AI. Omit to enter interactive query mode.",
    )
    parser.add_argument(
        "--project",
        help="Project folder to analyze (interactive mode asks if omitted).",
    )
    parser.add_argument(
        "--model",
        help=f"Gemini model (default: {DEFAULT_MODEL}).",
    )
    return parser


def _get_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    try:
        key = getpass.getpass("Gemini API key (input hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nNo API key entered.", file=sys.stderr)
        return ""
    return key


def _call_dock(query: str, project: Path, model: str, api_key: str) -> int:
    previous = {
        name: os.environ.get(name)
        for name in ("DOCK_AI_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL")
    }
    os.environ["DOCK_AI_PROVIDER"] = "gemini"
    os.environ["GEMINI_API_KEY"] = api_key
    os.environ["GEMINI_MODEL"] = model
    try:
        return dock_main(["ai", query, "--project", str(project)])
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    api_key = _get_api_key()
    if not api_key:
        print("GEMINI_API_KEY is required.", file=sys.stderr)
        return 1

    model = args.model or os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_MODEL
    if args.project:
        project = Path(args.project).expanduser().resolve()
    elif args.query:
        project = Path.cwd().resolve()
    else:
        default_project = str(Path.cwd())
        try:
            entered = input(f"Project folder [{default_project}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nNo project folder selected.")
            return 0
        project = Path(entered or default_project).expanduser().resolve()

    if args.query:
        return _call_dock(" ".join(args.query), project, model, api_key)

    print(f"Dock AI connected to Gemini ({model}).")
    print("Enter a query, or type :quit to exit. The key is held only for this session.\n")
    while True:
        try:
            query = input("dock-ai> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nDock AI session ended.")
            return 0
        if query.lower() in {":q", ":quit", ":exit"}:
            return 0
        if not query:
            continue
        _call_dock(query, project, model, api_key)
        print()


if __name__ == "__main__":
    raise SystemExit(main())
