"""Local session boundary. Source/model content cannot grant permissions."""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .i18n import language, translate


def error(status: int, code: str, message: str, details=None):
    return JSONResponse(
        {
            "code": code,
            "message": message,
            "retryable": False,
            "stage_id": None,
            "details": details or [],
            "correlation_id": secrets.token_hex(8),
        },
        status_code=status,
    )


def create_bootstrap(data_dir: Path) -> str:
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_urlsafe(48)
    path = data_dir / "bootstrap.token"
    temporary = data_dir / (".bootstrap-" + secrets.token_hex(8))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(token)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return token


class Sessions:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "bootstrap.token"
        self.lock = threading.Lock()
        self.sessions: dict[str, tuple[str, float]] = {}

    def bootstrap(self, token: str):
        with self.lock:
            try:
                if self.path.is_symlink() or time.time() - self.path.stat().st_mtime >= 5 * 60:
                    return None
                expected = self.path.read_text()
            except OSError:
                return None
            if not hmac.compare_digest(expected, token):
                return None
            self.path.unlink()
            session_id, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
            self.sessions[session_id] = (csrf, time.monotonic() + 12 * 60 * 60)
            return session_id, csrf

    def get(self, session_id: str):
        with self.lock:
            now = time.monotonic()
            self.sessions = {key: value for key, value in self.sessions.items() if value[1] > now}
            return self.sessions.get(session_id)


class LocalSecurity(BaseHTTPMiddleware):
    def __init__(self, app, *, origin: str, sessions: Sessions, secret_store=None):
        super().__init__(app)
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.path not in {"", "/"}
            or parsed.username
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("APP_ALLOWED_ORIGIN must be an exact loopback origin")
        self.origin = origin.rstrip("/")
        self.host = parsed.netloc
        self.sessions = sessions
        self.secret_store = secret_store
        self.bootstrap_attempts: list[float] = []

    async def dispatch(self, request, call_next):
        selected_language = request.headers.get("x-ui-language", "en")
        token = language.set("ru" if selected_language == "ru" else "en")
        try:
            response = await self.check(request, call_next)
            response.headers["Content-Language"] = language.get()
        finally:
            language.reset(token)
        response.headers.update(
            {
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Cache-Control": "no-store",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            }
        )
        return response

    async def check(self, request, call_next):
        if request.headers.getlist("host") != [self.host]:
            return error(403, "host_forbidden", translate("Недопустимый Host."))
        mutation = request.method not in {"GET", "HEAD", "OPTIONS"}
        if request.headers.get("origin") and request.headers["origin"] != self.origin:
            return error(403, "origin_forbidden", translate("Запрос с другого origin запрещён."))
        if mutation and request.headers.get("origin") != self.origin:
            return error(
                403, "origin_required", translate("Для изменения данных нужен локальный Origin.")
            )
        if request.url.path.startswith("/api/") and request.url.path != "/api/v1/session/bootstrap":
            session = self.sessions.get(request.cookies.get("session", ""))
            if session is None:
                return error(
                    401,
                    "session_required",
                    translate("Откройте одноразовую ссылку локального входа."),
                )
            if mutation and not hmac.compare_digest(
                request.headers.get("x-csrf-token", ""), session[0]
            ):
                return error(
                    403, "csrf_forbidden", translate("CSRF token отсутствует или устарел.")
                )
        if request.url.path == "/api/v1/session/bootstrap" and mutation:
            now = time.monotonic()
            self.bootstrap_attempts = [t for t in self.bootstrap_attempts if now - t < 60]
            if len(self.bootstrap_attempts) >= 10:
                return error(
                    429,
                    "bootstrap_rate_limit",
                    translate("Слишком много попыток входа; повторите через минуту."),
                )
            self.bootstrap_attempts.append(now)
        # Count bytes even when Content-Length is missing or incorrect.
        limit = (
            34 * 1024 * 1024
            if "/catalog-imports" in request.url.path
            or request.url.path == "/api/v1/settings/import/preview"
            else 22 * 1024 * 1024
            if "/documents" in request.url.path
            else 1024 * 1024
        )
        try:
            if int(request.headers.get("content-length", "0")) > limit:
                return error(413, "body_limit", translate("Превышен размер запроса."))
            parts = []
            size = 0
            async for part in request.stream():
                size += len(part)
                if size > limit:
                    return error(413, "body_limit", translate("Превышен размер запроса."))
                parts.append(part)
            request._body = b"".join(parts)
            if (
                self.secret_store is not None
                and mutation
                and not (
                    request.method == "POST"
                    and request.url.path.startswith("/api/v1/projects/")
                    and request.url.path.endswith("/secrets")
                )
            ):
                try:
                    self.secret_store.reject_known(request._body)
                except ValueError:
                    return error(
                        422,
                        "credential_in_content",
                        translate(
                            "Содержимое совпадает с настроенным credential. Уберите ключ из документа или конфигурации; используйте secret reference."
                        ),
                    )
        except ValueError:
            return error(400, "bad_length", translate("Некорректный размер запроса."))
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        # Match JSON MIME variants accepted by the framework, including vendor
        # suffixes. A different header spelling must not bypass the strict parser.
        json_media = (
            not media_type
            or media_type == "application/json"
            or (media_type.startswith("application/") and media_type.endswith("+json"))
        )
        if (
            mutation
            and (request._body or media_type)
            and json_media
            and "/catalog-imports" not in request.url.path
            and "/documents" not in request.url.path
            and request.url.path != "/api/v1/settings/import/preview"
        ):
            from .inputs import InputError, parse

            try:
                parse(request._body, "json")
            except InputError:
                return error(
                    422,
                    "invalid_json",
                    translate("Некорректный JSON или превышены лимиты структуры."),
                )
        return await call_next(request)
