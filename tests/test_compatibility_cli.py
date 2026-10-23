"""The public compatibility CLI remains useful without scientific grading."""

import json

import pytest
import yaml

from biosim.__main__ import main
from biosim import compatibility_cli as cli


@pytest.mark.parametrize("installed", [False, True])
def test_optional_catalogue_inspection(monkeypatch, capsys, installed):
    profile = {
        "profile": {"id": "protein.sequence", "version": "1", "title": "Sequence"},
        "checker": "sequence",
    }
    digest = "sha256:" + "a" * 64
    monkeypatch.setattr(cli, "CATALOGUE_VERSION", "0.1.0" if installed else None)
    monkeypatch.setattr(cli, "CATALOGUE_SHA256", digest if installed else None)
    monkeypatch.setattr(cli, "list_profiles", lambda: [profile] if installed else [])

    def get_profile(ref):
        if not installed:
            raise KeyError(ref)
        return profile

    monkeypatch.setattr(cli, "get_profile", get_profile)
    monkeypatch.setattr(cli, "profile_digest", lambda _ref: digest)
    main(["compatibility", "standard"])
    result = json.loads(capsys.readouterr().out)
    assert result["catalogue_version"] == ("0.1.0" if installed else None)
    main(["compatibility", "profiles"])
    result = json.loads(capsys.readouterr().out)
    assert result["count"] == (1 if installed else 0)
    if installed:
        main(["compatibility", "show", "protein.sequence/v1"])
        assert json.loads(capsys.readouterr().out)["sha256"] == digest
    else:
        with pytest.raises(SystemExit) as caught:
            main(["compatibility", "show", "protein.sequence/v1"])
        assert caught.value.code == 2
        assert "protein.sequence/v1" in capsys.readouterr().err


@pytest.fixture
def manifest(tmp_path):
    path = tmp_path / "model.yaml"
    path.write_text(yaml.safe_dump({
        "io": {
            "outputs": [{"name": "sequence", "signal_type": "scalar", "dtype": "str", "format": "sequence"}],
            "inputs": [{"name": "sequence", "signal_type": "scalar", "dtype": "str", "format": "sequence"}],
        },
    }))
    return path


def test_validate_and_compare_use_port_structure_not_payload_grading(manifest, capsys):
    main(["compatibility", "validate", str(manifest)])
    assert json.loads(capsys.readouterr().out) == {"valid": True, "findings": []}
    for sample in ([], ["--sample-json", '"not a biological sequence"']):
        main([
            "compatibility", "compare", f"{manifest}#outputs.sequence",
            f"{manifest}#inputs.sequence", *sample,
        ])
        result = json.loads(capsys.readouterr().out)
        assert result["compatible"] and result["issues"] == []
    data = yaml.safe_load(manifest.read_text())
    data["io"]["inputs"][0]["format"] = "fasta"
    manifest.write_text(yaml.safe_dump(data))
    with pytest.raises(SystemExit) as caught:
        main(["compatibility", "compare", f"{manifest}#outputs.sequence", f"{manifest}#inputs.sequence"])
    assert caught.value.code == 2
    assert json.loads(capsys.readouterr().out)["compatible"] is False


@pytest.mark.parametrize("source,target,extra,message", [
    ("", "#inputs.sequence", [], "Point to a port"),
    ("#invalid.sequence", "#inputs.sequence", [], "Port references"),
    ("#outputs.", "#inputs.sequence", [], "Port references"),
    ("#outputs.missing", "#inputs.sequence", [], "No output port"),
    ("#inputs.sequence", "#outputs.sequence", [], "source must be an output"),
    ("#outputs.sequence", "#inputs.sequence", ["--sample-json", "{"], "not valid JSON"),
])
def test_compare_reports_actionable_input_errors(manifest, capsys, source, target, extra, message):
    with pytest.raises(SystemExit) as caught:
        main(["compatibility", "compare", str(manifest) + source, str(manifest) + target, *extra])
    assert caught.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("ports,message", [
    ({"outputs": "invalid"}, "must be a list"),
    ({"outputs": [42]}, "must be a mapping"),
    ({"outputs": [{"name": ""}]}, "non-empty string"),
    ({"outputs": [{"name": "same"}, {"name": "same"}]}, "more than once"),
])
def test_validate_reports_invalid_port_declarations(manifest, capsys, ports, message):
    manifest.write_text(yaml.safe_dump({"io": ports}))
    with pytest.raises(SystemExit) as caught:
        main(["compatibility", "validate", str(manifest)])
    assert caught.value.code == 2
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is False and message in result["findings"][0]["message"]


@pytest.mark.parametrize("ports,message", [
    ({}, "has no io.outputs list"),
    ({"outputs": [{"name": "sequence"}]}, "does not declare signal_type"),
    ({"outputs": "invalid"}, "must be a list"),
])
def test_compare_handles_incomplete_manifests(manifest, capsys, ports, message):
    manifest.write_text(yaml.safe_dump({"io": ports}))
    with pytest.raises(SystemExit) as caught:
        main(["compatibility", "compare", f"{manifest}#outputs.sequence", f"{manifest}#inputs.sequence"])
    assert caught.value.code == 2
    assert message in capsys.readouterr().err
