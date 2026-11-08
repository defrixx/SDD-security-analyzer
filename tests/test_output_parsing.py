"""Synthetic provider failures exercise actual API repair and MCP framing."""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import test_pipeline as pipeline_fixture
from jsonschema import Draft202012Validator
from test_connections import Handler

from spec_enricher.connections import ConnectionProfile
from spec_enricher.llm import LLMAdapter, repair_text
from spec_enricher.mcp import MCPBroker
from spec_enricher.network import HTTPResult, NetworkError, is_event_stream
from spec_enricher.secrets import SecretStore


def malformed_responses():
    return [
        ("null_content", json.dumps({"choices": [{"message": {"content": None}}]}).encode()),
        ("integer_content", json.dumps({"choices": [{"message": {"content": 17}}]}).encode()),
        ("object_content", json.dumps({"choices": [{"message": {"content": {}}}]}).encode()),
        ("array_message", json.dumps({"choices": [{"message": []}]}).encode()),
        ("deep_envelope", b"[" * 10000 + b"]" * 10000),
    ]


class EventHandler(Handler):
    def do_POST(self):
        if hasattr(self.server, "event"):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.server.requests.append(request)
            self.reply(b"data: " + self.server.event + b"\n\n", content_type="text/event-stream")
        else:
            super().do_POST()

    def reply(self, body, *args, content_type="application/json", **kwargs):
        if content_type == "text/event-stream":
            content_type = getattr(self.server, "media", content_type)
        return super().reply(body, *args, content_type=content_type, **kwargs)


class OutputParsingTests(unittest.TestCase):
    def test_bound_schema_failure_still_rejects_a_structurally_valid_dto(self):
        harness = pipeline_fixture.PipelineTests(
            "test_p0_to_p8_regression_artifacts_idempotency_and_review_scope"
        )
        harness.setUp()
        value = {
            "operations": [
                {
                    "id": "unapproved",
                    "operation": "add",
                    "delta_block_id": "not-in-this-project",
                    "target_block_id": None,
                    "target_hash": None,
                    "target_quote": None,
                    "question": None,
                    "explanation": "Structurally valid but outside the allowed source IDs.",
                }
            ],
            "questions": [],
        }
        from spec_enricher.stage_contracts import OperationsOutput

        OperationsOutput.model_validate(value)
        raw = json.dumps({"choices": [{"message": {"content": json.dumps(value)}}]}).encode()

        def complete(adapter, messages, schema, *, before_request=None, after_response=None):
            with patch.object(
                adapter.transport,
                "request",
                return_value=HTTPResult(200, {"content-type": "application/json"}, raw, 1),
            ):
                return LLMAdapter.complete(
                    adapter,
                    messages,
                    schema,
                    before_request=before_request,
                    after_response=after_response,
                )

        try:
            with patch.object(pipeline_fixture.FixtureAdapter, "complete", complete):
                rid, _ = harness.start("schema-rejection-remains-rejection")
                run = harness.wait(rid)
                self.assertEqual(run["status"], "failed")
                prefix = f"/api/v1/projects/{harness.pid}/runs/{rid}"
                artifacts = harness.client.get(prefix + "/artifacts").json()
                self.assertEqual(sum(a["kind"] == "model.request" for a in artifacts), 2)
                self.assertEqual(sum(a["kind"] == "model.rejection" for a in artifacts), 2)
                self.assertFalse(
                    any(
                        a["kind"] in {"model.output", "stage.P2.output", "stage.P7.output"}
                        for a in artifacts
                    )
                )
        finally:
            harness.tearDown()

    def test_http_failure_preserves_only_safe_status_without_a_raw_body_or_schema_repair(self):
        harness = pipeline_fixture.PipelineTests(
            "test_p0_to_p8_regression_artifacts_idempotency_and_review_scope"
        )
        harness.setUp()

        def complete(adapter, messages, schema, *, before_request=None, after_response=None):
            with patch.object(
                adapter.transport, "request", side_effect=NetworkError("http_error", 400)
            ):
                return LLMAdapter.complete(
                    adapter,
                    messages,
                    schema,
                    before_request=before_request,
                    after_response=after_response,
                )

        try:
            with patch.object(pipeline_fixture.FixtureAdapter, "complete", complete):
                rid, _ = harness.start("safe-transport-status")
                run = harness.wait(rid)
                prefix = f"/api/v1/projects/{harness.pid}/runs/{rid}"
                artifacts = harness.client.get(prefix + "/artifacts").json()
                self.assertEqual(run["status"], "failed")
                self.assertEqual(sum(a["kind"] == "model.request" for a in artifacts), 1)
                self.assertFalse(
                    any(
                        a["kind"] in {"model.raw", "model.rejection", "stage.P2.output"}
                        for a in artifacts
                    )
                )
                errors = [a for a in artifacts if a["kind"] == "model.transport_error"]
                self.assertEqual(len(errors), 1)
                body = harness.client.get(prefix + f"/artifacts/{errors[0]['id']}").json()
                self.assertEqual(
                    body,
                    {
                        "stage": "P2",
                        "code": "http_error",
                        "http_status": 400,
                        "retry_after_seconds": None,
                    },
                )
        finally:
            harness.tearDown()

    def test_truncation_marks_partial_without_retry_or_success_for_a_valid_stage_object(self):
        for initial_malformed in (False, True):
            with self.subTest(initial_malformed=initial_malformed):
                harness = pipeline_fixture.PipelineTests(
                    "test_p0_to_p8_regression_artifacts_idempotency_and_review_scope"
                )
                harness.setUp()
                original = pipeline_fixture.FixtureAdapter.complete
                attempts = 0

                def complete(
                    adapter,
                    messages,
                    schema,
                    *,
                    before_request=None,
                    after_response=None,
                    _original=original,
                    _initial_malformed=initial_malformed,
                ):
                    nonlocal attempts
                    attempts += 1
                    if _initial_malformed and attempts == 1:
                        raw = {"choices": [{"message": {"content": None}}]}
                    else:
                        valid = _original(adapter, messages, schema)
                        Draft202012Validator(schema).validate(valid.value)
                        raw = valid.raw
                        raw["choices"][0]["finish_reason"] = "length"
                    with patch.object(
                        adapter.transport,
                        "request",
                        return_value=HTTPResult(
                            200, {"content-type": "application/json"}, json.dumps(raw).encode(), 1
                        ),
                    ):
                        return LLMAdapter.complete(
                            adapter,
                            messages,
                            schema,
                            before_request=before_request,
                            after_response=after_response,
                        )

                try:
                    with patch.object(pipeline_fixture.FixtureAdapter, "complete", complete):
                        rid, _ = harness.start("provider-cutoff")
                        run = harness.wait(rid)
                        prefix = f"/api/v1/projects/{harness.pid}/runs/{rid}"
                        metadata = harness.client.get(prefix + "/artifacts").json()
                        self.assertEqual(run["status"], "partial")
                        self.assertEqual(run["error_code"], "model_output_truncated")
                        self.assertEqual(run["stages"][-1]["status"], "partial")
                        self.assertEqual(attempts, 2 if initial_malformed else 1)
                        self.assertEqual(
                            sum(a["kind"] == "model.request" for a in metadata), attempts
                        )
                        self.assertEqual(sum(a["kind"] == "model.raw" for a in metadata), attempts)
                        self.assertFalse(
                            any(
                                a["kind"] in {"stage.P2.output", "stage.P7.output"}
                                for a in metadata
                            )
                        )
                        rejections = [a for a in metadata if a["kind"] == "model.rejection"]
                        last = harness.client.get(
                            prefix + f"/artifacts/{rejections[-1]['id']}"
                        ).json()
                        self.assertEqual(last["reason_code"], "model_output_truncated")
                        self.assertFalse(last["repair_permitted"])
                        self.assertIn("finish_reason=length", last["recovery_hint"])
                finally:
                    harness.tearDown()

    def exercise_api(self, raw, *, recover):
        harness = pipeline_fixture.PipelineTests(
            "test_p0_to_p8_regression_artifacts_idempotency_and_review_scope"
        )
        harness.setUp()
        original = pipeline_fixture.FixtureAdapter.complete
        attempts = 0

        def complete(adapter, messages, schema, *, before_request=None, after_response=None):
            nonlocal attempts
            attempts += 1
            if recover and attempts > 1:
                return original(
                    adapter,
                    messages,
                    schema,
                    before_request=before_request,
                    after_response=after_response,
                )
            with patch.object(
                adapter.transport,
                "request",
                return_value=HTTPResult(200, {"content-type": "application/json"}, raw, 1),
            ):
                return LLMAdapter.complete(
                    adapter,
                    messages,
                    schema,
                    before_request=before_request,
                    after_response=after_response,
                )

        try:
            with patch.object(pipeline_fixture.FixtureAdapter, "complete", complete):
                rid, _ = harness.start("malformed-raw-repair")
                run = harness.wait(rid)
                prefix = f"/api/v1/projects/{harness.pid}/runs/{rid}"
                artifacts = harness.client.get(prefix + "/artifacts").json()
                p2 = [a for a in artifacts if a["metadata"].get("stage") == "P2"]
                self.assertEqual(sum(a["kind"] == "model.request" for a in p2), 2)
                self.assertEqual(sum(a["kind"] == "model.raw" for a in p2), 2)
                self.assertTrue(all(stage["status"] != "running" for stage in run["stages"]))
                self.assertEqual(run["status"], "succeeded" if recover else "failed")
                self.assertNotEqual(run["error_code"], "worker_initialization_or_unexpected_error")
                if not recover:
                    self.assertEqual(attempts, 2)
                    self.assertFalse(any(a["kind"] == "stage.P7.output" for a in artifacts))
        finally:
            harness.tearDown()

    def test_malformed_provider_responses_fail_coherently_after_one_repair(self):
        for name, raw in malformed_responses():
            with self.subTest(case=name):
                self.exercise_api(raw, recover=False)

    def test_malformed_first_response_can_be_repaired_and_finish_the_pipeline(self):
        for name, raw in (malformed_responses()[0], malformed_responses()[-1]):
            with self.subTest(case=name):
                self.exercise_api(raw, recover=True)

    def test_repair_excerpt_is_bounded_valid_utf8_and_preserves_source_prefix(self):
        content = "Ж🛡" * 12000
        raw = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        excerpt = repair_text(raw)
        self.assertTrue(content.startswith(excerpt))
        self.assertLessEqual(len(excerpt.encode("utf-8")), 32000)
        self.assertGreater(len(excerpt.encode("utf-8")), 31996)

    def test_invalid_repair_envelopes_use_only_a_code_owned_placeholder(self):
        marker = "PRIVATE_DIAGNOSTIC_MARKER_78516"
        for raw in [
            None,
            b"null",
            b"[]",
            b"{}",
            b"invalid",
            b'{"choices":[],"' + marker.encode() + b'":0}',
            *[r for _, r in malformed_responses()],
        ]:
            with self.subTest(raw_type=type(raw).__name__):
                text = repair_text(raw)
                self.assertEqual(text, "Malformed response; no textual content recovered.")
                self.assertNotIn(marker, text)

    def server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), EventHandler)
        server.requests = []
        server.sse = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        profile = ConnectionProfile(
            name="Synthetic SSE parsing",
            kind="mcp",
            adapter="streamable_http",
            endpoint=f"http://127.0.0.1:{server.server_port}/mcp",
            local_addresses=["127.0.0.1"],
        )
        return server, profile

    def test_sse_rejects_nested_duplicate_nonfinite_and_invalid_utf8_events(self):
        server, profile = self.server()
        for event in (
            b"[" * 10000 + b"]" * 10000,
            b'{"id":1,"result":' + b'{"nested":' * 1000 + b"0" + b"}" * 1000 + b"}",
            b'{"id":1,"id":true,"result":{}}',
            b'{"id":1,"result":NaN}',
            b"\xff",
        ):
            with self.subTest(event_bytes=len(event)):
                server.event = event
                broker = MCPBroker("A", profile, SecretStore())
                result = broker.capability_test()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"], "invalid_sse_event")
                self.assertEqual(result["network"], "reachable")
                self.assertEqual(result["auth"], "accepted")
                self.assertEqual(result["discovery"], "failed")
                self.assertFalse(broker.initialized)
                self.assertEqual(broker.calls, 1)

    def test_sse_mime_is_exact_case_insensitive_and_supports_parameters(self):
        server, profile = self.server()
        for media, expected in (
            ("text/event-stream", "passed"),
            ("Text/Event-Stream; charset=utf-8", "passed"),
            ("text/event-streaming", "failed"),
        ):
            with self.subTest(media=media):
                server.media = media
                result = MCPBroker("A", profile, SecretStore()).capability_test()
                self.assertEqual(result["status"], expected)
                self.assertEqual(is_event_stream(media), expected == "passed")
