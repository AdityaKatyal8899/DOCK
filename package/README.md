# Dock

![Dock icon](https://dockcloud.online/icon.svg)

**Website:** [dockcloud.online](https://dockcloud.online)

**Dock** is a local project provisioning CLI. It reads a project's `dock.yaml`, inspects the local machine, and coordinates the steps needed to prepare and run that project with Docker.

Its name expands to **Deployment Operations & Configuration Kit**. The idea behind Dock is simple: describe the setup for a project once, then let a repeatable command carry out the supported setup steps on a machine.

> **Configure once. Initialize anywhere.**

## What Dock does today

Dock V0.2.1 provides a command-line workflow for one application per configuration:

1. Read and validate `dock.yaml`.
2. Inspect the host, required tools, Docker installation, Docker service, and Dock-managed project containers.
3. Show a plan of the changes that would be needed.
4. Apply supported changes: install configured system packages, make Docker available where supported, reuse or build a local image, pull a missing registry image, or fetch a GitHub project and build its Dockerfile; then create or update the application's container and optionally check an HTTP endpoint.

Docker is the engine doing the image and container work. Dock currently coordinates that work around a project configuration. This first release is an early, practical foundation; it does not claim to replace Docker or to hide every Docker concept.

## What the icon represents

The Dock mark combines a container-like cube with a terminal prompt (`>_`). The cube connects the idea of a packaged application to deployment; the prompt points to Dock's command-line workflow. Cobalt blue carries the infrastructure and reliability feel, while the warm cream terminal face keeps the mark legible and approachable. The small orange rays add a sense of action as a project is brought online.

The visual shorthand is: **a configured container, ready to be put to work**. It is a brand illustration, not a claim that Dock implements its own container engine.

## Requirements

- Python 3.10 or newer
- Docker CLI and a running Docker Engine for application workflows
- Git for the GitHub source workflow

Dock can install configured OS packages through supported package managers. Automatic Docker installation is currently limited to Debian/Ubuntu systems using `apt`; on other systems, install Docker yourself and start its engine before running Dock.

## Install from this repository

From the repository root:

```powershell
python -m pip install .\package
```

Or enter the distribution directory and install from there:

```powershell
cd package
python -m pip install .
```

The PyPI distribution is named `dock-api`; it installs the `dock` command.

## Quick start: build or pull an image

Create a project directory and put a `dock.yaml` file in it:

```yaml
name: hello-nginx

docker:
  enabled: true

application:
  image: nginx:alpine
  ports:
    - host: 8080
      container: 80
  healthcheck:
    type: http
    path: /
    port: 80
```

Run Dock from that project directory:

```powershell
dock validate
dock sync
dock init
```

`validate` checks the YAML shape. `sync` inspects the current state and previews planned changes without applying them. `init` applies the supported plan. For `application.image`, Dock reuses the image if it already exists locally. If it is missing and a `Dockerfile` is beside `dock.yaml`, Dock builds the configured image from that local project. If there is no local Dockerfile, Dock pulls the configured image from its registry. In this example, Dock pulls `nginx:alpine`, starts a container named `hello-nginx`, maps host port 8080 to container port 80, and checks the configured HTTP endpoint. Open <http://localhost:8080> to see the NGINX welcome page.

For a local application image, keep the same `application.image` field and add the project's `Dockerfile` beside `dock.yaml`. Dock detects that conventional filename when the image is missing. To use a differently named Dockerfile, configure its path:

```yaml
application:
  image: production-test:latest
  docker:
    dockerfile: Dockerfile.production
```

The build context is the directory containing `dock.yaml`. `dock sync` only reports the planned build; `dock init` performs it. A locally available image is reused without rebuilding.

An equivalent starter file is included at [`examples/dock.yaml`](examples/dock.yaml).

## Build and run from a GitHub project

To have Dock fetch source and build an image, use `application.source` and `application.docker` instead of `application.image`:

```yaml
name: hello-api

docker:
  enabled: true

application:
  source:
    type: github
    repository: owner/repository
    branch: main
  docker:
    dockerfile: Dockerfile
  ports:
    - host: 8000
      container: 8000
  healthcheck:
    type: http
    path: /
    port: 8000
```

The repository must contain the named Dockerfile. Dock clones the configured branch into `.dock/apps/<project-name>`, builds the image, starts the container, and checks the endpoint when a health check is configured.

## Commands

Run commands from the directory containing `dock.yaml`. Commands that inspect or operate on a project accept an optional config path, for example `dock validate path/to/dock.yaml`.

| Command | What it does |
| --- | --- |
| `dock -v` | Print the installed Dock version |
| `dock discover` | Report the current machine profile; add `--json` for JSON output |
| `dock validate` | Validate a Dock YAML configuration without applying it |
| `dock status` | Show current host, Docker, requirements, and this project's managed container status; add `--running` or `--stopped` to filter containers |
| `dock sync` | Compare current state with the configuration and preview all needed changes |
| `dock plan` | List pending prerequisite installations and application actions; add `--json` for JSON output |
| `dock init` | Apply the supported plan (`dock apply` is an alias) |
| `dock start` | Start existing Dock-managed containers for this project |
| `dock stop` | Stop those containers while keeping them available to start again |
| `dock logs` | Show recent logs from this project's Dock-managed containers |

V0.2.1 follows the application declared in the selected project configuration. It does not yet offer a container-name selector for choosing individual containers or logs. Fine-grained multi-container operations are future work.

## Current boundaries and direction

Dock is deliberately early. The current YAML format and command behavior may change as real projects exercise them. Its present application setup is built on Docker and, today, the configuration still refers to a Dockerfile for source builds.

One direction for future experimentation is a single `.dock` project file from which Dock could generate or manage the lower-level Docker configuration. A platform and UI for guided, verified setup is another future direction. These are goals to explore, not capabilities promised by V0.2.1.

The immediate purpose of this release is to make the existing local workflow understandable and usable: describe a project, inspect what Dock intends to do, and initialize it through one CLI.

Dock AI provider integration and the `dock ai` command are experimental development work and are not included in the published V0.2.1 release. See [`docs/dock-ai-development.md`](docs/dock-ai-development.md) for the development workflow and safety boundaries.

### Try the BharatCode integration from source

The source tree includes an experimental AI provider integration for BharatCode. To install its optional dependencies, select BharatCode, store the API key in the current PowerShell session, and try the CLI, follow [`docs/bharatcode-api.md`](docs/bharatcode-api.md). The guide documents the current API behavior and setup; AI commands are not part of the published 0.2.1 package yet.

## Development files

The Python distribution, its source modules, packaging metadata, sample configuration, and built archives are grouped in this `package/` directory. The documentation website is maintained separately at the repository root and is not part of this package directory.
