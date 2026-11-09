"""Typed, versionable connection configuration; never contains credentials."""

from __future__ import annotations

import ipaddress
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .network import Destination

DataScope = Literal["delta", "baseline_fragments", "catalog", "skill_outputs", "source_queries"]


class ConnectionProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["llm", "mcp"] = "llm"
    adapter: Literal["openai_compatible", "lm_studio", "lm_studio_native", "streamable_http"] = "openai_compatible"
    endpoint: str = Field(min_length=8, max_length=2048)
    local_addresses: list[str] = Field(default_factory=list, max_length=16)
    model: str = Field(default="", max_length=300)
    auth: Literal["none", "bearer"] = "none"
    secret_ref: str | None = Field(
        default=None, max_length=100, pattern=r"^(session:[0-9a-f-]{36}|file:llm_api_key)$"
    )
    headers: dict[str, str] = Field(default_factory=dict, max_length=20)
    secret_headers: dict[str, str] = Field(default_factory=dict, max_length=20)
    timeout: int = Field(default=30, ge=1, le=300)
    max_output_tokens: int = Field(default=2048, ge=32, le=32768)
    context_budget: int = Field(default=16000, ge=1024, le=2000000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None = None
    retries: int = Field(default=2, ge=0, le=2)
    output_mode: Literal["json_schema", "prompt_json"] = "json_schema"
    max_calls: int = Field(default=100, ge=1, le=1000)
    max_bytes: int = Field(default=2097152, ge=1024, le=10485760)
    allowed_tools: dict[str, dict] = Field(default_factory=dict, max_length=50)
    data_scopes: list[DataScope] = Field(default_factory=list, max_length=5)
    enabled: bool = False

    @model_validator(mode="after")
    def validate_profile(self):
        if len(self.data_scopes) != len(set(self.data_scopes)):
            raise ValueError("duplicate data scope")
        for value in self.local_addresses:
            address = ipaddress.ip_address(value)
            if (
                address.is_link_local
                or address.is_multicast
                or address.is_unspecified
                or address.is_global
            ):
                raise ValueError("local allowlist requires exact non-public IPs")
        Destination(self.endpoint, tuple(self.local_addresses)).parts()
        if self.auth == "bearer" and not self.secret_ref:
            raise ValueError("bearer requires a secret reference")
        if self.auth == "none" and self.secret_ref:
            raise ValueError("unused secret reference")
        forbidden_headers = {
            "host",
            "connection",
            "content-length",
            "content-type",
            "transfer-encoding",
            "accept-encoding",
            "upgrade",
            "proxy-authorization",
            "proxy-connection",
            "mcp-session-id",
            "mcp-protocol-version",
        }
        names = [key.lower() for key in self.headers] + [key.lower() for key in self.secret_headers]
        if len(names) != len(set(names)) or (self.auth == "bearer" and "authorization" in names):
            raise ValueError("conflicting headers")
        for key, value in (self.headers | self.secret_headers).items():
            if (
                not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,99}", key)
                or key.lower() in forbidden_headers
                or len(value) > 2048
                or any(ord(c) < 32 or ord(c) > 126 for c in value)
            ):
                raise ValueError("invalid custom header")
        for key in self.headers:
            if any(
                word in key.lower()
                for word in ("authorization", "cookie", "token", "secret", "api-key", "apikey")
            ):
                raise ValueError("credential headers require secret references")
        for reference in self.secret_headers.values():
            if not re.fullmatch(r"session:[0-9a-f-]{36}|file:llm_api_key", reference):
                raise ValueError("invalid header secret reference")
        if self.adapter == "lm_studio_native":
            if self.output_mode != "prompt_json":
                raise ValueError("native_lm_studio_requires_explicit_prompt_json")
            if self.reasoning_effort not in {None, "none", "low", "medium", "high"}:
                raise ValueError("unsupported_native_reasoning_setting")
            if self.temperature is not None and self.temperature > 1:
                raise ValueError("native_temperature_must_be_between_zero_and_one")
        if self.kind == "llm" and self.adapter == "streamable_http":
            raise ValueError("invalid LLM adapter")
        if self.kind == "mcp" and self.adapter != "streamable_http":
            raise ValueError("MCP requires Streamable HTTP")
        if self.kind == "llm" and self.allowed_tools:
            raise ValueError("LLM server-side tools are disabled")
        if self.kind == "mcp":
            for name, mapping in self.allowed_tools.items():
                if not name or set(mapping) - {
                    "input_schema",
                    "read_only_confirmed",
                    "argument_rules",
                }:
                    raise ValueError("invalid tool mapping")
                if mapping.get("read_only_confirmed") is not True:
                    raise ValueError("tool must be explicitly approved read-only")
                safe_schema(mapping.get("input_schema", {}))
                rules = mapping.get("argument_rules", {})
                if not isinstance(rules, dict):
                    raise ValueError("invalid argument rules")
                for key, values in rules.items():
                    if (
                        not isinstance(key, str)
                        or not isinstance(values, list)
                        or not values
                        or len(values) > 100
                    ):
                        raise ValueError("argument rules must enumerate permitted scalar values")
                    if any(type(value) not in {str, int, bool} for value in values):
                        raise ValueError("only scalar argument rules are supported")
        return self


def safe_schema(schema: object, *, timeout: float = 5) -> dict:
    """User schemas may refer only to local definitions; never remote resources."""
    from .schema_worker import validate_definition

    validate_definition(schema, timeout=timeout)
    if not isinstance(schema, dict):
        raise ValueError("schema must be an object")
    return schema
