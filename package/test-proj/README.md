# Dock test server

A minimal HTTP server for exercising Dock's local Docker image build, approval,
start, status, logs, and stop workflow.

From this directory, validate the config and ask Dock AI to inspect or operate
the project. The first provision/build will build `dock-test-proj:latest` from
the included `Dockerfile`; the server responds at `http://localhost:8000/`.

The app uses only Python's standard library and has no secrets or external
dependencies.
