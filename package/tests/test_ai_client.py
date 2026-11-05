import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openai import APIStatusError
from pydantic import BaseModel, ConfigDict

from docklib.ai.client import (
    AIClient,
    AIClientError,
    BHARATCODE_BASE_URL,
    GEMINI_BASE_URL,
    _gemini_json_schema,
)


class TinyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str


class AIClientTests(unittest.TestCase):
    def test_environment_selects_gemini_by_default_and_keeps_bharatcode_key_independent(self):
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "gemini-test-key",
            "BHARATCODE_API_KEY": "bharat-test-key",
        }, clear=True), patch("docklib.ai.client.OpenAI") as openai:
            result = AIClient.from_environment()
        self.assertEqual(result.provider, "gemini")
        self.assertEqual(result.model, "gemini-3.8-flash")
        openai.assert_called_once_with(api_key="gemini-test-key", base_url=GEMINI_BASE_URL, max_retries=0)

    def test_environment_can_select_bharatcode_explicitly(self):
        with patch.dict(os.environ, {
            "DOCK_AI_PROVIDER": "bharatcode",
            "BHARATCODE_API_KEY": "bharat-test-key",
            "GEMINI_API_KEY": "gemini-test-key",
        }, clear=True), patch("docklib.ai.client.OpenAI") as openai:
            result = AIClient.from_environment()
        self.assertEqual(result.provider, "bharatcode")
        self.assertEqual(result.model, "qwen-3.8-27b")
        openai.assert_called_once_with(api_key="bharat-test-key", base_url=BHARATCODE_BASE_URL, max_retries=0)

    def test_missing_key_and_invalid_provider_are_actionable(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(AIClientError, "GEMINI_API_KEY is required"):
                AIClient.from_environment()
        with patch.dict(os.environ, {"DOCK_AI_PROVIDER": "other"}, clear=True):
            with self.assertRaisesRegex(AIClientError, "DOCK_AI_PROVIDER"):
                AIClient.from_environment()

    def test_gemini_uses_json_schema_chat_completion_and_validates_model(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content='{"answer":"ready"}', refusal=None)
        )])
        ai = AIClient(provider="gemini", api_key="fake", model="gemini-test", client=client)
        result = ai.generate_json("test prompt", TinyResult)
        self.assertEqual(result.answer, "ready")
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "gemini-test")
        self.assertEqual(kwargs["response_format"]["type"], "json_schema")
        self.assertEqual(kwargs["response_format"]["json_schema"]["schema"], {
            "additionalProperties": False,
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "type": "object",
        })

    def test_gemini_validates_raw_json_when_parse_response_has_no_parsed_model(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(refusal=None, content='{"answer":"ok"}')
        )])
        result = AIClient(provider="gemini", api_key="fake", model="gemini-test", client=client).generate_json("p", TinyResult)
        self.assertEqual(result.answer, "ok")

    def test_gemini_refuses_incomplete_or_nonconforming_responses(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="length", message=SimpleNamespace(refusal=None, content='{"answer":"partial"}')
        )])
        ai = AIClient(provider="gemini", api_key="fake", model="gemini-test", client=client)
        with self.assertRaisesRegex(AIClientError, "finish_reason=length"):
            ai.generate_json("p", TinyResult)
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(refusal=None, content='{"wrong":1}')
        )])
        with self.assertRaises(AIClientError):
            ai.generate_json("p", TinyResult)

    def test_bharatcode_uses_responses_json_schema_and_store_false(self):
        response = SimpleNamespace(status="completed", output_text='{"answer":"ready"}')
        client = Mock()
        client.responses.create.return_value = response
        ai = AIClient(provider="bharatcode", api_key="fake", model="qwen-test", client=client)
        result = ai.generate_json("p", TinyResult)
        self.assertEqual(result.answer, "ready")
        kwargs = client.responses.create.call_args.kwargs
        self.assertIs(kwargs["store"], False)
        self.assertEqual(kwargs["text"]["format"]["type"], "json_schema")
        self.assertEqual(kwargs["text"]["format"]["schema"], TinyResult.model_json_schema())

    def test_bharatcode_retries_one_503_then_succeeds(self):
        response = SimpleNamespace(status="completed", output_text='{"answer":"ready"}')
        client = Mock()
        client.responses.create.side_effect = [self._status_error(503, {"error": {"code": "busy"}}), response]
        ai = AIClient(provider="bharatcode", api_key="fake", model="qwen-test", client=client)
        with patch("docklib.ai.client.time.sleep") as sleep:
            result = ai.generate_json("p", TinyResult)
        self.assertEqual(result.answer, "ready")
        self.assertEqual(client.responses.create.call_count, 2)
        sleep.assert_called_once_with(3)

    def test_provider_errors_are_normalized_without_credentials(self):
        client = Mock()
        client.chat.completions.create.side_effect = self._status_error(
            429, {"error": {"code": "rate_limit", "message": "Try again"}}
        )
        ai = AIClient(provider="gemini", api_key="never-display-this", model="gemini-test", client=client)
        with self.assertRaises(AIClientError) as caught:
            ai.generate_json("p", TinyResult)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.error_code, "rate_limit")
        self.assertNotIn("never-display-this", str(caught.exception))

    def test_google_list_shaped_auth_error_is_preserved_safely(self):
        client = Mock()
        client.chat.completions.create.side_effect = self._status_error(
            400, [{"error": {"code": 400, "message": "Invalid Auth key.", "status": "INVALID_ARGUMENT"}}]
        )
        ai = AIClient(provider="gemini", api_key="private-test-key", model="gemini-test", client=client)
        with self.assertRaises(AIClientError) as caught:
            ai.generate_json("p", TinyResult)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.error_code, "400")
        self.assertIn("Invalid Auth key", str(caught.exception))
        self.assertNotIn("private-test-key", str(caught.exception))

    def test_gemini_schema_inlines_references_and_removes_pydantic_constraints(self):
        from docklib.ai.schemas import AIResult

        schema = _gemini_json_schema(AIResult)
        encoded = json.dumps(schema)
        self.assertNotIn("$defs", encoded)
        self.assertNotIn("$ref", encoded)
        self.assertNotIn("maxLength", encoded)
        self.assertNotIn("minLength", encoded)
        self.assertNotIn("anyOf", encoded)
        self.assertIn('"type": ["string", "null"]', encoded)
        operation = schema["properties"]["operation"]
        self.assertEqual(operation["type"], ["string", "null"])
        self.assertIn(None, operation["enum"])

    def test_google_field_validation_details_are_included(self):
        client = Mock()
        client.chat.completions.create.side_effect = self._status_error(
            400,
            [{"error": {
                "code": 400,
                "message": "Request contains an invalid argument.",
                "details": [{"fieldViolations": [{
                    "field": "response_format.json_schema.schema",
                    "description": "Unsupported schema keyword: maxLength",
                }]}],
            }}],
        )
        ai = AIClient(provider="gemini", api_key="private-test-key", model="gemini-test", client=client)
        with self.assertRaises(AIClientError) as caught:
            ai.generate_json("p", TinyResult)
        self.assertIn("response_format.json_schema.schema", str(caught.exception))
        self.assertIn("Unsupported schema keyword: maxLength", str(caught.exception))
        self.assertNotIn("private-test-key", str(caught.exception))

    @staticmethod
    def _status_error(status, body):
        import httpx

        request = httpx.Request("POST", "https://example.test")
        response = httpx.Response(status, request=request, headers={"x-request-id": "request-test"})
        return APIStatusError("provider error", response=response, body=body)


if __name__ == "__main__":
    unittest.main()
