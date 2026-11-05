import builtins
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import dock


class CLIRegressionTests(unittest.TestCase):
    def test_existing_commands_remain_registered(self):
        parser = dock._build_parser()
        commands = parser._subparsers._group_actions[0].choices
        self.assertTrue({
            "discover", "validate", "status", "sync", "plan", "init", "apply",
            "start", "stop", "logs", "ai",
        }.issubset(commands))

    def test_existing_command_arguments_still_parse(self):
        parser = dock._build_parser()
        self.assertEqual(parser.parse_args(["validate", "configs/dock.yaml"]).config_path, "configs/dock.yaml")
        status = parser.parse_args(["status", "--running"])
        self.assertTrue(status.running)
        self.assertEqual(parser.parse_args(["plan", "--json"]).command, "plan")
        self.assertEqual(parser.parse_args(["init", "-c", "dock.yaml"]).config_option, "dock.yaml")
        self.assertEqual(parser.parse_args(["apply"]).command, "apply")
        ai = parser.parse_args(["ai", "Analyze this project", "--project", ".", "--container", "demo"])
        self.assertEqual(ai.request, "Analyze this project")
        self.assertEqual(ai.container, "demo")

    def test_version_and_help_do_not_require_ai_imports(self):
        real_import = builtins.__import__

        def import_without_ai(name, *args, **kwargs):
            if name.split(".", 1)[0] in {"openai", "pydantic"}:
                raise AssertionError(f"core CLI unexpectedly imported optional dependency {name}")
            return real_import(name, *args, **kwargs)

        output = io.StringIO()
        with patch("builtins.__import__", side_effect=import_without_ai), redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_info:
                dock.main(["--version"])
        self.assertEqual(exit_info.exception.code, 0)
        self.assertIn("Dock 0.2.1", output.getvalue())

    def test_system_discovery_command_does_not_need_ai_imports(self):
        real_import = builtins.__import__

        def import_without_ai(name, *args, **kwargs):
            if name.split(".", 1)[0] in {"openai", "pydantic"}:
                raise AssertionError(f"core command unexpectedly imported optional dependency {name}")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_without_ai), redirect_stdout(io.StringIO()):
            self.assertEqual(dock.main(["discover", "--json"]), 0)


if __name__ == "__main__":
    unittest.main()
