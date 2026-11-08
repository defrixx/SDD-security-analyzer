"""Bounded Streamable HTTP MCP client and explicitly read-only tool broker."""

from __future__ import annotations

import time
from collections.abc import Callable
from urllib.parse import urlsplit

from .catalog import digest
from .connections import ConnectionProfile, safe_schema
from .i18n import translate
from .inputs import parse
from .network import Destination, NetworkError, Transport, is_event_stream, rpc_id_matches
from .schema_worker import SchemaValidationUnavailable, validate_payload
from .secrets import SecretReferenceError, SecretStore

PROTOCOL_VERSION = "2025-06-18"


class MCPError(ValueError):
    pass


class MCPBroker:
    def __init__(
        self,
        project_id: str,
        profile: ConnectionProfile,
        secrets: SecretStore,
        *,
        authorize: Callable[[], object] | None = None,
    ):
        self.project_id = project_id
        self.profile = profile
        self.secrets = secrets
        self.authorize = authorize
        self.transport = Transport(
            Destination(profile.endpoint, tuple(profile.local_addresses)),
            timeout=profile.timeout,
            max_bytes=profile.max_bytes,
        )
        self.session_id: str | None = None
        self.next_id = 0
        self.calls = 0
        self.bytes = 0
        self.initialized = False
        self.tools: dict[str, dict] = {}
        self.deadline = time.monotonic() + profile.timeout
        self.last_raw_response = b""
        self.response_received = False
        self.server_info: dict[str, str] = {}

    def _request(self, method: str, params: dict, *, notification=False):
        if self.authorize:
            self.authorize()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise MCPError("mcp_wall_budget")
        self.transport.timeout = min(self.profile.timeout, remaining)
        if self.calls >= self.profile.max_calls:
            raise MCPError("mcp_call_budget")
        self.calls += 1
        headers = {"Accept": "application/json, text/event-stream"}
        headers.update(self.profile.headers)
        for key, reference in self.profile.secret_headers.items():
            headers[key] = self.secrets.get(self.project_id, reference)
        if self.profile.auth == "bearer":
            headers["Authorization"] = "Bearer " + self.secrets.get(
                self.project_id, self.profile.secret_ref or ""
            )
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.initialized:
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        credentials = self.secrets.header_credentials(
            headers, self.profile.secret_headers, bearer=self.profile.auth == "bearer"
        )
        self.next_id += 1
        payload: dict = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            payload["id"] = self.next_id
        if self.authorize:
            self.authorize()
        result = self.transport.request(
            "POST",
            value=payload,
            headers=headers,
            rpc_response_id=None if notification else self.next_id,
        )
        self.response_received = True
        try:
            self.secrets.reject_known(result.body, used_credentials=credentials)
        except ValueError:
            raise MCPError("credential_echo_rejected") from None
        self.last_raw_response = result.body
        self.bytes += len(result.body)
        if self.bytes > self.profile.max_bytes:
            raise MCPError("mcp_byte_budget")
        session_id = result.headers.get("mcp-session-id")
        if session_id:
            if len(session_id) > 200 or any(
                ord(char) < 33 or ord(char) > 126 for char in session_id
            ):
                raise MCPError("invalid_session_id")
            if self.session_id and self.session_id != session_id:
                raise MCPError("session_changed")
            self.session_id = session_id
        if notification:
            if result.status != 202 or result.body.strip():
                raise MCPError("invalid_notification_response")
            return None
        if is_event_stream(result.headers.get("content-type", "")):
            messages = []
            for event in result.body.decode().replace("\r\n", "\n").split("\n\n"):
                lines = [
                    line[5:].lstrip(" ") for line in event.splitlines() if line.startswith("data:")
                ]
                if lines:
                    messages.append(parse("\n".join(lines).encode(), "json", catalog=True))
            matches = [
                message
                for message in messages
                if isinstance(message, dict)
                and rpc_id_matches(message.get("id"), self.next_id)
                and ("result" in message or "error" in message)
            ]
            if len(matches) != 1:
                raise MCPError("invalid_sse_rpc_response")
            response = matches[0]
        else:
            response = parse(result.body, "json", catalog=True)
        if (
            not isinstance(response, dict)
            or response.get("jsonrpc") != "2.0"
            or not rpc_id_matches(response.get("id"), self.next_id)
            or "error" in response
            or "result" not in response
        ):
            raise MCPError("rpc_response_rejected")
        return response["result"]

    def discover(self) -> list[dict]:
        response = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "spec-enricher", "version": "1.0.0"},
            },
        )
        if not isinstance(response, dict) or response.get("protocolVersion") != PROTOCOL_VERSION:
            raise MCPError("unsupported_protocol")
        info = response.get("serverInfo", {})
        if isinstance(info, dict):
            self.server_info = {
                key: info[key]
                for key in ("name", "version")
                if isinstance(info.get(key), str) and len(info[key]) <= 200
            }
        self.initialized = True
        self._request("notifications/initialized", {}, notification=True)
        cursor = None
        seen_cursors = set()
        for _ in range(10):
            page = self._request("tools/list", {} if cursor is None else {"cursor": cursor})
            if not isinstance(page, dict) or not isinstance(page.get("tools"), list):
                raise MCPError("invalid_tools_list")
            for tool in page["tools"]:
                if (
                    not isinstance(tool, dict)
                    or not isinstance(tool.get("name"), str)
                    or not tool["name"]
                    or tool["name"] in self.tools
                ):
                    raise MCPError("invalid_or_duplicate_tool")
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise MCPError("mcp_wall_budget")
                safe_schema(tool.get("inputSchema"), timeout=min(5, remaining))
                self.tools[tool["name"]] = {
                    "name": tool["name"],
                    "description": str(tool.get("description", ""))[:10000],
                    "input_schema": tool["inputSchema"],
                    "annotations_untrusted": tool.get("annotations", {}),
                }
                if len(self.tools) > 200:
                    raise MCPError("tool_limit")
            cursor = page.get("nextCursor")
            if cursor is None:
                return list(self.tools.values())
            if not isinstance(cursor, str) or len(cursor) > 1000 or cursor in seen_cursors:
                raise MCPError("invalid_tools_cursor")
            seen_cursors.add(cursor)
        raise MCPError("tool_page_limit")

    def call(self, name: str, arguments: dict):
        mapping = self.profile.allowed_tools.get(name)
        if mapping is None or mapping.get("read_only_confirmed") is not True:
            raise MCPError("tool_not_allowlisted")
        schema = mapping["input_schema"]
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise MCPError("closed_object_schema_required")
        if validate_payload(schema, arguments):
            raise MCPError("tool_arguments_invalid")
        for key, allowed in mapping.get("argument_rules", {}).items():
            if key not in arguments or not any(
                type(arguments[key]) is type(value) and arguments[key] == value for value in allowed
            ):
                raise MCPError("tool_argument_scope")

        # Typed URL parameters must be explicitly enumerated in the mapping.
        # This is a permission boundary, independent of model/tool descriptions.
        def check_urls(value, key=""):
            if isinstance(value, dict):
                for name_, child in value.items():
                    check_urls(child, name_)
            elif isinstance(value, list):
                for child in value:
                    check_urls(child, key)
            elif isinstance(value, str):
                parsed = urlsplit(value)
                if parsed.scheme or key.lower() in {"url", "uri", "endpoint"}:
                    if value not in mapping.get("argument_rules", {}).get(key, []):
                        raise MCPError("tool_url_not_approved")
                    Destination(value, tuple(self.profile.local_addresses)).addresses()

        check_urls(arguments)
        if not self.initialized:
            self.discover()
        discovered = self.tools.get(name)
        if discovered is None or digest(discovered["input_schema"]) != digest(schema):
            raise MCPError("tool_schema_changed")
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if (
            not isinstance(result, dict)
            or ("isError" in result and type(result["isError"]) is not bool)
            or result.get("isError") is True
            or not isinstance(result.get("content"), list)
        ):
            raise MCPError("tool_result_failed")
        return {
            "tool": name,
            "arguments": arguments,
            "result": result,
            "trust": "untrusted_source_data",
            "execution": "configured_read_only_call",
            "residual_risk": translate(
                "Read-only mapping выражает доверие пользователя; приложение не изолирует сеть и реализацию стороннего MCP-сервера."
            ),
        }

    def capability_test(self):
        started = time.monotonic()
        report: dict = {
            "status": "failed",
            "network": "not_tested",
            "auth": "not_tested",
            "discovery": "not_tested",
            "read_only_call": "not_tested",
            "tools": [],
            "error": None,
            "residual_risk": translate(
                "Descriptions/annotations не доказывают read-only. Вызов выполняется отдельным действием после allowlist."
            ),
        }
        try:
            report["tools"] = self.discover()
            report.update(status="passed", network="reachable", auth="accepted", discovery="passed")
        except SecretReferenceError:
            report.update(auth="failed", error="credential_reference_unavailable")
            if self.initialized:
                report["network"] = "reachable"
        except NetworkError as exc:
            report["error"] = exc.code
            if exc.status is not None and 200 <= exc.status < 300:
                report.update(network="reachable", auth="accepted", discovery="failed")
            elif exc.code in {"auth_failed", "unsupported_auth"}:
                report.update(network="reachable", auth="failed")
        except SchemaValidationUnavailable:
            report["error"] = "local_schema_validation_unavailable"
            if self.response_received:
                report.update(network="reachable", auth="accepted", discovery="unavailable")
        except (MCPError, ValueError):
            report["error"] = "mcp_contract_failed"
            if self.response_received:
                report.update(network="reachable", auth="accepted", discovery="failed")
        report["latency_ms"] = (time.monotonic() - started) * 1000
        return report
