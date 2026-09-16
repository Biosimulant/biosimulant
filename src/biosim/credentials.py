# SPDX-FileCopyrightText: 2026-present Biosimulant Team
#
# SPDX-License-Identifier: MIT
"""Registry-scoped credential storage for the headless Biosimulant CLI."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .__about__ import __version__

DEFAULT_REGISTRY = "hub.biosimulant.com"
DEFAULT_HUB_API_BASE = "https://api.biosimulant.com/api"
CLI_USER_AGENT = f"biosimulant-cli/{__version__}"
TOKEN_ENV = "BIOSIMULANT_TOKEN"
WORKSPACE_TOKEN_ENV = "BIOSIMULANT_WORKSPACE_TOKEN"
LEGACY_ACCESS_TOKEN_ENV = "BIOSIMULANT_ACCESS_TOKEN"
LEGACY_REFRESH_TOKEN_ENV = "BIOSIMULANT_REFRESH_TOKEN"
CREDENTIAL_HELPER_ENV = "BIOSIMULANT_CREDENTIAL_HELPER"
CREDENTIALS_FILE_ENV = "BIOSIMULANT_CREDENTIALS_FILE"
DISABLE_KEYRING_ENV = "BIOSIMULANT_DISABLE_KEYRING"
_KEYRING_SERVICE = "biosimulant-registry"
_WORKSPACE_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_EXCHANGE_MAX_ATTEMPTS = 3
_EXCHANGE_RETRY_DELAYS_SECONDS = (0.25, 0.75)
_TRANSIENT_EXCHANGE_STATUSES = frozenset({404, 408, 429})
_JWT_PATTERN = re.compile(
    r"\b[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[^\s,;\"']+")


class CredentialError(RuntimeError):
    """Raised when registry credentials cannot be read or persisted safely."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "credential_error",
        details: dict[str, Any] | None = None,
        exit_code: int = 1,
    ) -> None:
        self.code = code
        self.details = details
        self.exit_code = exit_code
        super().__init__(message)


def normalize_registry_origin(value: str | None) -> str:
    """Return a stable HTTPS registry origin without credentials or a path."""

    raw = (value or DEFAULT_REGISTRY).strip()
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CredentialError(f"Invalid registry origin: {value}")
    if parsed.username or parsed.password:
        raise CredentialError("Registry origins must not contain credentials")
    host = parsed.hostname.lower()
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme.lower()}://{host}"


def credentials_file() -> Path:
    configured = os.environ.get(CREDENTIALS_FILE_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".config" / "biosimulant" / "credentials.json"


def resolve_token(registry: str | None = None) -> str | None:
    """Resolve a token using operation env, helper, keyring, then secure file."""

    value = os.environ.get(TOKEN_ENV, "").strip()
    if value:
        return value
    origin = normalize_registry_origin(registry)
    workspace_token = os.environ.get(WORKSPACE_TOKEN_ENV, "").strip()
    if workspace_token:
        return _exchange_workspace_token(origin, workspace_token)
    if origin == normalize_registry_origin(DEFAULT_REGISTRY):
        legacy_access = os.environ.get(LEGACY_ACCESS_TOKEN_ENV, "").strip()
        if legacy_access:
            return legacy_access
        legacy_refresh = os.environ.get(LEGACY_REFRESH_TOKEN_ENV, "").strip()
        if legacy_refresh:
            return _exchange_legacy_refresh_token(origin, legacy_refresh)
    helper_value = _helper_get(origin)
    if helper_value:
        return helper_value
    keyring_value = _keyring_get(origin)
    if keyring_value:
        return keyring_value
    return _file_credentials().get(origin)


def store_token(registry: str | None, token: str) -> str:
    origin = normalize_registry_origin(registry)
    secret = token.strip()
    if not secret:
        raise CredentialError("Token must not be empty")
    if _helper_store(origin, secret) or _keyring_store(origin, secret):
        return origin
    values = _file_credentials()
    values[origin] = secret
    _write_file_credentials(values)
    return origin


def delete_token(registry: str | None) -> bool:
    origin = normalize_registry_origin(registry)
    removed = _helper_delete(origin) or _keyring_delete(origin)
    values = _file_credentials()
    if origin in values:
        del values[origin]
        _write_file_credentials(values)
        removed = True
    return removed


def credential_status(registry: str | None) -> dict[str, Any]:
    origin = normalize_registry_origin(registry)
    source = None
    if os.environ.get(TOKEN_ENV, "").strip():
        source = "environment"
    elif os.environ.get(WORKSPACE_TOKEN_ENV, "").strip():
        source = "workspace"
    elif (
        origin == normalize_registry_origin(DEFAULT_REGISTRY)
        and os.environ.get(LEGACY_ACCESS_TOKEN_ENV, "").strip()
    ):
        source = "legacy_environment"
    elif _helper_get(origin):
        source = "credential_helper"
    elif _keyring_get(origin):
        source = "keyring"
    elif _file_credentials().get(origin):
        source = "file"
    return {"registry": origin, "authenticated": source is not None, "source": source}


def _file_credentials() -> dict[str, str]:
    path = credentials_file()
    if not path.exists():
        return {}
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if os.name != "nt" and mode & 0o077:
            raise CredentialError(
                f"Credential file permissions are too broad ({mode:o}); run chmod 600 {path}"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
    except CredentialError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialError(f"Could not read credential file: {path}") from exc
    registries = payload.get("registries") if isinstance(payload, dict) else None
    if not isinstance(registries, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in registries.items()
        if isinstance(key, str) and isinstance(value, str) and value
    }


def _write_file_credentials(values: dict[str, str]) -> None:
    path = credentials_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "registries": values}, handle, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _keyring_module() -> Any | None:
    if os.environ.get(DISABLE_KEYRING_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return None
    try:
        import keyring  # type: ignore
    except ImportError:
        return None
    return keyring


def _keyring_get(origin: str) -> str | None:
    module = _keyring_module()
    if module is None:
        return None
    try:
        return module.get_password(_KEYRING_SERVICE, origin)
    except Exception:
        return None


def _keyring_store(origin: str, token: str) -> bool:
    module = _keyring_module()
    if module is None:
        return False
    try:
        module.set_password(_KEYRING_SERVICE, origin, token)
        return True
    except Exception:
        return False


def _keyring_delete(origin: str) -> bool:
    module = _keyring_module()
    if module is None:
        return False
    try:
        module.delete_password(_KEYRING_SERVICE, origin)
        return True
    except Exception:
        return False


def _helper(action: str, origin: str, token: str | None = None) -> subprocess.CompletedProcess[str] | None:
    command = os.environ.get(CREDENTIAL_HELPER_ENV, "").strip()
    if not command:
        return None
    request = {"action": action, "registry": origin}
    if token is not None:
        request["token"] = token
    try:
        return subprocess.run(
            [command],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _helper_get(origin: str) -> str | None:
    completed = _helper("get", origin)
    if completed is None or completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    token = payload.get("token") if isinstance(payload, dict) else None
    return token if isinstance(token, str) and token else None


def _helper_store(origin: str, token: str) -> bool:
    completed = _helper("store", origin, token)
    return completed is not None and completed.returncode == 0


def _helper_delete(origin: str) -> bool:
    completed = _helper("delete", origin)
    return completed is not None and completed.returncode == 0


def _exchange_workspace_token(origin: str, identity_token: str) -> str:
    cached = _WORKSPACE_TOKEN_CACHE.get(origin)
    now = time.monotonic()
    if cached is not None and cached[1] > now:
        return cached[0]
    endpoint = os.environ.get("BIOSIMULANT_WORKSPACE_TOKEN_EXCHANGE_URL", "").strip()
    if not endpoint:
        endpoint = (
            f"{DEFAULT_HUB_API_BASE}/registry/v1/auth/exchange"
            if origin == normalize_registry_origin(DEFAULT_REGISTRY)
            else f"{origin}/api/registry/v1/auth/exchange"
        )
    body = json.dumps({"registry": origin}).encode("utf-8")
    request = Request(endpoint, data=body, method="POST")
    request.add_header("Authorization", f"Bearer {identity_token}")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", CLI_USER_AGENT)
    payload: Any = None
    for attempt in range(1, _EXCHANGE_MAX_ATTEMPTS + 1):
        try:
            with urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except HTTPError as exc:
            status = int(exc.code)
            backend_message = _safe_http_error_message(exc)
            if _is_transient_exchange_status(status):
                if attempt < _EXCHANGE_MAX_ATTEMPTS:
                    time.sleep(_EXCHANGE_RETRY_DELAYS_SECONDS[attempt - 1])
                    continue
                raise CredentialError(
                    "Workspace Registry exchange endpoint is temporarily unavailable",
                    code="workspace_registry_exchange_endpoint_unavailable",
                    details=_exchange_failure_details(
                        category="endpoint_unavailable",
                        status=status,
                        attempts=attempt,
                        backend_message=backend_message,
                    ),
                    exit_code=7,
                ) from exc
            if status in {401, 403}:
                if status == 403 and _is_gateway_client_block(backend_message):
                    raise CredentialError(
                        "Workspace Registry rejected this CLI client — upgrade and reconnect",
                        code="workspace_registry_exchange_client_blocked",
                        details=_exchange_failure_details(
                            category="client_blocked",
                            status=status,
                            attempts=attempt,
                            backend_message=backend_message,
                        ),
                        exit_code=7,
                    ) from exc
                raise CredentialError(
                    "Workspace credentials expired — reconnect to continue",
                    code="workspace_registry_exchange_unauthorized",
                    details=_exchange_failure_details(
                        category="unauthorized",
                        status=status,
                        attempts=attempt,
                        backend_message=backend_message,
                    ),
                    exit_code=3,
                ) from exc
            raise CredentialError(
                "Workspace Registry exchange is misconfigured",
                code="workspace_registry_exchange_configuration_error",
                details=_exchange_failure_details(
                    category="configuration",
                    status=status,
                    attempts=attempt,
                    backend_message=backend_message,
                ),
            ) from exc
        except (URLError, OSError) as exc:
            if attempt < _EXCHANGE_MAX_ATTEMPTS:
                time.sleep(_EXCHANGE_RETRY_DELAYS_SECONDS[attempt - 1])
                continue
            raise CredentialError(
                "Workspace Registry exchange endpoint is temporarily unavailable",
                code="workspace_registry_exchange_endpoint_unavailable",
                details=_exchange_failure_details(
                    category="network",
                    status=None,
                    attempts=attempt,
                ),
                exit_code=7,
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CredentialError(
                "Workspace Registry returned an invalid exchange response",
                code="workspace_registry_exchange_invalid_response",
                details=_exchange_failure_details(
                    category="invalid_response",
                    status=200,
                    attempts=attempt,
                ),
            ) from exc
    access_token = payload.get("accessToken") if isinstance(payload, dict) else None
    expires_in = payload.get("expiresIn", 300) if isinstance(payload, dict) else 300
    if not isinstance(access_token, str) or not access_token:
        raise CredentialError(
            "Workspace Registry returned an invalid exchange response",
            code="workspace_registry_exchange_invalid_response",
            details=_exchange_failure_details(
                category="invalid_response",
                status=200,
                attempts=attempt,
            ),
        )
    try:
        ttl = max(1, min(300, int(expires_in)))
    except (TypeError, ValueError):
        ttl = 300
    _WORKSPACE_TOKEN_CACHE[origin] = (access_token, now + max(1, ttl - 30))
    return access_token


def _is_transient_exchange_status(status: int) -> bool:
    return status in _TRANSIENT_EXCHANGE_STATUSES or status >= 500


def _sanitize_backend_message(value: str) -> str | None:
    text = " ".join(value.split()).strip()
    if not text:
        return None
    text = _BEARER_PATTERN.sub("Bearer [redacted]", text)
    text = _JWT_PATTERN.sub("[redacted-jwt]", text)
    text = re.sub(
        r"(?i)\b(authorization|access[_-]?token|refresh[_-]?token|workspace[_-]?token|token)"
        r"\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        text,
    )
    return text[:240]


def _safe_http_error_message(exc: HTTPError) -> str | None:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except (OSError, AttributeError):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return _sanitize_backend_message(raw)
    candidate: Any = None
    if isinstance(parsed, dict):
        candidate = parsed.get("detail")
        if isinstance(candidate, dict):
            candidate = candidate.get("message") or candidate.get("code")
        if candidate is None and isinstance(parsed.get("error"), dict):
            candidate = parsed["error"].get("message")
    return _sanitize_backend_message(candidate) if isinstance(candidate, str) else None


def _is_gateway_client_block(message: str | None) -> bool:
    normalized = (message or "").lower()
    return any(
        marker in normalized
        for marker in (
            "error code: 1010",
            "browser's signature",
            "browser signature",
            "blocked access based on",
        )
    )


def _exchange_failure_details(
    *,
    category: str,
    status: int | None,
    attempts: int,
    backend_message: str | None = None,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "failureCategory": category,
        "httpStatus": status,
        "attemptCount": attempts,
    }
    if backend_message:
        details["backendMessage"] = backend_message
    return details


def _exchange_legacy_refresh_token(origin: str, refresh_token: str) -> str:
    cache_key = f"{origin}#legacy-refresh"
    cached = _WORKSPACE_TOKEN_CACHE.get(cache_key)
    now = time.monotonic()
    if cached is not None and cached[1] > now:
        return cached[0]
    endpoint = os.environ.get("BIOSIMULANT_TOKEN_ENDPOINT", "").strip()
    if not endpoint:
        endpoint = (
            f"{DEFAULT_HUB_API_BASE}/users/refresh"
            if origin == normalize_registry_origin(DEFAULT_REGISTRY)
            else f"{origin}/api/users/refresh"
        )
    body = json.dumps({"token": refresh_token}).encode("utf-8")
    request = Request(endpoint, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", CLI_USER_AGENT)
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
        raise CredentialError("Legacy Hub token refresh failed") from exc
    access_token = (
        payload.get("accessToken") or payload.get("access_token")
        if isinstance(payload, dict)
        else None
    )
    expires_in = (
        payload.get("expiresIn", payload.get("expires_in", 300))
        if isinstance(payload, dict)
        else 300
    )
    if not isinstance(access_token, str) or not access_token:
        raise CredentialError("Legacy Hub token refresh returned no access token")
    try:
        ttl = max(1, min(3600, int(expires_in)))
    except (TypeError, ValueError):
        ttl = 300
    _WORKSPACE_TOKEN_CACHE[cache_key] = (
        access_token,
        now + max(1, ttl - 30),
    )
    return access_token
