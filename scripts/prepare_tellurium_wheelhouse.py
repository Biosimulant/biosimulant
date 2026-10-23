#!/usr/bin/env python3
"""Build the local Tellurium packaging candidate; never modify installed packages.

Only WHEEL and RECORD change. Native libraries, Python sources, METADATA and
licenses retain their upstream bytes. The build number identifies the derivative.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import urllib.request
import zipfile
from pathlib import Path

SOURCES = {
    "antimony": (
        "3.2.0",
        "antimony-3.2.0-py3-none-manylinux_2_28_x86_64.whl",
        "2036c1dcf699a8f1dedf35d26007e0060ddbd91f9f4bb9f4d05fa8a79cc7c2fd",
    ),
    "phrasedml": (
        "1.3.0",
        "phrasedml-1.3.0-py3-none-manylinux2014_x86_64.whl",
        "b0e7e58cb84377d9f021c335dd2bd44a6a1e608c8f3b779a1b511d6ea2961ce6",
    ),
}


def corrected_wheel(
    blob: bytes, *, name: str, version: str, python: str
) -> tuple[str, bytes, dict]:
    tag = f"cp{python.replace('.', '')}-none-linux_x86_64"
    filename = f"{name}-{version}-1bsim1-{tag}.whl"
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        files = {entry.filename: archive.read(entry) for entry in archive.infolist()}
        attributes = {
            entry.filename: (entry.create_system, entry.external_attr)
            for entry in archive.infolist()
        }
    wheel = f"{name}-{version}.dist-info/WHEEL"
    record = f"{name}-{version}.dist-info/RECORD"
    original_tag = [line for line in files[wheel].decode().splitlines() if line.startswith("Tag:")]
    lines = [
        line
        for line in files[wheel].decode().splitlines()
        if not line.startswith(("Tag:", "Build:"))
    ]
    files[wheel] = ("\n".join([*lines, "Build: 1bsim1", f"Tag: {tag}"]) + "\n").encode()
    files.pop(record)
    rows = []
    for path, payload in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
        rows.append((path, f"sha256={digest}", str(len(payload))))
    rows.append((record, "", ""))
    text = io.StringIO(newline="")
    csv.writer(text, lineterminator="\n").writerows(rows)
    files[record] = text.getvalue().encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for path, payload in sorted(files.items()):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system, info.external_attr = attributes[path]
            archive.writestr(info, payload)
    provenance = {
        "filename": filename,
        "sha256": hashlib.sha256(output.getvalue()).hexdigest(),
        "original_tags": original_tag,
        "tag": tag,
        "modified_members": [wheel, record],
        "unchanged_payload_sha256": {
            path: hashlib.sha256(payload).hexdigest()
            for path, payload in sorted(files.items())
            if path not in {wheel, record}
        },
    }
    return filename, output.getvalue(), provenance


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=True)
    provenance = {
        "schema_version": 1,
        "scope": "local CPython 3.11/3.12 Linux x86_64 candidate",
        "sources": {},
    }
    for name, (version, filename, expected) in SOURCES.items():
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/{name}/{version}/json", timeout=60
        ) as response:
            metadata = json.load(response)
        artifact = next(item for item in metadata["urls"] if item["filename"] == filename)
        if artifact["digests"]["sha256"] != expected:
            raise ValueError(f"Upstream checksum changed: {filename}")
        with urllib.request.urlopen(artifact["url"], timeout=60) as response:
            blob = response.read()
        if hashlib.sha256(blob).hexdigest() != expected:
            raise ValueError(f"Downloaded checksum mismatch: {filename}")
        (args.destination / filename).write_bytes(blob)
        candidates = []
        for python in ("3.11", "3.12"):
            output_name, payload, record = corrected_wheel(
                blob, name=name, version=version, python=python
            )
            (args.destination / output_name).write_bytes(payload)
            candidates.append(record)
        provenance["sources"][name] = {
            "url": artifact["url"],
            "filename": filename,
            "sha256": expected,
            "candidates": candidates,
        }
    (args.destination / "provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n"
    )
    print(
        json.dumps(
            {
                name: [
                    {"filename": c["filename"], "sha256": c["sha256"]} for c in item["candidates"]
                ]
                for name, item in provenance["sources"].items()
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
