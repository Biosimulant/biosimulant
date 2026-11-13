"""Render a standalone, offline view of an already serialized Lab result.

The page is one self-contained HTML file: inline CSS, no executable script and
no external assets, so it opens the same way from a terminal, CI artifact,
Studio's sandboxed preview or a printer. The complete result travels with it
as the ``biosimulant-report-data`` JSON block.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from itertools import islice
from pathlib import Path
from typing import Any

_ROW_LIMIT = 20
_COLUMN_LIMIT = 8
_OUTPUT_LIMIT = 40
_VISUAL_LIMIT = 12
_KEY_LIMIT = 8
_TEXT_LIMIT = 1200
_INLINE_TEXT_LIMIT = 160
_RAW_LIMIT = 8000

_FILE_SUFFIXES = ("_file", "_path", "_files", "_paths")
_UNSHOWN_RENDERS = {"heatmap": "heatmap", "graph": "graph"}


# ---------------------------------------------------------------------------
# Text and number formatting. Everything model-supplied goes through _text.


def _text(value: Any, limit: int = _TEXT_LIMIT) -> str:
    text = str(value)
    return escape(text[:limit] + ("…" if len(text) > limit else ""))


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _significant_digits(text: str) -> int:
    mantissa = text.lstrip("-").split("e")[0].split("E")[0]
    digits = mantissa.replace(".", "").lstrip("0")
    if "." not in mantissa:
        digits = digits.rstrip("0") or digits
    return len(digits)


def _number(value: int | float) -> str:
    """Show a number as recorded, up to six significant figures."""

    if isinstance(value, float) and not math.isfinite(value):
        return "Not available"
    if isinstance(value, int) and abs(value) < 10**15:
        return str(value)
    value = float(value)
    magnitude = abs(value)
    if value == 0 or 1e-4 <= magnitude < 1e6:
        shortest = repr(value)
        if "e" not in shortest and _significant_digits(shortest) <= 6:
            return shortest
        compact = f"{value:.6g}"
        if "e" not in compact:
            return compact
    mantissa, exponent = f"{value:.6g}".split("e")
    if "." in mantissa:
        mantissa = mantissa.rstrip("0").rstrip(".")
    power = str(int(exponent)).replace("-", "−")
    return f"{mantissa} × 10<sup>{power}</sup>"


def _scalar(value: Any, limit: int = _TEXT_LIMIT) -> str:
    if value is None:
        return '<span class="na">Not available</span>'
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, str):
        return _text(value, limit) if value else '<span class="na">Empty text</span>'
    if _is_number(value):
        return _number(value)
    if isinstance(value, list):
        return f"{len(value)} items"
    if isinstance(value, dict):
        if value.get("kind") == "artifact" and isinstance(value.get("file_name"), str):
            return _text(_file_name(value["file_name"]), 240)
        return f"{len(value)} fields"
    return '<span class="na">Not available</span>'


def _file_name(value: str) -> str:
    return value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _label(name: str) -> str:
    words = str(name).replace("_", " ").replace("-", " ").strip()
    return words[:1].upper() + words[1:]


def _unit(spec: dict) -> str | None:
    unit = spec.get("emitted_unit")
    if isinstance(unit, str) and unit.strip() not in {"", "1"}:
        return unit.strip()
    return None


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


# ---------------------------------------------------------------------------
# Tables


def _table(columns: list, rows: list, headers: list | None = None) -> tuple[str, int]:
    """Return a table and the number of rows it describes ('' when unusable)."""

    if not columns or not rows:
        return "", 0
    keys = columns[:_COLUMN_LIMIT]
    titles = (headers or columns)[:_COLUMN_LIMIT]
    grid: list[list[Any]] = []
    usable = 0
    for row in rows:
        if isinstance(row, dict):
            cells = [row.get(key, ...) if isinstance(key, str) else ... for key in keys]
        elif isinstance(row, (list, tuple)):
            cells = list(row[: len(keys)]) + [...] * (len(keys) - min(len(row), len(keys)))
        else:
            continue
        usable += 1
        if len(grid) < _ROW_LIMIT:
            grid.append(cells)
    if not grid:
        return "", 0
    numeric = [
        any(_is_number(row[i]) for row in grid)
        and all(_is_number(row[i]) or row[i] is None or row[i] is ... for row in grid)
        for i in range(len(keys))
    ]
    head = "".join(
        ('<th scope="col" class="num">' if numeric[i] else '<th scope="col">') + f"{_text(title, 120)}</th>"
        for i, title in enumerate(titles)
    )
    body = []
    for row in grid:
        cells = []
        for i, value in enumerate(row):
            content = "—" if value is ... else _scalar(value, 240)
            cells.append(f'<td class="num">{content}</td>' if numeric[i] else f"<td>{content}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    clipped = []
    if usable > _ROW_LIMIT:
        clipped.append(f"Showing the first {_ROW_LIMIT} of {usable} rows.")
    if len(columns) > _COLUMN_LIMIT:
        clipped.append(f"Showing {_COLUMN_LIMIT} of {len(columns)} columns.")
    foot = f'<p class="card-foot">{" ".join(clipped)}</p>' if clipped else ""
    html = (
        f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>{foot}'
    )
    return html, usable


def _record_columns(rows: list) -> list[str]:
    columns: dict[str, None] = {}
    for row in rows[:_ROW_LIMIT]:
        for key in row:
            columns.setdefault(key, None)
            if len(columns) > _COLUMN_LIMIT:
                return list(columns)
    return list(columns)


def _row_records(value: Any) -> tuple[str, int]:
    if not isinstance(value, list) or not value:
        return "", 0
    if not all(isinstance(row, dict) for row in value[:_ROW_LIMIT]):
        return "", 0
    columns = _record_columns(value)
    return _table(columns, value, [_label(key) for key in columns])


# ---------------------------------------------------------------------------
# Collecting what the result contains


@dataclass
class _Output:
    module: str
    name: str
    label: str
    value: Any
    unit: str | None
    description: str | None
    emitted_at: Any
    non_finite: int = 0


@dataclass
class _Card:
    module: str
    title: str
    body: str
    meta: list[str] = field(default_factory=list)
    description: str | None = None
    note: str = ""


@dataclass
class _Note:
    module: str
    title: str
    text: str


@dataclass
class _File:
    module: str
    name: str
    label: str
    description: str | None


@dataclass
class _Report:
    keys: list[_Output] = field(default_factory=list)
    rows: dict[str, list[_Output]] = field(default_factory=dict)
    cards: list[_Card] = field(default_factory=list)
    notes: list[_Note] = field(default_factory=list)
    files: list[_File] = field(default_factory=list)
    clipped: list[str] = field(default_factory=list)
    unshown: dict[str, int] = field(default_factory=dict)


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
            yield "", str(alias), entry
        else:
            for name, signal in entry.items():
                if isinstance(signal, dict) and "value" in signal:
                    yield str(alias), str(name), signal


def _non_finite_by_output(result: dict[str, Any]) -> tuple[dict[tuple[str, str], int], int]:
    record = result.get("non_finite_values")
    if not isinstance(record, dict):
        return {}, 0
    total = record.get("count") if isinstance(record.get("count"), int) else 0
    located: dict[tuple[str, str], int] = {}
    items = record.get("items")
    for item in items if isinstance(items, list) else []:
        path = item.get("path") if isinstance(item, dict) else None
        if not isinstance(path, str) or not path.startswith("outputs."):
            continue
        parts = path.split(".")
        if len(parts) >= 4 and parts[3].startswith("value"):
            key = (parts[1], parts[2])
        elif len(parts) >= 3 and parts[2].startswith("value"):
            key = ("", parts[1])
        else:
            continue
        located[key] = located.get(key, 0) + 1
    return located, max(total - sum(located.values()), 0)


def _file_entries(name: str, spec: dict, value: Any) -> list[tuple[str, str | None]] | None:
    """Return (file name, sub-label) pairs when an output names files."""

    named_file = spec.get("value_type") == "file" or name.endswith(_FILE_SUFFIXES)

    def one(item: Any) -> str | None:
        if isinstance(item, dict) and item.get("kind") == "artifact":
            source = item.get("file_name") or item.get("path")
            return _file_name(source) if isinstance(source, str) and source else None
        if named_file and isinstance(item, str) and item.strip():
            return _file_name(item)
        return None

    if (single := one(value)) is not None:
        return [(single, None)]
    if named_file and isinstance(value, list) and value:
        names = [one(item) for item in value[:_ROW_LIMIT]]
        if all(names):
            return [(item, None) for item in names if item]
    if isinstance(value, dict) and value and all(
        isinstance(key, str) and key.endswith(_FILE_SUFFIXES) for key in value
    ):
        pairs = []
        for key, item in islice(value.items(), _ROW_LIMIT):
            if isinstance(item, str) and item.strip():
                pairs.append((_file_name(item), _label(key)))
            elif isinstance(item, dict) and (entry := one(item)):
                pairs.append((entry, _label(key)))
            else:
                return None
        return pairs
    return None


def _value_body(value: Any) -> tuple[str, list[str]]:
    """Render a structured output value; return markup and caption facts."""

    if isinstance(value, dict):
        if isinstance(value.get("columns"), list) and isinstance(value.get("rows"), list):
            table, count = _table(value["columns"], value["rows"])
            if table:
                return table, [_plural(count, "row")]
        table, count = _row_records(value.get("rows"))
        if table:
            return table, [_plural(count, "row")]
        if value.get("type") == "array" and isinstance(value.get("data"), list):
            return _value_body(value["data"])
        fields = "".join(
            f"<div><dt>{_text(_label(key), 120)}</dt><dd>{_scalar(item)}</dd></div>"
            for key, item in islice(value.items(), _COLUMN_LIMIT)
        )
        if fields:
            more = (
                '<p class="card-foot">More fields are available in the results data.</p>'
                if len(value) > _COLUMN_LIMIT else ""
            )
            return f'<dl class="fields">{fields}</dl>{more}', [_plural(len(value), "field")]
    table, count = _row_records(value)
    if table:
        return table, [_plural(count, "row")]
    if isinstance(value, list) and value:
        items = "".join(f"<li>{_scalar(item, 240)}</li>" for item in value[:_ROW_LIMIT])
        more = (
            f'<p class="card-foot">Showing the first {_ROW_LIMIT} of {len(value)} items.</p>'
            if len(value) > _ROW_LIMIT else ""
        )
        return f'<ul class="items">{items}</ul>{more}', [_plural(len(value), "item")]
    return f'<p class="text">{_scalar(value)}</p>', []


def _non_finite_note(count: int) -> str:
    if not count:
        return ""
    noun = "value was" if count == 1 else "values were"
    return (
        f'<p class="inline-warn">{count} {noun} not finite (NaN or infinite) '
        "and appear as Not available.</p>"
    )


def _collect_outputs(result: dict[str, Any], report: _Report) -> None:
    located, _ = _non_finite_by_output(result)
    items = list(islice(_outputs(result), _OUTPUT_LIMIT + 1))
    if len(items) > _OUTPUT_LIMIT:
        report.clipped.append(f"Showing the first {_OUTPUT_LIMIT} outputs.")
    for alias, name, signal in items[:_OUTPUT_LIMIT]:
        spec = signal.get("spec")
        spec = spec if isinstance(spec, dict) else {}
        description = spec.get("description")
        output = _Output(
            module=alias,
            name=name,
            label=_label(name),
            value=signal["value"],
            unit=_unit(spec),
            description=description if isinstance(description, str) and description.strip() else None,
            emitted_at=signal.get("emitted_at"),
            non_finite=located.get((alias, name), 0),
        )
        value = output.value
        if (files := _file_entries(name, spec, value)) is not None:
            for file_name, sub in files:
                label = f"{output.label} · {sub}" if sub else output.label
                report.files.append(_File(alias, file_name, label, output.description))
        elif _is_number(value) and math.isfinite(value):
            report.keys.append(output)
        elif (
            value is None or isinstance(value, bool)
            or (isinstance(value, str) and len(value) <= _INLINE_TEXT_LIMIT)
        ):
            report.rows.setdefault(alias, []).append(output)
        else:
            body, meta = _value_body(value)
            title = output.label + (f" ({output.unit})" if output.unit else "")
            report.cards.append(
                _Card(alias, title, body, meta, output.description, _non_finite_note(output.non_finite))
            )


def _axis_label(data: dict, axis: str, fallback: str) -> str:
    label = data.get(f"{axis}_label") or fallback
    unit = data.get(f"{axis}_unit")
    return f"{label} ({unit})" if isinstance(unit, str) and unit else str(label)


def _visual_body(kind: Any, data: dict) -> tuple[str, list[str]]:
    if kind == "table" and isinstance(data.get("columns"), list) and isinstance(data.get("rows"), list):
        table, count = _table(data["columns"], data["rows"])
        return table, [_plural(count, "row")] if table else []
    if kind == "bar" and isinstance(data.get("items"), list):
        columns = ["label", "value", "unit"]
        if not any(isinstance(item, dict) and item.get("unit") for item in data["items"]):
            columns = ["label", "value"]
        table, count = _table(columns, data["items"], [_label(key) for key in columns])
        return table, [_plural(count, "bar")] if table else []
    if kind == "scatter" and isinstance(data.get("points"), list):
        points = [point for point in data["points"] if isinstance(point, dict)]
        rows = [[point.get("x"), point.get("y"), point.get("label", ...)] for point in points]
        headers = [_axis_label(data, "x", "X"), _axis_label(data, "y", "Y")]
        if any(point.get("label") is not None for point in points[:_ROW_LIMIT]):
            headers.append("Label")
        else:
            rows = [row[:2] for row in rows]
        table, count = _table(headers, rows)
        if table and count > _ROW_LIMIT:
            table = table.replace(
                f"Showing the first {_ROW_LIMIT} of {count} rows.",
                f"Showing the first {_ROW_LIMIT} of {count} points.",
            )
        return table, [_plural(count, "point")] if table else []
    if kind == "timeseries" and isinstance(data.get("series"), list):
        headers = [_axis_label(data, "x", "Time"), _axis_label(data, "y", "Value")]
        parts, shown = [], 0
        for series in data["series"][:_COLUMN_LIMIT]:
            if not isinstance(series, dict) or not isinstance(series.get("points"), list):
                continue
            rows = [
                [point.get("x"), point.get("y")] if isinstance(point, dict) else point
                for point in series["points"]
            ]
            table, _count = _table(headers, rows)
            if not table:
                continue
            shown += 1
            name = series.get("name")
            if isinstance(name, str) and name:
                parts.append(f'<p class="series">{_text(name, 120)}</p>')
            parts.append(table)
        if len(data["series"]) > _COLUMN_LIMIT:
            parts.append(f'<p class="card-foot">Showing {_COLUMN_LIMIT} of {len(data["series"])} series.</p>')
        return "".join(parts), [f"{shown} series"] if parts else []
    if kind in ("image", "structure3d"):
        body = ""
        source = data.get("source")
        if isinstance(source, dict) and source.get("kind") == "artifact":
            name = source.get("file_name") or source.get("path")
            if isinstance(name, str) and name:
                body = _file_list([(_file_name(name), "Figure file" if kind == "image" else "Structure file", None)])
        caption = data.get("caption") or data.get("alt")
        if isinstance(caption, str) and caption:
            body += f'<p class="caption">{_text(caption)}</p>'
        return body, []
    return "", []


def _collect_visuals(result: dict[str, Any], report: _Report) -> None:
    groups = result.get("visuals")
    if not isinstance(groups, list):
        return
    shown = 0
    hidden = False
    for group in groups[:64]:
        if not isinstance(group, dict) or not isinstance(group.get("visuals"), list):
            continue
        module = group.get("module") if isinstance(group.get("module"), str) else ""
        for visual in group["visuals"][:64]:
            data = visual.get("data") if isinstance(visual, dict) else None
            if not isinstance(data, dict):
                continue
            kind = visual.get("render")
            title = data.get("title")
            description = visual.get("description")
            description = description if isinstance(description, str) and description.strip() else None
            if kind == "text":
                text = data.get("text", data.get("markdown"))
                if not (isinstance(text, str) and text.strip()):
                    continue
                if shown >= _VISUAL_LIMIT:
                    hidden = True
                    continue
                shown += 1
                report.notes.append(_Note(module, str(title or "Note"), text))
                continue
            if kind in _UNSHOWN_RENDERS:
                report.unshown[_UNSHOWN_RENDERS[kind]] = report.unshown.get(_UNSHOWN_RENDERS[kind], 0) + 1
                continue
            body, meta = _visual_body(kind, data)
            if not body:
                continue
            if shown >= _VISUAL_LIMIT:
                hidden = True
                continue
            shown += 1
            report.cards.append(_Card(module, str(title or _label(str(kind))), body, meta, description))
    if hidden:
        report.clipped.append(f"Showing the first {_VISUAL_LIMIT} visual summaries.")


# ---------------------------------------------------------------------------
# Page sections

_ICON_INFO = (
    '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/>'
    '<path d="M12 11v5M12 8h.01"/></svg>'
)
_ICON_WARN = (
    '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.5 21.5 20h-19z"/>'
    '<path d="M12 10v4M12 17h.01"/></svg>'
)
_ICON_FILE = (
    '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3H6v18h12V7z"/>'
    '<path d="M14 3v4h4"/></svg>'
)
_ICON_EMPTY = (
    '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="2"/>'
    '<path d="M8 12h8"/></svg>'
)


def _file_list(files: list[tuple[str, str, str | None]]) -> str:
    items = []
    for name, label, description in files:
        about = f'<span class="file-about">{_text(description, 400)}</span>' if description else ""
        items.append(
            f'<li>{_ICON_FILE}<span class="file-text"><span class="file-name">{_text(name, 240)}</span>'
            f'<span class="file-label">{_text(label, 180)}</span>{about}</span></li>'
        )
    return f'<ul class="files">{"".join(items)}</ul>'


def _status(result: dict[str, Any]) -> tuple[str, str, str]:
    """Return (state, label, tone) from the recorded execution outcome."""

    execution = result.get("execution")
    outcome = execution.get("outcome") if isinstance(execution, dict) else None
    state = outcome.get("state") if isinstance(outcome, dict) else None
    if not isinstance(state, str) or not state:
        return "", "", ""
    labels = {
        "completed": ("Completed", "ok"),
        "partial": ("Partially completed", "warn"),
        "failed": ("Failed", "bad"),
    }
    label, tone = labels.get(state, (_label(state), "neutral"))
    return state, label, tone


def _banner(result: dict[str, Any], state: str) -> str:
    if state not in {"partial", "failed"}:
        return ""
    outcome = result["execution"]["outcome"]
    stage = outcome.get("stage")
    where = f" at {_text(stage, 120)}" if isinstance(stage, str) and stage else ""
    heading = f"Computation partially completed{where}" if state == "partial" else f"Computation failed{where}"
    error = outcome.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    detail = f"<p>{_text(message, 600)}</p>" if isinstance(message, str) and message.strip() else ""
    tail = "<p>Saved results and files remain available below.</p>" if state == "partial" else ""
    tone = "warn" if state == "partial" else "bad"
    return (
        f'<div class="banner banner-{tone}" role="status">{_ICON_WARN}<div>'
        f"<p class=\"banner-title\">{heading}</p>{detail}{tail}</div></div>"
    )


def _generated(generated_at: str) -> str:
    try:
        moment = datetime.fromisoformat(generated_at).astimezone(timezone.utc)
    except ValueError:
        return _text(generated_at, 80)
    return f'<time datetime="{escape(generated_at)}">{moment:%Y-%m-%d %H:%M} UTC</time>'


def _header(result: dict[str, Any], package_file: Path, generated_at: str, status: tuple[str, str, str]) -> str:
    title = _text(result.get("package") or package_file.name, 180)
    state, label, tone = status
    line = []
    if label:
        line.append(f'<span class="status status-{tone}"><span class="dot"></span>{_text(label, 60)}</span>')
    if result.get("version") is not None:
        line.append(f'<span>Version {_scalar(result["version"], 60)}</span>')
    facts: list[tuple[str, str]] = []
    if _is_number(result.get("duration")):
        facts.append(("Simulated duration", _number(result["duration"])))
    if _is_number(result.get("communication_step")):
        facts.append(("Communication step", _number(result["communication_step"])))
    modules = [m for m in result.get("modules", []) if isinstance(m, dict)] if isinstance(result.get("modules"), list) else []
    if modules:
        if len(modules) <= 3:
            parts = []
            for module in modules:
                alias = _text(module.get("alias") or module.get("package") or "module", 80)
                origin = " ".join(
                    _text(module[key], 80) for key in ("package", "version") if isinstance(module.get(key), str)
                )
                suffix = f' <span class="quiet">· {origin}</span>' if origin else ""
                parts.append(alias + suffix)
            facts.append(("Modules" if len(modules) > 1 else "Module", "<br>".join(parts)))
        else:
            facts.append(("Modules", _plural(len(modules), "module")))
    environment = result.get("environment")
    if isinstance(environment, dict) and isinstance(environment.get("biosimulant"), str):
        facts.append(("Runtime", f"Biosimulant {_text(environment['biosimulant'], 60)}"))
    facts.append(("Package file", _text(package_file.name, 180)))
    facts.append(("Generated", _generated(generated_at)))
    fact_html = "".join(f"<div><dt>{name}</dt><dd>{value}</dd></div>" for name, value in facts)
    meta = f'<p class="run-line">{"".join(line)}</p>' if line else ""
    return (
        '<header class="run">'
        '<p class="eyebrow">Biosimulant run report</p>'
        f"<h1>{title}</h1>{meta}"
        f'<dl class="facts">{fact_html}</dl>'
        "</header>"
    )


def _key_results(keys: list[_Output], multi_module: bool) -> str:
    shown = keys[:_KEY_LIMIT]
    hero = len(shown) == 1
    tiles = []
    for output in shown:
        unit = f'<span class="unit">{_text(output.unit, 80)}</span>' if output.unit else ""
        origin = []
        if output.module and (hero or multi_module):
            origin.append(f"From {_text(output.module, 120)}")
        if hero and _is_number(output.emitted_at):
            origin.append(f"emitted at t = {_number(output.emitted_at)}")
        about = f'<p class="about">{_text(output.description, 400)}</p>' if output.description else ""
        origin_html = f'<p class="origin">{" · ".join(origin)}</p>' if origin else ""
        tiles.append(
            f'<div class="tile{" tile-hero" if hero else ""}">'
            f'<div class="tile-main"><p class="tile-label">{_text(output.label, 160)}</p>'
            f'<p class="tile-value"><span class="value">{_number(output.value)}</span>{unit}</p></div>'
            f'<div class="tile-about">{about}{origin_html}</div>'
            "</div>"
        )
    heading = "Key result" if hero else "Key results"
    more = (
        f'<p class="quiet small">Showing {_KEY_LIMIT} of {len(keys)} numeric outputs; '
        "the rest are listed under Supporting data.</p>"
        if len(keys) > _KEY_LIMIT else ""
    )
    return (
        f'<section class="block" aria-labelledby="key-results"><h2 id="key-results">{heading}</h2>'
        f'<div class="tiles">{"".join(tiles)}</div>{more}</section>'
    )


def _caption(card: _Card, multi_module: bool) -> str:
    meta = list(card.meta)
    if card.module and multi_module:
        meta.append(card.module)
    meta_html = f'<span class="meta">{" · ".join(_text(item, 120) for item in meta)}</span>' if meta else ""
    about = f'<p class="card-about">{_text(card.description, 600)}</p>' if card.description else ""
    return f'<figcaption><span class="card-title">{_text(card.title, 180)}</span>{meta_html}</figcaption>{about}'


def _fields_card(module: str, outputs: list[_Output], multi_module: bool) -> str:
    rows = []
    for output in outputs:
        label = output.label + (f" ({output.unit})" if output.unit else "")
        about = f'<span class="field-about">{_text(output.description, 400)}</span>' if output.description else ""
        rows.append(
            f"<div><dt>{_text(label, 180)}{about}</dt><dd>{_scalar(output.value, _INLINE_TEXT_LIMIT)}"
            f"{_non_finite_note(output.non_finite)}</dd></div>"
        )
    meta = f'<span class="meta">{_text(module, 120)}</span>' if module and multi_module else ""
    return (
        f'<figure class="card"><figcaption><span class="card-title">Other outputs</span>{meta}</figcaption>'
        f'<dl class="fields">{"".join(rows)}</dl></figure>'
    )


def _note_html(note: _Note) -> str:
    return (
        f'<aside class="note">{_ICON_INFO}<div><p class="note-title">{_text(note.title, 180)}</p>'
        f'<p class="note-text">{_text(note.text)}</p></div></aside>'
    )


def _text_card(note: _Note, multi_module: bool) -> str:
    meta = f'<span class="meta">{_text(note.module, 120)}</span>' if note.module and multi_module else ""
    return (
        f'<figure class="card"><figcaption><span class="card-title">{_text(note.title, 180)}</span>{meta}</figcaption>'
        f'<p class="text-result">{_text(note.text)}</p></figure>'
    )


def _limitations(result: dict[str, Any], unlocated_non_finite: int) -> str:
    statements: list[str] = []
    intended = result.get("intended_use")
    if isinstance(intended, dict):
        intended = intended.get("statement")
    if isinstance(intended, list):
        statements.extend(
            f"<li><strong>Intended use.</strong> {_text(item)}</li>"
            for item in intended[:_ROW_LIMIT] if isinstance(item, str) and item.strip()
        )
    elif isinstance(intended, str) and intended.strip():
        statements.append(f"<li><strong>Intended use.</strong> {_text(intended)}</li>")
    caveats = result.get("caveats")
    if isinstance(caveats, str):
        caveats = [caveats]
    if isinstance(caveats, list):
        statements.extend(
            f"<li>{_text(item)}</li>" for item in caveats[:_ROW_LIMIT] if isinstance(item, str) and item.strip()
        )
    if unlocated_non_finite:
        statements.append(
            "<li>Some non-finite values are unavailable. See the results data for their locations.</li>"
        )
    if not statements:
        return ""
    return (
        f'<aside class="note note-warn" aria-label="Notes and limitations">{_ICON_WARN}<div>'
        f'<p class="note-title">Notes and limitations</p><ul>{"".join(statements)}</ul></div></aside>'
    )


def _definition_list(rows: list[tuple[str, str]]) -> str:
    return '<dl class="pairs">' + "".join(f"<dt>{name}</dt><dd>{value}</dd>" for name, value in rows) + "</dl>"


def _technical(result: dict[str, Any], raw: str) -> str:
    sections: list[tuple[str, str, str]] = []

    config: list[tuple[str, str]] = []
    for label, key in (
        ("Simulated duration", "duration"),
        ("Communication step", "communication_step"),
        ("Settle steps", "settle_steps"),
    ):
        if _is_number(result.get(key)):
            config.append((label, _number(result[key])))
    execution = result.get("execution") if isinstance(result.get("execution"), dict) else {}
    if isinstance(execution.get("timing"), str):
        config.append(("Timing", _text(execution["timing"], 80)))
    policies = execution.get("policies")
    if isinstance(policies, dict) and policies:
        config.append((
            "Policies",
            "<br>".join(f"{_text(k, 80)}: {_scalar(v, 80)}" for k, v in islice(policies.items(), _ROW_LIMIT)),
        ))
    if config:
        hint = []
        if isinstance(execution.get("timing"), str):
            hint.append(_text(execution["timing"], 40))
        if _is_number(result.get("settle_steps")):
            hint.append(_plural(int(result["settle_steps"]), "settle step"))
        sections.append(("Run configuration", " · ".join(hint), _definition_list(config)))

    modules = result.get("modules")
    if isinstance(modules, list) and modules:
        states = {}
        outcome = execution.get("outcome") if isinstance(execution.get("outcome"), dict) else {}
        for component in outcome.get("components", []) if isinstance(outcome.get("components"), list) else []:
            if isinstance(component, dict) and isinstance(component.get("module"), str):
                states[component["module"]] = component.get("state")
        rows = [
            {
                "alias": module.get("alias"),
                "package": module.get("package"),
                "version": module.get("version"),
                "path": module.get("path"),
                "state": _label(states[module["alias"]]) if isinstance(states.get(module.get("alias")), str) else None,
            }
            for module in modules if isinstance(module, dict)
        ]
        columns = [key for key in ("alias", "package", "version", "path", "state") if any(row[key] is not None for row in rows)]
        table, count = _table(columns, rows, [_label(key) for key in columns])
        if table:
            sections.append(("Modules", str(count), table))

    environment = result.get("environment")
    if isinstance(environment, dict):
        env: list[tuple[str, str]] = []
        for label, key in (("Biosimulant", "biosimulant"), ("Python", "python"), ("Platform", "platform")):
            if isinstance(environment.get(key), str):
                env.append((label, _text(environment[key], 160)))
        if isinstance(environment.get("packages"), dict):
            env.append(("Installed packages", str(len(environment["packages"]))))
        if env:
            python = environment.get("python")
            hint = f"Python {_text(python, 40)}" if isinstance(python, str) else ""
            sections.append(("Environment", hint, _definition_list(env)))

    compatibility = result.get("compatibility")
    if isinstance(compatibility, dict):
        compat: list[tuple[str, str]] = []
        for label, key in (("Standard", "standard"), ("Scope", "scope"), ("Catalogue version", "catalogue_version")):
            if isinstance(compatibility.get(key), str):
                compat.append((label, _text(compatibility[key], 160)))
        summary = compatibility.get("summary")
        if isinstance(summary, dict):
            for key, value in islice(summary.items(), 16):
                if _is_number(value):
                    compat.append((_text(_label(key), 80), _number(value)))
        if compat:
            hint = ""
            if isinstance(summary, dict) and _is_number(summary.get("wires")) and _is_number(summary.get("violations")):
                hint = f"{_plural(int(summary['wires']), 'wire')} · {_plural(int(summary['violations']), 'violation')}"
            sections.append(("Compatibility", hint, _definition_list(compat)))

    visualization = result.get("visualization")
    if isinstance(visualization, dict):
        vis: list[tuple[str, str]] = []
        if isinstance(visualization.get("status"), str):
            vis.append(("Status", _text(_label(visualization["status"]), 80)))
        for label, key in (("Valid visuals", "valid_count"), ("Diagnostics", "diagnostics_count")):
            if _is_number(visualization.get(key)):
                vis.append((label, _number(visualization[key])))
        if vis:
            sections.append(("Visualization checks", "", _definition_list(vis)))

    sections.append((
        "Results data",
        "JSON preview",
        '<p class="small quiet">The complete result is in results.json and embedded in this file.</p>'
        f'<pre id="biosimulant-result">{escape(raw)}</pre>',
    ))
    details = []
    for title, hint, body in sections:
        hint_html = f'<span class="hint">{hint}</span>' if hint else ""
        body_class = "details-body raw" if title == "Results data" else "details-body"
        details.append(
            f'<details><summary><span class="summary-title">{title}</span>{hint_html}</summary>'
            f'<div class="{body_class}">{body}</div></details>'
        )
    return (
        '<section class="block technical" aria-labelledby="technical"><h2 id="technical">Technical details</h2>'
        f'<div class="details-group">{"".join(details)}</div></section>'
    )


def _raw_preview(payload: dict[str, Any]) -> str:
    chunks = json.JSONEncoder(ensure_ascii=False, sort_keys=True, indent=2).iterencode(payload)
    pieces = []
    remaining = _RAW_LIMIT
    for chunk in chunks:
        pieces.append(chunk[:remaining])
        remaining -= len(chunk)
        if remaining <= 0:
            break
    raw = "".join(pieces)
    if remaining <= 0:
        raw += "\n… Full JSON is available in the results file and embedded report data."
    return raw


def _body(result: dict[str, Any], package_file: Path, generated_at: str, raw: str) -> str:
    report = _Report()
    _collect_outputs(result, report)
    _collect_visuals(result, report)
    _, unlocated = _non_finite_by_output(result)
    status = _status(result)

    modules = {item.module for item in [*report.keys, *report.cards, *report.notes, *report.files]}
    modules.update(alias for alias, rows in report.rows.items() if rows)
    multi_module = len(modules - {""}) > 1

    parts = [_header(result, package_file, generated_at, status), _banner(result, status[0])]
    limitations = _limitations(result, unlocated)

    supporting_items = bool(report.cards or report.rows or len(report.keys) > _KEY_LIMIT)
    text_primary = not (report.keys or supporting_items)

    if report.keys:
        parts.append(_key_results(report.keys, multi_module))
        if limitations:
            parts.append(limitations)
            limitations = ""

    # Each module's data first, then the notes that qualify it.
    order: list[str] = []
    for item in [*report.cards, *report.notes]:
        if item.module not in order:
            order.append(item.module)
    for alias in report.rows:
        if alias not in order:
            order.append(alias)
    blocks = []
    extra_keys = report.keys[_KEY_LIMIT:]
    for alias in order:
        rows = report.rows.get(alias, []) + [o for o in extra_keys if o.module == alias]
        if rows:
            blocks.append(_fields_card(alias, rows, multi_module))
        for card in (c for c in report.cards if c.module == alias):
            blocks.append(f'<figure class="card">{_caption(card, multi_module)}{card.body}{card.note}</figure>')
        for note in (n for n in report.notes if n.module == alias):
            blocks.append(_text_card(note, multi_module) if text_primary else _note_html(note))
    stray = [o for o in extra_keys if o.module not in order]
    if stray:
        blocks.append(_fields_card("", stray, multi_module))
    if report.unshown:
        kinds = ", ".join(_plural(count, f"{kind} visual") for kind, count in report.unshown.items())
        blocks.append(f'<p class="small quiet">Not shown here: {kinds}. Their data is in the results data.</p>')
    for line in report.clipped:
        blocks.append(f'<p class="small quiet">{line}</p>')
    if blocks:
        heading = "Supporting data" if report.keys else "Results" if not text_primary or len(report.notes) > 1 else "Result"
        parts.append(
            f'<section class="block" aria-labelledby="supporting"><h2 id="supporting">{heading}</h2>'
            f'{"".join(blocks)}</section>'
        )
    if report.files:
        files = [(item.name, item.label + (f" · {item.module}" if multi_module and item.module else ""), item.description) for item in report.files]
        parts.append(
            '<section class="block" aria-labelledby="files"><h2 id="files">Files</h2>'
            f"{_file_list(files)}"
            '<p class="small quiet">Files are saved with the run outputs. This report lists them by name only.</p>'
            "</section>"
        )
    if not (report.keys or blocks or report.files):
        parts.append(
            f'<section class="block empty" aria-labelledby="empty">{_ICON_EMPTY}'
            '<h2 id="empty">No readable outputs were recorded</h2>'
            "<p>The run produced no values, tables, text or files this report can show. "
            "See the results data for details.</p></section>"
        )
    if limitations:
        parts.append(limitations)
    parts.append(_technical(result, raw))
    parts.append(
        '<footer class="page-foot">Full results are in results.json and in this report\'s embedded data.</footer>'
    )
    return "".join(part for part in parts if part)


# ---------------------------------------------------------------------------

_STYLE = """
:root{color-scheme:light;--ink:#0a0f0f;--ink-2:#33494b;--muted:#4f6668;--faint:#5b6f71;
--line:#e0ebe9;--line-2:#d3e0de;--wash:#f0f4f4;--paper:#fff;--page:#fbfcfc;--accent:#056156;
--key:#f3faf7;--key-line:#d3ebe2;--ok:#059669;--ok-bg:#ecf9f3;--ok-line:#c6eadb;--ok-ink:#0b6b46;
--warn:#b7791f;--warn-bg:#fdf6e3;--warn-line:#f1d58f;--warn-ink:#5c3d00;--warn-head:#6b4600;
--bad:#e11d48;--bad-bg:#fff1f3;--bad-line:#fbc6d0;--bad-ink:#9f1239;--note:#f7f7f7;--note-line:#e6eceb;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{margin:0;background:var(--page);color:var(--ink);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,"Helvetica Neue",Arial,sans-serif}
main{max-width:55rem;margin:0 auto;padding:24px 16px 32px;display:flex;flex-direction:column;gap:28px;
overflow-wrap:break-word}
p,h1,h2,dl,dd,ul,figure,pre{margin:0}
sup{font-size:.7em;line-height:0}
.quiet{color:var(--faint)}.small{font-size:13px}.na{color:var(--faint)}
.icon{width:16px;height:16px;flex:none;margin-top:2px;fill:none;stroke:currentColor;stroke-width:2;
stroke-linecap:round;stroke-linejoin:round}
.run{display:flex;flex-direction:column;gap:12px;padding-bottom:22px;border-bottom:1px solid var(--line)}
.eyebrow{font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}
h1{font-size:22px;line-height:1.25;font-weight:600;letter-spacing:-.01em;overflow-wrap:anywhere}
.run-line{display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px;font-size:13px;color:var(--muted)}
.status{display:inline-flex;align-items:center;gap:6px;padding:1px 10px;border-radius:999px;font-weight:500;
border:1px solid var(--line);background:var(--wash);color:var(--ink-2)}
.status .dot{width:7px;height:7px;border-radius:50%;background:var(--faint)}
.status-ok{background:var(--ok-bg);border-color:var(--ok-line);color:var(--ok-ink)}.status-ok .dot{background:var(--ok)}
.status-warn{background:var(--warn-bg);border-color:var(--warn-line);color:var(--warn-head)}.status-warn .dot{background:var(--warn)}
.status-bad{background:var(--bad-bg);border-color:var(--bad-line);color:var(--bad-ink)}.status-bad .dot{background:var(--bad)}
.facts{display:grid;grid-template-columns:repeat(auto-fill,minmax(9.5rem,1fr));gap:12px 20px;margin-top:4px}
.facts dt{font-size:11px;color:var(--faint)}
.facts dd{font-size:13px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.block{display:flex;flex-direction:column;gap:12px}
.block>h2{font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--faint)}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(10rem,1fr));gap:12px}
.tile{padding:14px 16px;border-radius:12px;background:var(--key);border:1px solid var(--key-line);
display:flex;flex-direction:column;gap:6px;min-width:0}
.tile-label{font-size:13px;font-weight:500}
.tile-value{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 6px;font-variant-numeric:tabular-nums}
.tile-value .value{font-size:26px;line-height:1.1;font-weight:600;letter-spacing:-.015em;overflow-wrap:normal}
.tile-value .unit{font-size:15px;color:var(--muted)}
.about{font-size:13px;color:var(--muted)}.origin{font-size:12px;color:var(--faint)}
.tile-about:empty{display:none}
.tile-hero{grid-column:1/-1;padding:18px 20px;flex-direction:row;flex-wrap:wrap;justify-content:space-between;align-items:flex-end;gap:10px 32px}
.tile-hero .tile-main{display:flex;flex-direction:column;gap:6px}
.tile-hero .tile-label{font-size:15px}
.tile-hero .tile-value .value{font-size:44px;line-height:1.05;letter-spacing:-.02em}
.tile-hero .tile-value .unit{font-size:20px}
.tile-hero .tile-about{flex:0 1 20rem;display:flex;flex-direction:column;gap:2px}
.card{border:1px solid var(--line);border-radius:12px;background:var(--paper);overflow:hidden}
.card figcaption{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:2px 12px;padding:12px 16px}
.card-title{font-size:15px;font-weight:600}
.meta{font-size:12px;color:var(--faint)}
.card-about{padding:0 16px 12px;margin-top:-6px;font-size:13px;color:var(--muted)}
.card-foot{padding:8px 16px;font-size:12px;color:var(--faint);border-top:1px solid var(--line)}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;vertical-align:bottom;padding:8px 16px;font-size:12px;font-weight:600;color:var(--ink-2);
background:var(--wash);border-top:1px solid var(--line);border-bottom:1px solid var(--line-2)}
td{padding:8px 16px;vertical-align:top;border-top:1px solid var(--line);min-width:4rem;max-width:24rem}
tbody tr:first-child td{border-top:0}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.series{padding:8px 16px 6px;font-size:13px;font-weight:600;border-top:1px solid var(--line)}
.fields{display:flex;flex-direction:column;border-top:1px solid var(--line)}
.fields>div{display:flex;flex-wrap:wrap;justify-content:space-between;gap:2px 16px;padding:9px 16px;font-size:14px}
.fields>div+div{border-top:1px solid var(--line)}
.fields dt{color:var(--muted);display:flex;flex-direction:column}
.fields dd{text-align:right;font-variant-numeric:tabular-nums;max-width:100%;white-space:pre-wrap}
.field-about{font-size:12px;color:var(--faint)}
.items{padding:10px 16px 12px 34px;border-top:1px solid var(--line);font-size:14px;font-variant-numeric:tabular-nums}
.text,.text-result{padding:0 16px 14px;white-space:pre-wrap}
.text-result{font-size:16px;line-height:1.55}
.caption{padding:10px 16px 14px;font-size:13px;color:var(--muted)}
.inline-warn{padding:8px 16px;font-size:12px;color:var(--warn-ink);background:var(--warn-bg);border-top:1px solid var(--warn-line)}
.fields .inline-warn{padding:4px 0 0;background:none;border:0;text-align:right}
.note{display:flex;gap:10px;padding:12px 14px;border-radius:10px;background:var(--note);border:1px solid var(--note-line);
font-size:14px;color:var(--ink-2)}
.note .icon{color:var(--muted)}
.note-title{font-weight:600;color:var(--ink)}
.note-text{white-space:pre-wrap}
.note ul{margin:2px 0 0;padding-left:18px}
.note-warn{background:var(--warn-bg);border-color:var(--warn-line);color:var(--warn-ink)}
.note-warn .icon,.note-warn .note-title{color:var(--warn-head)}
.banner{display:flex;gap:10px;padding:12px 14px;border-radius:10px;font-size:14px}
.banner-title{font-weight:600}
.banner-warn{background:var(--warn-bg);border:1px solid var(--warn-line);color:var(--warn-ink)}
.banner-bad{background:var(--bad-bg);border:1px solid var(--bad-line);color:var(--bad-ink)}
.files{list-style:none;padding:0;border:1px solid var(--line);border-radius:12px;background:var(--paper);overflow:hidden}
.card .files{border:0;border-top:1px solid var(--line);border-radius:0}
.files li{display:flex;gap:12px;padding:11px 14px}
.files li+li{border-top:1px solid var(--line)}
.files .icon{width:18px;height:18px;color:var(--accent);stroke-width:1.8}
.file-text{display:flex;flex-direction:column;min-width:0}
.file-name{font-weight:500;overflow-wrap:anywhere}
.file-label,.file-about{font-size:12px;color:var(--faint)}
.empty{align-items:center;text-align:center;padding:28px 20px;border:1px dashed #c9d7d5;border-radius:12px;background:var(--paper)}
.empty .icon{width:28px;height:28px;color:var(--faint);stroke-width:1.6}
.empty h2{font-size:15px;font-weight:600;letter-spacing:0;text-transform:none;color:var(--ink)}
.empty p{max-width:24rem;font-size:13px;color:var(--muted)}
.details-group{border:1px solid var(--line);border-radius:12px;background:var(--paper);overflow:hidden;font-size:14px}
details+details{border-top:1px solid var(--line)}
summary{display:flex;align-items:center;gap:10px;padding:11px 14px;cursor:pointer;list-style:none;font-weight:500}
summary::-webkit-details-marker{display:none}
summary::before{content:"";flex:none;width:6px;height:6px;margin:0 3px;border-right:2px solid var(--faint);
border-bottom:2px solid var(--faint);transform:rotate(-45deg)}
details[open]>summary::before{transform:rotate(45deg)}
summary:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.hint{margin-left:auto;padding-left:8px;font-size:13px;font-weight:400;color:var(--faint);text-align:right}
.details-body{padding:2px 14px 14px;display:flex;flex-direction:column;gap:10px}
.details-body .scroll{border:1px solid var(--line);border-radius:8px}
.details-body th,.details-body td{padding:6px 10px;font-size:13px}
.pairs{display:grid;grid-template-columns:minmax(7rem,max-content) minmax(0,1fr);gap:4px 16px;font-size:13px}
.pairs dt{color:var(--faint)}.pairs dd{overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
pre{padding:12px;border-radius:8px;background:var(--wash);font:12px/1.5 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere}
.page-foot{padding-top:14px;border-top:1px solid var(--line);font-size:12px;color:var(--faint)}
@media (max-width:22rem){main{padding:18px 12px 24px;gap:24px}.tile-hero{padding:16px}
.tile-hero .tile-value .value{font-size:36px}.pairs{grid-template-columns:1fr;gap:0}.pairs dd{margin-bottom:6px}
.hint{display:none}}
@media (min-width:40rem){main{padding:40px 32px 48px;gap:32px}h1{font-size:30px}
.tile-hero{padding:24px 28px}.tile-hero .tile-value .value{font-size:56px}.tile-hero .tile-value .unit{font-size:24px}}
@media (min-width:48rem){.tile-hero .tile-about{text-align:right}}
@media print{
@page{margin:16mm 14mm}
:root{--page:#fff;--key:#fff;--wash:#fff}
body{font-size:10.5pt;background:#fff}
main{max-width:none;padding:0;gap:18px}
h1{font-size:20pt}
.run{border-bottom:1.5px solid var(--ink)}
.status{border:0;padding:0;background:none}
.tile,.card,.note,.banner,.files li,tr,details{break-inside:avoid}
h1,.block>h2,figcaption,summary{break-after:avoid}
.tile{border:1px solid var(--line-2)}.tile-hero{border:1.5px solid var(--ink)}
.tile-hero .tile-value .value{font-size:34pt}
.card{border:0;border-radius:0;overflow:visible}
.card figcaption{padding:0 0 6px}.card-about{padding:0 0 6px;margin:0}
.card-foot,.caption,.text,.text-result,.series{padding-left:0;padding-right:0}
.scroll{overflow:visible}
thead{display:table-header-group}
th{border-top:1.5px solid var(--ink);border-bottom:1px solid var(--ink);padding:5px 6px}
td{padding:5px 6px;max-width:none}
tbody tr:last-child td{border-bottom:1.5px solid var(--ink)}
.fields>div{padding:6px 0}
.note,.banner{border-radius:4px}
.details-group{border:0;border-radius:0}
details{border-top:1px solid var(--line-2)}
summary{padding:6px 0}summary::before{display:none}
details::details-content{content-visibility:visible;display:block}
.details-body{padding:0 0 8px}
.raw pre,.raw p{display:none}
.raw::after{content:"The complete result is in results.json and embedded in this file.";font-size:9pt;color:var(--faint)}
}
"""


def write_run_report(report_file: Path, package_file: Path, result: dict[str, Any]) -> None:
    generated_at = datetime.now(timezone.utc).isoformat()
    payload = {"package_file": str(package_file), "generated_at": generated_at, "result": result}
    embedded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )
    page_title = _text(result.get("package") or package_file.name, 120)
    body = _body(result, Path(package_file), generated_at, _raw_preview(payload))
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<meta name="color-scheme" content="light">
<title>{page_title} · Biosimulant Run Report</title>
<style>{_STYLE}</style>
</head>
<body>
<main>
{body}
</main>
<script type="application/json" id="biosimulant-report-data">{embedded}</script>
</body>
</html>
"""
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(document, encoding="utf-8")
