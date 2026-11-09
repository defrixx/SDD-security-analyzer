"""Explicit OpenAI-compatible and native LM Studio JSON adapters."""

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
        if "output" in raw:
            items = raw["output"]
            messages = [item for item in items if item.get("type") == "message"]
            if len(messages) != 1 or any(item.get("type") not in {"message", "reasoning"} for item in items):
                return placeholder
            content = messages[0]["content"]
        else:
            content = raw["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            return placeholder
        return content.encode("utf-8")[:32000].decode("utf-8", errors="ignore")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return placeholder


def output_object(
    content: str, output_mode: str, *, allow_missing_array_object_open: bool = False
) -> tuple[dict, str | None]:
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
    try:
        value = parse(content.encode(), "json", catalog=True)
    except InputError:
        if allow_missing_array_object_open and output_mode == "prompt_json" and normalization is None:
            from .json_normalization import missing_array_object_open

            normalized = missing_array_object_open(content)
            if normalized is not None:
                return normalized
        raise
    if not isinstance(value, dict):
        raise ModelError("output_not_object")
    return value, normalization


class LLMAdapter:
    def __init__(self, project_id: str, profile: ConnectionProfile, secrets: SecretStore):
        self.project_id = project_id
        self.profile = profile
        self.secrets = secrets
        self.inference_response_received = False
        self._allow_missing_array_object_open = False
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
        native = self.profile.adapter == "lm_studio_native"
        entries_key, id_key = ("models", "key") if native else ("data", "id")
        if not isinstance(payload, dict) or not isinstance(payload.get(entries_key), list):
            raise ModelError("invalid_models_response")
        models = []
        for entry in payload[entries_key][:1000]:
            if not isinstance(entry, dict) or not isinstance(entry.get(id_key), str):
                raise ModelError("invalid_model_id")
            if native and entry.get("type") != "llm":
                continue
            models.append(entry[id_key])
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
        if self.profile.adapter == "lm_studio_native":
            if self.profile.output_mode != "prompt_json":
                raise ModelError("native_lm_studio_requires_explicit_prompt_json")
            logical = value["messages"]
            if (len(logical) != 2 or logical[0].get("role") != "system"
                    or not isinstance(logical[0].get("content"), str)
                    or logical[1].get("role") != "user"
                    or not isinstance(logical[1].get("content"), str)):
                raise ModelError("unsupported_native_message_layout")
            # Native input lacks assistant-history roles. Fail closed rather
            # than flatten a structural repair history and change its trust.
            user_input = logical[1]["content"]
            native = {
                "model": self.profile.model, "system_prompt": logical[0]["content"],
                "input": user_input, "max_output_tokens": self.profile.max_output_tokens,
                "stream": False, "store": False, "integrations": [],
            }
            if self.profile.temperature is not None:
                native["temperature"] = self.profile.temperature
            if self.profile.reasoning_effort is not None:
                native["reasoning"] = "off" if self.profile.reasoning_effort == "none" else self.profile.reasoning_effort
            return native
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
        native = self.profile.adapter == "lm_studio_native"
        result = self.transport.request("POST", "/chat" if native else "/chat/completions", value=payload, headers=headers)
        self.inference_response_received = True
        self.guard_credentials(result.body, credentials)
        if after_response:
            after_response(result.body, result.latency_ms)
        raw = parse(result.body, "json", catalog=True)
        try:
            if native:
                return self._native_completion(raw, schema, payload, result.latency_ms)
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

    def _native_completion(self, raw, schema, payload, latency_ms):
        items = raw["output"]
        if not isinstance(items, list) or any(not isinstance(x, dict) for x in items):
            raise ModelError("output_invalid")
        if any(x.get("type") not in {"message", "reasoning"} for x in items):
            raise ModelError("unexpected_tool_call")
        stats = raw.get("stats", {})
        if not isinstance(stats, dict):
            raise ModelError("output_invalid")
        output_tokens = stats.get("total_output_tokens")
        # Native API has no documented finish_reason. Conservatively reject
        # budget-saturated output, even when it happens to parse as JSON.
        if type(output_tokens) is int and output_tokens >= self.profile.max_output_tokens:
            raise ModelOutputTruncated()
        reasoning = any(x.get("type") == "reasoning" and x.get("content") for x in items)
        if self.profile.reasoning_effort == "none" and (
            reasoning or (type(stats.get("reasoning_output_tokens")) is int and stats["reasoning_output_tokens"] > 0)
        ):
            raise ModelError("reasoning_option_not_honored")
        messages = [x for x in items if x.get("type") == "message"]
        if not messages or not any(isinstance(x.get("content"), str) and x["content"].strip() for x in messages):
            raise ModelError("empty_content_with_reasoning" if reasoning else "empty_content")
        if len(messages) != 1 or not isinstance(messages[0].get("content"), str):
            raise ModelError("output_invalid")
        value, normalization = output_object(
            messages[0]["content"], self.profile.output_mode,
            allow_missing_array_object_open=(
                self._allow_missing_array_object_open
                and schema.get("title") == "builtin.P3.native.fact-pins.v1"
            ),
        )
        issues = validate_payload(schema, value)
        if issues:
            raise OutputSchemaError(issues)
        def count(key):
            val = stats.get(key)
            return val if type(val) is int and val >= 0 else None
        prompt, completion = count("input_tokens"), count("total_output_tokens")
        usage = {"prompt_tokens": prompt, "completion_tokens": completion,
                 "total_tokens": prompt + completion if prompt is not None and completion is not None else None}
        return Completion(value, raw, usage, payload, latency_ms, normalization)

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
