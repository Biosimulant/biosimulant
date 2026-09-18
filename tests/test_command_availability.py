"""The command catalog must not advertise commands that always fail.

`biosimulant commands list` is the contract the docs are generated from, so a
command listed as available and a command that actually runs have to be the
same set.
"""
from __future__ import annotations

import json

import pytest

from biosimulant.__main__ import UNAVAILABLE_COMMANDS, main


def _catalog(capsys) -> list[dict[str, object]]:
    main(["--json", "commands", "list"])
    payload = json.loads(capsys.readouterr().out)
    return payload["data"]["commands"]


def test_every_catalog_entry_states_availability(capsys) -> None:
    for command in _catalog(capsys):
        assert isinstance(command["available"], bool), command["path"]


def test_unavailable_commands_are_marked_and_explained(capsys) -> None:
    listed = {
        command["path"]: command
        for command in _catalog(capsys)
        if command["available"] is False
    }
    assert set(listed) == set(UNAVAILABLE_COMMANDS)
    for path, command in listed.items():
        assert command["unavailableReason"] == UNAVAILABLE_COMMANDS[path]


def test_available_commands_carry_no_reason(capsys) -> None:
    for command in _catalog(capsys):
        if command["available"]:
            assert "unavailableReason" not in command


@pytest.mark.parametrize("path", sorted(UNAVAILABLE_COMMANDS))
def test_an_unavailable_command_fails_with_its_stated_reason(path, capsys) -> None:
    tokens = path.split()
    # `runs start` and `runs get`-style commands need a positional argument.
    argv = [*tokens, "placeholder"] if tokens[-1] in {"start", "upload", "get"} else tokens
    if tokens[0] == "runs" and tokens[-1] == "upload":
        argv = [*tokens, "placeholder", "file.json"]
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 7
    assert UNAVAILABLE_COMMANDS[path] in capsys.readouterr().err


def test_jobs_has_no_available_commands(capsys) -> None:
    # Every `jobs` command is unavailable; the docs must not render a Jobs section.
    jobs = [c for c in _catalog(capsys) if c["path"].startswith("jobs ")]
    assert jobs
    assert all(command["available"] is False for command in jobs)
