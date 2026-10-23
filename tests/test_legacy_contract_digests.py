"""Saved identities derived independently from the checksum-pinned old wheel."""

import json
from pathlib import Path

import pytest

from biosim import SignalEnvelope
from biosim import compatibility

SNAPSHOT = json.loads(
    (Path(__file__).parent / "fixtures" / "legacy_contract_digests.json").read_text()
)


@pytest.mark.parametrize("catalogue_installed", [False, True])
@pytest.mark.parametrize("ref, expected", SNAPSHOT["contracts"].items())
def test_saved_envelopes_keep_their_identity(monkeypatch, catalogue_installed, ref, expected):
    def optional_profile_digest(profile):
        if not catalogue_installed:
            raise KeyError(profile)
        return SNAPSHOT["profiles"][profile]

    monkeypatch.setattr(compatibility, "profile_digest", optional_profile_digest)
    contract = {"profile": ref}
    saved = {"schema_version": "0.1", "contract_digest": expected, "value": "retained output"}
    envelope = SignalEnvelope.from_dict(saved)
    envelope.validate_contract(contract)
    assert envelope.to_dict() == saved
    assert compatibility.contract_digest(contract) == expected
    with pytest.raises(ValueError, match="different contract"):
        envelope.validate_contract({**contract, "species": "NCBITaxon:9606"})


def test_optional_catalogue_cannot_change_new_or_existing_envelope_identity(monkeypatch):
    known = {"profile": "protein.sequence/v1", "species": "NCBITaxon:9606"}
    unknown = {"profile": "local.annotation/v1", "identifier_namespace": "local"}
    before = [compatibility.contract_digest(contract) for contract in (known, unknown)]
    monkeypatch.setattr(compatibility, "profile_digest", lambda _ref: "sha256:" + "f" * 64)
    assert [compatibility.contract_digest(contract) for contract in (known, unknown)] == before


def test_saved_artifact_envelope_retains_metadata_and_owns_its_data():
    ref = "protein.sequence/v1"
    saved = {
        "schema_version": "0.1",
        "contract_digest": SNAPSHOT["contracts"][ref],
        "artifact": {"uri": "file:///retained/sequence.txt", "sha256": "a" * 64},
        "origin": {"run": "historical-run"},
        "provenance": {"sources": ["retained-evidence"]},
        "observation_time": "2026-10-20T00:00:00Z",
    }
    envelope = SignalEnvelope.from_dict(saved)
    envelope.validate_contract({"profile": ref})
    assert envelope.to_dict() == saved
    saved["provenance"]["sources"].append("changed")
    exported = envelope.to_dict()
    exported["artifact"]["uri"] = "changed"
    assert envelope.provenance["sources"] == ["retained-evidence"]
    assert envelope.artifact["uri"] == "file:///retained/sequence.txt"
