# SPDX-FileCopyrightText: 2025-present Demi <bjaiye1@gmail.com>
#
# SPDX-License-Identifier: MIT
# PYTHON_ARGCOMPLETE_OK
"""Generic CLI runner for Biosimulant simulations.

Run any YAML/TOML config directly without needing a separate demo script.

Usage:
    biosimulant config.yaml                    # Run headless
    biosimulant config.yaml --duration 10.0

YAML config format (simplified):
    meta:
      title: "My Simulation"
      description: "Markdown description here"

    modules:
      my_module:
        class: some_package.CustomModule
        args:
          param: value
    runtime:
      communication_step: 0.1

    wiring:
      - from: module_a.signal
        to: [module_b.input]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import shlex
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from .world import BioWorld

from .__about__ import __version__
from ._starter import write_starter_model
from .labs_serve import serve_lab
from .execution_capabilities import inspect_local_execution_capability
from .compatibility import CompatibilityError
from .managed_runtime import (
    run_labs_serve_with_managed_python,
    run_package_with_managed_python,
)
from .package_repo import build_package_repo, validate_package_repo
from .pack import (
    DEFAULT_PACKAGE_NAMESPACE,
    PACKAGE_EXTENSIONS,
    PackageError,
    build_package,
    fetch_package,
    _local_lab_release_identity,
    _package_slug,
    run_package,
    unpack_package,
    validate_lab_source,
    validate_package,
)
from .run_overrides import apply_run_overrides
from .registry import (
    PublicRegistryClient,
    PackageReference,
    cached_lab_destination_for_reference,
    lab_destination_for_reference,
    parse_package_reference,
)
from .workspace import (
    add_model as workspace_add_model,
    change_model as workspace_change_model,
    create_lab as workspace_create_lab,
    delete_lab as workspace_delete_lab,
    get_lab as workspace_get_lab,
    inspect_owned as workspace_inspect_owned,
    list_labs as workspace_list_labs,
    rename_lab as workspace_rename_lab,
    save_lab as workspace_save_lab,
    vendor_model as workspace_vendor_model,
)


_ARGCOMPLETE_ENV = "_ARGCOMPLETE"
_TOP_LEVEL_COMPLETION_COMMANDS = ("labs",)


def _run_package_for_cli(
    package_file: str | Path,
    *,
    install_deps: bool,
    dependency_root: str | Path | None = None,
) -> dict[str, Any]:
    return run_package_with_managed_python(
        package_file,
        install_deps=install_deps,
        dependency_root=dependency_root,
        in_process_runner=lambda path: run_package(
            path, install_deps=install_deps, dependency_root=dependency_root
        ),
    )


def _labs_serve_child_argv(args: argparse.Namespace, resolved_lab_path: Path) -> list[str]:
    child_argv = [
        "labs",
        "serve",
        str(resolved_lab_path),
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    child_argv.append("--open" if args.open_browser else "--no-open")
    if args.no_install_deps:
        child_argv.append("--no-install-deps")
    if not getattr(args, "agent", True):
        child_argv.append("--no-agent")
    if getattr(args, "agent_read_only", False):
        child_argv.append("--agent-read-only")
    if args.json_output:
        child_argv.append("--json")
    return child_argv


def _is_completion_request() -> bool:
    return bool(os.environ.get(_ARGCOMPLETE_ENV))


def _completion_args_from_env() -> list[str]:
    comp_line = os.environ.get("COMP_LINE", "")
    try:
        comp_point = int(os.environ.get("COMP_POINT", len(comp_line)))
    except ValueError:
        comp_point = len(comp_line)
    comp_fragment = comp_line[: max(0, min(comp_point, len(comp_line)))]
    try:
        words = shlex.split(comp_fragment)
    except ValueError:
        words = comp_fragment.split()
    if len(words) >= 3 and words[1] == "-m" and words[2] in {"biosim", "biosimulant"}:
        return words[3:]
    if not words:
        return []
    return words[1:]


def _path_completer(prefix: str, **kwargs: Any) -> list[str]:
    from argcomplete.completers import FilesCompleter

    return list(FilesCompleter()(prefix, **kwargs) or [])


def _top_level_config_completer(prefix: str, **kwargs: Any) -> list[str]:
    command_matches = [
        f"{command} "
        for command in _TOP_LEVEL_COMPLETION_COMMANDS
        if command.startswith(prefix)
    ]
    return [*command_matches, *_path_completer(prefix, **kwargs)]


def _enable_path_completers(parser: argparse.ArgumentParser) -> None:
    for action in parser._actions:
        if getattr(action, "type", None) is Path and not hasattr(action, "completer"):
            action.completer = _path_completer
        if isinstance(action, argparse._SubParsersAction):
            for subparser in action.choices.values():
                if isinstance(subparser, argparse.ArgumentParser):
                    _enable_path_completers(subparser)


def _autocomplete(parser: argparse.ArgumentParser) -> None:
    if not _is_completion_request():
        return
    try:
        import argcomplete
    except ImportError:
        return
    argcomplete.autocomplete(parser)


def _autocomplete_for_completion_request(prog: str) -> None:
    args = _completion_args_from_env()
    if args and args[0] == "labs":
        _autocomplete(_build_completion_parser(prog=prog))
        return
    _autocomplete(_build_main_parser(prog=prog))


def load_config(path: Path) -> Dict[str, Any]:
    """Load YAML or TOML config file."""
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError:
            print("Error: PyYAML required. Install with: pip install pyyaml", file=sys.stderr)
            sys.exit(1)
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    if suffix in {".toml", ".tml"}:
        try:
            import tomllib  # type: ignore
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore
            except ImportError:
                print(
                    "Error: TOML support requires Python 3.11+ or tomli. Install with: pip install tomli",
                    file=sys.stderr,
                )
                sys.exit(1)
        with path.open("rb") as f:
            return tomllib.load(f)
    print(f"Error: Unsupported config format: {suffix}", file=sys.stderr)
    sys.exit(1)


def create_world(config: Dict[str, Any] | None = None, communication_step: float | None = None) -> "BioWorld":
    import biosim

    config = config or {}
    runtime = config.get("runtime") if isinstance(config.get("runtime"), dict) else {}
    step = communication_step if communication_step is not None else runtime.get("communication_step")
    if step is None:
        raise ValueError("runtime.communication_step is required")
    return biosim.BioWorld(communication_step=float(step))


def run_headless(world: "BioWorld", duration: float) -> None:
    """Run simulation without UI and print results."""
    print(f"Running simulation: duration={duration}")
    print("-" * 40)

    world.run(duration=duration)

    print("Simulation complete.")
    print("-" * 40)

    visuals = world.collect_visuals()
    if visuals:
        print(f"Collected visuals from {len(visuals)} module(s):")
        for entry in visuals:
            module_name = entry.get("module", "unknown")
            vis_list = entry.get("visuals", [])
            for v in vis_list:
                render_type = v.get("render", "unknown")
                print(f"  - {module_name}: {render_type}")
    else:
        print("No visuals collected.")


def _build_main_parser(*, prog: str = "biosimulant") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Run Biosimulant simulations from YAML/TOML config files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  {prog} labs init ./my-lab --name "My Lab"
  {prog} labs validate ./my-lab
  {prog} labs package ./my-lab --out dist/
  {prog} config.yaml --duration 10.0
        """,
    )

    config_action = parser.add_argument(
        "config",
        type=Path,
        help="Path to YAML or TOML config file",
    )
    config_action.completer = _top_level_config_completer
    parser.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="Simulation duration in BioWorld time units (default: 10.0)",
    )
    parser.add_argument(
        "--communication-step",
        type=float,
        default=None,
        help="Override runtime.communication_step from config",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{prog} {__version__}",
    )
    _enable_path_completers(parser)
    return parser


def _build_completion_parser(*, prog: str = "biosimulant") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Run Biosimulant simulations and manage local labs.",
    )
    subparsers = parser.add_subparsers(dest="command")
    labs_parser = subparsers.add_parser(
        "labs",
        help="Initialize, validate, run, and serve local Biosimulant labs",
    )
    _populate_labs_parser(labs_parser)
    return parser


def main(argv: list[str] | None = None, *, prog: str = "biosimulant") -> None:
    if _is_completion_request():
        _autocomplete_for_completion_request(prog)

    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] in {"pack", "packages", "hub", "models"}:
        _removed_command_or_exit(args_list[0], prog=prog, json_output="--json" in args_list)
    if args_list and args_list[0] == "labs":
        _main_labs(args_list[1:], prog=f"{prog} labs")
        return
    if args_list and args_list[0] == "compatibility":
        from .compatibility_cli import main as compatibility_main

        compatibility_main(args_list[1:], prog=f"{prog} compatibility")
        return
    if args_list and args_list[0] in {
        "auth",
        "commands",
        "doctor",
        "runtime",
        "runs",
        "jobs",
        "raw",
        "settings",
        "self",
    }:
        _canonical_cli_required_or_exit(args_list[0], prog=prog, json_output="--json" in args_list)

    parser = _build_main_parser(prog=prog)
    args = parser.parse_args(args_list)

    if not args.config.exists():
        print(f"Error: Config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)

    config = load_config(args.config)

    world = create_world(config, args.communication_step)

    import biosim
    biosim.load_wiring(world, args.config)

    try:
        module_count = len(world.module_names)
    except Exception:
        module_count = 0

    print(f"Loaded config: {args.config}")
    print(f"Modules: {module_count}")

    run_headless(world, duration=args.duration)


def _removed_command_or_exit(command: str, *, prog: str, json_output: bool) -> None:
    replacements = {
        "pack": "Use `biosimulant labs package`, `biosimulant labs validate`, `biosimulant labs run`, or `biosimulant labs pull`.",
        "packages": "Use `biosimulant labs package` or `biosimulant labs release ...`.",
        "hub": "Use object commands such as `biosimulant labs search`, `biosimulant labs info`, `biosimulant labs publish`, or `biosimulant runs remote ...`.",
        "models": "Use lab-scoped model commands: `biosimulant labs add-model`, `biosimulant labs vendor-model`, and `biosimulant labs change-model`.",
    }
    payload = {
        "error": "command_removed",
        "command": command,
        "replacement": replacements[command],
    }
    if json_output:
        print(json_dumps(payload), file=sys.stderr)
    else:
        print(f"Command removed: {prog} {command}", file=sys.stderr)
        print(replacements[command], file=sys.stderr)
    raise SystemExit(2)


def _removed_labs_command_or_exit(command: str, *, prog: str, json_output: bool) -> None:
    replacements = {
        "export": "Use `biosimulant labs package [lab] --out <path>`.",
    }
    payload = {
        "error": "command_removed",
        "command": f"labs {command}",
        "replacement": replacements[command],
    }
    if json_output:
        print(json_dumps(payload), file=sys.stderr)
    else:
        print(f"Command removed: {prog} {command}", file=sys.stderr)
        print(replacements[command], file=sys.stderr)
    raise SystemExit(2)


def _build_labs_parser(*, prog: str = "biosimulant labs") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Initialize, validate, run, and serve local Biosimulant labs.",
    )
    return _populate_labs_parser(parser)


def _populate_labs_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a local runnable lab")
    init_parser.add_argument("path", type=Path, nargs="?", default=Path("."))
    init_parser.add_argument("--name", required=True)
    init_parser.add_argument("--description", default=None)
    init_parser.add_argument("--force", action="store_true")
    init_parser.add_argument("--empty", action="store_true", help="Create only lab.yaml without a starter model")
    init_parser.add_argument("--json", action="store_true", dest="json_output")

    validate_parser = subparsers.add_parser("validate", help="Validate a local lab source tree or .bsilab")
    validate_parser.add_argument("lab", type=Path, nargs="?", default=Path("."))
    validate_parser.add_argument("--json", action="store_true", dest="json_output")

    capabilities_parser = subparsers.add_parser(
        "capabilities", help="Check whether this host can run a lab locally"
    )
    capabilities_lab_action = capabilities_parser.add_argument("lab", nargs="?", default=".")
    capabilities_lab_action.completer = _path_completer
    capabilities_parser.add_argument(
        "--target", type=Path, default=None, help="Destination for auto-pulled registry refs"
    )
    capabilities_parser.add_argument("--force", action="store_true")
    capabilities_parser.add_argument("--registry-url", default=None)
    capabilities_parser.add_argument("--json", action="store_true", dest="json_output")

    run_parser = subparsers.add_parser("run", help="Run a local lab source tree, .bsilab, or registry ref")
    run_lab_action = run_parser.add_argument("lab", nargs="?", default=".")
    run_lab_action.completer = _path_completer
    run_parser.add_argument("--target", type=Path, default=None, help="Destination for auto-pulled registry refs")
    run_parser.add_argument("--force", action="store_true", help="Replace an existing auto-pull target")
    run_parser.add_argument("--registry-url", default=None)
    run_parser.add_argument("--no-install-deps", action="store_true")
    run_parser.add_argument(
        "--dependency-root",
        type=Path,
        default=None,
        help="Writable Lab-local dependency state directory for a .bsilab archive",
    )
    run_parser.add_argument("--no-open", action="store_true", help=argparse.SUPPRESS)
    run_parser.add_argument(
        "--require-local-capability",
        action="store_true",
        help="Fail before execution unless declared CPU, memory, and GPU needs fit this host",
    )
    run_parser.add_argument("--results-file", type=Path, default=None)
    run_parser.add_argument(
        "--report-file",
        type=Path,
        default=None,
        help="Write a standalone HTML run report",
    )
    run_parser.add_argument(
        "--run-input-file",
        type=Path,
        default=None,
        help="JSON object containing parameters and simulation_config overlays",
    )
    run_parser.add_argument("--json", action="store_true", dest="json_output")

    serve_parser = subparsers.add_parser("serve", help="Serve a local lab or registry ref through the local lab UI")
    serve_lab_action = serve_parser.add_argument("lab", nargs="?", default=".")
    serve_lab_action.completer = _path_completer
    serve_parser.add_argument("--target", type=Path, default=None, help="Destination for auto-pulled registry refs")
    serve_parser.add_argument("--force", action="store_true", help="Replace an existing auto-pull target")
    serve_parser.add_argument("--registry-url", default=None)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.set_defaults(open_browser=True)
    serve_parser.add_argument("--open", action="store_true", dest="open_browser", help="Open browser automatically (default)")
    serve_parser.add_argument("--no-open", action="store_false", dest="open_browser", help="Do not open a browser automatically")
    serve_parser.add_argument("--no-install-deps", action="store_true")
    serve_parser.set_defaults(agent=True)
    serve_parser.add_argument(
        "--no-agent",
        action="store_false",
        dest="agent",
        help="Do not expose this lab to a local agent over MCP",
    )
    serve_parser.add_argument(
        "--agent-read-only",
        action="store_true",
        help="Let a connected agent read the lab but not change or run it",
    )
    serve_parser.add_argument("--json", action="store_true", dest="json_output")

    create_parser = subparsers.add_parser("create", help="Create a managed local lab source tree")
    create_parser.add_argument("path", type=Path, nargs="?", default=Path("."))
    create_parser.add_argument("--name", required=True)
    create_parser.add_argument("--description", default=None)
    create_parser.add_argument("--force", action="store_true")
    create_parser.add_argument("--empty", action="store_true")
    create_parser.add_argument("--id", default=None, help=argparse.SUPPRESS)
    create_parser.add_argument("--json", action="store_true", dest="json_output")

    list_parser = subparsers.add_parser("list", help="List local lab source trees under a root")
    list_parser.add_argument("root", type=Path, nargs="?", default=Path("."))
    list_parser.add_argument("--json", action="store_true", dest="json_output")

    get_parser = subparsers.add_parser("get", help="Inspect a local lab source tree")
    get_parser.add_argument("lab", nargs="?", default=".")
    get_parser.add_argument("--root", type=Path, default=Path("."))
    get_parser.add_argument("--json", action="store_true", dest="json_output")

    save_parser = subparsers.add_parser("save", help="Validate and mark a local lab source tree as saved")
    save_parser.add_argument("lab", type=Path, nargs="?", default=Path("."))
    save_parser.add_argument("--root", type=Path, default=Path("."))
    save_parser.add_argument("--manifest-file", type=Path, default=None)
    save_parser.add_argument("--wiring-layout-file", type=Path, default=None)
    save_parser.add_argument("--clear-wiring-layout", action="store_true")
    save_parser.add_argument("--allow-draft", action="store_true", help=argparse.SUPPRESS)
    save_parser.add_argument("--json", action="store_true", dest="json_output")

    rename_parser = subparsers.add_parser("rename", help="Rename a local lab source tree title")
    rename_parser.add_argument("target_or_name")
    rename_parser.add_argument("name", nargs="?")
    rename_parser.add_argument("--root", type=Path, default=Path("."))
    rename_parser.add_argument("--json", action="store_true", dest="json_output")

    delete_parser = subparsers.add_parser("delete", help="Delete a local lab source tree")
    delete_parser.add_argument("lab", type=Path, nargs="?", default=Path("."))
    delete_parser.add_argument("--root", type=Path, default=Path("."))
    delete_parser.add_argument("--yes", action="store_true")
    delete_parser.add_argument("--json", action="store_true", dest="json_output")

    package_parser = subparsers.add_parser("package", help="Package a local lab source tree as a .bsilab")
    package_parser.add_argument("lab", type=Path, nargs="?", default=Path("."))
    package_parser.add_argument("--out", type=Path, default=None)
    package_parser.add_argument("--package", dest="package_name", type=str, default=None)
    package_parser.add_argument("--version", type=str, default=None)
    package_parser.add_argument("--visibility", choices=("private", "public"), default="private")
    package_parser.add_argument("--vendor-dependencies", action="store_true")
    package_parser.add_argument("--registry-url", default=None)
    package_parser.add_argument("--json", action="store_true", dest="json_output")

    release_parser = subparsers.add_parser("release", help="Validate, build, and publish lab release manifests")
    release_subparsers = release_parser.add_subparsers(dest="release_command", required=True)
    release_validate_parser = release_subparsers.add_parser("validate", help="Validate a lab release manifest")
    release_validate_parser.add_argument("manifest", type=Path)
    release_validate_parser.add_argument("--json", action="store_true", dest="json_output")
    release_build_parser = release_subparsers.add_parser("build", help="Build packages from a lab release manifest")
    release_build_parser.add_argument("manifest", type=Path)
    release_build_parser.add_argument("--out", type=Path, default=Path("dist/biosimulant-packages"))
    release_build_parser.add_argument("--json", action="store_true", dest="json_output")
    release_publish_parser = release_subparsers.add_parser("publish", help="Publish a lab release manifest")
    release_publish_parser.add_argument("compatibility_args", nargs=argparse.REMAINDER)
    release_publish_parser.set_defaults(canonical_command_path="labs release publish")
    release_ci_parser = release_subparsers.add_parser("ci", help="Run lab release CI")
    release_ci_parser.add_argument("compatibility_args", nargs=argparse.REMAINDER)
    release_ci_parser.set_defaults(canonical_command_path="labs release ci")

    search_parser = subparsers.add_parser("search", help="Search public registry labs")
    search_parser.add_argument("query", nargs="?")
    search_parser.add_argument("--page", type=int, default=1)
    search_parser.add_argument("--page-size", type=int, default=20)
    search_parser.add_argument("--tags", action="append", default=[])
    search_parser.add_argument("--registry-url", default=None)
    search_parser.add_argument("--json", action="store_true", dest="json_output")

    info_parser = subparsers.add_parser("info", help="Inspect a public registry lab or lab package ref")
    info_parser.add_argument("reference")
    info_parser.add_argument("--registry-url", default=None)
    info_parser.add_argument("--json", action="store_true", dest="json_output")

    versions_parser = subparsers.add_parser("versions", help="List public registry versions for a lab")
    versions_parser.add_argument("reference")
    versions_parser.add_argument("--page", type=int, default=1)
    versions_parser.add_argument("--page-size", type=int, default=20)
    versions_parser.add_argument("--registry-url", default=None)
    versions_parser.add_argument("--json", action="store_true", dest="json_output")

    pull_parser = subparsers.add_parser("pull", help="Pull a public lab package into a local source tree")
    pull_parser.add_argument("reference")
    pull_parser.add_argument("--target", type=Path, default=None)
    pull_parser.add_argument("--force", action="store_true")
    pull_parser.add_argument("--no-deps", action="store_true")
    pull_parser.add_argument("--registry-url", default=None)
    pull_parser.add_argument("--json", action="store_true", dest="json_output")

    add_model_parser = subparsers.add_parser("add-model", help="Add a lab-local model source tree")
    add_model_parser.add_argument("model", type=Path)
    add_model_parser.add_argument("--lab", type=Path, default=Path("."))
    add_model_parser.add_argument("--root", type=Path, default=Path("."))
    add_model_parser.add_argument("--alias", default=None)
    add_model_parser.add_argument("--json", action="store_true", dest="json_output")

    change_model_parser = subparsers.add_parser("change-model", help="Replace the path for a lab model alias")
    change_model_parser.add_argument("alias")
    change_model_parser.add_argument("model", type=Path)
    change_model_parser.add_argument("--lab", type=Path, default=Path("."))
    change_model_parser.add_argument("--root", type=Path, default=Path("."))
    change_model_parser.add_argument("--json", action="store_true", dest="json_output")

    vendor_model_parser = subparsers.add_parser("vendor-model", help="Copy a local model source tree into a lab")
    vendor_model_parser.add_argument("model", type=Path)
    vendor_model_parser.add_argument("--lab", type=Path, default=Path("."))
    vendor_model_parser.add_argument("--root", type=Path, default=Path("."))
    vendor_model_parser.add_argument("--alias", default=None)
    vendor_model_parser.add_argument("--replace", action="store_true")
    vendor_model_parser.add_argument("--json", action="store_true", dest="json_output")

    inspect_owned_parser = subparsers.add_parser("inspect-owned", help="Inspect lab-local model ownership")
    inspect_owned_parser.add_argument("lab", type=Path, nargs="?", default=Path("."))
    inspect_owned_parser.add_argument("--root", type=Path, default=Path("."))
    inspect_owned_parser.add_argument("--json", action="store_true", dest="json_output")

    for name in ("import", "open", "publish", "sync-status"):
        _add_compatibility_subcommand(subparsers, name, f"labs {name}")

    _enable_path_completers(parser)
    return parser


def _main_labs(argv: list[str], *, prog: str = "biosimulant labs") -> None:
    if argv and argv[0] == "export":
        _removed_labs_command_or_exit("export", prog=prog, json_output="--json" in argv)

    parser = _build_labs_parser(prog=prog)
    _autocomplete(parser)
    args = parser.parse_args(argv)
    if canonical_command := getattr(args, "canonical_command_path", None):
        _canonical_cli_required_or_exit(
            canonical_command,
            prog=prog,
            json_output="--json" in argv,
        )
        return

    try:
        if args.command == "init":
            payload = _init_lab_project(
                args.path,
                name=args.name,
                description=args.description,
                force=args.force,
                empty=args.empty,
            )
            _print_lab_init_success(payload, json_output=args.json_output)
            return
        if args.command == "create":
            payload = workspace_create_lab(
                args.path,
                name=args.name,
                description=args.description,
                force=args.force,
                empty=args.empty,
                local_id=args.id,
            )
            _print_workspace_result(payload, json_output=args.json_output)
            return
        if args.command == "list":
            payload = {
                "command": "labs.list",
                "root": str(args.root.expanduser().resolve()),
                "labs": workspace_list_labs(args.root),
            }
            _print_workspace_result(payload, json_output=args.json_output)
            return
        if args.command == "get":
            payload = {
                "command": "labs.get",
                "lab": workspace_get_lab(args.lab, root=args.root).to_dict(),
            }
            _print_workspace_result(payload, json_output=args.json_output)
            return
        if args.command == "save":
            save_kwargs: dict[str, Any] = {}
            if args.manifest_file is not None:
                manifest = _load_structured_file(args.manifest_file)
                if not isinstance(manifest, dict):
                    raise PackageError("Lab manifest file must contain a mapping")
                save_kwargs["manifest"] = manifest
            if args.wiring_layout_file is not None:
                save_kwargs["wiring_layout"] = _load_structured_file(
                    args.wiring_layout_file
                )
            elif args.clear_wiring_layout:
                save_kwargs["wiring_layout"] = None
            _print_workspace_result(
                workspace_save_lab(
                    args.lab,
                    root=args.root,
                    allow_draft=args.allow_draft,
                    **save_kwargs,
                ),
                json_output=args.json_output,
            )
            return
        if args.command == "rename":
            target, name = _parse_lab_rename_args(args.target_or_name, args.name)
            _print_workspace_result(
                workspace_rename_lab(target, name=name, root=args.root),
                json_output=args.json_output,
            )
            return
        if args.command == "delete":
            _print_workspace_result(
                workspace_delete_lab(args.lab, yes=args.yes, root=args.root),
                json_output=args.json_output,
            )
            return
        if args.command == "package":
            payload = _package_lab_source(
                args.lab,
                output=args.out,
                package_name=args.package_name,
                version=args.version,
                visibility=args.visibility,
                vendor_dependencies=args.vendor_dependencies,
                registry_url=args.registry_url,
            )
            _print_lab_package_result(payload, json_output=args.json_output)
            return
        if args.command == "release":
            if args.release_command == "validate":
                manifest = validate_package_repo(args.manifest)
                _print_package_repo_validation_success(manifest, json_output=args.json_output)
                return
            if args.release_command == "build":
                built = build_package_repo(args.manifest, args.out)
                _print_package_repo_build_success(built, json_output=args.json_output)
                return
        if args.command == "search":
            payload = {
                "command": "labs.search",
                "registry_url": PublicRegistryClient(args.registry_url).base_url,
                "result": PublicRegistryClient(args.registry_url).search_labs(
                    args.query,
                    page=args.page,
                    page_size=args.page_size,
                    tags=args.tags,
                ),
            }
            _print_registry_result(payload, json_output=args.json_output)
            return
        if args.command == "info":
            client = _registry_client_for_reference(args.reference, args.registry_url)
            payload = {
                "command": "labs.info",
                "registry_url": client.base_url,
                "reference": args.reference,
                "result": client.lab_info(args.reference),
            }
            _print_registry_result(payload, json_output=args.json_output)
            return
        if args.command == "versions":
            client = _registry_client_for_reference(args.reference, args.registry_url)
            payload = {
                "command": "labs.versions",
                "registry_url": client.base_url,
                "reference": args.reference,
                "result": client.lab_versions(
                    args.reference,
                    page=args.page,
                    page_size=args.page_size,
                ),
            }
            _print_registry_result(payload, json_output=args.json_output)
            return
        if args.command == "pull":
            payload = _pull_public_lab(
                args.reference,
                target=args.target,
                force=args.force,
                registry_url=args.registry_url,
                emit_status=not args.json_output,
            )
            _print_registry_result(payload, json_output=args.json_output)
            return
        if args.command == "add-model":
            _print_workspace_result(
                workspace_add_model(
                    args.model,
                    lab=args.lab,
                    alias=args.alias,
                    root=args.root,
                ),
                json_output=args.json_output,
            )
            return
        if args.command == "change-model":
            _print_workspace_result(
                workspace_change_model(
                    args.alias,
                    args.model,
                    lab=args.lab,
                    root=args.root,
                ),
                json_output=args.json_output,
            )
            return
        if args.command == "vendor-model":
            _print_workspace_result(
                workspace_vendor_model(
                    args.model,
                    lab=args.lab,
                    alias=args.alias,
                    replace=args.replace,
                    root=args.root,
                ),
                json_output=args.json_output,
            )
            return
        if args.command == "inspect-owned":
            _print_workspace_result(
                workspace_inspect_owned(args.lab, root=args.root),
                json_output=args.json_output,
            )
            return
        if args.command == "validate":
            result = _validate_local_lab(args.lab)
            if not result.valid:
                _print_validation_failure(args.lab, result, json_output=args.json_output)
                raise SystemExit(1)
            _print_lab_validation_success(args.lab, result, json_output=args.json_output)
            return
        if args.command == "capabilities":
            lab_path, _pull = _resolve_runtime_lab_path(
                args.lab,
                target=args.target,
                force=args.force,
                registry_url=args.registry_url,
                emit_status=not args.json_output,
            )
            result = _validate_local_lab(lab_path)
            if not result.valid:
                _print_validation_failure(lab_path, result, json_output=args.json_output)
                raise SystemExit(1)
            capability = inspect_local_execution_capability(lab_path)
            _print_local_execution_capability(capability, json_output=args.json_output)
            return
        if args.command == "run":
            lab_path, _pull = _resolve_runtime_lab_path(
                args.lab,
                target=args.target,
                force=args.force,
                registry_url=args.registry_url,
                emit_status=not args.json_output,
            )
            with _lab_path_with_run_inputs(
                lab_path,
                args.run_input_file,
            ) as runnable_lab_path:
                local_capability = None
                if args.require_local_capability:
                    local_capability = inspect_local_execution_capability(runnable_lab_path)
                    if not local_capability["local_supported"]:
                        reasons = "; ".join(
                            str(item.get("message") or item.get("code"))
                            for item in local_capability["blockers"]
                        )
                        raise PackageError(f"Local execution capability check failed: {reasons}")
                with _package_file_for_lab(runnable_lab_path) as package_file:
                    run_kwargs: dict[str, Any] = {
                        "install_deps": not args.no_install_deps,
                    }
                    if args.dependency_root is not None:
                        run_kwargs["dependency_root"] = args.dependency_root
                    result = _run_package_for_cli(package_file, **run_kwargs)
                    if local_capability is not None:
                        result = {**result, "local_execution": local_capability}
                    if args.report_file:
                        report_path = args.report_file.expanduser().resolve()
                        result = {**result, "report_file": str(report_path)}
                        _write_run_report(report_path, package_file, result)
                    if args.results_file:
                        args.results_file.parent.mkdir(parents=True, exist_ok=True)
                        args.results_file.write_text(
                            json_dumps(result) + "\n",
                            encoding="utf-8",
                        )
                    _print_run_result(
                        package_file,
                        result,
                        json_output=args.json_output,
                    )
            return
        if args.command == "serve":
            lab_path, _pull = _resolve_runtime_lab_path(
                args.lab,
                target=args.target,
                force=args.force,
                registry_url=args.registry_url,
                emit_status=not args.json_output,
            )
            resolved_lab_path = lab_path.expanduser().resolve()
            with _package_file_for_lab(resolved_lab_path) as package_file:
                managed_exit = run_labs_serve_with_managed_python(
                    package_file,
                    _labs_serve_child_argv(args, resolved_lab_path),
                )
                if managed_exit is not None:
                    if managed_exit != 0:
                        raise SystemExit(managed_exit)
                    return
            if resolved_lab_path.is_file():
                with tempfile.TemporaryDirectory(prefix="biosim-serve-package-") as temp_dir:
                    unpacked = unpack_package(resolved_lab_path, dest=Path(temp_dir) / "unpacked")
                    serve_lab(
                        unpacked / "payload",
                        host=args.host,
                        port=args.port,
                        open_browser=args.open_browser,
                        install_deps=not args.no_install_deps,
                        emit_json=args.json_output,
                        agent=args.agent,
                        agent_read_only=args.agent_read_only,
                    )
            else:
                serve_lab(
                    resolved_lab_path,
                    host=args.host,
                    port=args.port,
                    open_browser=args.open_browser,
                    install_deps=not args.no_install_deps,
                    emit_json=args.json_output,
                    agent=args.agent,
                    agent_read_only=args.agent_read_only,
                )
            return
    except CompatibilityError as exc:
        if args.command == "run" and getattr(args, "results_file", None):
            _write_compatibility_failure_results(args.results_file, exc)
        _print_compatibility_error(exc, json_output=getattr(args, "json_output", False))
        raise SystemExit(2) from exc
    except PackageError as exc:
        _print_pack_error(exc, json_output=getattr(args, "json_output", False))
        raise SystemExit(1) from exc


def _main_packages(argv: list[str], *, prog: str = "biosimulant packages") -> None:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Validate/build package repositories and run local package archives.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="Validate a package repo manifest or package archive")
    validate_parser.add_argument("target", type=Path)
    validate_parser.add_argument("--json", action="store_true", dest="json_output")

    build_parser = subparsers.add_parser("build", help="Build packages declared in a repo manifest")
    build_parser.add_argument("manifest", type=Path)
    build_parser.add_argument("--out", type=Path, default=Path("dist/biosimulant-packages"))
    build_parser.add_argument("--json", action="store_true", dest="json_output")

    run_parser = subparsers.add_parser("run", help="Run a local .bsimodel or .bsilab package")
    run_parser.add_argument("package_file", type=Path)
    run_parser.add_argument("--no-install-deps", action="store_true")
    run_parser.add_argument("--dependency-root", type=Path, default=None)
    run_parser.add_argument("--json", action="store_true", dest="json_output")

    for name in ("preview", "import", "export-model", "export-lab", "publish", "ci"):
        _add_compatibility_subcommand(subparsers, name, f"packages {name}")

    args = parser.parse_args(argv)
    if canonical_command := getattr(args, "canonical_command_path", None):
        _canonical_cli_required_or_exit(
            canonical_command,
            prog=prog,
            json_output="--json" in argv,
        )
        return

    try:
        if args.command == "validate":
            if args.target.suffix in PACKAGE_EXTENSIONS:
                result = validate_package(args.target)
                if not result.valid:
                    _print_validation_failure(args.target, result, json_output=args.json_output)
                    raise SystemExit(1)
                _print_validation_success(args.target, result, json_output=args.json_output)
                return
            manifest = validate_package_repo(args.target)
            _print_package_repo_validation_success(manifest, json_output=args.json_output)
            return
        if args.command == "build":
            built = build_package_repo(args.manifest, args.out)
            _print_package_repo_build_success(built, json_output=args.json_output)
            return
        if args.command == "run":
            run_kwargs: dict[str, Any] = {"install_deps": not args.no_install_deps}
            if args.dependency_root is not None:
                run_kwargs["dependency_root"] = args.dependency_root
            result = _run_package_for_cli(args.package_file, **run_kwargs)
            _print_run_result(args.package_file, result, json_output=args.json_output)
            return
    except CompatibilityError as exc:
        _print_compatibility_error(exc, json_output=getattr(args, "json_output", False))
        raise SystemExit(2) from exc
    except PackageError as exc:
        _print_pack_error(exc, json_output=getattr(args, "json_output", False))
        raise SystemExit(1) from exc


def _main_pack(argv: list[str], *, prog: str = "biosimulant pack") -> None:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Build, validate, fetch, and run Biosimulant package files.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print machine-readable JSON output instead of human-readable summaries",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Build a model or self-contained lab package")
    build_parser.add_argument("source", type=Path)
    build_parser.add_argument("--out", type=Path, default=None)
    build_parser.add_argument("--package", dest="package_name", type=str, default=None)
    build_parser.add_argument("--version", type=str, default="0.1.0")
    build_parser.add_argument("--visibility", type=str, default="private")
    build_parser.add_argument("--vendor-dependencies", action="store_true")
    build_parser.add_argument("--registry-url", default=None)

    validate_parser = subparsers.add_parser("validate", help="Validate a model or lab package")
    validate_parser.add_argument("package_file", type=Path)

    fetch_parser = subparsers.add_parser("fetch", help="Fetch a package into the local cache")
    fetch_parser.add_argument("reference", type=str, help="Reference in package@version form")

    run_parser = subparsers.add_parser("run", help="Run a model or lab package")
    run_parser.add_argument("package_file", type=Path)
    run_parser.add_argument("--no-install-deps", action="store_true")
    run_parser.add_argument("--dependency-root", type=Path, default=None)

    args = parser.parse_args(argv)

    try:
        if args.command == "build":
            target = build_package(
                args.source,
                output_path=args.out,
                package_name=args.package_name,
                version=args.version,
                visibility=args.visibility,
                vendor_dependencies=args.vendor_dependencies,
                registry_url=args.registry_url,
            )
            validation = validate_package(target)
            _print_pack_result(
                args.json_output,
                {
                    "command": "build",
                    "package_file": str(target),
                    "valid": validation.valid,
                    "package": validation.metadata.get("package") if validation.metadata else None,
                    "version": validation.metadata.get("version") if validation.metadata else None,
                    "package_type": validation.metadata.get("package_type") if validation.metadata else None,
                    "warnings": validation.warnings,
                },
            )
            return
        if args.command == "validate":
            result = validate_package(args.package_file)
            if not result.valid:
                _print_validation_failure(args.package_file, result, json_output=args.json_output)
                raise SystemExit(1)
            _print_validation_success(args.package_file, result, json_output=args.json_output)
            return
        if args.command == "fetch":
            package_name, version = _parse_package_reference(args.reference)
            target = fetch_package(package_name, version)
            _print_pack_result(
                args.json_output,
                {
                    "command": "fetch",
                    "package": package_name,
                    "version": version,
                    "package_file": str(target),
                },
            )
            return
        if args.command == "run":
            run_kwargs: dict[str, Any] = {"install_deps": not args.no_install_deps}
            if args.dependency_root is not None:
                run_kwargs["dependency_root"] = args.dependency_root
            result = _run_package_for_cli(args.package_file, **run_kwargs)
            _print_run_result(args.package_file, result, json_output=args.json_output)
            return
    except CompatibilityError as exc:
        _print_compatibility_error(exc, json_output=args.json_output)
        raise SystemExit(2) from exc
    except PackageError as exc:
        _print_pack_error(exc, json_output=args.json_output)
        raise SystemExit(1) from exc


def _parse_package_reference(value: str) -> tuple[str, str]:
    package_name, sep, version = value.rpartition("@")
    if not sep or not package_name.strip() or not version.strip():
        raise PackageError("Package reference must be in package@version form")
    return package_name.strip(), version.strip()


def _package_lab_source(
    lab: Path,
    *,
    output: Path | None,
    package_name: str | None,
    version: str | None,
    visibility: str,
    vendor_dependencies: bool = False,
    registry_url: str | None = None,
) -> dict[str, Any]:
    target = _resolve_lab_package_target(
        lab,
        output=output,
        package_name=package_name,
        version=version,
        visibility=visibility,
        vendor_dependencies=vendor_dependencies,
        registry_url=registry_url,
    )
    validation = validate_package(target)
    if not validation.valid or not validation.metadata:
        raise PackageError("; ".join(validation.errors))
    if validation.metadata.get("package_type") != "lab":
        raise PackageError("labs package can only package lab source trees")
    return {
        "command": "labs.package",
        "package_file": str(target),
        "valid": True,
        "package": validation.metadata.get("package"),
        "version": validation.metadata.get("version"),
        "package_type": validation.metadata.get("package_type"),
        "warnings": validation.warnings,
        "metadata": validation.metadata,
    }


def _resolve_lab_package_target(
    lab: Path,
    *,
    output: Path | None,
    package_name: str | None,
    version: str | None,
    visibility: str,
    vendor_dependencies: bool = False,
    registry_url: str | None = None,
) -> Path:
    if output is None or output.suffix == ".bsilab":
        return build_package(
            lab,
            output_path=output,
            package_name=package_name,
            version=version,
            visibility=visibility,
            vendor_dependencies=vendor_dependencies,
            registry_url=registry_url,
        )

    output_dir = output.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="biosim-lab-package-") as temp_dir:
        temp_target = Path(temp_dir) / "package.bsilab"
        built = build_package(
            lab,
            output_path=temp_target,
            package_name=package_name,
            version=version,
            visibility=visibility,
            vendor_dependencies=vendor_dependencies,
            registry_url=registry_url,
        )
        validation = validate_package(built)
        if not validation.valid or not validation.metadata:
            raise PackageError("; ".join(validation.errors))
        if validation.metadata.get("package_type") != "lab":
            raise PackageError("labs package can only package lab source trees")
        target = output_dir / (
            f"{_package_slug(str(validation.metadata['package']))}-"
            f"{validation.metadata['version']}.bsilab"
        )
        shutil.copy2(built, target)
        return target


def _pull_public_lab(
    reference: str,
    *,
    target: Path | None,
    force: bool,
    registry_url: str | None,
    emit_status: bool = False,
    client: PublicRegistryClient | None = None,
    artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parsed = parse_package_reference(reference, allow_missing_version=True)
    if parsed is None:
        raise PackageError("labs pull requires a package reference: namespace/name[@version]")
    registry_client = client or PublicRegistryClient(registry_url)
    if client is None:
        registry_client = _new_registry_client(parsed, registry_url)
    if artifact is None:
        _print_registry_status(f"Resolving {reference}...", enabled=emit_status)
        artifact = registry_client.resolve_package(parsed.package_name, parsed.version)
    if artifact.get("package_type") != "lab":
        raise PackageError(
            f"Package {reference} is type `{artifact.get('package_type')}`, expected `lab`"
        )
    destination = lab_destination_for_reference(reference, target)
    if destination.exists() and not force:
        raise PackageError(
            f"Target already exists: {destination}; re-run with --force to replace it"
        )

    _print_registry_status("Downloading package...", enabled=emit_status)
    archive_bytes = _download_registry_artifact(
        registry_client,
        artifact,
        package_name=parsed.package_name,
        version=parsed.version or str(artifact.get("version") or ""),
    )
    actual_sha = hashlib.sha256(archive_bytes).hexdigest()
    expected_sha = str(artifact.get("sha256") or "")
    if expected_sha and actual_sha != expected_sha:
        raise PackageError("Downloaded lab package hash does not match registry metadata")

    if destination.exists():
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()

    with tempfile.TemporaryDirectory(prefix="biosim-registry-pull-") as temp_dir:
        temp_path = Path(temp_dir)
        archive_path = temp_path / "download.bsilab"
        archive_path.write_bytes(archive_bytes)
        _print_registry_status("Validating package...", enabled=emit_status)
        validation = validate_package(archive_path)
        if not validation.valid:
            raise PackageError("; ".join(validation.errors))
        unpacked = unpack_package(archive_path, dest=temp_path / "unpacked")
        payload_dir = unpacked / "payload"
        if not payload_dir.is_dir():
            raise PackageError("Downloaded lab package is missing payload/")
        _print_registry_status(f"Extracting lab to {destination}...", enabled=emit_status)
        shutil.copytree(payload_dir, destination)

    save_result = workspace_save_lab(destination)
    return {
        "command": "labs.pull",
        "registry_url": registry_client.base_url,
        "reference": reference,
        "path": str(destination),
        "artifact": artifact,
        "lab": save_result["lab"],
    }


def _lab_manifest_exists(path: Path) -> bool:
    return path.joinpath("lab.yaml").is_file() or path.joinpath("lab.yml").is_file()


def _print_registry_status(message: str, *, enabled: bool) -> None:
    if enabled:
        print(message, file=sys.stderr, flush=True)


def _resolve_runtime_lab_path(
    lab: str | Path,
    *,
    target: Path | None,
    force: bool,
    registry_url: str | None,
    emit_status: bool = False,
) -> tuple[Path, dict[str, Any] | None]:
    local_candidate = Path(lab).expanduser()
    if local_candidate.exists():
        return local_candidate, None

    reference = str(lab)
    parsed = parse_package_reference(reference, allow_missing_version=True)
    if parsed is None:
        return lab, None

    client = _new_registry_client(parsed, registry_url)
    _print_registry_status(f"Resolving {reference}...", enabled=emit_status)
    artifact = client.resolve_package(parsed.package_name, parsed.version)
    if artifact.get("package_type") != "lab":
        raise PackageError(
            f"Package {reference} is type `{artifact.get('package_type')}`, expected `lab`"
        )

    destination = (
        target.expanduser().resolve()
        if target is not None
        else cached_lab_destination_for_reference(reference, artifact)
    )
    if destination.exists() and _lab_manifest_exists(destination) and not force:
        _print_registry_status(f"Using cached lab at {destination}", enabled=emit_status)
        return destination, {
            "command": "labs.pull",
            "registry_url": client.base_url,
            "reference": reference,
            "path": str(destination),
            "artifact": artifact,
            "reused": True,
        }
    if destination.exists() and not force:
        raise PackageError(
            f"Target already exists and is not a lab source tree: {destination}; re-run with --force to replace it"
        )

    pull_result = _pull_public_lab(
        reference,
        target=destination,
        force=force,
        registry_url=registry_url,
        emit_status=emit_status,
        client=client,
        artifact=artifact,
    )
    return destination, pull_result


def _registry_client_for_reference(
    reference: str,
    registry_url: str | None,
) -> PublicRegistryClient:
    parsed = parse_package_reference(reference, allow_missing_version=True)
    if parsed is None:
        return PublicRegistryClient(registry_url)
    return _new_registry_client(parsed, registry_url)


def _new_registry_client(
    reference: PackageReference,
    registry_url: str | None,
) -> PublicRegistryClient:
    factory = getattr(PublicRegistryClient, "for_reference", None)
    if callable(factory):
        return factory(reference, base_url=registry_url)
    return PublicRegistryClient(registry_url)


def _download_registry_artifact(
    client: PublicRegistryClient,
    artifact: dict[str, Any],
    *,
    package_name: str,
    version: str,
) -> bytes:
    if getattr(client, "_v1", False):
        return client.download_package(
            str(artifact.get("id") or ""),
            package_name=package_name,
            version=version,
        )
    return client.download_package(str(artifact["id"]))


def _init_lab_project(
    path: Path,
    *,
    name: str,
    description: str | None,
    force: bool,
    empty: bool,
) -> dict[str, Any]:
    target = path.expanduser().resolve()
    if target.exists() and not target.is_dir():
        raise PackageError(f"Lab path must be a directory: {target}")
    if target.exists() and not force and any(target.iterdir()):
        raise PackageError(
            f"Lab path is not empty: {target}. Re-run with --force to write lab files."
        )
    target.mkdir(parents=True, exist_ok=True)

    slug = _slugify(name)
    if empty:
        models_block = "models: []\nchildren: []\nwiring: []"
    else:
        write_starter_model(target / "models" / "hello")
        models_block = """models:
  - path: models/hello
    alias: hello
children: []
wiring: []"""

    lab_yaml = f"""schema_version: "2.0"
title: {_yaml_string(name)}
description: {_yaml_string(description) if description is not None else "null"}
package: {DEFAULT_PACKAGE_NAMESPACE}/{slug}
version: 0.1.0
{models_block}
runtime:
  communication_step: 1.0
  duration: 1.0
"""
    (target / "lab.yaml").write_text(lab_yaml, encoding="utf-8")
    return {
        "created": True,
        "path": str(target),
        "manifest": str(target / "lab.yaml"),
        "starter_model": None if empty else str(target / "models" / "hello"),
    }


def _validate_local_lab(path: Path) -> Any:
    target = path.expanduser().resolve()
    if target.is_file():
        if target.suffix != ".bsilab":
            raise PackageError(f"Expected a .bsilab package: {target}")
        return validate_package(target)
    if not target.is_dir():
        raise PackageError(f"Lab path not found: {target}")
    _lab_config_path(target)
    return validate_lab_source(target)


@contextmanager
def _package_file_for_lab(path: Path) -> Iterator[Path]:
    target = path.expanduser().resolve()
    if target.is_file():
        if target.suffix != ".bsilab":
            raise PackageError(f"Expected a .bsilab package: {target}")
        yield target
        return
    if not target.is_dir():
        raise PackageError(f"Lab path not found: {target}")
    _lab_config_path(target)
    package_name, version = _local_lab_release_identity(target)
    with tempfile.TemporaryDirectory(prefix="biosim-lab-") as temp_dir:
        yield build_package(
            target,
            output_path=Path(temp_dir) / f"{target.name or 'lab'}.bsilab",
            package_name=package_name,
            version=version,
        )


@contextmanager
def _lab_path_with_run_inputs(
    path: Path,
    run_input_file: Path | None,
) -> Iterator[Path]:
    if run_input_file is None:
        yield path
        return
    input_path = run_input_file.expanduser().resolve()
    try:
        run_inputs = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageError(f"Invalid run input file {input_path}: {exc}") from exc
    if not isinstance(run_inputs, dict):
        raise PackageError("Run input file must contain a JSON object")
    unknown = sorted(set(run_inputs) - {"parameters", "simulation_config"})
    if unknown:
        raise PackageError(
            f"Run input file contains unsupported keys: {', '.join(unknown)}"
        )
    for key in ("parameters", "simulation_config"):
        value = run_inputs.get(key)
        if value is not None and not isinstance(value, dict):
            raise PackageError(f"Run input `{key}` must be a JSON object or null")

    source = path.expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="biosim-run-inputs-") as temp_dir:
        temp_root = Path(temp_dir)
        if source.is_file():
            if source.suffix != ".bsilab":
                raise PackageError(f"Expected a .bsilab package: {source}")
            unpacked = unpack_package(source, dest=temp_root / "unpacked")
            staged = unpacked / "payload"
        elif source.is_dir():
            staged = temp_root / "lab"
            shutil.copytree(source, staged)
        else:
            raise PackageError(f"Lab path not found: {source}")

        manifest_path = _lab_config_path(staged)
        manifest = _load_structured_file(manifest_path)
        if not isinstance(manifest, dict):
            raise PackageError(f"{manifest_path} must contain a YAML mapping")
        apply_run_overrides(
            manifest,
            parameters=run_inputs.get("parameters"),
            simulation_config=run_inputs.get("simulation_config"),
        )
        try:
            import yaml
        except ImportError as exc:
            raise PackageError(
                "Run input overlays require PyYAML. Install with: pip install pyyaml"
            ) from exc
        manifest_path.write_text(
            yaml.safe_dump(manifest, sort_keys=False),
            encoding="utf-8",
        )
        yield staged


def _lab_config_path(path: Path) -> Path:
    target = path.expanduser().resolve()
    if target.is_file():
        return target
    for name in ("lab.yaml", "lab.yml"):
        manifest = target / name
        if manifest.is_file():
            return manifest
    raise PackageError(f"Could not find lab.yaml or lab.yml in {target}")


def _slugify(value: str) -> str:
    slug = "".join(char.lower() if char.isalnum() else "-" for char in value)
    slug = "-".join(part for part in slug.split("-") if part)
    return slug or "lab"


def _yaml_string(value: str) -> str:
    import json

    return json.dumps(value)


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True)


def _parse_lab_rename_args(target_or_name: str, name: str | None) -> tuple[Path, str]:
    if name is None:
        return Path("."), target_or_name
    return Path(target_or_name), name


def _load_structured_file(path: Path) -> Any:
    try:
        import yaml
    except ImportError as exc:
        raise PackageError(
            "Structured lab input requires PyYAML. Install with: pip install pyyaml"
        ) from exc
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _print_workspace_result(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json_dumps(payload))
        return

    command = str(payload.get("command") or "labs")
    print(f"Biosimulant {command} succeeded.")
    labs = payload.get("labs")
    if isinstance(labs, list):
        if not labs:
            print("No local labs found.")
            return
        for lab in labs:
            if not isinstance(lab, dict):
                continue
            title = lab.get("title") or lab.get("package") or lab.get("id")
            print(f"- {title} ({lab.get('id')})")
            print(f"  Path: {lab.get('path')}")
        return

    lab = payload.get("lab")
    if isinstance(lab, dict):
        print(f"ID: {lab.get('id')}")
        if lab.get("title"):
            print(f"Title: {lab['title']}")
        print(f"Path: {lab.get('path')}")
        print(f"Package: {lab.get('package')}@{lab.get('version')}")
    if payload.get("path"):
        print(f"Path: {payload['path']}")
    if payload.get("alias"):
        print(f"Alias: {payload['alias']}")


def _print_local_execution_capability(
    payload: dict[str, Any], *, json_output: bool
) -> None:
    if json_output:
        print(json_dumps(payload))
        return
    print("Biosimulant local execution capability check completed.")
    print(f"Local execution supported: {'yes' if payload['local_supported'] else 'no'}")
    print(f"Selected backend: {payload.get('selected_backend') or 'none'}")
    for blocker in payload.get("blockers") or []:
        print(f"- {blocker.get('message') or blocker.get('code')}")


def _print_lab_package_result(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json_dumps(payload))
        return
    print("Biosimulant lab package built.")
    print(f"Package: {payload.get('package')}@{payload.get('version')}")
    print(f"File: {payload.get('package_file')}")
    for warning in payload.get("warnings") or []:
        print(f"Warning: {warning}")


def _print_registry_result(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json_dumps(payload))
        return
    command = str(payload.get("command") or "labs")
    print(f"Biosimulant {command} succeeded.")
    result = payload.get("result")
    if isinstance(result, dict):
        items = result.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                title = item.get("title") or item.get("qualified_package_name") or item.get("id")
                print(f"- {title}")
                if item.get("qualified_package_name"):
                    print(f"  Package: {item['qualified_package_name']}")
                if item.get("id"):
                    print(f"  ID: {item['id']}")
            if not items:
                print("No public labs found.")
            return
        artifact = result.get("artifact")
        if isinstance(artifact, dict):
            print(f"Package: {artifact.get('qualified_name') or artifact.get('package_name')}")
            print(f"Version: {artifact.get('version')}")
            print(f"Artifact: {artifact.get('id')}")
        lab = result.get("lab")
        if isinstance(lab, dict):
            print(f"Lab: {lab.get('title') or lab.get('id')}")
            if lab.get("qualified_package_name"):
                print(f"Package: {lab['qualified_package_name']}")
    if payload.get("path"):
        print(f"Path: {payload['path']}")


def _add_compatibility_subcommand(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    command_path: str,
) -> None:
    parser = subparsers.add_parser(
        name,
        help="Deprecated compatibility command; use the canonical biosimulant CLI",
    )
    parser.add_argument("compatibility_args", nargs=argparse.REMAINDER)
    parser.set_defaults(canonical_command_path=command_path)


def _canonical_cli_required_or_exit(
    command: str,
    *,
    prog: str,
    json_output: bool,
) -> None:
    payload = {
        "error": "canonical_cli_required",
        "command": command,
        "message": (
            "This compatibility entrypoint does not implement this command. "
            "Run it through the headless `biosimulant` CLI."
        ),
    }
    if json_output:
        print(json_dumps(payload), file=sys.stderr)
    else:
        print(f"Command unavailable in compatibility mode: {prog} {command}", file=sys.stderr)
        print(payload["message"], file=sys.stderr)
    raise SystemExit(7)


def _print_lab_init_success(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json_dumps(payload))
        return
    print("Biosimulant lab initialized.")
    print(f"Path: {payload['path']}")
    print(f"Manifest: {payload['manifest']}")
    if payload.get("starter_model"):
        print(f"Starter model: {payload['starter_model']}")


def _execution_timing_summary(execution: Mapping[str, Any]) -> str:
    timing = execution.get("timing")
    if timing == "finite":
        return "finite (every module runs once; duration and communication step don't apply)"
    if timing == "temporal":
        return "temporal (duration and communication step apply)"
    undeclared = execution.get("undeclared") or []
    if undeclared:
        return f"unknown (execution_policy not declared for: {', '.join(str(item) for item in undeclared)})"
    return "unknown"


def _print_lab_validation_success(package_file: Path, result: Any, *, json_output: bool) -> None:
    payload = {
        "command": "labs.validate",
        "lab": str(package_file),
        "valid": True,
        "package": result.metadata.get("package") if result.metadata else None,
        "version": result.metadata.get("version") if result.metadata else None,
        "warnings": result.warnings,
        "metadata": result.metadata,
    }
    compatibility = result.metadata.get("compatibility") if result.metadata else None
    if compatibility is not None:
        payload["compatibility"] = compatibility
    if json_output:
        print(json_dumps(payload))
        return
    print("Biosimulant lab validation passed.")
    if isinstance(compatibility, dict):
        summary = compatibility["summary"]
        print(
            f"Wires: {summary['verified']} verified, {summary['partial']} partial, "
            f"{summary['structural']} structural"
        )
    print(f"Lab: {package_file}")
    if result.metadata:
        print(f"Package: {result.metadata.get('package')}@{result.metadata.get('version')}")
        execution = result.metadata.get("execution")
        if isinstance(execution, dict) and execution.get("timing"):
            print(f"Execution: {_execution_timing_summary(execution)}")
    for warning in result.warnings:
        print(f"Warning: {warning}")


def _print_package_repo_validation_success(manifest: Any, *, json_output: bool) -> None:
    payload = {
        "command": "labs.release.validate",
        "manifest": str(manifest.path),
        "valid": True,
        "package_count": len(manifest.packages),
        "packages": [
            {
                "package": entry.package,
                "version": entry.version,
                "package_type": entry.package_type,
                "path": entry.path.as_posix(),
                "visibility": entry.visibility,
            }
            for entry in manifest.packages
        ],
    }
    if json_output:
        print(json_dumps(payload))
        return
    print("Biosimulant package manifest validation passed.")
    print(f"Manifest: {manifest.path}")
    print(f"Packages: {len(manifest.packages)}")
    for entry in manifest.packages:
        print(f"  - {entry.package}@{entry.version} ({entry.package_type})")


def _print_package_repo_build_success(built: list[dict[str, Any]], *, json_output: bool) -> None:
    payload = {"command": "labs.release.build", "built": built}
    if json_output:
        print(json_dumps(payload))
        return
    print("Biosimulant package manifest build succeeded.")
    print(f"Built packages: {len(built)}")
    for entry in built:
        print(
            f"  - {entry['package']}@{entry['version']} "
            f"({entry['package_type']}): {entry['path']}"
        )


def _print_pack_result(json_output: bool, payload: dict[str, Any]) -> None:
    if json_output:
        print(json_dumps(payload))
        return
    command = payload.get("command", "pack")
    print(f"Biosimulant package {command} succeeded.")
    if payload.get("package"):
        print(f"Package: {payload['package']}@{payload.get('version')}")
    if payload.get("package_type"):
        print(f"Type: {payload['package_type']}")
    if payload.get("package_file"):
        print(f"File: {payload['package_file']}")
    warnings = payload.get("warnings") or []
    for warning in warnings:
        print(f"Warning: {warning}")


def _print_validation_success(package_file: Path, result: Any, *, json_output: bool) -> None:
    payload = {
        "command": "validate",
        "package_file": str(package_file),
        "valid": True,
        "package": result.metadata.get("package") if result.metadata else None,
        "version": result.metadata.get("version") if result.metadata else None,
        "package_type": result.metadata.get("package_type") if result.metadata else None,
        "warnings": result.warnings,
        "metadata": result.metadata,
    }
    if json_output:
        print(json_dumps(payload))
        return
    print("Biosimulant package validation passed.")
    print(f"File: {package_file}")
    if result.metadata:
        print(f"Package: {result.metadata.get('package')}@{result.metadata.get('version')}")
        print(f"Type: {result.metadata.get('package_type')}")
    if result.warnings:
        for warning in result.warnings:
            print(f"Warning: {warning}")


def _print_validation_failure(package_file: Path, result: Any, *, json_output: bool) -> None:
    payload = {
        "command": "validate",
        "package_file": str(package_file),
        "valid": False,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
    }
    if json_output:
        print(json_dumps(payload), file=sys.stderr)
        return
    print("Biosimulant package validation failed.", file=sys.stderr)
    print(f"File: {package_file}", file=sys.stderr)
    for error in result.errors:
        print(f"Error: {error}", file=sys.stderr)
    for warning in result.warnings:
        print(f"Warning: {warning}", file=sys.stderr)


def _print_run_result(package_file: Path, result: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json_dumps(result))
        return
    print("Biosimulant package run completed.")
    print(f"File: {package_file}")
    if result.get("package"):
        print(f"Package: {result['package']}@{result.get('version')}")
    if "outputs" in result:
        outputs = ", ".join(result.get("outputs") or [])
        print(f"Outputs: {outputs or '(none)'}")
    if "modules" in result:
        modules = ", ".join(
            f"{item.get('alias')}={item.get('path')}"
            for item in result.get("modules", [])
        )
        print(f"Resolved models: {modules or '(none)'}")
    if "duration" in result:
        print(f"Duration: {result['duration']}")
    if result.get("report_file"):
        print(f"Report: {result['report_file']}")


def _write_run_report(
    report_file: Path,
    package_file: Path,
    result: dict[str, Any],
) -> None:
    payload = {
        "package_file": str(package_file),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "result": result,
    }
    embedded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Biosimulant Run Report</title>
  <style>
    body {{ margin: 0; background: #f7f4ed; color: #18201b;
      font: 16px/1.5 ui-sans-serif, system-ui, sans-serif; }}
    main {{ max-width: 960px; margin: 0 auto; padding: 40px 20px 64px; }}
    h1 {{ letter-spacing: -0.04em; }}
    .card {{ padding: 20px; border: 1px solid #ded6c8; border-radius: 16px;
      background: #fffdf7; box-shadow: 0 16px 48px rgba(24, 32, 27, 0.08); }}
    pre {{ overflow: auto; white-space: pre-wrap; word-break: break-word; }}
  </style>
</head>
<body>
  <main>
    <p>Biosimulant</p>
    <h1>Biosimulant run report</h1>
    <section class="card">
      <h2>Run result</h2>
      <pre id="biosimulant-result"></pre>
    </section>
  </main>
  <script type="application/json" id="biosimulant-report-data">{embedded}</script>
  <script>
    const payload = JSON.parse(
      document.getElementById("biosimulant-report-data").textContent
    );
    document.getElementById("biosimulant-result").textContent =
      JSON.stringify(payload, null, 2);
  </script>
</body>
</html>
"""
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(document, encoding="utf-8")


def _write_compatibility_failure_results(results_file: Path, exc: CompatibilityError) -> None:
    payload: dict[str, Any] = {
        "status": "failed",
        "error": {"code": exc.code, "message": str(exc)},
    }
    if exc.compatibility is not None:
        payload["compatibility"] = exc.compatibility
    results_file.parent.mkdir(parents=True, exist_ok=True)
    results_file.write_text(json_dumps(payload) + "\n", encoding="utf-8")


def _print_compatibility_error(exc: CompatibilityError, *, json_output: bool) -> None:
    if json_output:
        print(json_dumps({"error": exc.to_dict()}), file=sys.stderr)
        return
    print("Biosimulant compatibility check blocked the run.", file=sys.stderr)
    print(f"Error: {exc}", file=sys.stderr)
    summary = (exc.compatibility or {}).get("summary")
    if isinstance(summary, dict):
        print(
            "Wires: "
            f"{summary.get('verified', 0)} verified, {summary.get('partial', 0)} partial, "
            f"{summary.get('structural', 0)} structural, {summary.get('blocked', 0)} blocked",
            file=sys.stderr,
        )


def _print_pack_error(exc: Exception, *, json_output: bool) -> None:
    payload = {"error": str(exc)}
    if json_output:
        print(json_dumps(payload), file=sys.stderr)
        return
    print("Biosimulant package command failed.", file=sys.stderr)
    print(f"Error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
