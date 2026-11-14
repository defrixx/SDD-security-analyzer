"""Opt-in 2026-07-28 MCP wire rules. No callbacks, cache or automatic replay.

Protocol: https://modelcontextprotocol.io/specification/2026-07-28/
Authorization, schema pinning, transport and budgets remain broker-owned.
"""
from __future__ import annotations
import base64
import re

VERSION = "2026-07-28"
ADAPTER = "streamable_http_2026_07_28"
PREFIX = "io.modelcontextprotocol/"
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_MAPS = {"$defs", "definitions", "patternProperties", "dependentSchemas", "dependencies"}
_SINGLE = {"additionalProperties", "unevaluatedProperties", "propertyNames", "contains", "items", "additionalItems", "unevaluatedItems", "not", "if", "then", "else", "contentSchema"}
_ARRAYS = {"allOf", "anyOf", "oneOf", "prefixItems"}

class ModernProtocolError(ValueError):
    pass


def encode_header(value: object) -> str:
    if type(value) is bool:
        text = "true" if value else "false"
    elif type(value) is int:
        if not -(2**53 - 1) <= value <= 2**53 - 1:
            raise ModernProtocolError("mcp_header_integer_range")
        text = str(value)
    elif type(value) is str:
        text = value
    else:
        raise ModernProtocolError("mcp_header_value_type")
    if (text != text.strip() or any(ord(c) < 32 or ord(c) > 126 for c in text)
            or (text.startswith("=?base64?") and text.endswith("?="))):
        text = "=?base64?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="
    if len(text) > 8192:
        raise ModernProtocolError("mcp_header_value_limit")
    return text


def header_bindings(schema: dict) -> list[tuple[tuple[str, ...], str, str]]:
    """Inspect schema locations, not instance data in default/enum/examples."""
    bindings = []
    names = set()
    def visit(node, path=(), reachable=True, depth=0):
        if depth > 64:
            raise ModernProtocolError("mcp_header_schema_depth")
        if not isinstance(node, dict):
            return
        if "x-mcp-header" in node:
            name, kind = node["x-mcp-header"], node.get("type")
            if (not reachable or not path or not isinstance(name, str)
                    or not _TOKEN.fullmatch(name) or len(name) > 100
                    or name.lower() in names or kind not in ("string", "integer", "boolean")):
                raise ModernProtocolError("invalid_mcp_header_annotation")
            names.add(name.lower())
            bindings.append((path, name, kind))
            if len(bindings) > 50:
                raise ModernProtocolError("mcp_header_count_limit")
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                for prop, child in value.items():
                    visit(child, path + (prop,), reachable, depth + 1)
            elif key in _MAPS and isinstance(value, dict):
                for child in value.values():
                    visit(child, (), False, depth + 1)
            elif key in _SINGLE:
                for child in value if isinstance(value, list) else [value]:
                    visit(child, (), False, depth + 1)
            elif key in _ARRAYS and isinstance(value, list):
                for child in value:
                    visit(child, (), False, depth + 1)
    visit(schema)
    return bindings


def request_parts(method: str, params: dict, schema: dict | None = None) -> tuple[dict, dict]:
    if method not in {"server/discover", "tools/list", "tools/call"}:
        raise ModernProtocolError("unsupported_modern_method")
    if "_meta" in params:
        raise ModernProtocolError("application_owned_mcp_metadata")
    body = dict(params)
    body["_meta"] = {PREFIX + "protocolVersion": VERSION,
        PREFIX + "clientInfo": {"name": "spec-enricher", "version": "1.0.0"},
        PREFIX + "clientCapabilities": {}}
    headers = {"MCP-Protocol-Version": VERSION, "Mcp-Method": method}
    if method == "tools/call":
        if schema is None or not isinstance(params.get("name"), str):
            raise ModernProtocolError("missing_modern_tool_schema")
        headers["Mcp-Name"] = encode_header(params["name"])
        for path, name, kind in header_bindings(schema):
            value = params.get("arguments", {})
            for part in path:
                if not isinstance(value, dict) or part not in value:
                    value = None
                    break
                value = value[part]
            if value is None:
                continue
            if type(value) is not {"string": str, "integer": int, "boolean": bool}[kind]:
                raise ModernProtocolError("mcp_header_value_type")
            headers["Mcp-Param-" + name] = encode_header(value)
    if sum(len(k) + len(v) for k, v in headers.items()) > 32768:
        raise ModernProtocolError("mcp_header_total_limit")
    return body, headers


def complete_result(result: object) -> dict:
    # An explicitly selected modern server must supply its result discriminator.
    if not isinstance(result, dict) or result.get("resultType") != "complete":
        raise ModernProtocolError("unsupported_modern_result")
    if any(k in result for k in ("inputRequests", "requestState", "task")):
        raise ModernProtocolError("unsupported_modern_interaction")
    return result


def discovery_info(result: dict) -> dict:
    versions, capabilities = result.get("supportedVersions"), result.get("capabilities")
    if (not isinstance(versions, list) or not versions
            or any(not isinstance(v, str) for v in versions)
            or VERSION not in versions or len(versions) != len(set(versions))
            or not isinstance(capabilities, dict) or not isinstance(capabilities.get("tools"), dict)):
        raise ModernProtocolError("unsupported_modern_server")
    meta = result.get("_meta", {})
    if not isinstance(meta, dict):
        raise ModernProtocolError("invalid_server_metadata")
    info = meta.get(PREFIX + "serverInfo", {})
    if not isinstance(info, dict):
        raise ModernProtocolError("invalid_server_metadata")
    return info
