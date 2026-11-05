# Use BharatCode with Dock AI

This guide configures the experimental Dock AI workflow to use BharatCode's model API. Dock uses the selected model to interpret a request and return a structured result; Dock validates that result locally and routes any deployment operation through Dock's existing planner and executor. File changes and infrastructure operations still require user approval.

> **Availability:** The AI workflow is currently in the source tree and is not included in the published `dock-api` 0.2.1 package. Install the optional AI dependencies from a local checkout as shown below. The project has not yet completed a successful live BharatCode smoke test; an earlier attempt returned `subscription_required`.

## 1. Get an API key

Create or retrieve a developer API key from [your BharatCode account](https://bharatcode.ai/account). BharatCode's API guide currently lists Qwen 3.8 27B as the default coding and reasoning model; API access requires a Lite or Pro plan, or verified student access. The DeepSeek model requires Pro access. Check the [current API guide](https://bharatcode.ai/build) for the live model catalog and account requirements.

Treat the key like a password. Do not put it in `dock.yaml`, source code, screenshots, command examples, or a public repository. Dock reads it from the process environment and does not save it.

## 2. Install Dock's AI dependencies

Open PowerShell in the repository's `package` directory:

```powershell
python -m pip install -e ".[ai]"
```

This installs Dock in editable mode with the optional OpenAI-compatible client and Pydantic dependencies. It does not change the published package.

## 3. Select BharatCode and provide the key

These settings apply to the current PowerShell session. Dock's source currently defaults to Gemini, so set the provider explicitly:

```powershell
$env:DOCK_AI_PROVIDER = "bharatcode"
$env:BHARATCODE_MODEL = "qwen-3.8-27b"
$secureKey = Read-Host "BharatCode API key" -AsSecureString
$env:BHARATCODE_API_KEY = [System.Net.NetworkCredential]::new("", $secureKey).Password
Remove-Variable secureKey
```

The secure prompt does not display the key as you type or paste it. Press Enter to finish. The key remains available to Dock processes started from this PowerShell window until you close the window or remove the variable:

```powershell
Remove-Item Env:BHARATCODE_API_KEY
Remove-Item Env:DOCK_AI_PROVIDER
Remove-Item Env:BHARATCODE_MODEL
```

Dock defaults to `qwen-3.8-27b` when `BHARATCODE_MODEL` is unset. Use another model ID only if it is listed by BharatCode for your account.

## 4. Check API access directly (optional)

This read-only request checks whether the key can access the default model before invoking Dock. It does not write project files or run Docker commands.

```powershell
$headers = @{ Authorization = "Bearer $env:BHARATCODE_API_KEY" }
$body = @{
    model = $env:BHARATCODE_MODEL
    input = "Reply with: BharatCode API connection successful."
    store = $false
} | ConvertTo-Json -Compress

$response = Invoke-RestMethod `
    -Uri "https://bharatcode.ai/api/model/v1/responses" `
    -Method Post `
    -Headers $headers `
    -ContentType "application/json" `
    -Body $body

$response.output_text
```

BharatCode documents the base URL as `https://bharatcode.ai/api/model/v1`, uses Bearer authentication, and supports the Responses API. Requests should set `store: false`. See [BharatCode's API guide](https://bharatcode.ai/build) for direct API examples, models, and limits.

## 5. Run Dock AI

Point Dock to a project directory that contains a valid `dock.yaml`:

```powershell
python -m dock ai "Analyze this project and summarize its deployment plan" `
    --project ".\test-proj" `
    --config "dock.yaml"
```

Try a read-only status request:

```powershell
python -m dock ai "Show Docker availability, image state, and Dock-managed containers" `
    --project ".\test-proj" `
    --config "dock.yaml"
```

Use `--project` to choose the project directory; a relative `--config` path is resolved inside that directory. Analysis and status are read-only. For operations such as build, start, stop, or provision, Dock shows the affected plan and asks for approval before executing it.

## What Dock sends and how it uses the response

- Dock sends the user's request and selected project context to the configured provider. Project collection is bounded and excludes common secret files; `.env` file contents are not read, and common inline credentials are redacted on a best-effort basis. Review the [AI development and safety notes](dock-ai-development.md) before using it with sensitive projects.
- Dock calls BharatCode's Responses endpoint with structured JSON output and `store: false`, then validates the response with local Pydantic models.
- BharatCode does not execute Dock operations. Dock interprets the validated operation and uses its own allowlisted planner/executor flow.
- Generated files are previewed before a write approval. Docker Compose is preview-only in this workflow.

## Troubleshooting

| Message or status | What to check |
| --- | --- |
| `BHARATCODE_API_KEY is required` | Set the key in the same PowerShell session that runs Dock, and set `DOCK_AI_PROVIDER=bharatcode`. |
| HTTP `401` or `403` | Check that the key is copied correctly, active, and eligible for API access under the account's plan or student status. |
| `subscription_required` or HTTP `402` | The account may need an API-eligible plan or verified student access. Check the current [BharatCode API access requirements](https://bharatcode.ai/build). |
| HTTP `400` | Check the model ID and request compatibility. Dock currently requests strict structured JSON output through `text.format`. |
| HTTP `503` | The model is busy. BharatCode shares model capacity and may queue requests; wait and retry later. Dock retries a first 503 once after a short delay. |
| Structured response validation error | The model returned JSON that does not match Dock's required schema. Retry once, then report the sanitized error and request ID if shown. |

Do not paste API keys into bug reports. Include the provider, model ID, sanitized error text, and request ID when available.

## API references

- [BharatCode API guide](https://bharatcode.ai/build)
- [BharatCode account and API key page](https://bharatcode.ai/account)
- [BharatCode Build League](https://www.bharatcode.ai/hackathon)
