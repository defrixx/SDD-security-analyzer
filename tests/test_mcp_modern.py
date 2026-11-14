"""Synthetic local HTTP fixtures. No external server, credential or model."""
import base64
import copy
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from spec_enricher.connections import ConnectionProfile
from spec_enricher.mcp import MCPBroker, MCPError
from spec_enricher.mcp_modern import (
    ADAPTER, VERSION, PREFIX, ModernProtocolError, encode_header,
    header_bindings, request_parts,
)
from spec_enricher.secrets import SecretStore

SCHEMA = {"type": "object", "properties": {
    "document": {"type": "string", "x-mcp-header": "Document"}},
    "required": ["document"], "additionalProperties": False}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((request, dict(self.headers)))
        method = request["method"]
        if method == "server/discover":
            result = {"resultType": "complete", "supportedVersions": [VERSION],
                      "capabilities": {"tools": {}}, "_meta": {
                          PREFIX + "serverInfo": {"name": "synthetic", "version": "1"}}}
        elif method == "tools/list":
            result = {"resultType": "complete", "tools": self.server.tools}
        elif method == "tools/call":
            result = {"resultType": "complete", "content": [{"type": "text", "text": "synthetic"}], "isError": False}
        else:
            result = {}
        override = self.server.overrides.get(method)
        if override is not None:
            result = copy.deepcopy(override)
        if self.server.reply_hook:
            result = self.server.reply_hook(request, result)
        response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        if self.server.rpc_error:
            response = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32022, "message": "unsupported version", "data": {"supported": ["2025-06-18"]}}}
        if getattr(self.server, "mixed_envelope", False):
            response["method"] = "sampling/createMessage"
        if self.server.bad_id:
            response["id"] = True
        if self.server.after_request:
            self.server.after_request(method)
        data = json.dumps(response).encode()
        content_type = "application/json"
        if self.server.sse:
            content_type = "text/event-stream"
            prefix = b": keep alive\n\n"
            if self.server.server_request:
                prefix += b'data: {"jsonrpc":"2.0","id":900,"method":"sampling/createMessage","params":{}}\n\n'
            else:
                prefix += b'data: {"jsonrpc":"2.0","method":"notifications/message","params":{}}\n\n'
            data = prefix + b"data: " + data + b"\n\n"
        self.send_response(self.server.http_status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if self.server.session_header:
            self.send_header("Mcp-Session-Id", "must-not-be-retained")
        self.end_headers()
        self.wfile.write(data)


class ModernMCPTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.requests = []
        self.server.overrides = {}
        self.server.tools = [{"name": "read_document", "inputSchema": copy.deepcopy(SCHEMA)}]
        self.server.reply_hook = self.server.after_request = None
        self.server.sse = self.server.server_request = self.server.rpc_error = self.server.bad_id = False
        self.server.session_header = True
        self.server.http_status = 200
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def profile(self, **values):
        config = dict(name="Modern synthetic", kind="mcp", adapter=ADAPTER,
                      endpoint=self.url, local_addresses=["127.0.0.1"],
                      allowed_tools={"read_document": {"read_only_confirmed": True,
                          "input_schema": copy.deepcopy(SCHEMA)}})
        config.update(values)
        return ConnectionProfile(**config)

    def broker(self, **values):
        return MCPBroker("test", self.profile(**values), SecretStore())

    def test_json_and_sse_complete_calls_are_stateless_and_scoped(self):
        for sse in (False, True):
            with self.subTest(sse=sse):
                self.server.requests.clear()
                self.server.sse = sse
                broker = self.broker()
                result = broker.call("read_document", {"document": "notice\n名前"})
                self.assertEqual(result["trust"], "untrusted_source_data")
                self.assertEqual([r[0]["method"] for r in self.server.requests],
                                 ["server/discover", "tools/list", "tools/call"])
                self.assertIsNone(broker.session_id)
                self.assertEqual(broker.server_info, {"name": "synthetic", "version": "1"})
                for request, headers in self.server.requests:
                    h = {k.lower(): v for k, v in headers.items()}
                    self.assertNotIn("mcp-session-id", h)
                    self.assertEqual(h["mcp-protocol-version"], VERSION)
                    self.assertEqual(h["mcp-method"], request["method"])
                    self.assertEqual(request["params"]["_meta"][PREFIX + "protocolVersion"], VERSION)
                    self.assertEqual(request["params"]["_meta"][PREFIX + "clientCapabilities"], {})
                call, headers = self.server.requests[-1]
                self.assertEqual(headers["Mcp-Name"], "read_document")
                self.assertEqual(headers["Mcp-Param-Document"], encode_header("notice\n名前"))
                self.assertEqual(call["params"]["arguments"], {"document": "notice\n名前"})

    def test_modern_profile_is_explicit_and_legacy_dump_has_no_new_default(self):
        legacy = self.profile(adapter="streamable_http")
        self.assertEqual(legacy.adapter, "streamable_http")
        self.assertNotIn("mcp_protocol_version", legacy.model_dump())
        with self.assertRaises(ValueError):
            self.profile(kind="llm", allowed_tools={})
        with self.assertRaises(ValueError):
            self.profile(adapter="unknown_revision")

    def test_protocol_headers_cannot_be_overridden_by_case_or_secrets(self):
        for header in ("MCP-Method", "mCp-Name", "MCP-Param-Document", "Mcp-Session-Id", "MCP-Protocol-Version", "Last-Event-ID", "Accept"):
            for field in ("headers", "secret_headers"):
                with self.subTest(header=header, field=field), self.assertRaises(ValueError):
                    value = "spoof" if field == "headers" else "session:00000000-0000-0000-0000-000000000000"
                    self.profile(**{field: {header: value}})

    def test_capability_report_version_on_success_and_failure(self):
        report = self.broker().capability_test()
        self.assertEqual(report["protocol_version"], VERSION)
        self.assertEqual(report["status"], "passed")
        self.server.rpc_error = True
        report = self.broker().capability_test()
        self.assertEqual(report["protocol_version"], VERSION)
        self.assertEqual(report["status"], "failed")

    def test_no_legacy_fallback_on_error_or_unsupported_version(self):
        for status in (200, 400):
            with self.subTest(status=status):
                self.server.requests.clear()
                self.server.rpc_error = True
                self.server.http_status = status
                report = self.broker().capability_test()
                self.assertEqual(report["status"], "failed")
                self.assertEqual([r[0]["method"] for r in self.server.requests], ["server/discover"])

    def test_rejects_incomplete_unknown_and_missing_result_types_without_replay(self):
        for result in ({"resultType": "input_required", "inputRequests": {"x": {}}},
                       {"resultType": "task"}, {"content": []},
                       {"resultType": "complete", "content": [], "requestState": "opaque"}):
            with self.subTest(result=result):
                self.server.requests.clear()
                self.server.overrides["tools/call"] = result
                with self.assertRaises(ValueError):
                    self.broker().call("read_document", {"document": "synthetic"})
                self.assertEqual(len(self.server.requests), 3)

    def test_invalid_discovery_does_not_list_tools(self):
        for result in ({"resultType": "complete", "supportedVersions": ["2025-06-18"], "capabilities": {"tools": {}}},
                       {"resultType": "complete", "supportedVersions": [VERSION], "capabilities": {}},
                       {"resultType": "complete", "supportedVersions": [VERSION], "capabilities": {"tools": {}}, "_meta": []}):
            with self.subTest(result=result):
                self.server.requests.clear()
                self.server.overrides["server/discover"] = result
                self.assertEqual(self.broker().capability_test()["status"], "failed")
                self.assertEqual(len(self.server.requests), 1)

    def test_sse_server_requests_are_rejected_without_callback(self):
        self.server.sse = self.server.server_request = True
        with self.assertRaisesRegex(MCPError, "unexpected_modern_server_request"):
            self.broker().discover()
        self.assertEqual(len(self.server.requests), 1)

    def test_mixed_json_request_response_envelope_is_rejected(self):
        self.server.mixed_envelope = True
        with self.assertRaisesRegex(MCPError, "rpc_response_rejected"):
            self.broker().discover()
        self.assertEqual(len(self.server.requests), 1)

    def test_typed_response_id_does_not_accept_boolean(self):
        self.server.bad_id = True
        with self.assertRaises(ValueError):
            self.broker().discover()

    def test_invalid_header_tool_is_excluded_and_reported(self):
        invalid = copy.deepcopy(SCHEMA)
        invalid["properties"]["document"]["x-mcp-header"] = "Bad\r\nInjected"
        self.server.tools.append({"name": "invalid", "inputSchema": invalid})
        broker = self.broker()
        report = broker.capability_test()
        self.assertEqual(report["status"], "passed")
        self.assertEqual([t["name"] for t in report["tools"]], ["read_document"])
        self.assertEqual(report["rejected_tools"], [{"name": "invalid", "reason": "invalid_mcp_header_annotation"}])
        with self.assertRaisesRegex(MCPError, "tool_not_allowlisted"):
            broker.call("invalid", {})

    def test_revoked_authorization_stops_after_discovery(self):
        revoked = False
        def revoke(method):
            nonlocal revoked
            revoked = True
        def authorize():
            if revoked:
                raise PermissionError("revoked")
        self.server.after_request = revoke
        broker = MCPBroker("test", self.profile(), SecretStore(), authorize=authorize)
        with self.assertRaises(PermissionError):
            broker.discover()
        self.assertEqual(len(self.server.requests), 1)

    def test_scope_rejections_send_no_requests(self):
        for name, args in (("delete_all", {}), ("read_document", {"document": "http://example.com/"}),
                           ("read_document", {"unknown": "synthetic"})):
            with self.subTest(name=name, args=args), self.assertRaises(ValueError):
                self.broker().call(name, args)
        self.assertEqual(self.server.requests, [])

    def test_schema_change_is_rejected_before_call(self):
        self.server.tools[0]["inputSchema"]["properties"]["document"]["x-mcp-header"] = "Different"
        with self.assertRaisesRegex(MCPError, "tool_schema_changed"):
            self.broker().call("read_document", {"document": "synthetic"})
        self.assertEqual(len(self.server.requests), 2)

    def test_pagination_reads_all_and_rejects_duplicate_names(self):
        def reply(request, result):
            if request["method"] != "tools/list":
                return result
            if "cursor" not in request["params"]:
                return result | {"nextCursor": "next"}
            return {"resultType": "complete", "tools": []}
        self.server.reply_hook = reply
        self.assertEqual(len(self.broker().discover()), 1)
        self.assertEqual(len(self.server.requests), 3)
        self.server.requests.clear()
        self.server.reply_hook = lambda request, result: (result | {"nextCursor": "next"}) if request["method"] == "tools/list" else result
        with self.assertRaisesRegex(MCPError, "invalid_or_duplicate_tool"):
            self.broker().discover()

    def test_call_and_byte_budgets_remain_enforced(self):
        with self.assertRaisesRegex(MCPError, "mcp_call_budget"):
            self.broker(max_calls=2).call("read_document", {"document": "synthetic"})
        self.assertEqual(len(self.server.requests), 2)
        self.server.requests.clear()
        broker = self.broker()
        broker.deadline = time.monotonic() - 1
        with self.assertRaisesRegex(MCPError, "mcp_wall_budget"):
            broker.discover()
        self.assertEqual(self.server.requests, [])
        # Each response fits the transport ceiling, but cumulative bytes do not.
        def padded(request, result):
            return result | {"padding": "x" * 550}
        self.server.reply_hook = padded
        with self.assertRaisesRegex(MCPError, "mcp_byte_budget"):
            self.broker(max_bytes=1024).discover()


class HeaderRulesTests(unittest.TestCase):
    def test_encoding_and_limits(self):
        self.assertEqual(encode_header(False), "false")
        self.assertEqual(encode_header(42), "42")
        self.assertEqual(encode_header(""), "")
        for value in ("padded ", "\t", "\r\nx", "日本語", "=?base64?literal?="):
            encoded = encode_header(value)
            self.assertEqual(base64.b64decode(encoded[9:-2]).decode(), value)
        for value in (2**53, -(2**53), 1.1, [], {}, "x" * 8193):
            with self.subTest(value=str(value)[:25]), self.assertRaises(ValueError):
                encode_header(value)

    def test_nested_properties_are_mirrored_null_absent_omitted(self):
        schema = {"type": "object", "properties": {"context": {"type": "object", "properties": {
            "region": {"type": "string", "x-mcp-header": "Region"},
            "optional": {"type": "string", "x-mcp-header": "Optional"}}}}}
        body, headers = request_parts("tools/call", {"name": "名前", "arguments": {
            "context": {"region": "east", "optional": None}}}, schema)
        self.assertEqual(headers["Mcp-Name"], encode_header("名前"))
        self.assertEqual(headers["Mcp-Param-Region"], "east")
        self.assertNotIn("Mcp-Param-Optional", headers)
        self.assertEqual(body["_meta"][PREFIX + "clientCapabilities"], {})

    def test_schema_locations_and_annotations(self):
        target = {"type": "string", "x-mcp-header": "X"}
        invalids = [target, {"properties": {"x": target, "y": target}},
                    {"properties": {"x": target | {"type": "number"}}},
                    {"properties": {"x": target | {"x-mcp-header": ""}}},
                    {"properties": {"x": target | {"x-mcp-header": "with space"}}},
                    {"$defs": {"x": target}}, {"allOf": [{"properties": {"x": target}}]},
                    {"items": {"properties": {"x": target}}},
                    {"contentSchema": {"properties": {"x": target}}}]
        for schema in invalids:
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                header_bindings(schema)
        self.assertEqual(header_bindings({"type": "object", "default": {"x-mcp-header": "instance"}}), [])

    def test_unknown_method_metadata_injection_and_wrong_type_rejected(self):
        for method, params, schema in (("initialize", {}, None), ("tools/list", {"_meta": {}}, None),
                ("tools/call", {"name": "read_document", "arguments": {"document": False}}, SCHEMA)):
            with self.subTest(method=method), self.assertRaises(ValueError):
                request_parts(method, params, schema)


if __name__ == "__main__":
    unittest.main()
