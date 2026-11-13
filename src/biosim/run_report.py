"""Render a standalone, offline view of an already serialized Lab result."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from html import escape
from itertools import islice
from pathlib import Path
from typing import Any

_ROW_LIMIT = 20
_COLUMN_LIMIT = 8
_OUTPUT_LIMIT = 40
_TEXT_LIMIT = 1200


def _text(value: Any, limit: int = _TEXT_LIMIT) -> str:
    text = str(value)
    return escape(text[:limit] + ("…" if len(text) > limit else ""))


def _scalar(value: Any, limit: int = _TEXT_LIMIT) -> str:
    if value is None:
        return "Not available"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, str):
        return _text(value, limit) if value else "Empty text"
    if isinstance(value, (int, float)):
        return _text(value)
    if isinstance(value, list):
        return f"{len(value)} items"
    if isinstance(value, dict):
        return f"{len(value)} fields"
    return "Not available"


def _file_name(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def _label(name: str) -> str:
    words = name.replace("_", " ").replace("-", " ").strip()
    return words[:1].upper() + words[1:]


def _table(columns: list, rows: list) -> str:
    if not columns or not rows:
        return ""
    headers = "".join(f'<th scope="col">{_text(key, 120)}</th>' for key in columns[:_COLUMN_LIMIT])
    body = []
    for row in rows[:_ROW_LIMIT]:
        if isinstance(row, dict):
            cells = [
                _scalar(row[key], 240) if isinstance(key, str) and key in row else "—"
                for key in columns[:_COLUMN_LIMIT]
            ]
        elif isinstance(row, (list, tuple)):
            cells = [_scalar(value, 240) for value in row[:min(len(columns), _COLUMN_LIMIT)]]
            cells.extend(["—"] * (min(len(columns), _COLUMN_LIMIT) - len(cells)))
        else:
            continue
        body.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    if not body:
        return ""
    note = ""
    if len(rows) > _ROW_LIMIT or len(columns) > _COLUMN_LIMIT:
        note = f"<p>Showing up to {_ROW_LIMIT} rows and {_COLUMN_LIMIT} columns.</p>"
    return f'<div class="table"><table><thead><tr>{headers}</tr></thead><tbody>{"".join(body)}</tbody></table></div>{note}'


def _row_records(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return ""
    sample = value[:_ROW_LIMIT]
    if not all(isinstance(row, dict) for row in sample):
        return ""
    columns: dict[str, None] = {}
    for row in sample:
        for key in row:
            columns.setdefault(key, None)
            if len(columns) > _COLUMN_LIMIT:
                break
        if len(columns) > _COLUMN_LIMIT:
            break
    return _table(list(columns), value)


def _value(value: Any) -> str:
    if isinstance(value, dict):
        if value.get("kind") == "artifact" and isinstance(value.get("file_name"), str):
            return f"File · {_text(_file_name(value['file_name']))}"
        if isinstance(value.get("columns"), list) and isinstance(value.get("rows"), list):
            if table := _table(value["columns"], value["rows"]):
                return table
        if table := _row_records(value.get("rows")):
            return table
        if value.get("type") == "array" and isinstance(value.get("data"), list):
            return _value(value["data"])
        fields = "".join(
            f"<dt>{_text(_label(key), 120)}</dt><dd>{_scalar(item)}</dd>"
            for key, item in islice(value.items(), _COLUMN_LIMIT)
        )
        if fields:
            note = "<p>More fields are available in the results data.</p>" if len(value) > _COLUMN_LIMIT else ""
            return f"<dl>{fields}</dl>{note}"
    if table := _row_records(value):
        return table
    if isinstance(value, list) and value:
        items = "".join(f"<li>{_scalar(item, 240)}</li>" for item in value[:_ROW_LIMIT])
        note = f"<p>Showing the first {_ROW_LIMIT} items.</p>" if len(value) > _ROW_LIMIT else ""
        return f"<ul>{items}</ul>{note}"
    return f'<p class="value">{_scalar(value)}</p>'


def _outputs(result: dict[str, Any]) -> Iterator[tuple[str, str, dict]]:
    outputs = result.get("outputs")
    if not isinstance(outputs, dict):
        return
    for alias, entry in outputs.items():
        if not isinstance(entry, dict):
            continue
        if "value" in entry and (
            not isinstance(entry["value"], dict) or "value" not in entry["value"] or "spec" in entry
        ):
            yield "", alias, entry
        else:
            for name, signal in entry.items():
                if isinstance(signal, dict) and "value" in signal:
                    yield alias, name, signal


def _output_html(alias: str, name: str, signal: dict) -> str:
    spec = signal.get("spec")
    spec = spec if isinstance(spec, dict) else {}
    description = spec.get("description")
    unit = spec.get("emitted_unit")
    heading = _text(f"{alias} · {_label(name)}" if alias else _label(name), 180)
    if isinstance(unit, str) and unit.strip() not in {"", "1"}:
        heading += f" ({_text(unit.strip(), 80)})"
    note = f"<p>{_text(description)}</p>" if isinstance(description, str) and description else ""
    value = signal["value"]
    file_port = spec.get("value_type") == "file" or name.endswith(("_file", "_path"))
    if file_port and isinstance(value, str) and value:
        content = f"<p>File · {_text(_file_name(value))}</p>"
    elif file_port and isinstance(value, list):
        names = [_text(_file_name(item)) for item in value[:_ROW_LIMIT] if isinstance(item, str) and item]
        content = "<ul>" + "".join(f"<li>{item}</li>" for item in names) + "</ul>" if names else _value(value)
    else:
        content = _value(value)
    return f"<section><h3>{heading}</h3>{note}{content}</section>"


def _visuals(result: dict[str, Any]) -> Iterator[str]:
    groups = result.get("visuals")
    if not isinstance(groups, list):
        return
    for group in groups[:64]:
        visuals = group.get("visuals") if isinstance(group, dict) else None
        if not isinstance(visuals, list):
            continue
        for visual in visuals[:64]:
            data = visual.get("data") if isinstance(visual, dict) else None
            if not isinstance(data, dict):
                continue
            kind = visual.get("render")
            content = ""
            if kind == "text":
                text = data.get("text", data.get("markdown"))
                if isinstance(text, str) and text.strip():
                    content = f'<p class="value">{_text(text)}</p>'
            elif kind == "table" and isinstance(data.get("columns"), list) and isinstance(data.get("rows"), list):
                content = _table(data["columns"], data["rows"])
            elif kind == "bar" and isinstance(data.get("items"), list):
                content = _table(["label", "value", "unit"], data["items"])
            elif kind == "scatter" and isinstance(data.get("points"), list):
                points = data["points"]
                rows = [
                    [point.get("x"), point.get("y"), point.get("label")]
                    for point in points[:_ROW_LIMIT] if isinstance(point, dict)
                ]
                content = _table([_axis_label(data, "x", "X"), _axis_label(data, "y", "Y"), "Label"], rows)
                if content and len(points) > _ROW_LIMIT:
                    content += f"<p>Showing the first {_ROW_LIMIT} points.</p>"
            elif kind == "timeseries" and isinstance(data.get("series"), list):
                columns = [
                    _axis_label(data, "x", "Time"),
                    _axis_label(data, "y", "Value"),
                ]
                for series in data["series"][:_COLUMN_LIMIT]:
                    if isinstance(series, dict) and isinstance(series.get("points"), list):
                        if table := _table(columns, series["points"]):
                            series_name = series.get("name")
                            if isinstance(series_name, str) and series_name:
                                content += f"<p>{_text(series_name, 120)}</p>"
                            content += table
                if len(data["series"]) > _COLUMN_LIMIT:
                    content += f"<p>Showing up to {_COLUMN_LIMIT} series.</p>"
            elif kind in ("image", "structure3d"):
                source = data.get("source")
                if isinstance(source, dict) and source.get("kind") == "artifact":
                    name = source.get("file_name") or source.get("path")
                    if isinstance(name, str) and name:
                        content = f"<p>File · {_text(_file_name(name))}</p>"
                caption = data.get("caption") or data.get("alt")
                if isinstance(caption, str) and caption:
                    content += f"<p>{_text(caption)}</p>"
            if content:
                title = _text(data.get("title") or _label(str(kind)), 180)
                description = visual.get("description")
                note = f"<p>{_text(description)}</p>" if isinstance(description, str) else ""
                yield f"<section><h3>{title}</h3>{note}{content}</section>"


def _axis_label(data: dict, axis: str, fallback: str) -> str:
    label = data.get(f"{axis}_label") or fallback
    unit = data.get(f"{axis}_unit")
    return f"{label} ({unit})" if isinstance(unit, str) and unit else str(label)


def write_run_report(report_file: Path, package_file: Path, result: dict[str, Any]) -> None:
    generated_at = datetime.now(timezone.utc).isoformat()
    payload = {"package_file": str(package_file), "generated_at": generated_at, "result": result}
    embedded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    title = _text(result.get("package") or package_file.name, 180)
    metadata = []
    for label, key in (("Version", "version"), ("Duration", "duration")):
        if result.get(key) is not None:
            metadata.append(f"<dt>{label}</dt><dd>{_scalar(result[key])}</dd>")
    metadata.append(f"<dt>Generated (UTC)</dt><dd>{_text(generated_at)}</dd>")
    output_items = list(islice(_outputs(result), _OUTPUT_LIMIT + 1))
    outputs = "".join(_output_html(*item) for item in output_items[:_OUTPUT_LIMIT])
    if len(output_items) > _OUTPUT_LIMIT:
        outputs += f"<p>Showing the first {_OUTPUT_LIMIT} outputs.</p>"
    visual_items = list(islice(_visuals(result), 13))
    visuals = "".join(visual_items[:12])
    if len(visual_items) > 12:
        visuals += "<p>Showing the first 12 visual summaries.</p>"
    cautions = []
    for key in ("intended_use", "caveats"):
        values = result.get(key)
        if isinstance(values, str):
            values = [values]
        if isinstance(values, list):
            cautions.extend(f"<li>{_text(item)}</li>" for item in values[:_ROW_LIMIT] if isinstance(item, str) and item.strip())
    diagnostics = result.get("non_finite_values")
    if isinstance(diagnostics, dict) and diagnostics.get("count"):
        cautions.append("<li>Some non-finite values are unavailable. See the results data for their locations.</li>")
    sections = []
    if outputs:
        sections.append(f"<h2>Outputs</h2>{outputs}")
    if visuals:
        sections.append(f"<h2>Visual summaries</h2>{visuals}")
    if cautions:
        sections.append(f"<h2>Notes and limitations</h2><ul>{''.join(cautions)}</ul>")
    if not (outputs or visuals):
        sections.insert(0, "<p>No readable outputs were recorded. See the results data for details.</p>")
    # Keep the machine payload once. Show a bounded text preview in the page,
    # including on browsers where scripts are disabled.
    chunks = json.JSONEncoder(ensure_ascii=False, sort_keys=True, indent=2).iterencode(payload)
    pieces = []
    remaining = 8000
    for chunk in chunks:
        pieces.append(chunk[:remaining])
        remaining -= len(chunk)
        if remaining <= 0:
            break
    raw = "".join(pieces)
    if remaining <= 0:
        raw += "\n… Full JSON is available in the results file and embedded report data."
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Biosimulant Run Report</title>
<style>
  body {{ margin: 0; color: #18201b; background: #f7f4ed;
    font: 16px/1.5 system-ui, sans-serif; overflow-wrap: anywhere; }}
  main {{ max-width: 58rem; margin: auto; padding: 2rem 1rem 4rem; }}
  section, details {{ padding: 1rem; margin: 0 0 1rem; border: 1px solid #d4cfc5;
    border-radius: .5rem; background: #fffdf7; }}
  h3 {{ margin: 0 0 .5rem; }}
  dt, th {{ font-weight: 600; }}
  dd {{ margin: 0 0 .5rem; }}
  table {{ border-collapse: collapse; width: 100%; text-align: left; }}
  th, td {{ padding: .4rem .6rem; border-bottom: 1px solid #d4cfc5; vertical-align: top; }}
  .table {{ overflow-x: auto; }}
  .value, pre {{ white-space: pre-wrap; }}
  summary {{ cursor: pointer; }}
</style>
</head>
<body>
<main>
  <h1>Biosimulant run report</h1>
  <p>{title}</p>
  <dl>{"".join(metadata)}</dl>
  {"".join(sections)}
  <details><summary>Technical details</summary><pre id="biosimulant-result">{escape(raw)}</pre></details>
</main>
<script type="application/json" id="biosimulant-report-data">{embedded}</script>
</body>
</html>
"""
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(document, encoding="utf-8")
