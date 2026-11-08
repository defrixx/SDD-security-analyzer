"""OpenAI-compatible JSON adapter, also used by the explicit LM Studio profile."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from .catalog import digest
from .connections import ConnectionProfile, safe_schema
from .inputs import InputError, parse
from .network import Destination, NetworkError, Transport
from .schema_definition import has_builtin_binding, valid_structure
from .schema_worker import SchemaValidationUnavailable, validate_payload
from .secrets import SecretReferenceError, SecretStore


@dataclass(frozen=True)
class Completion:
    value: dict
    raw: dict
    usage: dict
    request: dict
    latency_ms: float
    format_normalization: str | None = None


class ModelError(ValueError):
    pass


class ModelOutputTruncated(ModelError):
    def __init__(self):
        super().__init__("model_output_truncated")


class OutputSchemaError(ModelError):
    """Keep structural locations for the separate code-owned diagnostic sanitizer."""

    def __init__(self, issues: list[dict]):
        super().__init__("output_schema_invalid")
        self.validation_issues = issues


def repair_text(raw_content: bytes | None) -> str:
    """Extract bounded text only; malformed envelopes never break repair itself."""
    placeholder = "Malformed response; no textual content recovered."
    if not raw_content:
        return placeholder
    try:
        raw = parse(raw_content, "json", catalog=True)
        content = raw["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            return placeholder
        return content.encode("utf-8")[:32000].decode("utf-8", errors="ignore")
    except (ValueError, KeyError, IndexError, TypeError):
        return placeholder


def output_object(content: str, output_mode: str) -> tuple[dict, str | None]:
    """One bounded decoder shared by validation and diagnostic-only inspection."""
    normalization = None
    if output_mode == "prompt_json":
        first, opening, rest = content.strip().partition("\n")
        inner, closing, last = rest.rpartition("\n")
        if (
            opening
            and closing
            and first.removesuffix("\r") in {"```", "```json", "```JSON"}
            and last == "```"
        ):
            content = inner
            normalization = "single_json_code_fence"
    value = parse(content.encode(), "json", catalog=True)
    if not isinstance(value, dict):
        raise ModelError("output_not_object")
    return value, normalization


class LLMAdapter:
    def __init__(self, project_id: str, profile: ConnectionProfile, secrets: SecretStore):
        self.project_id = project_id
        self.profile = profile
        self.secrets = secrets
        self.inference_response_received = False
        self._validated_schema_hash: str | None = None
        self.transport = Transport(
            Destination(profile.endpoint, tuple(profile.local_addresses)),
            timeout=profile.timeout,
            max_bytes=profile.max_bytes,
        )

    def headers(self):
        result = dict(self.profile.headers)
        for key, reference in self.profile.secret_headers.items():
            result[key] = self.secrets.get(self.project_id, reference)
        if self.profile.auth == "bearer":
            result["Authorization"] = "Bearer " + self.secrets.get(
                self.project_id, self.profile.secret_ref or ""
            )
        return result

    def models(self):
        headers = self.headers()
        credentials = self.secrets.header_credentials(
            headers, self.profile.secret_headers, bearer=self.profile.auth == "bearer"
        )
        result = self.transport.request("GET", "/models", headers=headers)
        self.guard_credentials(result.body, credentials)
        try:
            payload = parse(result.body, "json", catalog=True)
        except InputError:
            raise ModelError("invalid_models_response") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ModelError("invalid_models_response")
        models = []
        for entry in payload["data"][:1000]:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                raise ModelError("invalid_model_id")
            models.append(entry["id"])
        try:
            self.secrets.reject_known(json.dumps(models, ensure_ascii=False).encode())
        except ValueError:
            raise ModelError("credential_echo_rejected") from None
        return models, result.latency_ms

    def guard_credentials(self, content: bytes, credentials: tuple[str, ...] = ()):
        try:
            self.secrets.reject_known(content, used_credentials=credentials)
        except ValueError:
            raise ModelError("credential_echo_rejected") from None

    def payload(self, messages: list[dict], schema: dict) -> dict:
        # Adapter instances belong to one project/logical call. A preview,
        # consent resume or structural repair can reuse only the exact schema
        # already validated by this instance; mutable schemas are re-hashed.
        if not valid_structure(schema):
            raise ValueError("invalid_schema")
        schema_hash = digest(schema)
        if getattr(self, "_validated_schema_hash", None) != schema_hash:
            if not has_builtin_binding(schema, schema_hash):
                safe_schema(schema)
            self._validated_schema_hash = schema_hash
        if not self.profile.model:
            raise ModelError("model_not_selected")
        value: dict = {
            "model": self.profile.model,
            "messages": messages,
            "max_tokens": self.profile.max_output_tokens,
            "stream": False,
        }
        if self.profile.temperature is not None:
            value["temperature"] = self.profile.temperature
        if self.profile.reasoning_effort is not None:
            value["reasoning_effort"] = self.profile.reasoning_effort
        if self.profile.output_mode == "json_schema":
            value["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "stage_output", "strict": True, "schema": schema},
            }
        else:
            from .catalog import canonical

            instruction = (
                "Return only a JSON object conforming to this schema. Do not use Markdown fences. Schema: "
                + canonical(schema).decode()
            )
            if messages and messages[0].get("role") == "system":
                value["messages"] = [
                    messages[0] | {"content": messages[0]["content"] + "\n\n" + instruction}
                ] + messages[1:]
            else:
                value["messages"] = [{"role": "system", "content": instruction}] + messages
        # No tools/integrations/server-side MCP or unconfigured provider options.
        return value

    def complete(
        self,
        messages: list[dict],
        schema: dict,
        *,
        before_request: Callable[[dict], None] | None = None,
        after_response: Callable[[bytes, float], None] | None = None,
    ) -> Completion:
        self.inference_response_received = False
        payload = self.payload(messages, schema)
        headers = self.headers()  # Resolve only configured references before recording payloads.
        credentials = self.secrets.header_credentials(
            headers, self.profile.secret_headers, bearer=self.profile.auth == "bearer"
        )
        self.guard_credentials(json.dumps(payload, ensure_ascii=False).encode(), credentials)
        if before_request:
            before_request(payload)
        result = self.transport.request("POST", "/chat/completions", value=payload, headers=headers)
        self.inference_response_received = True
        self.guard_credentials(result.body, credentials)
        if after_response:
            after_response(result.body, result.latency_ms)
        raw = parse(result.body, "json", catalog=True)
        try:
            choice = raw["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ModelOutputTruncated()
            message = choice["message"]
            if message.get("tool_calls") or message.get("function_call"):
                raise ModelError("unexpected_tool_call")
            if self.profile.reasoning_effort == "none" and message.get("reasoning_content"):
                raise ModelError("reasoning_option_not_honored")
            if not isinstance(message.get("content"), str) or not message["content"].strip():
                raise ModelError(
                    "empty_content_with_reasoning"
                    if message.get("reasoning_content")
                    else "empty_content"
                )
            value, normalization = output_object(message["content"], self.profile.output_mode)
            issues = validate_payload(schema, value)
            if issues:
                raise OutputSchemaError(issues)
        except (KeyError, IndexError, TypeError, AttributeError, InputError) as exc:
            raise ModelError("output_invalid") from exc
        usage = raw.get("usage")
        if not isinstance(usage, dict):
            usage = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
        else:
            usage = {
                key: usage.get(key) if type(usage.get(key)) is int and usage[key] >= 0 else None
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            }
        return Completion(value, raw, usage, payload, result.latency_ms, normalization)

    def capability_test(self) -> dict:
        started = time.monotonic()
        report = {
            "status": "failed",
            "synthetic_only": True,
            "network": "not_tested",
            "auth": "not_tested",
            "models": "not_tested",
            "inference": "not_tested",
            "schema": "not_tested",
            "output_mode": self.profile.output_mode,
            "requested_reasoning_effort": self.profile.reasoning_effort,
            "model_ids": [],
            "model": self.profile.model,
            "error": None,
        }
        try:
            models, latency = self.models()
            report.update(
                network="reachable",
                auth="accepted",
                models="passed",
                model_ids=models,
                models_latency_ms=latency,
            )
            if not self.profile.model:
                report["error"] = "select_model"
                return report
            if self.profile.model not in models:
                report["error"] = "selected_model_unavailable"
                return report
            schema = {
                "type": "object",
                "properties": {"ok": {"const": True}},
                "required": ["ok"],
                "additionalProperties": False,
            }
            messages = [
                {
                    "role": "system",
                    "content": 'Synthetic capability test. Return JSON matching {"ok":true}. No tools.',
                },
                {"role": "user", "content": 'Return {"ok":true}.'},
            ]
            report["inference"] = "running"
            completion = self.complete(messages, schema)
            report.update(
                status="passed" if self.profile.output_mode == "json_schema" else "degraded",
                inference="passed",
                schema="passed",
                inference_latency_ms=completion.latency_ms,
                usage=completion.usage,
                format_normalization=completion.format_normalization,
            )
        except SchemaValidationUnavailable:
            report.update(
                inference="response_received" if self.inference_response_received else "not_tested",
                schema="unavailable",
                error="local_schema_validation_unavailable",
            )
        except SecretReferenceError:
            report.update(auth="failed", error="credential_reference_unavailable")
            if report["inference"] == "running":
                report["inference"] = "not_tested"
        except NetworkError as exc:
            report["error"] = exc.code
            if report["inference"] == "running":
                report["inference"] = "failed"
            if exc.code in {"auth_failed", "unsupported_auth"}:
                report.update(network="reachable", auth="failed")
            elif exc.code in {
                "http_error",
                "rate_limited",
                "redirect_forbidden",
                "response_too_large",
            }:
                report["network"] = "reachable"
        except ModelError as exc:
            report["error"] = str(exc)
            if report["inference"] == "running":
                report.update(inference="response_received", schema="failed")
            else:
                report.update(network="reachable", auth="accepted", models="failed")
        except (InputError, ValueError):
            report["error"] = "model_or_schema_contract_failed"
            if report["inference"] == "running":
                report.update(inference="response_received", schema="failed")
        finally:
            report["latency_ms"] = (time.monotonic() - started) * 1000
        return report
