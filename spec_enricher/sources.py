"""Explicit local/HTTPS/read-only MCP ingestion; source content stays untrusted."""

from __future__ import annotations

import fnmatch
import os
import stat
import time
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from pydantic import Field, model_validator

from .catalog import canonical
from .connections import ConnectionProfile
from .mcp import MCPBroker
from .network import Destination, Transport, endpoint_parts
from .parser_worker import parse_bounded
from .registry import Registry
from .run_contracts import VersionRef
from .snapshots import sha256
from .stage_contracts import Contract
from .workers import bounded

EXTENSIONS = {".md": "md", ".txt": "md", ".json": "json", ".yaml": "yaml", ".yml": "yml"}
DEFAULT_EXCLUSIONS = [
    ".git",
    ".env*",
    "secrets",
    "*.pem",
    "*.key",
    "id_rsa*",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    "fixtures",
    "qa",
    "expected",
    "oracle",
]


def relative_parts(value: str, *, root=False):
    if value == "." and root:
        return ()
    parts = value.split("/")
    if (
        not value
        or len(value) > 1024
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in parts)
        or PurePosixPath(value).is_absolute()
    ):
        raise ValueError("relative_regular_source_path_required")
    return tuple(parts)


def page_parts(value):
    parts = urlsplit(value)
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    endpoint_parts(clean)
    if any(
        any(
            word in key.lower()
            for word in (
                "token",
                "secret",
                "password",
                "credential",
                "api_key",
                "apikey",
                "signature",
                "authorization",
            )
        )
        for key, _ in parse_qsl(parts.query)
    ):
        raise ValueError("query_credentials_require_secret_reference_header")
    if (
        parts.fragment
        or parts.username
        or parts.password
        or "\\" in value
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        raise ValueError("invalid_source_page")
    return parts, clean


class SourceDefinition(Contract):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["local_folder", "https", "mcp"]
    uri: str = Field(default=".", min_length=1, max_length=2048)
    purpose: Literal["master_spec", "context", "catalog"] = "master_spec"
    catalog_format: Literal["json", "yaml"] = "json"
    priority: int = Field(default=50, ge=0, le=100)
    authority: str = Field(default="user_selected_source", min_length=1, max_length=500)
    freshness: Literal["pinned_manual_refresh"] = "pinned_manual_refresh"
    enabled: bool = False
    required: bool = True
    paths: list[str] = Field(default_factory=list, max_length=1000)
    recursive: bool = False
    exclusions: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUSIONS), max_length=100)
    pages: list[str] = Field(default_factory=list, max_length=100)
    crawl_depth: Literal[0] = 0
    local_addresses: list[str] = Field(default_factory=list, max_length=16)
    auth: Literal["none", "bearer"] = "none"
    secret_ref: str | None = None
    connection: VersionRef | None = None
    tool: str = Field(default="", max_length=200)
    arguments: dict = Field(default_factory=dict)
    timeout: int = Field(default=30, ge=1, le=120)
    max_files: int = Field(default=100, ge=1, le=1000)
    max_bytes: int = Field(default=1024 * 1024, ge=1, le=32 * 1024 * 1024)
    max_total_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=64 * 1024 * 1024)

    @model_validator(mode="after")
    def valid(self):
        if self.purpose != "catalog" and (
            self.max_bytes > 1024**2 or self.max_total_bytes > 10 * 1024**2
        ):
            raise ValueError("documentation_source_size_limits")
        if self.kind == "local_folder":
            relative_parts(self.uri, root=True)
            for path in self.paths:
                relative_parts(path)
            if (
                self.auth != "none"
                or self.secret_ref
                or self.local_addresses
                or self.pages
                or self.connection
                or self.tool
                or self.arguments
            ):
                raise ValueError("local_source_has_no_network_credentials")
        elif self.kind == "https":
            first, _ = page_parts(self.uri)
            if self.paths or self.connection or self.tool or self.arguments:
                raise ValueError("https_source_requires_explicit_pages_only")
            for page in [self.uri, *self.pages]:
                parsed, clean = page_parts(page)
                if (parsed.scheme, parsed.netloc) != (first.scheme, first.netloc):
                    raise ValueError("source_pages_must_share_exact_origin_and_credential_scope")
                # Reuse the same typed IP/auth/header policy as connection adapters.
                ConnectionProfile(
                    name="Source transport validation",
                    endpoint=clean,
                    local_addresses=self.local_addresses,
                    auth=self.auth,
                    secret_ref=self.secret_ref,
                )
        elif (
            self.connection is None
            or not self.tool
            or self.auth != "none"
            or self.secret_ref
            or self.paths
            or self.pages
            or self.local_addresses
        ):
            raise ValueError("mcp_source_requires_pinned_broker_connection_and_tool")
        if any(not value or len(value) > 200 or "\x00" in value for value in self.exclusions):
            raise ValueError("invalid_exclusion")
        return self


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.output = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template", "iframe", "object", "svg", "math"}:
            self.hidden += 1
        if not self.hidden and tag in {
            "p",
            "div",
            "section",
            "article",
            "li",
            "br",
            "h1",
            "h2",
            "h3",
            "h4",
            "pre",
        }:
            self.output.append("\n")

    def handle_endtag(self, tag):
        if (
            tag in {"script", "style", "template", "iframe", "object", "svg", "math"}
            and self.hidden
        ):
            self.hidden -= 1
        if not self.hidden and tag in {
            "p",
            "div",
            "section",
            "article",
            "li",
            "h1",
            "h2",
            "h3",
            "h4",
            "pre",
        }:
            self.output.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.output.append(data)


def html_text(raw: bytes):
    parser = PageText()
    parser.feed(raw.decode("utf-8-sig"))
    lines = [line.strip() for line in "".join(parser.output).splitlines()]
    text = "\n".join(line for line in lines if line).strip() + "\n"
    if not text.strip() or len(text.encode()) > 1024**2:
        raise ValueError("empty_or_oversized_html_text")
    return text.encode()


def open_directory(root: Path, parts):
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_local(root: Path, source: SourceDefinition, *, test=False):
    selected = open_directory(root, relative_parts(source.uri, root=True))
    excluded, files = [], []
    visited = 0
    patterns = source.exclusions + [
        ".git",
        ".env*",
        "secrets",
        "*.pem",
        "*.key",
        "id_rsa*",
        "expected",
        "oracle",
        "fixtures",
        "qa",
    ]

    def omitted(path):
        return any(
            fnmatch.fnmatch(part, pattern) for part in path.split("/") for pattern in patterns
        )

    def visit(directory, prefix, depth):
        nonlocal visited
        if depth > 12:
            raise ValueError("source_directory_depth_limit")
        # Bound directory metadata before sorting or descending.
        with os.scandir(directory) as scan:
            entries = []
            for entry in scan:
                visited += 1
                if visited > 10000:
                    raise ValueError("source_directory_entry_limit")
                entries.append(entry)
        for entry in sorted(entries, key=lambda item: item.name):
            name = prefix + entry.name
            if omitted(name) or entry.is_symlink():
                excluded.append({"path": name, "reason": "exclusion_or_symlink"})
                continue
            if entry.is_dir(follow_symlinks=False):
                if source.recursive:
                    child = os.open(
                        entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                    )
                    try:
                        visit(child, name + "/", depth + 1)
                    finally:
                        os.close(child)
            elif entry.is_file(follow_symlinks=False):
                if Path(name).suffix.lower() in EXTENSIONS:
                    files.append(name)
                    if len(files) > source.max_files:
                        raise ValueError("source_file_count_limit")
                else:
                    excluded.append({"path": name, "reason": "unsupported_extension"})

    try:
        if source.paths:
            files = list(source.paths)
            if len(files) != len(set(files)) or len(files) > source.max_files:
                raise ValueError("duplicate_or_excess_source_paths")
        else:
            visit(selected, "", 0)
        collected, total = [], 0
        for name in files[:1] if test else files:
            parts = relative_parts(name)
            if omitted(name) or Path(name).suffix.lower() not in EXTENSIONS:
                raise ValueError("selected_source_path_excluded")
            directory = os.dup(selected)
            try:
                for part in parts[:-1]:
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                    )
                    os.close(directory)
                    directory = child
                descriptor = os.open(
                    parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                with os.fdopen(descriptor, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > source.max_bytes:
                        raise ValueError("source_file_size_or_type_limit")
                    raw = stream.read(source.max_bytes + 1)
                    if len(raw) > source.max_bytes:
                        raise ValueError("source_file_grew_past_limit")
            finally:
                os.close(directory)
            total += len(raw)
            if total > source.max_total_bytes:
                raise ValueError("source_total_size_limit")
            fmt = EXTENSIONS[Path(name).suffix.lower()]
            collected.append(
                {
                    "uri": source.uri + "/" + name,
                    "name": Path(name).name,
                    "raw": raw,
                    "derived": raw,
                    "format": fmt,
                    "transform": "identity_utf8",
                }
            )
        return collected, {
            "available_files": len(files),
            "excluded": excluded[:100],
            "excluded_count": len(excluded),
            "read_files": len(collected),
        }
    finally:
        os.close(selected)


def _local_worker(root: str, definition: dict, test: bool):
    return read_local(Path(root), SourceDefinition.model_validate(definition), test=test)


def permission(registry, project_id, source_id, revision, *, test=False):
    record = registry.get(project_id, "source", source_id, revision)
    if record is None:
        raise ValueError("source_not_in_project")
    source = SourceDefinition.model_validate(record["content"])
    if (not source.enabled and not test) or "source_queries" not in record["granted_scopes"]:
        raise ValueError("source_read_scope_not_accepted")
    if (
        source.kind == "https"
        and not source.local_addresses
        and not registry.external_egress_allowed()
    ):
        raise ValueError("external_source_egress_disabled")
    return record, source


def collect(registry, secrets, project_id, source_id, revision, source_root, *, test=False):
    deadline = time.monotonic()
    record, source = permission(registry, project_id, source_id, revision, test=test)
    deadline += source.timeout
    if source.kind == "local_folder":
        files, summary = bounded(
            _local_worker, str(source_root), source.model_dump(), test, timeout=source.timeout
        )
    elif source.kind == "https":
        files, total = [], 0
        pages = list(dict.fromkeys([source.uri, *source.pages]))
        if len(pages) > source.max_files:
            raise ValueError("source_page_count_limit")
        for uri in pages[:1] if test else pages:
            permission(registry, project_id, source_id, revision, test=test)
            parts, clean = page_parts(uri)
            headers = {"Accept": "text/html, text/plain, application/json, application/yaml"}
            credential = None
            if source.auth == "bearer":
                credential = secrets.get(project_id, source.secret_ref)
                headers["Authorization"] = "Bearer " + credential
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("source_wall_timeout")
            transport = Transport(
                Destination(clean, tuple(source.local_addresses)),
                timeout=remaining,
                max_bytes=source.max_bytes,
            )
            permission(registry, project_id, source_id, revision, test=test)
            response = transport.request(
                "GET", headers=headers, query=parts.query, preserve_path=True
            )
            used_credentials = (credential,) if credential is not None else ()
            secrets.reject_known(response.body, used_credentials=used_credentials)
            total += len(response.body)
            if total > source.max_total_bytes:
                raise ValueError("source_total_size_limit")
            mime = response.headers.get("content-type", "").split(";")[0].lower()
            html = mime in {"text/html", "application/xhtml+xml"}
            raw = response.body
            derived = bounded(html_text, raw, timeout=5) if html else raw
            extension = Path(parts.path).suffix.lower()
            fmt = EXTENSIONS.get(
                extension,
                "json"
                if mime == "application/json"
                else "yaml"
                if mime in {"application/yaml", "text/yaml"}
                else "md",
            )
            transport_metadata = {
                "etag": response.headers.get("etag"),
                "last_modified": response.headers.get("last-modified"),
                "content_type": response.headers.get("content-type"),
                "http_status": response.status,
                "canonical_uri": uri,
            }
            if any(
                isinstance(value, str) and len(value) > 4096
                for value in transport_metadata.values()
            ):
                raise ValueError("source_header_metadata_limit")
            secrets.reject_known(canonical(transport_metadata), used_credentials=used_credentials)
            files.append(
                {
                    "uri": uri,
                    "name": Path(parts.path).name[:200] or "page.md",
                    "raw": raw,
                    "derived": derived,
                    "format": "md" if html else fmt,
                    "transform": "html_to_inert_text_v1" if html else "identity_utf8",
                    "transport_metadata": transport_metadata,
                }
            )
        summary = {
            "available_files": len(pages),
            "read_files": len(files),
            "crawl_depth": 0,
            "redirects_followed": 0,
        }
    else:
        _, profile = registry.require_egress(
            project_id, source.connection.id, source.connection.revision, {"source_queries"}
        )
        if profile.kind != "mcp":
            raise ValueError("source_requires_read_only_mcp_profile")
        profile = profile.model_copy(update={"timeout": min(profile.timeout, source.timeout)})

        def authorize_mcp():
            permission(registry, project_id, source_id, revision, test=test)
            registry.require_egress(
                project_id, source.connection.id, source.connection.revision, {"source_queries"}
            )

        broker = MCPBroker(project_id, profile, secrets, authorize=authorize_mcp)
        result = broker.call(source.tool, source.arguments)
        text = "\n\n".join(
            item["text"]
            for item in result["result"]["content"]
            if item.get("type") == "text" and isinstance(item.get("text"), str)
        ).encode()
        if (
            not text
            or len(text) > source.max_bytes
            or len(broker.last_raw_response) > source.max_bytes
        ):
            raise ValueError("mcp_source_text_size_or_type")
        files = [
            {
                "uri": profile.endpoint + "#tool=" + source.tool,
                "name": source.name + ".md",
                "raw": broker.last_raw_response,
                "derived": text,
                "format": "md",
                "transform": "mcp_text_content_v1",
                "transport_metadata": {
                    "server_declared_info": broker.server_info,
                    "canonical_uri": profile.endpoint + "#tool=" + source.tool,
                },
            }
        ]
        summary = {
            "available_files": 1,
            "read_files": 1,
            "broker_calls": broker.calls,
            "residual_risk": result["residual_risk"],
        }
    permission(registry, project_id, source_id, revision, test=test)
    if not files:
        raise ValueError("source_returned_no_supported_documents")
    for item in files:
        secrets.reject_known(item["raw"])
        secrets.reject_known(item["derived"])
        if time.monotonic() >= deadline:
            raise ValueError("source_wall_timeout")
        if source.kind == "mcp" and source.purpose == "catalog":
            item["format"] = source.catalog_format
        if item["format"] == "md" and source.purpose != "catalog":
            text = item["derived"].decode("utf-8-sig")
            if "\x00" in text or len(item["derived"]) > 1024**2:
                raise ValueError("invalid_source_text")
            issues = []
        else:
            _, issues = parse_bounded(
                item["derived"], item["format"], catalog=source.purpose == "catalog"
            )
        item["validation_issues"] = issues
    return record, source, files, summary


def ingest(store, record, source, project_id, files):
    registry = Registry(store)
    # Recheck revocation after the in-flight read, before a new active snapshot.
    permission(registry, project_id, record["id"], record["revision"])
    output = []
    for item in files:
        document_id, import_id = None, None
        if source.purpose == "catalog":
            value, issues = parse_bounded(item["derived"], item["format"], catalog=True)
            if not isinstance(value, dict):
                raise ValueError("source_catalog_must_be_mapping")
            import_id = store.stage_catalog(project_id, value, issues)
        else:
            document_id = store.add_document(
                project_id,
                item["name"],
                "baseline",
                item["derived"],
                role=source.purpose,
                fmt=item["format"],
            )
        metadata = {
            "source_config_sha256": record["sha256"],
            "transform": item["transform"],
            "transform_implementation_sha256": sha256(Path(__file__).read_bytes()),
            "derived_sha256": sha256(item["derived"]),
            "format": item["format"],
            "purpose": source.purpose,
            "priority": source.priority,
            "authority": source.authority,
            "freshness": source.freshness,
            "validation_issues": item["validation_issues"],
            "transport_metadata": item.get("transport_metadata", {}),
            "trust": "untrusted_source_data",
            "fetched_at": datetime.now(UTC).isoformat(),
        }
        sid = str(uuid.uuid4())
        with store.connection if not store.connection.in_transaction else nullcontext():
            store.connection.execute(
                "INSERT INTO source_snapshots(project_id,id,source_id,source_revision,uri,raw_content,raw_sha256,document_id,catalog_import_id,metadata) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    project_id,
                    sid,
                    record["id"],
                    record["revision"],
                    item["uri"],
                    item["raw"],
                    sha256(item["raw"]),
                    document_id,
                    import_id,
                    canonical(metadata).decode(),
                ),
            )
        output.append(
            {
                "id": sid,
                "uri": item["uri"],
                "raw_sha256": sha256(item["raw"]),
                "document_id": document_id,
                "catalog_import_id": import_id,
                "metadata": metadata,
            }
        )
    return output
