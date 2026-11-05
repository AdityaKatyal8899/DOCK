# Dock AI development notes

Dock AI is experimental source-tree work and is not part of the published `dock-api` 0.2.1 release. Existing Dock commands do not require the optional AI dependencies, an API key, or network access.

## Install the optional development dependencies

From `package/`:

```powershell
python -m pip install -e ".[ai]"
```

## Select a provider

Gemini is the current test provider. Set the provider and API key in the process environment or an approved local secret store. Do not commit keys or put them in `dock.yaml`.

For the Windows-first BharatCode setup, API access check, Dock commands, and troubleshooting, see [`bharatcode-api.md`](bharatcode-api.md).

```powershell
$env:DOCK_AI_PROVIDER = "gemini"
# Set GEMINI_API_KEY using your local secret-management method.
```

Defaults and overrides:

| Provider | Provider selector | Key variable | Default model | Optional model override |
| --- | --- | --- | --- | --- |
| Gemini | `DOCK_AI_PROVIDER=gemini` (default) | `GEMINI_API_KEY` | `gemini-3.8-flash` | `GEMINI_MODEL` |
| BharatCode | `DOCK_AI_PROVIDER=bharatcode` | `BHARATCODE_API_KEY` | `qwen-3.8-27b` | `BHARATCODE_MODEL` |

Both keys may be present in the environment. `DOCK_AI_PROVIDER` selects which one Dock reads. The Gemini adapter uses Google's OpenAI-compatible Chat Completions endpoint; the BharatCode adapter uses its Responses endpoint and sends `store: false`.

## Try the CLI

```powershell
dock ai "Analyze this project and prepare a deployment plan"
dock ai "Generate a Dockerfile and Compose preview for this app"
dock ai "Why is my app container failing?" --config .\dock.yaml
dock ai "Explain these failures" --container my-api --config .\dock.yaml
dock ai "Review and apply the current Dock setup" --apply --config .\dock.yaml
python .\dock_gemini.py
```

For a natural-language diagnosis without `--container`, Dock uses the application name from the selected `dock.yaml`. `--container` selects a specific Dock-managed container in that project.

The interactive `dock_gemini.py` launcher prompts once for a hidden Gemini key when it is not already in the environment, then accepts multiple queries in one session. The key is held in memory and is not saved. Natural-language operation requests are limited to:

| Request | Dock behavior |
| --- | --- |
| Status | Read the live project and Docker state; no approval prompt |
| Start / stop | Show the Dock-managed project containers and ask before changing them |
| Build | Execute only `fetch_application` and `build_image` steps already present in Dock's plan; ask before execution |
| Provision / initialize | Show and apply the full existing Dock plan, which may pull/build an image and start the application; ask before execution |

After a mutation, Dock inspects the project again and prints the resulting state. These actions operate on Dock-managed containers. Compose files remain previews and are not executed by this workflow.

## Approval and validation behavior

- Analysis, project detection, deployment planning, and log diagnosis are read-only.
- Generated artifact content is previewed before Dock asks whether to write it. Replacing an existing file requires a second confirmation.
- Writes are restricted to the selected project workspace.
- Dock YAML proposals are checked with Dock's current configuration validator.
- Compose proposals are checked for valid YAML and a non-empty `services` mapping. Dock does not run Compose.
- Dockerfile proposals receive a basic `FROM` check. Dock does not build them during generation.
- Dockerfile and Compose proposals are paired with a `.dockerignore` containing Dock's secret-file exclusions. Dock asks to write `.dockerignore` first and will not write the related build artifact if that approval is declined.
- `--apply` displays a plan created by Dock's existing `inspect_project()`/planner flow, then asks separately before calling the existing executor.
- The model never supplies shell commands to Dock's executor.

## Project context and privacy

Dock sends selected project context to the selected provider when `dock ai` runs. The collector limits the number and size of files, excludes common secret files and generated/vendor directories, and redacts common inline credential assignments. Environment-file paths are included as relative-path metadata only; their contents are not read or sent. These checks are best-effort; inspect the project files and generated previews before using the feature. Log diagnosis sends bounded recent logs and container metadata after best-effort redaction.

Repository files and logs are treated as untrusted input in prompts. Model output is validated locally with Pydantic before Dock uses it.

## Current test status

The provider clients, AI workflows, operation approvals, and artifact safeguards have mocked unit tests. Gemini has been exercised manually by the project owner. BharatCode returned `subscription_required` during an earlier live smoke test, so successful BharatCode model calls remain unverified.

