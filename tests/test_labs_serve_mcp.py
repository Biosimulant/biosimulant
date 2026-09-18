from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from biosim.labs_serve.mcp import PROTOCOL_VERSION
from biosim.labs_serve.server import LabServeSession, RunRecord, create_app
from tests.test_pack import _write_lab

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _client(tmp_path: Path, **kwargs: object) -> tuple[TestClient, LabServeSession]:
    lab = _write_lab(tmp_path / "lab")
    session = LabServeSession(lab, install_deps=False)
    return TestClient(create_app(session, mcp_token=TOKEN, **kwargs)), session


def _rpc(client: TestClient, method: str, params: object = None, *, message_id: int | None = 1):
    body: dict[str, object] = {"jsonrpc": "2.0", "method": method}
    if message_id is not None:
        body["id"] = message_id
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers=AUTH)


def _call(client: TestClient, name: str, arguments: dict[str, object] | None = None):
    response = _rpc(client, "tools/call", {"name": name, "arguments": arguments or {}})
    assert response.status_code == 200
    return response.json()["result"]


def test_initialize_reports_the_protocol_and_server(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    result = _rpc(client, "initialize", {"protocolVersion": PROTOCOL_VERSION}).json()["result"]

    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "biosimulant-lab"
    assert "tools" in result["capabilities"]


def test_tools_list_covers_reading_editing_and_running(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    names = {tool["name"] for tool in _rpc(client, "tools/list").json()["result"]["tools"]}

    assert {"lab_get", "lab_validate", "lab_set_parameters", "run_start", "registry_search"} <= names


def test_lab_get_returns_the_served_lab(tmp_path: Path) -> None:
    client, session = _client(tmp_path)

    result = _call(client, "lab_get")

    assert result["isError"] is False
    text = result["content"][0]["text"]
    assert "Test: Lab" in text
    assert str(session.lab_path) in text


def test_setting_parameters_writes_through_to_the_lab(tmp_path: Path) -> None:
    client, session = _client(tmp_path)

    result = _call(client, "lab_set_parameters", {"alias": "counter", "parameters": {"step": 4}})

    assert result["isError"] is False
    models = session.lab_payload()["manifest"]["models"]
    counter = next(entry for entry in models if entry["alias"] == "counter")
    assert counter["parameters"] == {"step": 4}


def test_a_failing_tool_reports_through_the_result(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    result = _call(client, "lab_set_parameters", {"alias": "missing", "parameters": {}})

    assert result["isError"] is True
    assert "missing" in result["content"][0]["text"]


def test_unknown_tools_list_what_is_available(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    result = _call(client, "nope")

    assert result["isError"] is True
    assert "lab_get" in result["content"][0]["text"]


def test_unknown_methods_are_a_protocol_error(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    payload = _rpc(client, "resources/list").json()

    assert payload["error"]["code"] == -32601


def test_notifications_get_no_body(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    response = _rpc(client, "notifications/initialized", message_id=None)

    assert response.status_code == 202
    assert not response.content


def test_read_only_mode_hides_and_refuses_edits(tmp_path: Path) -> None:
    client, session = _client(tmp_path, mcp_read_only=True)

    names = {tool["name"] for tool in _rpc(client, "tools/list").json()["result"]["tools"]}
    result = _call(client, "lab_set_parameters", {"alias": "counter", "parameters": {"step": 9}})

    assert "lab_get" in names
    assert "lab_set_parameters" not in names
    assert "run_start" not in names
    assert result["isError"] is True
    models = session.lab_payload()["manifest"]["models"]
    counter = next(entry for entry in models if entry["alias"] == "counter")
    assert counter.get("parameters", {}) != {"step": 9}


def test_the_endpoint_needs_the_serve_token(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert response.status_code == 401


def test_a_web_page_cannot_drive_the_lab(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={**AUTH, "Origin": "https://evil.example"},
    )

    assert response.status_code == 403


def test_the_local_page_may_call_the_endpoint(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={**AUTH, "Origin": "http://localhost:8765"},
    )

    assert response.status_code == 200


def test_the_page_can_show_how_to_connect(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)

    payload = client.get("/api/agent").json()["data"]["agent"]

    assert payload["enabled"] is True
    assert payload["token"] == TOKEN
    assert payload["path"] == "/mcp"
    assert "lab_get" in payload["tools"]


def test_serving_without_an_agent_closes_the_endpoint(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, mcp_enabled=False)

    response = _rpc(client, "tools/list")

    assert response.status_code == 404
    assert client.get("/api/agent").json()["data"]["agent"]["enabled"] is False


def test_runs_come_back_newest_first(tmp_path: Path) -> None:
    client, session = _client(tmp_path)
    session._runs = {}
    for run_id, created in (("run-old", "2026-01-01T00:00:00Z"), ("run-new", "2026-09-18T00:00:00Z")):
        record = RunRecord(
            id=run_id,
            lab_id="lab",
            parameters=None,
            simulation_config=None,
            status="completed",
        )
        record.created_at = created
        session._runs[run_id] = record
    # Insertion order puts the old run first, so only sorting can fix this.
    session._runs = dict(reversed(list(session._runs.items())))

    result = _call(client, "runs_list")
    listed = json.loads(result["content"][0]["text"])["runs"]

    assert [run["id"] for run in listed] == ["run-new", "run-old"]
    assert [run["id"] for run in client.get("/api/runs").json()["data"]["runs"]] == [
        "run-new",
        "run-old",
    ]
