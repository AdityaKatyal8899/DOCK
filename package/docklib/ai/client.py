"""Optional model clients for Gemini testing and BharatCode deployment."""

from __future__ import annotations

import json
import os
import re
import time
from typing import TypeVar

from openai import APIConnectionError, APIStatusError, OpenAI, OpenAIError
from pydantic import BaseModel


ModelT = TypeVar("ModelT", bound=BaseModel)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
BHARATCODE_BASE_URL = "https://bharatcode.ai/api/model/v1"


class AIClientError(RuntimeError):
    """A provider error safe to show in Dock's CLI (never includes a key)."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        error_code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.error_code = error_code
        self.request_id = request_id


class AIClient:
    """Use a configured provider while validating every response locally.

    Gemini uses Google's documented OpenAI-compatible Chat Completions path.
    BharatCode uses its documented Responses path with ``store=False``.
    """

    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        model: str,
        client: OpenAI | None = None,
    ) -> None:
        if provider not in {"gemini", "bharatcode"}:
            raise ValueError("provider must be 'gemini' or 'bharatcode'")
        if not api_key.strip():
            raise ValueError("api_key cannot be empty")
        self.provider = provider
        self.model = model
        base_url = GEMINI_BASE_URL if provider == "gemini" else BHARATCODE_BASE_URL
        self._client = client or OpenAI(api_key=api_key, base_url=base_url, max_retries=0)

    @classmethod
    def from_environment(cls) -> "AIClient":
        """Create the selected client from environment variables only.

        ``DOCK_AI_PROVIDER`` defaults to Gemini for the current test phase.
        Set it to ``bharatcode`` to select ``BHARATCODE_API_KEY`` instead.
        Both credentials may remain configured side by side.
        """
        provider = os.environ.get("DOCK_AI_PROVIDER", "gemini").strip().lower()
        if provider not in {"gemini", "bharatcode"}:
            raise AIClientError(
                "DOCK_AI_PROVIDER must be 'gemini' or 'bharatcode'.",
                provider=provider or "unknown",
            )
        key_name = "GEMINI_API_KEY" if provider == "gemini" else "BHARATCODE_API_KEY"
        api_key = os.environ.get(key_name, "").strip()
        if not api_key:
            raise AIClientError(
                f"{key_name} is required for the selected AI provider.",
                provider=provider,
            )
        model_name = "GEMINI_MODEL" if provider == "gemini" else "BHARATCODE_MODEL"
        default_model = "gemini-3.8-flash" if provider == "gemini" else "qwen-3.8-27b"
        model = os.environ.get(model_name, "").strip() or default_model
        return cls(provider=provider, api_key=api_key, model=model)

    def generate_json(self, prompt: str, response_model: type[ModelT]) -> ModelT:
        """Request one structured result and validate it with the given model."""
        if not prompt.strip():
            raise ValueError("prompt cannot be empty")
        try:
            if self.provider == "gemini":
                result = self._generate_gemini(prompt, response_model)
            else:
                result = self._generate_bharatcode(prompt, response_model)
        except AIClientError:
            raise
        except APIStatusError as exc:
            raise self._status_error(exc) from exc
        except APIConnectionError as exc:
            raise AIClientError(
                f"Could not connect to {self.provider}: {type(exc).__name__}.",
                provider=self.provider,
            ) from exc
        except OpenAIError as exc:
            raise AIClientError(
                f"{self.provider} SDK request failed: {type(exc).__name__}.",
                provider=self.provider,
            ) from exc
        except (ValueError, TypeError) as exc:
            raise AIClientError(
                f"{self.provider} returned an invalid structured response: {exc}",
                provider=self.provider,
            ) from exc
        return result

    def _generate_gemini(self, prompt: str, response_model: type[ModelT]) -> ModelT:
        schema = _gemini_json_schema(response_model)
        schema_name = re.sub(r"[^A-Za-z0-9_-]", "_", response_model.__name__)[:64] or "dock_result"
        completion = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "Return a response that matches the supplied structured response model."},
                {"role": "user", "content": prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
        )
        if not completion.choices:
            raise ValueError("No completion choices were returned.")
        choice = completion.choices[0]
        message = choice.message
        if choice.finish_reason in {"length", "content_filter"}:
            raise ValueError(f"Generation ended with finish_reason={choice.finish_reason}.")
        if getattr(message, "refusal", None):
            raise ValueError("The model declined this request.")
        content = getattr(message, "content", None)
        if not content:
            raise ValueError("The model returned no structured content.")
        return response_model.model_validate_json(content)

    def _generate_bharatcode(self, prompt: str, response_model: type[ModelT]) -> ModelT:
        schema = response_model.model_json_schema()
        schema_name = re.sub(r"[^A-Za-z0-9_-]", "_", response_model.__name__)[:64] or "dock_result"
        request = {
            "model": self.model,
            "input": prompt,
            "store": False,
            "max_output_tokens": 8192,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        for attempt in range(2):
            try:
                response = self._client.responses.create(**request)
                break
            except APIStatusError as exc:
                if exc.status_code == 503 and attempt == 0:
                    time.sleep(3)
                    continue
                raise
        status = getattr(response, "status", None)
        if status in {"incomplete", "failed"}:
            details = getattr(response, "incomplete_details", None)
            reason = getattr(details, "reason", None) if details else None
            raise ValueError(f"Response ended with status={status}" + (f", reason={reason}" if reason else "."))
        output = getattr(response, "output_text", "")
        if not output:
            raise ValueError("The Responses API returned no output_text.")
        return response_model.model_validate_json(output)

    def _status_error(self, exc: APIStatusError) -> AIClientError:
        body = getattr(exc, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], dict):
            body = body[0]
        error = body.get("error", body) if isinstance(body, dict) else {}
        if isinstance(error, list) and error and isinstance(error[0], dict):
            error = error[0]
        error_code = error.get("code") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        details = error.get("details", []) if isinstance(error, dict) else []
        request_id = getattr(exc, "request_id", None)
        if not message:
            message = "Request was rejected by the model provider."
        message = str(message).replace("\n", " ")[:500]
        diagnostic_parts: list[str] = []
        if isinstance(details, list):
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                violations = detail.get("fieldViolations", [])
                if isinstance(violations, list):
                    for violation in violations:
                        if not isinstance(violation, dict):
                            continue
                        field = violation.get("field")
                        description = violation.get("description") or violation.get("message")
                        if description:
                            diagnostic = f"{field}: {description}" if field else str(description)
                            diagnostic_parts.append(str(diagnostic).replace("\n", " ")[:240])
                            if len(diagnostic_parts) == 3:
                                break
                if len(diagnostic_parts) == 3:
                    break
        if diagnostic_parts:
            message = f"{message} Details: {'; '.join(diagnostic_parts)}"
        return AIClientError(
            f"{self.provider} API error (HTTP {exc.status_code}): {message}",
            provider=self.provider,
            status_code=exc.status_code,
            error_code=str(error_code) if error_code else None,
            request_id=request_id,
        )


def _gemini_json_schema(response_model: type[BaseModel]) -> dict[str, object]:
    """Build a compact JSON Schema subset for Gemini; Pydantic validates locally."""
    raw_schema = response_model.model_json_schema()
    definitions = raw_schema.get("$defs", {})
    omitted = {
        "$defs", "$ref", "title", "default", "minLength", "maxLength",
        "minItems", "maxItems", "minimum", "maximum", "exclusiveMinimum",
        "exclusiveMaximum", "pattern", "examples",
    }

    def simplify(node: object, resolving: frozenset[str] = frozenset()) -> object:
        if isinstance(node, list):
            return [simplify(item, resolving) for item in node]
        if not isinstance(node, dict):
            return node
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.rsplit("/", 1)[-1]
            if name in resolving:
                raise ValueError("Recursive schemas are not supported for Gemini structured output.")
            target = definitions.get(name)
            if not isinstance(target, dict):
                raise ValueError(f"Unresolved Gemini schema reference: {name}")
            return simplify(target, resolving | {name})
        result: dict[str, object] = {}
        for key, value in node.items():
            if key in omitted:
                continue
            if key == "anyOf" and isinstance(value, list):
                options = [simplify(item, resolving) for item in value if isinstance(item, dict)]
                null_options = [item for item in options if isinstance(item, dict) and item.get("type") == "null"]
                non_null_options = [item for item in options if item not in null_options]
                if len(options) == 2 and len(null_options) == 1 and len(non_null_options) == 1 and isinstance(non_null_options[0], dict):
                    result.update(non_null_options[0])
                    result["type"] = [non_null_options[0].get("type"), "null"]
                    enum_values = result.get("enum")
                    if isinstance(enum_values, list) and None not in enum_values:
                        result["enum"] = [*enum_values, None]
                else:
                    raise ValueError("Gemini structured output cannot represent this schema union.")
                continue
            result[key] = simplify(value, resolving)
        if result.get("type") == "object":
            properties = result.get("properties", {})
            if isinstance(properties, dict):
                result["required"] = list(properties)
                result["additionalProperties"] = False
        return result

    simplified = simplify(raw_schema)
    if not isinstance(simplified, dict):
        raise ValueError("Gemini structured output requires an object schema.")
    return simplified

