"""Test-only real-adapter API driver, shared by opt-in corpus evaluations.

No fake responses, policy/oracle interpretation or model fallback lives here.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi.testclient import TestClient

from spec_enricher.api import create_app
from spec_enricher.catalog import canonical
from spec_enricher.network import resolve_host

ORIGIN = "http://127.0.0.1:8080"


def accepts_capability(result, output_mode):
    return result["status"] == "passed" or (
        output_mode == "prompt_json"
        and result["status"] == "degraded"
        and result["inference"] == "passed"
        and result["schema"] == "passed"
    )


class RealHarness:
    def __init__(
        self,
        root: Path,
        endpoint: str,
        model: str,
        *,
        context_budget=32768,
        max_output_tokens=4096,
        output_mode="json_schema",
        reasoning_effort="none",
    ):
        self.root, self.endpoint, self.model = root, endpoint, model
        self.context_budget, self.max_output_tokens = context_budget, max_output_tokens
        self.output_mode, self.reasoning_effort = output_mode, reasoning_effort
        self.client = TestClient(create_app(root, ORIGIN), base_url=ORIGIN)
        self.csrf = ""

    def __enter__(self):
        self.client.__enter__()
        self.csrf = self.api(
            "/session/bootstrap", {"token": (self.root / "bootstrap.token").read_text()}
        )["csrf_token"]
        return self

    def __exit__(self, *exc):
        return self.client.__exit__(*exc)

    def response(self, path, body=None, *, raw=None, method=None):
        return self.client.request(
            method or ("POST" if raw is not None or body is not None else "GET"),
            "/api/v1" + path,
            json=body,
            content=raw,
            headers={"Origin": ORIGIN, "X-CSRF-Token": self.csrf},
        )

    def api(self, path, body=None, *, raw=None, method=None):
        response = self.response(path, body, raw=raw, method=method)
        if response.status_code >= 400:
            raise RuntimeError(f"real_api_rejected:{path}:{response.text[:400]}")
        return response.json()

    def project(self, name):
        return self.api(
            "/projects", {"name": name, "description": "Synthetic handoff evaluation only."}
        )["id"]

    def profile(self):
        parts = urlsplit(self.endpoint)
        return {
            "name": "Handoff LM Studio",
            "adapter": "lm_studio",
            "endpoint": self.endpoint,
            "local_addresses": resolve_host(parts.hostname, parts.port or 1234),
            "model": self.model,
            "timeout": 300,
            "max_output_tokens": self.max_output_tokens,
            "retries": 2,
            "context_budget": self.context_budget,
            "temperature": 0,
            "output_mode": self.output_mode,
            "reasoning_effort": self.reasoning_effort,
            "enabled": True,
            "data_scopes": ["delta", "baseline_fragments", "catalog"],
        }

    def accepts_capability(self, result):
        return accepts_capability(result, self.output_mode)

    def connection(self, project_id):
        profile = self.profile()
        connection = self.api(f"/projects/{project_id}/connections", profile)
        capability = self.api(f"/projects/{project_id}/connections/{connection['id']}/test", {})
        if self.accepts_capability(capability["result"]):
            self.api(
                f"/projects/{project_id}/connections/{connection['id']}/grants",
                {
                    "confirmed": True,
                    "revision": connection["revision"],
                    "expected_sha256": connection["sha256"],
                    "data_scopes": profile["data_scopes"],
                },
            )
        return connection, capability

    def catalog(self, project_id, policy):
        preview = self.api(f"/projects/{project_id}/catalog-imports/preview", raw=canonical(policy))
        return self.api(
            f"/projects/{project_id}/catalog-imports/{preview['id']}/commit",
            {"confirmed": True, "expected_sha256": preview["sha256"]},
        )["id"]

    def document(self, project_id, name, raw, role="master_spec"):
        return self.api(
            f"/projects/{project_id}/documents?filename={quote(name)}&role={role}", raw=raw
        )["id"]

    def run(self, project_id, request, *, detection_only):
        plan = self.api(f"/projects/{project_id}/runs/preflight", request)
        rid = self.api(
            f"/projects/{project_id}/runs",
            {
                "request": request,
                "expected_plan_sha256": plan["plan_sha256"],
                "idempotency_key": str(uuid.uuid4()),
            },
        )["id"]
        print({"event": "handoff_run_started", "project_id": project_id, "run_id": rid}, flush=True)
        while True:
            run = self.api(f"/projects/{project_id}/runs/{rid}")
            if run["status"] == "waiting_approval":
                checkpoint = run["checkpoint"]
                if detection_only and checkpoint["next_stage"] >= 7:
                    self.api(f"/projects/{project_id}/runs/{rid}/cancel", {})
                    return self.api(f"/projects/{project_id}/runs/{rid}")
                pending = checkpoint.get("pending_call")
                consent = {"expected_checkpoint_sha256": checkpoint["sha256"]}
                if pending:
                    preview = self.api(
                        f"/projects/{project_id}/runs/{rid}/artifacts/{pending['artifact_id']}"
                    )
                    if preview["payload_sha256"] != pending["payload_sha256"]:
                        raise RuntimeError("pending_payload_hash_changed")
                    consent.update(
                        confirmed_call=True,
                        call_preview_id=pending["artifact_id"],
                        expected_call_preview_sha256=pending["sha256"],
                    )
                self.api(f"/projects/{project_id}/runs/{rid}/resume", consent)
            elif run["status"] not in {"queued", "running"}:
                return run
            time.sleep(0.2)

    def artifacts(self, project_id, run_id):
        metadata = self.api(f"/projects/{project_id}/runs/{run_id}/artifacts")
        return metadata, {
            item["id"]: self.api(f"/projects/{project_id}/runs/{run_id}/artifacts/{item['id']}")
            for item in metadata
            if item["kind"]
            in {
                "stage.P1.output",
                "stage.P2.output",
                "model.call_preview",
                "model.request",
                "model.rejection",
            }
        }
