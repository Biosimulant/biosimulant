import json
import re
from html.parser import HTMLParser

from biosim.run_report import _number, write_run_report


class ReportMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.details = []
        self.embedded = None
        self.in_payload = False
        self.body_text = []

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "script":
            self.scripts.append(attributes)
            self.in_payload = attributes.get("id") == "biosimulant-report-data"
        if tag == "details":
            self.details.append(attributes)

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_payload = False

    def handle_data(self, data):
        if self.in_payload:
            self.embedded = json.loads(data)
        else:
            self.body_text.append(data)


def _render(tmp_path, result, name="report.html"):
    path = tmp_path / name
    write_run_report(path, tmp_path / "sample.bsilab", result)
    source = path.read_text(encoding="utf-8")
    markup = ReportMarkup()
    markup.feed(source)
    # What a reader sees without opening the collapsed technical details.
    visible = source.split('<section class="block technical"')[0]
    return source, visible, markup


def _section(visible, heading_id):
    match = re.search(rf'<section[^>]*aria-labelledby="{heading_id}".*?</section>', visible, re.S)
    return match.group(0) if match else ""


SAMPLE = {
    "package": "local/sample-measurements",
    "version": "0.1.0",
    "duration": 1.0,
    "communication_step": 1.0,
    "settle_steps": 0,
    "modules": [{"alias": "measurements", "package": "local/hello", "path": "models/hello", "version": "0.1.0"}],
    "environment": {"biosimulant": "0.0.41", "python": "3.12.9", "platform": "macOS-26.5.1-arm64-arm-64bit"},
    "execution": {
        "outcome": {"components": [{"module": "measurements", "state": "completed"}], "state": "completed"},
        "timing": "temporal",
    },
    "outputs": {"measurements": {"mean_mass": {
        "emitted_at": 1.0,
        "spec": {"description": "Arithmetic mean of the three samples.", "emitted_unit": "g"},
        "value": 12.67,
    }}},
    "visuals": [{"module": "measurements", "visuals": [
        {"render": "table", "data": {"title": "Sample measurements", "columns": ["Sample", "Mass (g)"],
                                     "rows": [["A", 12.4], ["B", 12.9], ["C", 12.7]]}},
        {"render": "text", "data": {"title": "About this result",
                                    "text": "These illustrative measurements come from a toy lab."}},
    ]}],
}


def test_report_leads_with_the_run_and_key_result_then_its_supporting_table(tmp_path):
    source, visible, markup = _render(tmp_path, SAMPLE)
    text = " ".join(markup.body_text)

    assert markup.scripts == [{"type": "application/json", "id": "biosimulant-report-data"}]
    assert markup.embedded["result"] == SAMPLE
    assert "<h1>local/sample-measurements</h1>" in visible
    assert 'class="status status-ok"' in visible and "Completed" in text
    for fact in ("Version 0.1.0", "Simulated duration", "Biosimulant 0.0.41", "sample.bsilab"):
        assert fact in text
    key = _section(visible, "key-results")
    assert '<span class="value">12.67</span><span class="unit">g</span>' in key
    assert "Mean mass" in key and "Arithmetic mean of the three samples." in key
    assert "emitted at t = 1.0" in key
    # The three measurements sit immediately after the key result, numbers right-aligned.
    supporting = _section(visible, "supporting")
    assert visible.index(key) + len(key) == visible.index(supporting)
    assert '<th scope="col" class="num">Mass (g)</th>' in supporting
    assert '<td>A</td><td class="num">12.4</td>' in supporting
    # The module's note follows the data it describes.
    assert supporting.index("Sample measurements") < supporting.index("About this result")
    assert '<aside class="note">' in supporting
    # Technical details exist and are collapsed.
    assert markup.details and all("open" not in item for item in markup.details)
    assert source.index("Technical details") > source.index("About this result")
    assert '<pre id="biosimulant-result">' in source
    assert "Biosimulant run report" in visible


def test_report_shows_results_in_html_and_keeps_the_machine_payload(tmp_path):
    note = "Samples A < B & C </script>"
    result = {
        "package": "local/sample",
        "outputs": {"measure": {
            "mass": {"value": 0, "spec": {"emitted_unit": "g"}},
            "approved": {"value": False},
            "comment": {"value": note},
            "samples": {"value": {"columns": ["Sample", "Mass (g)"],
                                   "rows": [[f"Sample {i:02}", i] for i in range(25)]}},
        }},
        "visuals": [{"module": "measure", "visuals": [
            {"render": "text", "data": {"title": "Method", "text": "Repeated twice."}},
        ]}],
        "caveats": ["Toy data"],
    }
    source, visible, markup = _render(tmp_path, result)
    assert markup.embedded["result"] == result
    assert markup.embedded["package_file"] == str(tmp_path / "sample.bsilab")
    assert '<p class="tile-label">Mass</p>' in visible
    assert '<span class="value">0</span><span class="unit">g</span>' in visible
    assert "No" in markup.body_text
    assert note in markup.body_text
    assert "Repeated twice." in markup.body_text
    assert "Toy data" in markup.body_text
    assert "<td>Sample 19</td>" in source
    assert "<td>Sample 20</td>" not in source
    assert "Showing the first 20 of 25 rows." in visible
    # Limitations sit directly under the key result they qualify.
    key = _section(visible, "key-results")
    after_key = visible[visible.index(key) + len(key):]
    assert after_key.startswith('<aside class="note note-warn"')


def test_report_lays_out_several_scalars_as_tiles(tmp_path):
    result = {
        "package": "local/scalars",
        "outputs": {"growth": {
            "rate": {"value": 0.0423, "spec": {"emitted_unit": "1/h", "description": "Specific growth rate"}},
            "doubling_time": {"value": 16.386, "spec": {"emitted_unit": "h"}},
            "yield": {"value": 0.5, "spec": {"emitted_unit": "1"}},
        }},
    }
    _source, visible, markup = _render(tmp_path, result)
    key = _section(visible, "key-results")
    assert "Key results" in key and "tile-hero" not in key
    assert key.count('class="tile"') == 3
    assert '<span class="value">16.386</span><span class="unit">h</span>' in key
    assert '<span class="value">0.5</span></p>' in key  # dimensionless unit hidden
    assert "Specific growth rate" in key
    assert 'aria-labelledby="supporting"' not in visible
    assert "No readable outputs" not in visible


def test_report_promotes_a_text_only_result(tmp_path):
    result = {
        "package": "local/text",
        "visuals": [{"module": "summary", "visuals": [
            {"render": "text", "data": {"title": "Interpretation", "markdown": "Line one\nLine two"}},
        ]}],
    }
    _source, visible, _markup = _render(tmp_path, result)
    supporting = _section(visible, "supporting")
    assert '<h2 id="supporting">Result</h2>' in supporting
    assert '<p class="text-result">Line one\nLine two</p>' in supporting
    assert 'class="note"' not in supporting


def test_report_lists_file_only_outputs_by_name(tmp_path):
    result = {
        "package": "local/files",
        "outputs": {"exporter": {
            "files": {"value": {"report_file": "/private/run/outputs/report.csv",
                                "plot_path": r"C:\\runs\\plot.png"},
                      "spec": {"description": "Exported tables and figures"}},
            "archive": {"value": "/private/run/out.zip", "spec": {"value_type": "file"}},
        }},
    }
    _source, visible, _markup = _render(tmp_path, result)
    files = _section(visible, "files")
    for expected in ("report.csv", "plot.png", "out.zip", "Files · Report file", "Exported tables and figures"):
        assert expected in files
    assert "/private/" not in visible and "C:" not in visible
    assert "No readable outputs" not in visible
    assert 'aria-labelledby="key-results"' not in visible


def test_report_explains_a_run_with_no_readable_outputs(tmp_path):
    _source, visible, markup = _render(tmp_path, {"package": "local/empty", "outputs": {}, "visuals": []})
    empty = _section(visible, "empty")
    assert "No readable outputs were recorded" in empty
    assert "<h1>local/empty</h1>" in visible
    assert markup.details  # results data stays available


def test_report_flags_partial_runs_and_non_finite_values(tmp_path):
    result = {
        "package": "local/partial",
        "execution": {"outcome": {"state": "partial", "stage": "comparison",
                                  "error": {"message": "Solver <diverged>"}}},
        "outputs": {"model": {"rate": {"value": None}, "count": {"value": 3}}},
        "non_finite_values": {"count": 2, "truncated": False, "items": [
            {"path": "outputs.model.rate.value", "value": "NaN"},
            {"path": "visuals[0].visuals[0].data.points[1].y", "value": "Infinity"},
        ]},
    }
    _source, visible, _markup = _render(tmp_path, result)
    assert 'class="status status-warn"' in visible and "Partially completed" in visible
    assert "Computation partially completed at comparison" in visible
    assert "Solver &lt;diverged&gt;" in visible
    assert "1 value was not finite (NaN or infinite) and appear as Not available." in visible
    assert "Some non-finite values are unavailable" in visible


def test_report_summarizes_legacy_files_arrays_and_series_without_links(tmp_path):
    result = {
        "version": "1.0",
        "duration": 4,
        "outputs": {
            "file": {"value": r"C:\\results\\sample.csv", "spec": {"value_type": "file"}},
            "plots_file": {"value": ["/private/figure.png"]},
            "kept_file": {"value": {"kind": "artifact", "file_name": "kept.csv", "download_url": "https://example.com/kept.csv"}},
            "records": {"value": [{"sample": "A", "mass": 0}, {"sample": "B", "mass": None}]},
            "array": {"value": {"type": "array", "data": [1, 2, 3]}},
            "record": {"value": {"ok": True, "counts": [1], "summary": {}, "comment": ""}},
        },
        "visuals": [
            None,
            {"visuals": [None, {"render": "unknown", "data": {}}, {"render": "table", "data": {"columns": [], "rows": []}}]},
            {"visuals": [
                {"render": "bar", "data": {"items": [{"label": "A", "value": 12}]}},
                {"render": "scatter", "data": {"points": [{"x": i, "y": i + 1} for i in range(21)]}},
                {"render": "timeseries", "description": "Toy trajectory", "data": {
                    "x_label": "Time", "x_unit": "s", "y_label": "Mass", "y_unit": "g",
                    "series": [{"name": "Sample A", "points": [[0, 3], [1, 4]]}],
                }},
                {"render": "image", "data": {
                    "source": {"kind": "artifact", "path": "/private/figure.png"},
                    "caption": "Recorded figure",
                }},
                {"render": "heatmap", "data": {"values": [[1]]}},
            ]},
        ],
        "intended_use": {"statement": "Formatting example"},
        "non_finite_values": {"count": 1},
    }
    source, visible, _markup = _render(tmp_path, result)
    for expected in (
        "sample.csv", "figure.png", "kept.csv", "<td>B</td>", "Not available",
        "<li>3</li>", "Empty text", "Time (s)", "Mass (g)", "Sample A", "Toy trajectory",
        "Showing the first 20 of 21 points", "Recorded figure", "Formatting example",
        "Some non-finite values are unavailable", "Not shown here: 1 heatmap visual",
    ):
        assert expected in visible
    assert '<th scope="col" class="num">Value</th>' in visible
    assert "/private/" not in visible
    assert "https://example.com" not in visible
    assert "<a " not in source and "<img " not in source


def test_report_bounds_large_sections_but_retains_full_embedded_results(tmp_path):
    result = {
        "outputs": {"module": {
            f"output_{i}": {"value": {"comment": "x" * 9000, **{f"field_{j}": j for j in range(10)}}}
            for i in range(42)
        }},
        "visuals": [{"visuals": [
            {"render": "text", "data": {"markdown": f"Note {i:02}"}}
            for i in range(14)
        ]}],
    }
    source, visible, markup = _render(tmp_path, result)
    assert "Showing the first 40 outputs." in visible
    assert "Showing the first 12 visual summaries." in visible
    assert "More fields are available" in visible
    assert "Note 11" in visible and "Note 12" not in visible
    assert markup.embedded["result"] == result
    assert "Full JSON is available" in source


def test_report_escapes_model_supplied_text_everywhere(tmp_path):
    hostile = '<img src=x onerror="alert(1)">'
    result = {
        "package": hostile,
        "version": hostile,
        "modules": [{"alias": hostile, "package": hostile, "version": "1"}],
        "outputs": {hostile: {
            "value_out": {"value": 1.5, "spec": {"emitted_unit": hostile, "description": hostile}},
            "table": {"value": {"columns": [hostile], "rows": [[hostile]]}},
            "out_file": {"value": f"/tmp/{hostile}"},
        }},
        "visuals": [{"module": hostile, "visuals": [
            {"render": "text", "data": {"title": hostile, "text": hostile}},
        ]}],
        "caveats": [hostile],
        "environment": {"biosimulant": hostile, "python": hostile},
    }
    _source, visible, markup = _render(tmp_path, result)
    assert "<img" not in visible and "<script" not in visible.lower()
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in visible
    assert markup.embedded["result"] == result


def test_report_is_self_contained_and_printable(tmp_path):
    source, _visible, _markup = _render(tmp_path, SAMPLE)
    page = source.split('<script type="application/json"')[0]
    assert "default-src 'none'; style-src 'unsafe-inline'" in source
    for forbidden in ("<link", "@import", "url(", "http://", "https://", "<iframe", "<img", "<a ", "<script"):
        assert forbidden not in page
    assert not re.search(r"<[^>]+\son[a-z]+=", page)
    assert "@media print" in source and "thead{display:table-header-group}" in source
    assert "@media (max-width:22rem)" in source and '<meta name="viewport"' in source


def test_numbers_keep_recorded_precision_up_to_six_figures():
    assert _number(12.67) == "12.67"
    assert _number(1.0) == "1.0"
    assert _number(42) == "42"
    assert _number(0.1 + 0.2) == "0.3"
    assert _number(123456.7) == "123457"
    assert _number(1234567.0) == "1.23457 × 10<sup>6</sup>"
    assert _number(0.0000123) == "1.23 × 10<sup>−5</sup>"
    assert _number(float("nan")) == "Not available"
