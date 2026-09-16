"""CLI commands for Biosimulant compatibility profiles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

from biosimulant_model_compatibility_standard import (
    CATALOGUE_SHA256,
    CATALOGUE_VERSION,
    STANDARD_ID,
    STANDARD_VERSION,
    get_profile,
    list_profiles,
    profile_digest,
)

from .compatibility import check_compatibility, load_yaml, validate_manifest
from .signals import SignalSpec, validate_port_spec_direction


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="Inspect and validate compatibility profiles.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("standard", help="Show the installed standard and catalogue release")
    commands.add_parser("profiles", help="List installed profiles")
    show = commands.add_parser("show", help="Show one exact profile")
    show.add_argument("profile")
    validate = commands.add_parser("validate", help="Validate compatibility declarations in model.yaml")
    validate.add_argument("manifest", type=Path)
    compare = commands.add_parser("compare", help="Compare one output port with one input port")
    compare.add_argument("source", help="model.yaml#outputs.port_name")
    compare.add_argument("target", help="model.yaml#inputs.port_name")
    compare.add_argument("--sample-json", help="Optional JSON value to check")
    return parser


def _port_reference(value: str) -> tuple[Path, str, str]:
    path_text, marker, fragment = value.partition("#")
    if not marker or "." not in fragment:
        raise ValueError("Point to a port like model.yaml#outputs.value or model.yaml#inputs.value")
    direction, name = fragment.split(".", 1)
    if direction not in {"inputs", "outputs"} or not name:
        raise ValueError("Port references must use #outputs.name or #inputs.name")
    return Path(path_text), direction, name


def _load_port(value: str) -> tuple[SignalSpec, str]:
    path, direction, name = _port_reference(value)
    manifest = load_yaml(path)
    blocked = [item for item in validate_manifest(manifest) if item["level"] == "blocked"]
    if blocked:
        raise ValueError("; ".join(f"{item['path']}: {item['message']}" for item in blocked))
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
            validate_port_spec_direction(spec, direction="output" if direction == "outputs" else "input")
            return spec, direction
    raise ValueError(f"No {direction[:-1]} port named '{name}' in {path}")


def _run(args: argparse.Namespace) -> int:
    if args.command == "standard":
        print(
            _json(
                {
                    "standard": STANDARD_ID,
                    "version": STANDARD_VERSION,
                    "catalogue_package": "biosimulant-model-compatibility-standard",
                    "catalogue_version": CATALOGUE_VERSION,
                    "catalogue_sha256": CATALOGUE_SHA256,
                }
            )
        )
        return 0
    if args.command == "profiles":
        profiles = [
            {
                "ref": f"{profile['profile']['id']}/v{profile['profile']['version']}",
                "title": profile["profile"]["title"],
                "checker": profile["checker"],
            }
            for profile in list_profiles()
        ]
        print(_json({"count": len(profiles), "profiles": profiles}))
        return 0
    if args.command == "show":
        profile = get_profile(args.profile)
        print(_json({"ref": args.profile, "sha256": profile_digest(args.profile), "profile": profile}))
        return 0
    if args.command == "validate":
        findings = validate_manifest(load_yaml(args.manifest))
        blocked = any(item["level"] == "blocked" for item in findings)
        print(_json({"valid": not blocked, "findings": findings}))
        return 2 if blocked else 0
    if args.command == "compare":
        source, source_direction = _load_port(args.source)
        target, target_direction = _load_port(args.target)
        if source_direction != "outputs" or target_direction != "inputs":
            raise ValueError("The source must be an output and the target must be an input")
        sample = NO_SAMPLE
        if args.sample_json is not None:
            try:
                sample = json.loads(args.sample_json)
            except json.JSONDecodeError as exc:
                raise ValueError(f"--sample-json is not valid JSON: {exc.msg}") from exc
        result = check_compatibility(source, target, sample=sample)
        print(_json(result.to_dict()))
        return 0 if result.compatible else 2
    raise AssertionError(f"Unhandled command: {args.command}")


from .compatibility import NO_SAMPLE


def main(argv: list[str] | None = None, *, prog: str = "biosimulant compatibility") -> None:
    parser = _parser(prog)
    try:
        code = _run(parser.parse_args(argv))
    except (KeyError, OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
