"""HTTP egress to an exact configured destination, with DNS-to-socket pinning."""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import ssl
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from urllib.parse import urlsplit

from .inputs import parse

_DNS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="endpoint-dns")
_DNS_SLOTS = threading.BoundedSemaphore(4)


class NetworkError(ValueError):
    def __init__(self, code: str, status: int | None = None, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after


def rpc_id_matches(actual: object, expected: int | None) -> bool:
    """Correlate numeric JSON-RPC IDs without Python's bool/int coercion."""
    return expected is not None and type(actual) in (int, float) and actual == expected


def retry_after_seconds(value: str) -> int | None:
    """Bound decimal delay before integer conversion; other formats use backoff."""
    value = value.strip()
    if not value.isascii() or not value.isdecimal():
        return None
    digits = value.lstrip("0")
    return 60 if len(digits) > 2 else min(int(digits or "0"), 60)


def is_event_stream(content_type: str) -> bool:
    return content_type.partition(";")[0].strip().lower() == "text/event-stream"


def external_egress_denied() -> bool:
    """Operator environment can only narrow saved policy, never grant access."""
    value = os.environ.get("APP_DENY_EXTERNAL_EGRESS", "false").strip().lower()
    if value in {"true", "1"}:
        return True
    if value in {"false", "0"}:
        return False
    raise NetworkError("invalid_operator_egress_policy")


def endpoint_parts(url: str):
    parts = urlsplit(url)
    if (
        parts.scheme not in {"https", "http"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.fragment
        or parts.query
        or "\\" in url
        or any(ord(char) < 33 for char in url)
    ):
        raise NetworkError("invalid_destination")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise NetworkError("invalid_port") from exc
    if not 1 <= port <= 65535:
        raise NetworkError("invalid_port")
    return parts, port


def resolve_host(host: str, port: int, timeout: float = 5) -> list[str]:
    slots = _DNS_SLOTS
    if not slots.acquire(blocking=False):
        raise NetworkError("dns_busy")
    try:
        future = _DNS_POOL.submit(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    except RuntimeError as exc:
        slots.release()
        raise NetworkError("dns_unavailable") from exc
    future.add_done_callback(lambda completed: slots.release())
    try:
        candidates = sorted({str(item[4][0]) for item in future.result(timeout=timeout)})
    except FutureTimeout as exc:
        future.cancel()
        raise NetworkError("dns_timeout") from exc
    except OSError as exc:
        raise NetworkError("dns_failed") from exc
    if not candidates:
        raise NetworkError("dns_failed")
    return candidates


@dataclass(frozen=True)
class Destination:
    url: str
    local_addresses: tuple[str, ...] = ()

    def parts(self):
        parts, port = endpoint_parts(self.url)
        if parts.scheme == "http" and not self.local_addresses:
            raise NetworkError("https_required")
        return parts, port

    def addresses(self, timeout: float = 5) -> list[str]:
        parts, port = self.parts()
        approved = set()
        try:
            for configured_address in self.local_addresses:
                approved.add(ipaddress.ip_address(configured_address))
        except ValueError as exc:
            raise NetworkError("invalid_local_address") from exc
        if external_egress_denied() and (
            not approved or any(address.is_global for address in approved)
        ):
            raise NetworkError("operator_external_egress_denied")
        candidates = resolve_host(parts.hostname, port, timeout)
        for candidate in candidates:
            address = ipaddress.ip_address(candidate)
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
            # Metadata, multicast and unspecified addresses are never allowlisted.
            if (
                address.is_link_local
                or address.is_multicast
                or address.is_unspecified
                or address
                in {
                    ipaddress.ip_address("100.100.100.200"),
                    ipaddress.ip_address("168.63.129.16"),
                    ipaddress.ip_address("fd00:ec2::254"),
                }
            ):
                raise NetworkError("forbidden_address")
            if not address.is_global and address not in approved:
                raise NetworkError("private_address_not_approved")
            if self.local_addresses and address not in approved:
                raise NetworkError("local_address_changed")
        return candidates


class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


class PinnedHTTPS(PinnedHTTP):
    def connect(self):
        raw = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


@dataclass(frozen=True)
class HTTPResult:
    status: int
    headers: dict[str, str]
    body: bytes
    latency_ms: float


class Transport:
    """No proxies, redirects, automatic discovery, fallback, or arbitrary URLs."""

    def __init__(
        self, destination: Destination, *, timeout: float = 30, max_bytes: int = 2 * 1024 * 1024
    ):
        self.destination = destination
        self.timeout = timeout
        self.max_bytes = max_bytes

    def request(
        self,
        method: str,
        suffix: str = "",
        *,
        value: object = None,
        headers: dict[str, str] | None = None,
        rpc_response_id: int | None = None,
        query: str = "",
        preserve_path: bool = False,
    ) -> HTTPResult:
        started = time.monotonic()
        parts, port = self.destination.parts()
        if "#" in query or any(ord(char) < 33 or ord(char) > 126 for char in query):
            raise NetworkError("invalid_query")
        if suffix and (
            not suffix.startswith("/") or "?" in suffix or "#" in suffix or ".." in suffix
        ):
            raise NetworkError("invalid_endpoint_suffix")
        addresses = self.destination.addresses(min(self.timeout, 5))
        remaining = self.timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise NetworkError("request_timeout")
        factory = PinnedHTTPS if parts.scheme == "https" else PinnedHTTP
        connection = factory(parts.hostname, port, addresses[0], remaining)
        body = (
            None
            if value is None
            else json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        )
        outgoing = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if body is not None:
            outgoing["Content-Type"] = "application/json"
        if headers:
            outgoing.update(headers)
        if any("\r" in key + val or "\n" in key + val for key, val in outgoing.items()):
            raise NetworkError("invalid_header")
        expired = threading.Event()
        sockets: list[socket.socket] = []

        def expire():
            # Socket timeouts bound an individual recv, not the whole request:
            # a peer can otherwise drip header/body bytes indefinitely. Retain
            # the socket before getresponse (HTTP/1.0 may clear connection.sock).
            expired.set()
            active = connection.sock or (sockets[0] if sockets else None)
            if active is not None:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        deadline = threading.Timer(max(0, self.timeout - (time.monotonic() - started)), expire)
        deadline.daemon = True
        deadline.start()
        try:
            path = ((parts.path if preserve_path else parts.path.rstrip("/")) or "") + suffix or "/"
            connection.request(
                method, path + ("?" + query if query else ""), body=body, headers=outgoing
            )
            if connection.sock is not None:
                sockets.append(connection.sock)
            if expired.is_set():
                raise NetworkError("request_timeout")
            response = connection.getresponse()
            received = {key.lower(): val for key, val in response.getheaders()}
            if 300 <= response.status < 400:
                raise NetworkError("redirect_forbidden", response.status)
            if response.status in {401, 403}:
                if "resource_metadata" in received.get("www-authenticate", "").lower():
                    raise NetworkError("unsupported_auth", response.status)
                raise NetworkError("auth_failed", response.status)
            if response.status == 429:
                raise NetworkError(
                    "rate_limited",
                    response.status,
                    retry_after_seconds(received.get("retry-after", "")),
                )
            if not 200 <= response.status < 300:
                raise NetworkError("http_error", response.status)
            chunks = bytearray()
            sse = rpc_response_id is not None and is_event_stream(received.get("content-type", ""))
            event_lines: list[bytes] = []
            while True:
                if expired.is_set() or time.monotonic() - started > self.timeout:
                    raise NetworkError("request_timeout")
                chunk = (
                    response.readline(self.max_bytes + 1 - len(chunks))
                    if sse
                    else response.read(min(65536, self.max_bytes + 1 - len(chunks)))
                )
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > self.max_bytes:
                    raise NetworkError("response_too_large")
                if sse:
                    line = chunk.rstrip(b"\r\n")
                    if line.startswith(b"data:"):
                        event_lines.append(line[5:].lstrip(b" "))
                    elif not line and event_lines:
                        try:
                            event = parse(b"\n".join(event_lines), "json", catalog=True)
                        except ValueError:
                            raise NetworkError("invalid_sse_event", response.status) from None
                        event_lines.clear()
                        if (
                            isinstance(event, dict)
                            and rpc_id_matches(event.get("id"), rpc_response_id)
                            and ("result" in event or "error" in event)
                        ):
                            break
            content = bytes(chunks)
            encoding = received.get("content-encoding", "identity").lower()
            if encoding == "gzip":
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                content = decoder.decompress(content, self.max_bytes + 1)
                if len(content) > self.max_bytes or decoder.unconsumed_tail or not decoder.eof:
                    raise NetworkError("decompression_limit")
            elif encoding != "identity":
                raise NetworkError("unsupported_encoding")
            if expired.is_set() or time.monotonic() - started > self.timeout:
                raise NetworkError("request_timeout")
            return HTTPResult(
                response.status, received, content, (time.monotonic() - started) * 1000
            )
        except TimeoutError as exc:
            raise NetworkError("request_timeout") from exc
        except (OSError, http.client.HTTPException, zlib.error) as exc:
            if expired.is_set():
                raise NetworkError("request_timeout") from exc
            raise NetworkError("connect_or_protocol_failed") from exc
        finally:
            deadline.cancel()
            deadline.join()
            connection.close()
