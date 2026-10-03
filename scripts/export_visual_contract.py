#!/usr/bin/env python3
"""Export canonical v1 catalog/schema for docs and client contract tests."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from biosim.visual_contract import visualization_catalog, visual_spec_schema

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--directory", type=Path, action="append", required=True)
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
for directory in args.directory:
    for name, document in [
        ("visual-catalog-v1.json", visualization_catalog()),
        ("visual-spec-v1.json", visual_spec_schema()),
    ]:
        expected = json.dumps(document, indent=2, sort_keys=True) + "\n"
        path = directory / name
        if args.check:
            if not path.is_file() or path.read_text() != expected:
                raise SystemExit(
                    f"Contract drift in {path}; run export_visual_contract.py"
                )
        else:
            directory.mkdir(parents=True, exist_ok=True)
            path.write_text(expected)
