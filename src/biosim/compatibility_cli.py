"""Commands for inspecting the runtime-owned port compatibility checks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

from .compatibility import (
    check_compatibility,
    load_yaml,
    registered_types,
    validate_manifest,
)
from .signals import SignalSpec, validate_port_spec_direction


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Check the small compatibility declarations on model ports.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="Validate port contracts in model.yaml")
    validate.add_argument("manifest", type=Path)

    compare = commands.add_parser("compare", help="Compare one output port with one input port")
    compare.add_argument("source", help="model.yaml#outputs.port_name")
    compare.add_argument("target", help="model.yaml#inputs.port_name")
    compare.add_argument(
        "--sample-json",
        help="Optional JSON value to run through the type-specific checker",
    )

    commands.add_parser("types", help="List the type-specific checks available now")
    return parser


def _port_reference(value: str) -> tuple[Path, str, str]:
    path_text, marker, fragment = value.partition("#")
    if not marker or "." not in fragment:
        raise ValueError(
            "Point to a port like model.yaml#outputs.molecules or model.yaml#inputs.molecules"
        )
    direction, name = fragment.split(".", 1)
    if direction not in {"inputs", "outputs"} or not name:
        raise ValueError(
            "Port references must use #outputs.name or #inputs.name"
        )
    return Path(path_text), direction, name


def _load_port(value: str) -> tuple[SignalSpec, str]:
    path, direction, name = _port_reference(value)
    manifest = load_yaml(path)
    io = manifest.get("io")
    ports = io.get(direction) if isinstance(io, Mapping) else None
    if not isinstance(ports, list):
        raise ValueError(f"{path} has no io.{direction} list")
    for port in ports:
        if isinstance(port, Mapping) and port.get("name") == name:
            data = dict(port)
            data.pop("name", None)
            if "signal_type" not in data:
                raise ValueError(f"Port '{name}' in {path} does not declare signal_type")
            spec = SignalSpec.from_dict(data)
            validate_port_spec_direction(
                spec,
                direction="output" if direction == "outputs" else "input",
            )
            return spec, direction
    raise ValueError(f"No {direction[:-1]} port named '{name}' in {path}")


def _run(args: argparse.Namespace) -> int:
    if args.command == "validate":
        manifest = load_yaml(args.manifest)
        findings = validate_manifest(manifest)
        print(_json({"valid": not findings, "findings": findings}))
        return 0 if not findings else 2

    if args.command == "types":
        types = registered_types()
        print(_json({"count": len(types), "types": list(types)}))
        return 0

    if args.command == "compare":
        source, source_direction = _load_port(args.source)
        target, target_direction = _load_port(args.target)
        if source_direction != "outputs":
            raise ValueError("The source must be an output port")
        if target_direction != "inputs":
            raise ValueError("The target must be an input port")
        if args.sample_json is None:
            result = check_compatibility(source, target)
        else:
            try:
                sample = json.loads(args.sample_json)
            except json.JSONDecodeError as exc:
                raise ValueError(f"--sample-json is not valid JSON: {exc.msg}") from exc
            result = check_compatibility(source, target, sample=sample)
        print(_json(result.to_dict()))
        return 0 if result.compatible else 2

    raise AssertionError(f"Unhandled command: {args.command}")


def main(argv: list[str] | None = None, *, prog: str = "biosimulant compatibility") -> None:
    parser = _parser(prog)
    try:
        code = _run(parser.parse_args(argv))
    except (OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if code:
        raise SystemExit(code)


if __name__ == "__main__":  # pragma: no cover
    main()
