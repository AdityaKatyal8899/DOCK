# Dock

![Dock brand icon](https://dockcloud.online/icon.svg)

**Website:** [dockcloud.online](https://dockcloud.online)

**Dock is a deployment control environment built around a project’s `dock.yaml`.** It helps developers describe setup once, inspect a repeatable plan, and use Docker to build or run the application. The experimental AI layer lets users ask for those workflows in plain English, review the proposed action, and approve it before Dock executes it.

> **Configure once. Initialize anywhere.**

## What Dock does

- Validates a project's `dock.yaml`.
- Inspects the host, Docker Engine, images, and Dock-managed containers.
- Plans supported setup and deployment steps.
- Applies approved steps through Dock's existing planner and executor.
- In the experimental AI workflow, analyzes project context, explains plans, previews files, and requests approval before supported operations.

Dock uses Docker Engine to build images and run containers. It does not implement a container engine of its own. AI features are experimental source-tree work and are not included in the published `dock-api` 0.2.1 package.

## Quick start

Install the current Python package from the repository root:

```powershell
python -m pip install .\package
```

Create a project directory with a `dock.yaml`, then run Dock from that directory:

```yaml
name: hello-nginx

docker:
  enabled: true

application:
  image: nginx:alpine
  ports:
    - host: 8080
      container: 80
```

```powershell
dock validate
dock sync
dock init
dock status
```

The [package README](package/README.md) explains the configuration schema, commands, local builds, GitHub source builds, and current boundaries.

## Try the BharatCode AI integration

The source tree has an optional BharatCode provider for the experimental `dock ai` workflow. It reads `BHARATCODE_API_KEY` from the current process environment, uses BharatCode's Responses API, and sends `store: false`. Dock validates the structured model response and requires approval before file writes or deployment operations.

Follow the [BharatCode API setup guide](package/docs/bharatcode-api.md) to create an API key, install the optional AI dependencies, configure PowerShell, check API access, and run Dock AI. The [AI development notes](package/docs/dock-ai-development.md) describe provider selection, safety boundaries, and the current test status.

**Current limitation:** an earlier live BharatCode attempt returned `subscription_required`; a successful end-to-end BharatCode model call has not yet been verified. The guide calls this out and explains how to check account access.

## Project layout

- `package/` — Python source, package metadata, examples, tests, and product documentation.
- `package/docs/bharatcode-api.md` — BharatCode API setup and troubleshooting for Dock AI.
- `package/test-proj/` — small HTTP app for testing Dock's local Docker workflow.
- `dock-website-development/` — separate website project.

## Development

From `package/`, install the source and optional AI dependencies:

```powershell
python -m pip install -e ".[ai]"
```

Then configure `DOCK_AI_PROVIDER` and the matching API key as described in the provider guide. Never commit API keys or put them in `dock.yaml`.
