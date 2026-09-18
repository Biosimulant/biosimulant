# SPDX-FileCopyrightText: 2026-present Biosimulant Team
#
# SPDX-License-Identifier: MIT
"""Browser-assisted sign-in for the headless CLI.

The CLI opens a Biosimulant console page, the person approves there, and the
page hands the resulting credential back over a loopback listener this process
owns. The secret never travels in a URL, a redirect, or a server log, and the
CLI never asks anyone to read a token out of a browser and paste it.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlencode, urlsplit

from .credentials import (
    DEFAULT_REGISTRY,
    CredentialError,
    normalize_registry_origin,
)

DEFAULT_WEB_LOGIN_URL = "https://studio.biosimulant.com/auth/cli"
WEB_LOGIN_URL_ENV = "BIOSIMULANT_WEB_LOGIN_URL"
DEFAULT_WEB_LOGIN_SCOPES = ("packages:read", "packages:write")
_MAX_BODY_BYTES = 8192


def web_login_url(registry: str | None) -> str:
    """Return the console page that can issue a credential for this registry."""

    configured = os.environ.get(WEB_LOGIN_URL_ENV, "").strip()
    if configured:
        return configured
    origin = normalize_registry_origin(registry)
    if origin == normalize_registry_origin(DEFAULT_REGISTRY):
        return DEFAULT_WEB_LOGIN_URL
    raise CredentialError(
        f"{origin} has no known browser sign-in page. "
        f"Set {WEB_LOGIN_URL_ENV}, or sign in with a token: "
        "biosimulant auth login --token-stdin",
        code="web_login_unsupported",
        exit_code=2,
    )


class _Result:
    """The single credential this listener is waiting for."""

    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None
        self.error: str | None = None
        self.ready = threading.Event()


class _CallbackHandler(BaseHTTPRequestHandler):
    server_version = "BiosimulantCLI/1.0"

    # Silence the default stderr request log; this process is a CLI, not a server.
    def log_message(self, *args: Any) -> None:
        return

    @property
    def _state(self) -> str:
        return self.server.expected_state  # type: ignore[attr-defined]

    @property
    def _result(self) -> _Result:
        return self.server.result  # type: ignore[attr-defined]

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", self.server.allowed_origin)  # type: ignore[attr-defined]
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")

    def do_OPTIONS(self) -> None:  # noqa: N802 - http.server naming
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        # Reached only if someone opens the callback by hand; say so plainly.
        body = b"Biosimulant CLI sign-in listener. Approve the request in your browser tab."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_BODY_BYTES:
            self._reject(400, "malformed_request")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._reject(400, "malformed_request")
            return
        if not isinstance(payload, dict):
            self._reject(400, "malformed_request")
            return

        # The state proves this POST answers the request this process started.
        # Without it, any page in the browser could feed a token to the CLI.
        supplied = payload.get("state")
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied, self._state):
            self._reject(403, "state_mismatch")
            return
        if self._result.ready.is_set():
            self._reject(409, "already_completed")
            return

        denied = payload.get("error")
        if isinstance(denied, str) and denied:
            self._result.error = denied
            self._finish(200)
            self._result.ready.set()
            return

        token = payload.get("token")
        if not isinstance(token, str) or not token.strip():
            self._reject(400, "missing_token")
            return
        self._result.payload = {
            "token": token.strip(),
            "account": payload.get("account"),
            "scopes": payload.get("scopes"),
        }
        self._finish(200)
        self._result.ready.set()

    def _finish(self, code: int) -> None:
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reject(self, code: int, reason: str) -> None:
        body = json.dumps({"ok": False, "error": reason}).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _host_label() -> str:
    try:
        host = socket.gethostname().split(".")[0]
    except OSError:
        host = "unknown host"
    return f"Biosimulant CLI on {host}"


def browser_login(
    registry: str | None,
    *,
    scopes: tuple[str, ...] = DEFAULT_WEB_LOGIN_SCOPES,
    timeout: float = 300.0,
    open_browser: bool = True,
    on_url: Any = None,
) -> dict[str, Any]:
    """Run a browser sign-in and return the credential the console issued.

    Raises ``CredentialError`` when the person denies the request, the wait
    times out, or the registry has no known console page.
    """

    login_url = web_login_url(registry)
    parts = urlsplit(login_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise CredentialError(
            f"Invalid browser sign-in URL: {login_url}",
            code="web_login_unsupported",
            exit_code=2,
        )
    allowed_origin = f"{parts.scheme}://{parts.netloc}"

    state = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.expected_state = state  # type: ignore[attr-defined]
    server.result = _Result()  # type: ignore[attr-defined]
    server.allowed_origin = allowed_origin  # type: ignore[attr-defined]
    port = server.server_address[1]
    callback = f"http://127.0.0.1:{port}/callback"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        query = urlencode(
            {
                "callback": callback,
                "state": state,
                "label": _host_label(),
                "scopes": ",".join(scopes),
            }
        )
        url = f"{login_url}?{query}"
        if on_url is not None:
            on_url(url)
        if open_browser:
            webbrowser.open(url)
        result: _Result = server.result  # type: ignore[attr-defined]
        if not result.ready.wait(timeout):
            raise CredentialError(
                "Timed out waiting for browser sign-in; nothing was stored.",
                code="web_login_timeout",
                exit_code=1,
            )
        if result.error:
            raise CredentialError(
                f"Browser sign-in was not completed ({result.error}).",
                code="web_login_denied",
                exit_code=3,
            )
        assert result.payload is not None
        return result.payload
    finally:
        server.shutdown()
        server.server_close()
