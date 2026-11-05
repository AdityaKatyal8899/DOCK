import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from docklib.docker_engine import DockerError, DockerStatus
from docklib.planner import create_plan
from docklib.system import SystemProfile
from docklib.executor import Executor
from dock import _print_sync


def profile():
    return SystemProfile(
        os="linux", distribution="ubuntu", version="24.04", architecture="x86_64",
        package_manager="apt", docker_installed=True, docker_version="Docker 1",
        cpu={}, memory={}, disks=[], gpu={},
    )


def config(image):
    return {
        "name": "production-test",
        "docker": {"enabled": True},
        "application": {"image": image},
    }


def make_plan(image, available):
    return create_plan(
        config(image),
        profile(),
        package_statuses=[],
        docker_status=DockerStatus(installed=True, daemon_running=True),
        containers=[],
        image_available=available,
    )


class FakeDocker:
    def __init__(self, available, containers=None):
        self.available = available
        self.containers = containers or []
        self.pulled = []
        self.built = []
        self.created = []
        self.started = []

    def check(self):
        return DockerStatus(installed=True, daemon_running=True)

    def image_exists(self, image):
        return self.available

    def pull(self, image):
        self.pulled.append(image)
        self.available = True

    def build(self, image, context, dockerfile):
        self.built.append((image, Path(context), Path(dockerfile)))
        self.available = True

    def list_project_containers(self, project):
        return self.containers

    def create(self, **kwargs):
        self.created.append(kwargs)
        return "container-id"

    def start(self, container):
        self.started.append(container)
        return container


class LocalImageProvisioningTests(unittest.TestCase):
    def test_sync_output_distinguishes_local_and_missing_images(self):
        for available, expected in (
            (True, "locally available Docker image"),
            (False, "Pull the configured Docker image 'production-test:latest'."),
        ):
            with self.subTest(available=available):
                plan = make_plan("production-test:latest", available=available)
                snapshot = SimpleNamespace(
                    plan=plan,
                    config_path="dock.yaml",
                    requirements=[],
                    config=config("production-test:latest"),
                    docker=DockerStatus(installed=True, daemon_running=True),
                    containers=[],
                )
                output = StringIO()
                with redirect_stdout(output):
                    _print_sync(snapshot, show_next_step=False)
                self.assertIn(expected, output.getvalue())

    def test_local_image_is_not_pulled_and_container_is_created(self):
        engine = FakeDocker(available=True)
        plan = make_plan("production-test:latest", available=True)

        results = Executor(docker=engine).execute(plan)

        self.assertNotIn("pull_image", [step.action for step in plan.steps])
        self.assertEqual(engine.pulled, [])
        self.assertEqual(engine.created[0]["image"], "production-test:latest")
        self.assertEqual(engine.started, ["production-test"])
        self.assertTrue(any("locally available" in step.description for step in plan.steps))

    def test_missing_local_image_plans_and_attempts_pull(self):
        engine = FakeDocker(available=False)
        plan = make_plan("production-test:latest", available=False)

        Executor(docker=engine).execute(plan)

        self.assertIn("pull_image", [step.action for step in plan.steps])
        self.assertEqual(engine.pulled, ["production-test:latest"])
        self.assertEqual(engine.created[0]["image"], "production-test:latest")

    def test_registry_image_workflow_still_pulls_when_absent(self):
        engine = FakeDocker(available=False)
        plan = make_plan("hello-world:latest", available=False)

        Executor(docker=engine).execute(plan)

        self.assertEqual(engine.pulled, ["hello-world:latest"])
        self.assertEqual(engine.created[0]["image"], "hello-world:latest")

    def test_missing_image_and_local_dockerfile_build_then_create_with_ports(self):
        from docklib.project import inspect_project

        with TemporaryDirectory() as directory:
            project_dir = Path(directory)
            (project_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            config_path = project_dir / "dock.yaml"
            config_path.write_text(
                "name: production-test\n"
                "docker:\n  enabled: true\n"
                "application:\n  image: production-test:latest\n"
                "  ports:\n    - host: 8000\n      container: 8000\n",
                encoding="utf-8",
            )
            engine = FakeDocker(available=False)
            with patch("docklib.project.discover_system", return_value=profile()), \
                 patch("docklib.project.inspect_requirements", return_value=[]):
                snapshot = inspect_project(config_path, docker=engine)

            actions = [step.action for step in snapshot.plan.steps]
            self.assertEqual(actions, ["build_image", "run_application"])
            self.assertIn("Docker image", snapshot.plan.steps[0].description)
            self.assertEqual(engine.built, [])  # sync inspection is read-only
            self.assertEqual(engine.pulled, [])

            Executor(docker=engine).execute(snapshot.plan)

            self.assertEqual(len(engine.built), 1)
            self.assertEqual(engine.built[0][0], "production-test:latest")
            self.assertEqual(engine.built[0][1], project_dir)
            self.assertEqual(engine.created[0]["image"], "production-test:latest")
            self.assertEqual(engine.created[0]["ports"], [{"host": 8000, "container": 8000}])
            self.assertEqual(engine.started, ["production-test"])

    def test_explicit_missing_dockerfile_is_actionable_blocker(self):
        from docklib.project import inspect_project

        with TemporaryDirectory() as directory:
            project_dir = Path(directory)
            config_path = project_dir / "dock.yaml"
            config_path.write_text(
                "name: production-test\n"
                "docker:\n  enabled: true\n"
                "application:\n  image: production-test:latest\n"
                "  docker:\n    dockerfile: Dockerfile.missing\n",
                encoding="utf-8",
            )
            engine = FakeDocker(available=False)
            with patch("docklib.project.discover_system", return_value=profile()), \
                 patch("docklib.project.inspect_requirements", return_value=[]):
                snapshot = inspect_project(config_path, docker=engine)

            self.assertTrue(any("Dockerfile" in blocker and "Create it" in blocker for blocker in snapshot.plan.blockers))
            self.assertNotIn("build_image", [step.action for step in snapshot.plan.steps])
            self.assertNotIn("pull_image", [step.action for step in snapshot.plan.steps])

    def test_matching_running_container_is_unchanged(self):
        from docklib.planner import _application_fingerprint

        desired = config("production-test:latest")
        container = {
            "name": "production-test", "status": "Up 5 minutes",
            "labels": {"dock.config-hash": _application_fingerprint(desired["application"])},
        }
        plan = create_plan(
            desired, profile(), package_statuses=[],
            docker_status=DockerStatus(installed=True, daemon_running=True),
            containers=[container], image_available=True,
        )
        self.assertEqual(plan.steps, [])

    def test_matching_stopped_container_only_plans_start(self):
        from docklib.planner import _application_fingerprint

        desired = config("production-test:latest")
        container = {
            "name": "production-test", "status": "Exited (0)",
            "labels": {"dock.config-hash": _application_fingerprint(desired["application"])},
        }
        plan = create_plan(
            desired, profile(), package_statuses=[],
            docker_status=DockerStatus(installed=True, daemon_running=True),
            containers=[container], image_available=False,
        )
        self.assertEqual([step.action for step in plan.steps], ["start_project_containers"])

    def test_local_build_skips_build_if_image_appeared_after_sync(self):
        engine = FakeDocker(available=True)
        plan = create_plan(
            config("production-test:latest"), profile(), package_statuses=[],
            docker_status=DockerStatus(installed=True, daemon_running=True), containers=[],
            image_available=False, local_build_context=".", local_dockerfile="Dockerfile",
        )
        Executor(docker=engine).execute(plan)
        self.assertEqual(engine.built, [])
        self.assertEqual(engine.pulled, [])

    def test_docker_engine_treats_missing_image_as_absent(self):
        from unittest.mock import patch
        from docklib.docker_engine import DockerEngine

        engine = DockerEngine()
        with patch.object(engine, "_run", side_effect=DockerError("Error: No such image: example:latest")):
            self.assertFalse(engine.image_exists("example:latest"))
        with patch.object(engine, "_run", side_effect=DockerError("Cannot connect to the Docker daemon")):
            with self.assertRaises(DockerError):
                engine.image_exists("example:latest")


if __name__ == "__main__":
    unittest.main()
