"""A Model Context Protocol server over the lab this process already serves.

`biosimulant labs serve` owns the lab on disk and the local run store, so the
agent talks to the same session the page reads. Edits an agent makes show up in
the page on its next poll, and runs an agent starts appear in its history.

The transport is the JSON-RPC half of MCP's streamable HTTP: one POST carries
one message, and the reply is a plain JSON body. Nothing here streams, so the
server never opens an SSE channel.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..__about__ import __version__
from ..pack import validate_lab_source
from ..registry import PublicRegistryClient
from ..workspace import add_model as workspace_add_model

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters for type checkers
    from .server import LabServeSession

# The revision of MCP this server speaks. Clients that ask for a different one
# are told what they get instead; every client we know of accepts that.
PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "biosimulant-lab"

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    handler: Callable[..., Any]
    properties: dict[str, Any] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    writes: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": self.properties,
                "required": list(self.required),
                "additionalProperties": False,
            },
        }


class ToolError(Exception):
    """A tool failed in a way the agent should see and can act on."""


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{name} must be a non-empty string")
    return value.strip()


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ToolError(f"{name} must be an object")
    return dict(value)


class LabMcpServer:
    """Dispatches MCP messages against one :class:`LabServeSession`."""

    def __init__(self, session: "LabServeSession", *, read_only: bool = False) -> None:
        self.session = session
        self.read_only = read_only
        self._tools: dict[str, Tool] = {}
        for tool in self._build_tools():
            if tool.writes and read_only:
                continue
            self._tools[tool.name] = tool

    # ------------------------------------------------------------------ tools

    def _build_tools(self) -> list[Tool]:
        return [
            Tool(
                name="lab_get",
                title="Read the lab",
                description=(
                    "Return the lab being served: title, path, models with their ports and "
                    "parameters, wiring, world inputs and outputs, and the execution profile."
                ),
                handler=self._lab_get,
            ),
            Tool(
                name="lab_validate",
                title="Validate the lab",
                description="Check the lab on disk and report whether it is valid, with any errors.",
                handler=self._lab_validate,
            ),
            Tool(
                name="lab_set_parameters",
                title="Set model parameters",
                description=(
                    "Replace the parameters of one model in the lab. Pass the alias as it "
                    "appears in lab_get and the full parameter object to store."
                ),
                handler=self._lab_set_parameters,
                properties={
                    "alias": {"type": "string", "description": "Model alias in the lab"},
                    "parameters": {"type": "object", "description": "Parameters to store"},
                },
                required=("alias", "parameters"),
                writes=True,
            ),
            Tool(
                name="lab_set_wiring",
                title="Set the lab wiring",
                description=(
                    "Replace the lab wiring. Each connection names a source port and a target "
                    "port as alias.port, so wiring is a list of {from, to} objects."
                ),
                handler=self._lab_set_wiring,
                properties={
                    "wiring": {
                        "type": "array",
                        "description": "Connections as {from, to} port references",
                        "items": {"type": "object"},
                    }
                },
                required=("wiring",),
                writes=True,
            ),
            Tool(
                name="lab_add_model",
                title="Add a model to the lab",
                description=(
                    "Add a model to the lab from a path inside the lab folder, an absolute "
                    "path, or a registry reference such as namespace/name@version."
                ),
                handler=self._lab_add_model,
                properties={
                    "model": {"type": "string", "description": "Model path or registry reference"},
                    "alias": {"type": "string", "description": "Alias for the model in the lab"},
                },
                required=("model",),
                writes=True,
            ),
            Tool(
                name="run_start",
                title="Start a run",
                description=(
                    "Start a local run of the lab and return the run record. The run keeps "
                    "going after this returns; poll run_get for its status."
                ),
                handler=self._run_start,
                properties={
                    "parameters": {"type": "object", "description": "World input overrides"},
                    "simulation_config": {
                        "type": "object",
                        "description": "Runtime overrides such as duration and communication_step",
                    },
                },
                writes=True,
            ),
            Tool(
                name="runs_list",
                title="List runs",
                description="List the runs recorded for this lab, newest first.",
                handler=self._runs_list,
            ),
            Tool(
                name="run_get",
                title="Inspect a run",
                description="Return one run's status, timing and progress.",
                handler=self._run_get,
                properties={"run_id": {"type": "string"}},
                required=("run_id",),
            ),
            Tool(
                name="run_results",
                title="Read run results",
                description=(
                    "Return the results document of a finished run, including per-connection "
                    "compatibility evidence when the runtime recorded it."
                ),
                handler=self._run_results,
                properties={"run_id": {"type": "string"}},
                required=("run_id",),
            ),
            Tool(
                name="run_logs",
                title="Read run logs",
                description="Return a run's log lines, optionally only those after a sequence number.",
                handler=self._run_logs,
                properties={
                    "run_id": {"type": "string"},
                    "since_seq": {"type": "integer", "description": "Return lines after this seq"},
                },
                required=("run_id",),
            ),
            Tool(
                name="registry_search",
                title="Search the registry",
                description=(
                    "Search the Biosimulant registry for labs and models to add. Works signed "
                    "out; a stored token is used when one exists."
                ),
                handler=self._registry_search,
                properties={
                    "query": {"type": "string"},
                    "page": {"type": "integer"},
                    "page_size": {"type": "integer"},
                },
                required=("query",),
            ),
            Tool(
                name="registry_lab_info",
                title="Inspect a registry lab",
                description="Return registry metadata for one reference such as namespace/name@version.",
                handler=self._registry_lab_info,
                properties={"reference": {"type": "string"}},
                required=("reference",),
            ),
        ]

    # --------------------------------------------------------------- handlers

    def _lab_get(self) -> dict[str, Any]:
        return {"lab": self.session.lab_payload()}

    def _lab_validate(self) -> dict[str, Any]:
        result = validate_lab_source(self.session.lab_path)
        errors = list(getattr(result, "errors", []) or [])
        return {
            "valid": bool(getattr(result, "valid", not errors)),
            "errors": [str(error) for error in errors],
            "lab_path": str(self.session.lab_path),
        }

    def _lab_set_parameters(self, alias: Any = None, parameters: Any = None) -> dict[str, Any]:
        return {
            "lab": self.session.update_model(
                _string(alias, "alias"),
                {"parameters": _mapping(parameters, "parameters")},
            )
        }

    def _lab_set_wiring(self, wiring: Any = None) -> dict[str, Any]:
        if not isinstance(wiring, list):
            raise ToolError("wiring must be a list of connections")
        return {"lab": self.session.update_world({"wiring": wiring})}

    def _lab_add_model(self, model: Any = None, alias: Any = None) -> dict[str, Any]:
        result = workspace_add_model(
            _string(model, "model"),
            lab=self.session.lab_path,
            alias=alias if isinstance(alias, str) and alias.strip() else None,
        )
        return {"added": _as_plain(result), "lab": self.session.lab_payload()}

    def _run_start(self, parameters: Any = None, simulation_config: Any = None) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if parameters is not None:
            body["parameters"] = _mapping(parameters, "parameters")
        if simulation_config is not None:
            body["simulation_config"] = _mapping(simulation_config, "simulation_config")
        return {"run": self.session.create_run(body).to_dict()}

    def _runs_list(self) -> dict[str, Any]:
        return {"runs": self.session.list_runs()}

    def _run_get(self, run_id: Any = None) -> dict[str, Any]:
        return {"run": self.session.get_run(_string(run_id, "run_id")).to_dict()}

    def _run_results(self, run_id: Any = None) -> dict[str, Any]:
        return {"results": self.session.get_run(_string(run_id, "run_id")).results}

    def _run_logs(self, run_id: Any = None, since_seq: Any = None) -> dict[str, Any]:
        logs = self.session.get_run(_string(run_id, "run_id")).logs
        if since_seq is not None:
            try:
                cutoff = int(since_seq)
            except (TypeError, ValueError) as exc:
                raise ToolError("since_seq must be an integer") from exc
            logs = [entry for entry in logs if int(entry.get("seq", 0)) > cutoff]
        return {"logs": logs}

    def _registry_search(self, query: Any = None, page: Any = None, page_size: Any = None) -> Any:
        client = PublicRegistryClient()
        kwargs: dict[str, Any] = {}
        if page is not None:
            kwargs["page"] = int(page)
        if page_size is not None:
            kwargs["page_size"] = int(page_size)
        return {
            "registry_url": client.base_url,
            "result": client.search_labs(_string(query, "query"), **kwargs),
        }

    def _registry_lab_info(self, reference: Any = None) -> Any:
        client = PublicRegistryClient()
        return {"result": client.lab_info(_string(reference, "reference"))}

    # ------------------------------------------------------------- dispatching

    def list_tools(self) -> list[dict[str, Any]]:
        return [tool.describe() for tool in self._tools.values()]

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            known = ", ".join(sorted(self._tools)) or "none"
            return _tool_error(f"Unknown tool: {name}. Available tools: {known}")
        try:
            payload = tool.handler(**dict(arguments or {}))
        except TypeError as exc:
            return _tool_error(f"{name} rejected those arguments: {exc}")
        except Exception as exc:  # Tool failures belong in the result, not the protocol.
            return _tool_error(f"{name} failed: {exc}")
        return {
            "content": [{"type": "text", "text": _dump(payload)}],
            "isError": False,
        }

    def handle(self, message: Any) -> dict[str, Any] | None:
        """Return the JSON-RPC reply for one message, or None for a notification."""

        if not isinstance(message, Mapping):
            return jsonrpc_error(None, JSONRPC_INVALID_REQUEST, "Expected a JSON-RPC object")
        method = message.get("method")
        message_id = message.get("id")
        if not isinstance(method, str):
            return jsonrpc_error(message_id, JSONRPC_INVALID_REQUEST, "Missing method")
        if method.startswith("notifications/"):
            return None
        params = message.get("params")
        params = params if isinstance(params, Mapping) else {}

        if method == "initialize":
            return _result(
                message_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "title": "Biosimulant lab",
                        "version": __version__,
                    },
                    "instructions": (
                        "This server exposes one local Biosimulant lab: the folder the user is "
                        "serving. Read it with lab_get before changing anything, and validate "
                        "after edits. Runs are local to this machine."
                    ),
                },
            )
        if method == "ping":
            return _result(message_id, {})
        if method == "tools/list":
            return _result(message_id, {"tools": self.list_tools()})
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                return jsonrpc_error(message_id, JSONRPC_INVALID_PARAMS, "tools/call needs a tool name")
            arguments = params.get("arguments")
            if arguments is not None and not isinstance(arguments, Mapping):
                return jsonrpc_error(message_id, JSONRPC_INVALID_PARAMS, "arguments must be an object")
            return _result(message_id, self.call_tool(name, arguments))
        return jsonrpc_error(message_id, JSONRPC_METHOD_NOT_FOUND, f"Unsupported method: {method}")


def _result(message_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def jsonrpc_error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _as_plain(value: Any) -> Any:
    """Turn dataclasses and other result objects into JSON-friendly data."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _as_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_as_plain(item) for item in value]
    for attribute in ("to_dict", "_asdict"):
        method = getattr(value, attribute, None)
        if callable(method):
            return _as_plain(method())
    if hasattr(value, "__dict__"):
        return {
            str(key): _as_plain(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


def _dump(payload: Any) -> str:
    return json.dumps(_as_plain(payload), indent=2, sort_keys=False, default=str)
