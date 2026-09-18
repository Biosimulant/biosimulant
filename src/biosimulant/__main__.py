# SPDX-FileCopyrightText: 2026-present Biosimulant Team
#
# SPDX-License-Identifier: MIT
# PYTHON_ARGCOMPLETE_OK
"""Canonical, headless ``biosimulant`` command-line interface."""
from __future__ import annotations

import argparse
import contextlib
import getpass
import importlib.util
import hashlib
import io
import json
import os
import platform
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from biosim.__about__ import __version__
from biosim.__main__ import main as _legacy_main
from biosim.cloud import Client as CloudClient
from biosim.cloud.errors import ApiError as CloudApiError
from biosim.cloud.errors import AuthenticationError as CloudAuthenticationError
from biosim.credentials import (
    DEFAULT_REGISTRY,
    DEFAULT_TOKEN_CONSOLE_URL,
    CredentialError,
    credential_status,
    delete_token,
    normalize_registry_origin,
    resolve_token,
    store_token,
    verify_token,
)
from biosim.web_login import (
    DEFAULT_WEB_LOGIN_SCOPES,
    browser_login,
)
from biosim.managed_runtime import (
    _current_python_minor,
    _runtime_cache_root,
    ensure_executor_python,
)
from biosim.package_repo import build_package_repo, validate_package_repo
from biosim.pack import PackageError, build_package, validate_package
from biosim.registry import PublicRegistryClient, RegistryError, parse_package_reference


SCHEMA_VERSION = "1"
EXIT_OPERATION = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_NOT_FOUND = 4
EXIT_INVALID = 5
EXIT_CONFLICT = 6
EXIT_UNAVAILABLE = 7

GLOBAL_FLAGS = {
    "--json": "json_output",
    "--json-stream": "json_stream",
    "--no-open": "no_open",
}
GLOBAL_VALUES = {
    "--profile": "profile",
    "--data-dir": "data_dir",
    "--legacy-json": "legacy_json",
}

COMMANDS: tuple[tuple[str, str], ...] = (
    ("doctor", "Check the headless CLI and local runtime installation"),
    ("commands list", "List the canonical machine-readable command catalog"),
    ("auth login", "Verify and store a registry token for one registry"),
    ("auth status", "Inspect, and optionally verify, credentials for one registry"),
    ("auth logout", "Remove credentials for one registry"),
    ("labs init", "Create a local runnable lab"),
    ("labs create", "Create a managed local lab"),
    ("labs list", "List local labs"),
    ("labs get", "Inspect a local lab"),
    ("labs save", "Save a local lab"),
    ("labs rename", "Rename a local lab"),
    ("labs delete", "Delete a local lab"),
    ("labs add-model", "Add a model to a lab"),
    ("labs change-model", "Change a lab model"),
    ("labs vendor-model", "Vendor a model into a lab"),
    ("labs inspect-owned", "Inspect lab-owned model sources"),
    ("labs validate", "Validate a lab"),
    ("labs capabilities", "Check local CPU, memory, and GPU support"),
    ("labs run", "Run a lab"),
    ("labs serve", "Serve a lab locally"),
    ("labs package", "Build a lab package"),
    ("labs search", "Search a Biosimulant registry"),
    ("labs info", "Inspect registry lab metadata"),
    ("labs versions", "List immutable registry versions"),
    ("labs pull", "Pull a registry lab"),
    ("labs publish", "Publish an immutable lab version"),
    ("labs sync-status", "Compare a local lab with its registry version"),
    ("labs release validate", "Validate a release manifest"),
    ("labs release build", "Build a release manifest"),
    ("labs release publish", "Build and publish a release manifest"),
    ("labs release ci", "Validate, build, and publish a release manifest"),
    ("compatibility standard", "Show the installed compatibility standard release"),
    ("compatibility profiles", "List installed compatibility profiles"),
    ("compatibility show", "Show one exact compatibility profile"),
    ("compatibility validate", "Validate compatibility declarations in model.yaml"),
    ("compatibility compare", "Compare one output port with one input port"),
    ("validate", "Alias for labs validate"),
    ("run", "Alias for labs run"),
    ("runtime status", "Inspect the managed runtime"),
    ("runtime detect-python", "Report the current Python runtime"),
    ("runtime prepare", "Prepare an isolated execution runtime"),
    ("runs list", "List managed runs"),
    ("runs get", "Inspect a managed run"),
    ("runs create", "Create a managed run"),
    ("runs start", "Start a created managed run"),
    ("runs cancel", "Cancel a managed run"),
    ("runs results", "Get managed run results"),
    ("runs logs", "Get managed run events or logs"),
    ("runs upload", "Upload a managed run artifact"),
    ("runs remote catalog", "List remote compute profiles"),
    ("runs remote create", "Create a remote managed run"),
    ("runs remote get", "Inspect a remote managed run"),
    ("runs remote results", "Get remote run results"),
    ("jobs list", "List hosted jobs"),
    ("jobs get", "Inspect a hosted job"),
)

# Commands the catalog lists but this CLI cannot perform against the public API.
# The reason is shared with the runtime failure so the two cannot drift apart.
UNAVAILABLE_COMMANDS: dict[str, str] = {
    "runs start": (
        "A managed run starts when it is created; use `biosimulant runs create`."
    ),
    "runs upload": "Uploading run artifacts is not available in this CLI.",
    "jobs list": (
        "Background jobs are not exposed by the public API; "
        "use `biosimulant runs list` for managed runs."
    ),
    "jobs get": (
        "Background jobs are not exposed by the public API; "
        "use `biosimulant runs get` for a managed run."
    ),
}


_DEPRECATED_DESKTOP_COMMANDS = {
    "raw",
    "settings",
    "which",
    "install-cli",
    "uninstall-cli",
    "self",
    "config",
    "agent",
    "agents",
    "chat",
}


@dataclass
class GlobalOptions:
    json_output: bool = False
    json_stream: bool = False
    no_open: bool = False
    profile: str | None = None
    data_dir: str | None = None
    legacy_json: str | None = None

    @property
    def machine(self) -> bool:
        return self.json_output or self.json_stream or self.legacy_json is not None


class CliFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: int = EXIT_OPERATION,
        details: Any = None,
    ) -> None:
        self.code = code
        self.exit_code = exit_code
        self.details = details
        super().__init__(message)


class CliArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliFailure("usage", message, exit_code=EXIT_USAGE)


def _parser(*args: Any, **kwargs: Any) -> CliArgumentParser:
    return CliArgumentParser(*args, **kwargs)


def main(argv: list[str] | None = None) -> None:
    raw = list(sys.argv[1:] if argv is None else argv)
    if os.environ.get("_ARGCOMPLETE"):
        _legacy_main(raw, prog="biosimulant")
        return
    try:
        options, args = _extract_global_options(raw)
        _apply_global_options(options)
    except CliFailure as exc:
        _emit_failure(exc, GlobalOptions(), "biosimulant")
        raise SystemExit(exc.exit_code) from exc
    command = _command_name(args)
    if options.json_stream and args and args[0] not in {"-h", "--help", "-V", "--version"}:
        _emit_event(
            command,
            "progress",
            seq=1,
            stage="starting",
            message="Command started",
            percentage=0,
        )

    try:
        if not args or args[0] in {"-h", "--help"}:
            _root_parser().parse_args(args)
            return
        if args[0] in {"-V", "--version"}:
            print(f"biosimulant {__version__}")
            return
        if args[0] == "doctor":
            payload = _doctor(args[1:])
        elif args[:2] == ["commands", "list"]:
            payload = _commands_list(args[2:])
        elif args[0] == "auth":
            payload = _auth(args[1:])
        elif args[0] == "runtime":
            payload = _runtime(args[1:])
        elif args[0] == "runs":
            payload = _runs(args[1:])
        elif args[0] == "jobs":
            payload = _jobs(args[1:])
        elif args[:3] in (
            ["labs", "release", "publish"],
            ["labs", "release", "ci"],
        ):
            payload = _labs_release_publish(args[3:], ci=args[2] == "ci")
        elif args[:2] == ["labs", "publish"]:
            payload = _labs_publish(args[2:])
        elif args[:2] == ["labs", "sync-status"]:
            payload = _labs_sync_status(args[2:])
        elif args[0] in _DEPRECATED_DESKTOP_COMMANDS or (
            args[:2] in (["labs", "open"], ["labs", "import"], ["runs", "open"])
        ):
            raise CliFailure(
                "deprecated_desktop_command",
                f"`biosimulant {' '.join(args[:2])}` is no longer a public CLI command; "
                "use the corresponding Desktop interface.",
                exit_code=EXIT_UNAVAILABLE,
            )
        else:
            delegated = list(args)
            if delegated[0] in {"validate", "run"}:
                delegated = ["labs", delegated[0], *delegated[1:]]
            payload = _run_legacy(delegated, options)
        _emit_success(payload, options, command)
    except CliFailure as exc:
        _emit_failure(exc, options, command)
        raise SystemExit(exc.exit_code) from exc
    except (CredentialError, RegistryError, PackageError) as exc:
        failure = _failure_for_exception(exc)
        _emit_failure(failure, options, command)
        raise SystemExit(failure.exit_code) from exc
    except KeyboardInterrupt as exc:
        # Ctrl-C is a user decision, not a crash; never print a traceback for it.
        failure = CliFailure("cancelled", "Cancelled.", exit_code=EXIT_OPERATION)
        _emit_failure(failure, options, command)
        raise SystemExit(130) from exc


def _root_parser() -> argparse.ArgumentParser:
    parser = _parser(
        prog="biosimulant",
        description=(
            "Headless Biosimulant CLI for local terminals, Desktop, Studio, CI, "
            "servers, and containers."
        ),
    )
    parser.add_argument("--version", action="version", version=f"biosimulant {__version__}")
    parser.add_argument("--json", action="store_true", help="Emit schema-v1 JSON")
    parser.add_argument("--json-stream", action="store_true", help="Emit schema-v1 JSONL")
    parser.add_argument("--no-open", action="store_true", help="Never open a browser")
    parser.add_argument("--profile", help="Select a configuration profile")
    parser.add_argument("--data-dir", help="Override the Biosimulant data directory")
    parser.add_argument(
        "--legacy-json",
        choices=("bare", "desktop"),
        metavar="FORMAT",
        help="Migration-only JSON adapter: bare or desktop",
    )
    parser.epilog = "Commands:\n  " + "\n  ".join(
        f"{name:<24} {summary}" for name, summary in COMMANDS
    )
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    return parser


def _extract_global_options(argv: list[str]) -> tuple[GlobalOptions, list[str]]:
    options = GlobalOptions()
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in GLOBAL_FLAGS:
            setattr(options, GLOBAL_FLAGS[item], True)
            index += 1
            continue
        matched_value = False
        for flag, attribute in GLOBAL_VALUES.items():
            if item == flag:
                if index + 1 >= len(argv):
                    raise CliFailure(
                        "usage",
                        f"{flag} requires a value",
                        exit_code=EXIT_USAGE,
                    )
                setattr(options, attribute, argv[index + 1])
                index += 2
                matched_value = True
                break
            if item.startswith(f"{flag}="):
                setattr(options, attribute, item.split("=", 1)[1])
                index += 1
                matched_value = True
                break
        if matched_value:
            continue
        remaining.append(item)
        index += 1
    return options, remaining


def _apply_global_options(options: GlobalOptions) -> None:
    if options.legacy_json not in {None, "bare", "desktop"}:
        raise CliFailure(
            "usage",
            "--legacy-json must be either `bare` or `desktop`",
            exit_code=EXIT_USAGE,
        )
    if options.no_open:
        os.environ["BIOSIMULANT_NO_OPEN"] = "1"
    if options.profile:
        os.environ["BIOSIMULANT_PROFILE"] = options.profile
    if options.data_dir:
        os.environ["BIOSIMULANT_DATA_DIR"] = str(
            Path(options.data_dir).expanduser().resolve()
        )


def _command_name(args: list[str]) -> str:
    if not args:
        return "help"
    if args[0] == "labs" and len(args) >= 3 and args[1] == "release":
        return ".".join(args[:3])
    if args[0] in {"labs", "auth", "runtime", "commands"} and len(args) >= 2:
        return ".".join(args[:2])
    if args[0] in {"validate", "run"}:
        return f"labs.{args[0]}"
    return args[0]


def _doctor(argv: list[str]) -> dict[str, Any]:
    parser = _parser(prog="biosimulant doctor")
    parser.parse_args(argv)
    configured_uv = os.environ.get("BIOSIM_UV_PATH")
    uv_path = (
        str(Path(configured_uv).expanduser().resolve())
        if configured_uv and Path(configured_uv).expanduser().is_file()
        else shutil.which("uv")
    )
    uv_available = uv_path is not None or importlib.util.find_spec("uv") is not None
    data_dir = Path(
        os.environ.get("BIOSIMULANT_DATA_DIR", Path.home() / ".local" / "share" / "biosimulant")
    ).expanduser()
    parent = next((candidate for candidate in [data_dir, *data_dir.parents] if candidate.exists()), None)
    writable = bool(parent and os.access(parent, os.W_OK))
    checks = {
        "python": {
            "ok": sys.version_info >= (3, 10),
            "version": platform.python_version(),
            "executable": sys.executable,
        },
        "uv": {
            "ok": uv_available,
            "path": uv_path or (f"{sys.executable} -m uv" if uv_available else None),
        },
        "dataDir": {"ok": writable, "path": str(data_dir)},
    }
    return {"healthy": all(item["ok"] for item in checks.values()), "checks": checks}


def _commands_list(argv: list[str]) -> dict[str, Any]:
    parser = _parser(prog="biosimulant commands list")
    parser.parse_args(argv)
    return {
        "commands": [
            {
                "path": path,
                "summary": summary,
                "public": True,
                "available": path not in UNAVAILABLE_COMMANDS,
                **(
                    {"unavailableReason": UNAVAILABLE_COMMANDS[path]}
                    if path in UNAVAILABLE_COMMANDS
                    else {}
                ),
            }
            for path, summary in COMMANDS
        ],
        "globalOptions": sorted([*GLOBAL_FLAGS, *GLOBAL_VALUES]),
    }


def _is_default_registry(origin: str) -> bool:
    return origin == normalize_registry_origin(DEFAULT_REGISTRY)


def _note(message: str) -> None:
    """Write operator guidance to stderr so stdout stays machine-readable."""

    print(message, file=sys.stderr)


def _login_guidance(origin: str) -> None:
    _note(f"Signing in to {origin}")
    if _is_default_registry(origin):
        _note("Paste a developer API key (it starts with `bsk_live_`).")
        _note(f"Create one at {DEFAULT_TOKEN_CONSOLE_URL}")
        _note("It needs the packages:read and packages:write scopes.")
    else:
        _note(f"Paste a token issued by {origin}.")
    _note("")
    _note(
        "Sign-in is only needed for private labs and publishing. "
        "Local runs and public pulls work without it."
    )
    _note("")


def _prompt_for_token() -> str:
    try:
        return getpass.getpass("Token: ").strip()
    except (KeyboardInterrupt, EOFError) as exc:
        _note("")
        raise CliFailure(
            "cancelled",
            "Sign-in cancelled; no credentials were stored.",
            exit_code=EXIT_OPERATION,
        ) from exc


def _auth(argv: list[str]) -> dict[str, Any]:
    parser = _parser(prog="biosimulant auth")
    subparsers = parser.add_subparsers(dest="command", required=True)
    login = subparsers.add_parser("login")
    login.add_argument("registry", nargs="?", default=None)
    login.add_argument("--token-stdin", action="store_true")
    login.add_argument(
        "--web",
        action="store_true",
        help="Sign in through the browser instead of pasting a token",
    )
    login.add_argument(
        "--no-verify",
        action="store_true",
        help="Store the token without checking it against the registry",
    )
    status = subparsers.add_parser("status")
    status.add_argument("registry", nargs="?", default=None)
    status.add_argument(
        "--verify",
        action="store_true",
        help="Resolve the stored credential to an account",
    )
    logout = subparsers.add_parser("logout")
    logout.add_argument("registry", nargs="?", default=None)
    args = parser.parse_args(argv)
    if args.command == "status":
        return credential_status(args.registry, verify=args.verify)
    if args.command == "logout":
        origin = normalize_registry_origin(args.registry)
        return {"registry": origin, "removed": delete_token(origin)}

    origin = normalize_registry_origin(args.registry)
    if args.web and args.token_stdin:
        raise CliFailure(
            "usage",
            "Use either --web or --token-stdin, not both.",
            exit_code=EXIT_USAGE,
        )
    web_identity: dict[str, Any] | None = None
    if args.web:
        # `--no-open` is a global flag; it lands in the environment.
        headless_browser = bool(os.environ.get("BIOSIMULANT_NO_OPEN"))
        _note(f"Signing in to {origin} through the browser.")
        try:
            issued = browser_login(
                origin,
                scopes=DEFAULT_WEB_LOGIN_SCOPES,
                open_browser=not headless_browser,
                on_url=lambda url: _note(
                    f"Approve the request here:\n  {url}\n"
                    if headless_browser
                    else f"Opening {url}\nWaiting for you to approve it…"
                ),
            )
        except KeyboardInterrupt as exc:
            raise CliFailure(
                "cancelled",
                "Sign-in cancelled; no credentials were stored.",
                exit_code=EXIT_OPERATION,
            ) from exc
        token = str(issued.get("token") or "")
        web_identity = issued
    elif args.token_stdin:
        token = sys.stdin.readline().strip()
    elif sys.stdin.isatty():
        _login_guidance(origin)
        token = _prompt_for_token()
    else:
        raise CliFailure(
            "token_required",
            "Headless sign-in needs a token on stdin: "
            "printf '%s\\n' \"$TOKEN\" | biosimulant auth login --token-stdin "
            "(or use --web on a machine with a browser)",
            exit_code=EXIT_USAGE,
        )
    if not token:
        raise CliFailure(
            "token_required",
            "No token was provided; nothing was stored.",
            exit_code=EXIT_USAGE,
        )

    # Verify before storing so a bad paste fails here, not at the next publish.
    identity: dict[str, Any] | None = None
    if not args.no_verify:
        identity = verify_token(origin, token)
        if not identity.get("verified"):
            _note(
                f"Could not confirm the token with {origin} "
                f"({identity.get('reason')}); storing it unverified."
            )

    stored_origin = store_token(args.registry, token)
    payload: dict[str, Any] = {"registry": stored_origin, "authenticated": True}
    if identity is not None:
        payload["verified"] = bool(identity.get("verified"))
        if identity.get("verified"):
            payload["account"] = identity.get("account")
            payload["scopes"] = identity.get("scopes")
            payload["canPublishPackages"] = identity.get("canPublishPackages")
            _note(f"Signed in to {stored_origin} as {identity.get('account')}")
            if not identity.get("canPublishPackages"):
                _note(
                    "This credential cannot publish. Add the packages:write "
                    f"scope at {DEFAULT_TOKEN_CONSOLE_URL} if you need to."
                    if _is_default_registry(stored_origin)
                    else "This credential cannot publish."
                )
    return payload


def _runtime(argv: list[str]) -> dict[str, Any]:
    parser = _parser(prog="biosimulant runtime")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("detect-python")
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--python", default=_current_python_minor())
    args = parser.parse_args(argv)
    detected = {
        "version": platform.python_version(),
        "minor": _current_python_minor(),
        "executable": sys.executable,
        "implementation": platform.python_implementation(),
    }
    if args.command == "detect-python":
        return detected
    if args.command == "status":
        return {
            "python": detected,
            "cacheDir": str(_runtime_cache_root()),
            "uv": shutil.which("uv"),
        }
    executable = ensure_executor_python(args.python)
    return {
        "pythonVersion": args.python,
        "executable": str(executable),
        "cacheDir": str(_runtime_cache_root()),
        "isolated": Path(executable).resolve() != Path(sys.executable).resolve(),
    }


def _labs_publish(argv: list[str]) -> dict[str, Any]:
    parser = _parser(prog="biosimulant labs publish")
    parser.add_argument("lab", type=Path)
    parser.add_argument("reference", nargs="?")
    parser.add_argument("--visibility", choices=("private", "public"), default="private")
    parser.add_argument("--registry-url", default=None)
    args = parser.parse_args(argv)

    with contextlib.ExitStack() as stack:
        source = args.lab.expanduser().resolve()
        if source.is_dir():
            temp_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="biosimulant-publish-")))
            package_file = build_package(source, output_path=temp_dir / "package.bsilab")
        else:
            package_file = source
        validation = validate_package(package_file)
        if not validation.valid or not validation.metadata:
            raise CliFailure(
                "invalid_content",
                "; ".join(validation.errors) or "Lab package validation failed",
                exit_code=EXIT_INVALID,
            )
        metadata = validation.metadata
        package_name = str(metadata.get("package") or "")
        version = str(metadata.get("version") or "")
        if args.reference:
            parsed = parse_package_reference(args.reference)
            if parsed is None or parsed.version is None:
                raise CliFailure(
                    "invalid_reference",
                    "Publish reference must use [registry/]namespace/name@version",
                    exit_code=EXIT_USAGE,
                )
            package_name, version = parsed.package_name, parsed.version
            client = PublicRegistryClient.for_reference(parsed, base_url=args.registry_url)
        else:
            if not package_name or not version:
                raise CliFailure(
                    "invalid_content",
                    "Package metadata must declare package and version",
                    exit_code=EXIT_INVALID,
                )
            client = PublicRegistryClient(args.registry_url)
        result = client.publish_package(
            package_file,
            package_name=package_name,
            version=version,
            visibility=args.visibility,
        )
        package_sha256 = hashlib.sha256(package_file.read_bytes()).hexdigest()
    return {
        "reference": f"{package_name}@{version}",
        "registry": client.registry_origin,
        "sha256": package_sha256,
        "result": result,
    }


def _labs_release_publish(argv: list[str], *, ci: bool) -> dict[str, Any]:
    command = "ci" if ci else "publish"
    parser = _parser(prog=f"biosimulant labs release {command}")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--out", type=Path, default=Path("dist/biosimulant-packages"))
    parser.add_argument("--registry-url", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    manifest = validate_package_repo(args.manifest)
    built = build_package_repo(args.manifest, args.out)
    if args.dry_run:
        return {
            "manifest": str(manifest.path),
            "built": built,
            "published": [],
            "dryRun": True,
        }
    published = []
    for entry in built:
        package_name = str(entry["package"])
        version = str(entry["version"])
        package_file = Path(str(entry["path"]))
        reference = parse_package_reference(f"{package_name}@{version}")
        if reference is None:
            raise CliFailure(
                "invalid_reference",
                f"Invalid release package reference: {package_name}@{version}",
                exit_code=EXIT_INVALID,
            )
        client = PublicRegistryClient.for_reference(
            reference,
            base_url=args.registry_url,
        )
        result = client.publish_package(
            package_file,
            package_name=package_name,
            version=version,
            visibility=str(entry.get("visibility") or "private"),
        )
        published.append(
            {
                "reference": f"{package_name}@{version}",
                "sha256": hashlib.sha256(package_file.read_bytes()).hexdigest(),
                "result": result,
            }
        )
    return {
        "manifest": str(manifest.path),
        "built": built,
        "published": published,
        "dryRun": False,
    }


def _labs_sync_status(argv: list[str]) -> dict[str, Any]:
    parser = _parser(prog="biosimulant labs sync-status")
    parser.add_argument("lab", type=Path, nargs="?", default=Path("."))
    parser.add_argument("--registry-url", default=None)
    args = parser.parse_args(argv)
    source = args.lab.expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="biosimulant-sync-status-") as temp_dir:
        package_file = (
            build_package(source, output_path=Path(temp_dir) / "package.bsilab")
            if source.is_dir()
            else source
        )
        validation = validate_package(package_file)
        if not validation.valid or not validation.metadata:
            raise CliFailure(
                "invalid_content",
                "; ".join(validation.errors) or "Lab validation failed",
                exit_code=EXIT_INVALID,
            )
        package_name = str(validation.metadata.get("package") or "")
        version = str(validation.metadata.get("version") or "")
        reference = parse_package_reference(f"{package_name}@{version}")
        if reference is None:
            raise CliFailure(
                "invalid_reference",
                "Lab metadata must declare package and version",
                exit_code=EXIT_INVALID,
            )
        client = PublicRegistryClient.for_reference(
            reference,
            base_url=args.registry_url,
        )
        remote = client.resolve_package(package_name, version)
        local_sha = hashlib.sha256(package_file.read_bytes()).hexdigest()
    remote_sha = str(remote.get("sha256") or "").lower()
    return {
        "reference": f"{package_name}@{version}",
        "localSha256": local_sha,
        "remoteSha256": remote_sha or None,
        "synced": bool(remote_sha and remote_sha == local_sha),
    }


def _cloud_client() -> CloudClient:
    """Build a managed-run client, reusing what `auth login` already stored.

    A developer API key authenticates both the registry and the managed-run
    API, so signing in once should cover both. Only a developer key is reused:
    registry operation tokens and workspace tokens are scoped to the registry.
    The fallback is limited to the default API host so a Hub key is never sent
    to a staging or self-hosted endpoint.
    """

    if not os.environ.get("BIOSIMULANT_API_KEY", "").strip() and not os.environ.get(
        "BIOSIMULANT_API_BASE_URL", ""
    ).strip():
        try:
            stored = resolve_token(DEFAULT_REGISTRY)
        except CredentialError:
            stored = None
        if stored and stored.startswith(("bsk_live_", "bsk_test_")):
            return CloudClient(api_key=stored)
    try:
        return CloudClient()
    except CloudAuthenticationError as exc:
        raise CliFailure(
            "authentication_required",
            "Managed runs need credentials. Run `biosimulant auth login --web`, "
            "or set BIOSIMULANT_API_KEY to a developer API key from "
            f"{DEFAULT_TOKEN_CONSOLE_URL}",
            exit_code=EXIT_AUTH,
        ) from exc


def _runs(argv: list[str]) -> dict[str, Any]:
    parser = _parser(prog="biosimulant runs")
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--limit", type=int, default=50)
    list_parser.add_argument("--cursor")
    get_parser = subparsers.add_parser("get")
    get_parser.add_argument("run_id")
    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("ref")
    create_parser.add_argument("--inputs", default="{}")
    create_parser.add_argument("--compute-profile")
    create_parser.add_argument("--idempotency-key")
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("run_id")
    cancel_parser = subparsers.add_parser("cancel")
    cancel_parser.add_argument("run_id")
    results_parser = subparsers.add_parser("results")
    results_parser.add_argument("run_id")
    logs_parser = subparsers.add_parser("logs")
    logs_parser.add_argument("run_id")
    logs_parser.add_argument("--after", type=int, default=0)
    logs_parser.add_argument("--limit", type=int, default=100)
    upload_parser = subparsers.add_parser("upload")
    upload_parser.add_argument("run_id")
    upload_parser.add_argument("file", type=Path)
    remote = subparsers.add_parser("remote")
    remote_subparsers = remote.add_subparsers(dest="remote_command", required=True)
    remote_subparsers.add_parser("catalog")
    remote_create = remote_subparsers.add_parser("create")
    remote_create.add_argument("ref")
    remote_create.add_argument("--inputs", default="{}")
    remote_create.add_argument("--compute-profile")
    remote_get = remote_subparsers.add_parser("get")
    remote_get.add_argument("run_id")
    remote_results = remote_subparsers.add_parser("results")
    remote_results.add_argument("run_id")
    args = parser.parse_args(argv)
    if args.command in {"start", "upload"}:
        raise CliFailure(
            "capability_unavailable",
            UNAVAILABLE_COMMANDS[f"runs {args.command}"],
            exit_code=EXIT_UNAVAILABLE,
            details={"capability": f"runs.{args.command}"},
        )

    try:
        with _cloud_client() as client:
            if args.command == "list":
                return {
                    "items": [
                        run.data
                        for run in client.runs.list(limit=args.limit, cursor=args.cursor)
                    ]
                }
            if args.command == "get":
                return client.runs.retrieve(args.run_id).data
            if args.command == "create":
                return client.runs.create(
                    ref=args.ref,
                    inputs=_json_object(args.inputs, "--inputs"),
                    compute_profile=args.compute_profile,
                    idempotency_key=args.idempotency_key,
                ).data
            if args.command == "cancel":
                return client.runs.retrieve(args.run_id).cancel().data
            if args.command == "results":
                return _run_result_data(client.runs.retrieve(args.run_id).result())
            if args.command == "logs":
                return client.runs.retrieve(args.run_id).events(
                    after=args.after, limit=args.limit
                )
            if args.remote_command == "catalog":
                return {"items": client.compute_profiles()}
            if args.remote_command == "create":
                return client.runs.create(
                    ref=args.ref,
                    inputs=_json_object(args.inputs, "--inputs"),
                    compute_profile=args.compute_profile,
                ).data
            if args.remote_command == "get":
                return client.runs.retrieve(args.run_id).data
            if args.remote_command == "results":
                return _run_result_data(client.runs.retrieve(args.run_id).result())
    except CloudApiError as exc:
        raise CliFailure(
            getattr(exc, "code", None) or "cloud_api_error",
            str(exc),
            exit_code=EXIT_AUTH if getattr(exc, "status_code", None) in {401, 403} else EXIT_OPERATION,
            details={"statusCode": getattr(exc, "status_code", None)},
        ) from exc
    raise CliFailure("usage", "Unsupported runs command", exit_code=EXIT_USAGE)


def _jobs(argv: list[str]) -> dict[str, Any]:
    parser = _parser(prog="biosimulant jobs")
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--limit", type=int, default=50)
    get_parser = subparsers.add_parser("get")
    get_parser.add_argument("job_id")
    args = parser.parse_args(argv)
    raise CliFailure(
        "capability_unavailable",
        UNAVAILABLE_COMMANDS[f"jobs {args.command}"],
        exit_code=EXIT_UNAVAILABLE,
        details={"capability": f"jobs.{args.command}"},
    )


def _json_object(raw: str, flag: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliFailure("usage", f"{flag} must be a JSON object", exit_code=EXIT_USAGE) from exc
    if not isinstance(value, dict):
        raise CliFailure("usage", f"{flag} must be a JSON object", exit_code=EXIT_USAGE)
    return value


def _run_result_data(result: Any) -> dict[str, Any]:
    return {
        "run_id": result.run_id,
        "outputs": result.outputs,
        "artifacts": [artifact.__dict__ for artifact in result.artifacts],
        "provenance": result.provenance,
    }


def _run_legacy(argv: list[str], options: GlobalOptions) -> Any:
    delegated = list(argv)
    if options.no_open and delegated[:2] == ["labs", "serve"]:
        delegated.append("--no-open")
    if options.machine:
        delegated.append("--json")
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                _legacy_main(delegated, prog="biosimulant")
        except SystemExit as exc:
            code = int(exc.code or 0)
            if code == 0:
                text = stdout.getvalue()
                if text:
                    print(text, end="")
                return None
            raise _legacy_failure(code, stdout.getvalue(), stderr.getvalue()) from exc
        return _parse_legacy_payload(stdout.getvalue())
    try:
        _legacy_main(delegated, prog="biosimulant")
    except SystemExit as exc:
        if int(exc.code or 0) == 0:
            raise
        raise CliFailure(
            "command_failed",
            f"Command failed with exit code {exc.code}",
            exit_code=_normalize_exit_code(int(exc.code or 1)),
        ) from exc
    return None


def _parse_legacy_payload(stdout: str) -> Any:
    for line in reversed(stdout.splitlines()):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {"output": stdout.rstrip()} if stdout.strip() else None


def _legacy_failure(code: int, stdout: str, stderr: str) -> CliFailure:
    payload = _parse_legacy_payload(stderr) or _parse_legacy_payload(stdout)
    message = stderr.strip() or stdout.strip() or f"Command failed with exit code {code}"
    error_code = "command_failed"
    details: Any = payload
    if isinstance(payload, dict):
        message = str(payload.get("error") or payload.get("message") or message)
        if payload.get("valid") is False:
            error_code = "invalid_content"
    message_lower = message.lower()
    if "not found" in message_lower or "could not find" in message_lower:
        error_code = "not_found"
    elif "already exists" in message_lower or "conflict" in message_lower:
        error_code = "conflict"
    return CliFailure(
        error_code,
        message,
        exit_code=(
            EXIT_NOT_FOUND
            if error_code == "not_found"
            else EXIT_CONFLICT
            if error_code == "conflict"
            else _normalize_exit_code(code, invalid=error_code == "invalid_content")
        ),
        details=details,
    )


def _normalize_exit_code(code: int, *, invalid: bool = False) -> int:
    if code == 130:
        return 130
    if invalid:
        return EXIT_INVALID
    if code == 2:
        return EXIT_USAGE
    return EXIT_OPERATION


def _failure_for_exception(exc: Exception) -> CliFailure:
    message = str(exc)
    if isinstance(exc, CredentialError):
        return CliFailure(
            exc.code,
            message,
            exit_code=exc.exit_code,
            details=exc.details,
        )
    lower = message.lower()
    if "authentication" in lower or "unauthorized" in lower or "forbidden" in lower:
        return CliFailure("authentication", message, exit_code=EXIT_AUTH)
    if "not found" in lower:
        return CliFailure("not_found", message, exit_code=EXIT_NOT_FOUND)
    if "already exists" in lower or "immutable" in lower or "conflict" in lower:
        return CliFailure("conflict", message, exit_code=EXIT_CONFLICT)
    return CliFailure("operation_failed", message)


def _envelope(command: str, *, data: Any = None, error: Any = None) -> dict[str, Any]:
    return {
        "ok": error is None,
        "data": data if error is None else None,
        "error": error,
        "meta": {
            "schemaVersion": SCHEMA_VERSION,
            "command": command,
            "cliVersion": __version__,
        },
    }


def _legacy_desktop_envelope(
    *,
    data: Any = None,
    error: Any = None,
) -> dict[str, Any]:
    return {
        "ok": error is None,
        "data": data if error is None else None,
        "error": error,
        "meta": {
            "format": "json",
            "cwd": str(Path.cwd()),
        },
    }


def _emit_success(payload: Any, options: GlobalOptions, command: str) -> None:
    if options.json_stream:
        _emit_event(
            command,
            "result",
            seq=2,
            stage="complete",
            message="Command completed",
            percentage=100,
            result=_envelope(command, data=payload),
        )
    elif options.legacy_json == "bare":
        print(json.dumps(payload, sort_keys=True))
    elif options.legacy_json == "desktop":
        print(json.dumps(_legacy_desktop_envelope(data=payload), sort_keys=True))
    elif options.json_output:
        print(json.dumps(_envelope(command, data=payload), sort_keys=True))
    else:
        _emit_human(payload, command)


def _emit_human(payload: Any, command: str) -> None:
    if payload is None:
        return
    print(f"Biosimulant {command.replace('.', ' ')} succeeded.")
    if isinstance(payload, dict):
        if "healthy" in payload:
            print(f"Healthy: {'yes' if payload['healthy'] else 'no'}")
        elif "authenticated" in payload:
            print(f"Registry: {payload.get('registry')}")
            print(f"Authenticated: {'yes' if payload['authenticated'] else 'no'}")
        elif "commands" in payload:
            for item in payload["commands"]:
                print(f"{item['path']:<28} {item['summary']}")
        else:
            print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(payload)


def _emit_failure(error: CliFailure, options: GlobalOptions, command: str) -> None:
    payload = _envelope(
        command,
        error={
            "code": error.code,
            "message": str(error),
            "details": error.details,
        },
    )
    if options.json_stream:
        _emit_event(
            command,
            "result",
            seq=2,
            stage="failed",
            message=str(error),
            result=payload,
        )
    elif options.legacy_json == "bare":
        print(
            json.dumps(
                {
                    "error": error.code,
                    "message": str(error),
                    "details": error.details,
                },
                sort_keys=True,
            )
        )
    elif options.legacy_json == "desktop":
        print(
            json.dumps(
                _legacy_desktop_envelope(error=payload["error"]),
                sort_keys=True,
            )
        )
    elif options.json_output:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"Error: {error}", file=sys.stderr)


def _emit_event(
    command: str,
    event_type: str,
    *,
    seq: int,
    stage: str,
    message: str,
    percentage: int | None = None,
    **payload: Any,
) -> None:
    event = {
        "type": event_type,
        "seq": seq,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": command,
        "stage": stage,
        "message": message,
        **payload,
    }
    if percentage is not None:
        event["percentage"] = percentage
    print(json.dumps(event, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
