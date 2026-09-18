"""Sign-in behaviour: verify before storing, and browser-assisted login.

These cover the failure the CLI used to have — accepting any string as a
credential and only failing much later — and the loopback listener that
receives a credential from the browser.
"""
from __future__ import annotations

import io
import json
import threading
import urllib.request
from urllib.error import HTTPError

import pytest

from biosim import credentials, web_login
from biosimulant.__main__ import CliFailure, main


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


IDENTITY = {
    "authenticated": True,
    "authKind": "developer_api_key",
    "userId": "11111111-1111-1111-1111-111111111111",
    "email": "scientist@example.org",
    "scopes": ["packages:read", "packages:write"],
    "canReadPackages": True,
    "canPublishPackages": True,
}


@pytest.fixture()
def isolated_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv(credentials.CREDENTIALS_FILE_ENV, str(tmp_path / "credentials.json"))
    monkeypatch.setenv(credentials.DISABLE_KEYRING_ENV, "1")
    monkeypatch.delenv(credentials.TOKEN_ENV, raising=False)
    return tmp_path / "credentials.json"


def _http_error(status: int) -> HTTPError:
    return HTTPError(
        "https://api.biosimulant.com/api/registry/v1/auth/whoami",
        status,
        "error",
        {},
        io.BytesIO(json.dumps({"detail": "nope"}).encode("utf-8")),
    )


def test_verify_token_reports_the_account(monkeypatch) -> None:
    monkeypatch.setattr(credentials, "urlopen", lambda request, timeout: _Response(IDENTITY))
    result = credentials.verify_token("hub.biosimulant.com", "bsk_live_example")
    assert result["verified"] is True
    assert result["account"] == "scientist@example.org"
    assert result["canPublishPackages"] is True


def test_verify_token_raises_when_the_registry_rejects_it(monkeypatch) -> None:
    def reject(request, timeout):
        raise _http_error(401)

    monkeypatch.setattr(credentials, "urlopen", reject)
    with pytest.raises(credentials.CredentialError) as excinfo:
        credentials.verify_token("hub.biosimulant.com", "not-a-key")
    assert excinfo.value.exit_code == 3


def test_verify_token_degrades_when_the_registry_lacks_the_endpoint(monkeypatch) -> None:
    def missing(request, timeout):
        raise _http_error(404)

    monkeypatch.setattr(credentials, "urlopen", missing)
    result = credentials.verify_token("registry.example.com", "token")
    assert result == {
        "verified": False,
        "reason": "unsupported",
        "registry": "https://registry.example.com",
    }


def test_login_does_not_store_a_rejected_token(monkeypatch, isolated_credentials) -> None:
    def reject(request, timeout):
        raise _http_error(401)

    monkeypatch.setattr(credentials, "urlopen", reject)
    monkeypatch.setattr("sys.stdin", io.StringIO("not-a-key\n"))
    with pytest.raises(SystemExit) as excinfo:
        main(["auth", "login", "--token-stdin"])
    assert excinfo.value.code == 3
    assert not isolated_credentials.exists()


def test_login_stores_a_verified_token(monkeypatch, isolated_credentials, capsys) -> None:
    monkeypatch.setattr(credentials, "urlopen", lambda request, timeout: _Response(IDENTITY))
    monkeypatch.setattr("sys.stdin", io.StringIO("bsk_live_example\n"))
    main(["--json", "auth", "login", "--token-stdin"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["data"]["verified"] is True
    assert payload["data"]["account"] == "scientist@example.org"
    stored = json.loads(isolated_credentials.read_text(encoding="utf-8"))
    assert stored["registries"]["https://hub.biosimulant.com"] == "bsk_live_example"


def test_login_rejects_web_and_token_stdin_together(isolated_credentials) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["auth", "login", "--web", "--token-stdin"])
    assert excinfo.value.code == 2


def test_web_login_url_refuses_an_unknown_registry(monkeypatch) -> None:
    monkeypatch.delenv(web_login.WEB_LOGIN_URL_ENV, raising=False)
    with pytest.raises(credentials.CredentialError) as excinfo:
        web_login.web_login_url("registry.example.com")
    assert excinfo.value.code == "web_login_unsupported"


def _post(url: str, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _run_browser_login(monkeypatch, responder) -> dict[str, object]:
    """Run browser_login with `responder` standing in for the console page."""

    monkeypatch.setenv(web_login.WEB_LOGIN_URL_ENV, "https://console.example.com/auth/cli")
    captured: dict[str, str] = {}

    def on_url(url: str) -> None:
        captured["url"] = url
        threading.Thread(target=responder, args=(url,), daemon=True).start()

    return web_login.browser_login(
        "hub.biosimulant.com",
        timeout=10,
        open_browser=False,
        on_url=on_url,
    )


def test_browser_login_accepts_the_matching_state(monkeypatch) -> None:
    from urllib.parse import parse_qs, urlsplit

    def respond(url: str) -> None:
        query = parse_qs(urlsplit(url).query)
        _post(
            query["callback"][0],
            {"state": query["state"][0], "token": "bsk_live_from_browser"},
        )

    result = _run_browser_login(monkeypatch, respond)
    assert result["token"] == "bsk_live_from_browser"


def test_browser_login_refuses_a_foreign_state(monkeypatch) -> None:
    from urllib.parse import parse_qs, urlsplit

    statuses: list[int] = []

    def respond(url: str) -> None:
        query = parse_qs(urlsplit(url).query)
        # A page that never saw the state must not be able to inject a token.
        status, _ = _post(query["callback"][0], {"state": "guessed", "token": "evil"})
        statuses.append(status)
        _post(query["callback"][0], {"state": query["state"][0], "token": "real"})

    result = _run_browser_login(monkeypatch, respond)
    assert statuses == [403]
    assert result["token"] == "real"


def test_browser_login_surfaces_a_denial(monkeypatch) -> None:
    from urllib.parse import parse_qs, urlsplit

    def respond(url: str) -> None:
        query = parse_qs(urlsplit(url).query)
        _post(query["callback"][0], {"state": query["state"][0], "error": "access_denied"})

    with pytest.raises(credentials.CredentialError) as excinfo:
        _run_browser_login(monkeypatch, respond)
    assert excinfo.value.code == "web_login_denied"


def test_managed_runs_reuse_a_stored_developer_key(monkeypatch, isolated_credentials) -> None:
    """One sign-in should cover the registry and managed runs."""
    from biosimulant.__main__ import _cloud_client

    monkeypatch.delenv("BIOSIMULANT_API_KEY", raising=False)
    monkeypatch.delenv("BIOSIMULANT_API_BASE_URL", raising=False)
    monkeypatch.setattr(credentials, "urlopen", lambda request, timeout: _Response(IDENTITY))
    monkeypatch.setattr("sys.stdin", io.StringIO("bsk_live_example\n"))
    main(["auth", "login", "--token-stdin"])

    with _cloud_client() as client:
        assert client.api_key == "bsk_live_example"


def test_managed_runs_ignore_a_registry_only_token(monkeypatch, isolated_credentials) -> None:
    """A registry operation token is scoped to the registry; never resend it."""
    from biosimulant.__main__ import _cloud_client

    monkeypatch.delenv("BIOSIMULANT_API_KEY", raising=False)
    monkeypatch.delenv("BIOSIMULANT_API_BASE_URL", raising=False)
    credentials.store_token(None, "eyJhbGciOi.registry.token")

    with pytest.raises(CliFailure) as excinfo:
        _cloud_client()
    assert excinfo.value.exit_code == 3


def test_managed_runs_do_not_send_a_hub_key_elsewhere(monkeypatch, isolated_credentials) -> None:
    """A Hub key must not reach a staging or self-hosted API host."""
    from biosimulant.__main__ import _cloud_client

    monkeypatch.delenv("BIOSIMULANT_API_KEY", raising=False)
    monkeypatch.setenv("BIOSIMULANT_API_BASE_URL", "https://staging.example.com/api")
    credentials.store_token(None, "bsk_live_example")

    with pytest.raises(CliFailure) as excinfo:
        _cloud_client()
    assert excinfo.value.exit_code == 3
