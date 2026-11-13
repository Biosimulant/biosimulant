import json
from html.parser import HTMLParser

from biosim.run_report import write_run_report


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
    report_path = tmp_path / "report.html"
    package_path = tmp_path / "sample.bsilab"
    write_run_report(report_path, package_path, result)

    source = report_path.read_text(encoding="utf-8")
    markup = ReportMarkup()
    markup.feed(source)
    text = "".join(markup.body_text)
    assert markup.scripts == [{"type": "application/json", "id": "biosimulant-report-data"}]
    assert markup.details == [{}]
    assert markup.embedded["result"] == result
    assert markup.embedded["package_file"] == str(package_path)
    assert "measure · Mass (g)" in text
    assert "No" in markup.body_text
    assert note in markup.body_text
    assert "Repeated twice." in markup.body_text
    assert "Toy data" in markup.body_text
    assert "<td>Sample 19</td>" in source
    assert "<td>Sample 20</td>" not in source


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
            ]},
        ],
        "intended_use": "Formatting example",
        "non_finite_values": {"count": 1},
    }
    path = tmp_path / "report.html"
    write_run_report(path, tmp_path / "sample.bsilab", result)
    source = path.read_text()
    visible = source.split("<details>")[0]
    for expected in (
        "sample.csv", "figure.png", "kept.csv", "<td>B</td>", "Not available",
        "<li>3</li>", "Empty text", "Time (s)", "Mass (g)", "Sample A",
        "Showing the first 20 points", "Recorded figure", "Formatting example",
        "Some non-finite values are unavailable",
    ):
        assert expected in visible
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
    path = tmp_path / "report.html"
    write_run_report(path, tmp_path / "sample.bsilab", result)
    source = path.read_text()
    visible = source.split("<details>")[0]
    assert "Showing the first 40 outputs." in visible
    assert "Showing the first 12 visual summaries." in visible
    assert "More fields are available" in visible
    assert "Note 11" in visible and "Note 12" not in visible
    markup = ReportMarkup()
    markup.feed(source)
    assert markup.embedded["result"] == result
    assert "Full JSON is available" in source
