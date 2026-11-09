"""HTML/SVG run report (src/testing/visualization.py) and the ``plot`` CLI command."""
import json
import math
import re
import xml.etree.ElementTree as ET

import pytest

from Ammeters.client import AmmeterConnectionError, AmmeterTimeoutError
from src import archive_cli
from src.testing import archive as archive_module
from src.testing.analysis import AnalysisError, failed_analysis, summarize_measurements
from src.testing.performance import PerformanceLimits
from src.testing.visualization import (
    MAX_RENDERED_POINTS,
    axis,
    histogram,
    render_run_report,
    select_points,
    tick_labels,
)
from src.utils.config import PROJECT_ROOT
from test_archive import snapshot
from test_archive_queries import FailingStream, archive_with, run_id, variant

SVG = "{http://www.w3.org/2000/svg}"


def make_run(outcomes, *, ammeter="greenlee", skipped=0):
    """A complete run structure; each outcome is a current value (float) or None for a failed attempt."""
    attempts = []
    for number, value in enumerate(outcomes, start=1):
        scheduled = (number - 1) * 0.2
        attempt = {"attempt": number, "slot": number - 1, "scheduled_offset_s": scheduled,
                   "start_offset_s": scheduled + 0.001 * number, "lateness_s": 0.001 * number,
                   "elapsed_s": 0.5 if value is None else 0.002, "effective_timeout_s": 1.0,
                   "timeout_clipped_by_deadline": False, "ok": value is not None}
        if value is None:
            attempt["error"] = {"type": "AmmeterTimeoutError", "message": "timed out"}
        else:
            attempt["current_a"] = value
        attempts.append(attempt)
    values = [v for v in outcomes if v is not None]
    return {
        "run_id": run_id(1), "ammeter": ammeter, "host": "127.0.0.1", "port": 5001,
        "command": "MEASURE_GREENLEE -get_measurement",
        "started_at_utc": "2026-11-08T17:30:00.000000+00:00", "ended_at_utc": "2026-11-08T17:30:02.000000+00:00",
        "settings": {"sampling_frequency_hz": 5.0, "measurements_count": len(outcomes), "total_duration_seconds": None,
                     "request_timeout_seconds": 1.0},
        "counts": {"attempted": len(attempts), "succeeded": len(values), "failed": len(attempts) - len(values),
                   "skipped_slots": skipped},
        "elapsed_s": 2.0, "stop_reason": "count_reached", "attempts": attempts,
        "analysis": summarize_measurements(values),
    }


def charts(document):
    """Each inline SVG chart, parsed as XML (which also proves it is well-formed)."""
    return {svg.get("id"): svg for svg in
            (ET.fromstring(match) for match in re.findall(r"<svg .*?</svg>", document, re.S))}


def points(svg, css):
    return [e for e in svg.iter() if e.get("class") == css]


def circle_ys(svg):
    return [float(c.get("cy")) for c in points(svg, "ok")]


# --- Scales, ticks, histogram and the rendering cap ------------------------------------------------

def test_axis_ticks_cover_the_data_and_labels_are_distinct():
    lo, hi, ticks = axis(-0.73, 0.41)
    assert lo <= -0.73 and hi >= 0.41 and ticks[0] == lo and ticks[-1] == pytest.approx(hi)
    assert tick_labels(ticks) == ["-0.75", "-0.5", "-0.25", "0", "0.25", "0.5"]  # step 0.25 (1.14 / 5 rounded up)
    close = [1.0000001, 1.0000002, 1.0000003]
    assert len(set(tick_labels(close))) == 3  # more digits are used when needed


def test_axis_survives_extreme_ranges():
    lo, hi, ticks = axis(-1.7e308, 1.7e308)
    assert all(math.isfinite(t) for t in (lo, hi, *ticks)) and lo <= -1.7e308 and hi >= 1.7e308


def test_histogram_counts_follow_sturges_rule():
    values = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0]  # n = 10 -> ceil(log2 10) + 1 = 5 bins
    hist = histogram(values)

    assert len(hist["counts"]) == 5 and sum(hist["counts"]) == 10
    assert hist["edges"][0] == 0.0 and hist["edges"][-1] == 1.0
    assert hist["counts"] == [2, 2, 2, 2, 2]  # [0,.2) [.2,.4) [.4,.6) [.6,.8) [.8,1.0]: the maximum is in the last bin


def test_histogram_of_identical_values_or_one_value_has_one_bin():
    assert histogram([0.25, 0.25, 0.25])["counts"] == [3]
    one = histogram([-0.5])
    assert one["counts"] == [1] and one["edges"][0] < -0.5 < one["edges"][1]
    assert histogram([]) == {"edges": [], "counts": []}


def test_histogram_handles_negative_and_extreme_values():
    hist = histogram([-1.7e308, 0.0, 1.7e308])
    assert sum(hist["counts"]) == 3 and all(math.isfinite(e) for e in hist["edges"])


def test_points_are_not_sampled_below_the_cap():
    attempts = make_run([1.0, None, 2.0])["attempts"]
    assert select_points(attempts, cap=3) == {"attempts": attempts, "sampled": False}


def test_downsampling_keeps_all_failures_when_they_fit_and_stays_ordered():
    outcomes = [None if i % 50 == 0 else float(i) for i in range(1000)]  # 20 failures
    attempts = make_run(outcomes)["attempts"]

    selection = select_points(attempts, cap=100)

    assert selection["sampled"] and len(selection["attempts"]) == 100
    assert (selection["shown_failed"], selection["total_failed"]) == (20, 20)
    assert (selection["shown_ok"], selection["total_ok"]) == (80, 980)
    numbers = [a["attempt"] for a in selection["attempts"]]
    assert numbers == sorted(numbers) and numbers[0] == 1 and numbers[-1] == 1000


def test_downsampling_limits_failures_to_half_the_cap():
    attempts = make_run([None] * 300 + [1.0] * 300)["attempts"]
    selection = select_points(attempts, cap=100)
    assert (selection["shown_failed"], selection["shown_ok"]) == (50, 50)


def test_sampled_report_says_so_but_uses_the_full_run_for_statistics(monkeypatch):
    run = make_run([None if i % 10 == 0 else float(i) for i in range(60)])

    document = render_run_report(run, max_points=20)

    assert "Downsampled charts:" in document and "20 of 60 attempts" in document
    assert "6 of 6 failed and 14 of 54 successful attempts" in document
    svg = charts(document)
    assert len(points(svg["chart-current"], "ok")) == 14 and len(points(svg["chart-current"], "fail")) == 6
    assert sum(int(b.get("data-count")) for b in points(svg["chart-histogram"], "bin")) == 54  # full run
    assert "n = 54" in document


def test_full_runs_are_not_labelled_as_sampled():
    assert "Downsampled" not in render_run_report(make_run([1.0, 2.0]))
    assert MAX_RENDERED_POINTS == 2000


# --- Faithful presentation ----------------------------------------------------------------------

def test_failed_attempts_are_never_plotted_as_zero_current():
    document = render_run_report(make_run([1.0, None, 2.0, None]))
    current = charts(document)["chart-current"]

    readings, failures = points(current, "ok"), points(current, "fail")
    assert len(readings) == 2 and len(failures) == 2
    plot_bottom = max(float(r.get("y")) + float(r.get("height")) for r in current.iter(f"{SVG}rect")
                      if r.get("fill") == "#fafafa")
    for failure in failures:  # every failure cross is below the plot area, in the separate strip
        ys = [float(n) for n in re.findall(r"[\d.]+", failure.find(f"{SVG}path").get("d"))[1::2]]
        assert min(ys) > plot_bottom
    assert all("FAILED" in f.find(f"{SVG}title").text for f in failures)


def test_timing_charts_include_failed_attempts():
    svg = charts(render_run_report(make_run([1.0, None, 2.0])))
    for chart in ("chart-lateness", "chart-duration"):
        assert len(points(svg[chart], "ok")) == 2 and len(points(svg[chart], "fail")) == 1


def test_negative_currents_are_not_clipped():
    svg = charts(render_run_report(make_run([-0.7, -0.2, 0.4])))
    ticks = [t.text for t in svg["chart-current"].iter(f"{SVG}text") if t.get("class") == "tick"]
    assert min(float(t) for t in ticks[:6]) <= -0.7  # the value axis extends below the lowest reading
    ys = circle_ys(svg["chart-current"])
    assert len(set(ys)) == 3  # three distinct heights; none pinned to an axis edge
    assert max(ys) < 250 - 48 and min(ys) > 34


def test_empty_run_renders_messages_and_null_summaries():
    document = render_run_report(make_run([]))
    svg = charts(document)
    assert "no attempts were made" in document and "no successful readings" in document
    assert all(not points(chart, "ok") and not points(chart, "fail") for chart in svg.values())
    assert "No successful readings, so no current statistics." in document
    assert "n/a" in document and "Thresholds: not evaluated" in document


def test_all_failed_run_has_only_failure_markers():
    document = render_run_report(make_run([None, None, None]))
    svg = charts(document)
    assert not points(svg["chart-current"], "ok") and len(points(svg["chart-current"], "fail")) == 3
    assert "no successful readings" in document and not points(svg["chart-histogram"], "bin")
    assert document.count("<td>AmmeterTimeoutError</td>") == 3


@pytest.mark.parametrize("outcomes", [[0.0147], [0.25, 0.25, 0.25], [0.0, 0.0], [-3.5]])
def test_single_and_constant_values_render_inside_the_plot(outcomes):
    svg = charts(render_run_report(make_run(outcomes)))
    ys = circle_ys(svg["chart-current"])
    assert len(ys) == len(outcomes) and all(34 < y < 250 - 48 for y in ys)
    bins = points(svg["chart-histogram"], "bin")
    assert len(bins) == 1 and bins[0].get("data-count") == str(len(outcomes))


def test_extreme_values_render_without_infinity_or_nan():
    document = render_run_report(make_run([1.7e308, 1.7e308, -1.7e308]))
    assert not re.search(r"\binf\b|\bnan\b", document.lower())
    svg = charts(document)  # still well-formed
    assert all(math.isfinite(y) for y in circle_ys(svg["chart-current"]))


def test_failed_analysis_is_shown_with_its_error():
    run = make_run([1.0, 2.0])
    run["analysis"] = failed_analysis(2, AnalysisError("median_a is too large"))
    document = render_run_report(run)
    assert "could not be calculated for 2 successful readings: AnalysisError: median_a is too large" in document
    assert len(points(charts(document)["chart-current"], "ok")) == 2  # the readings are still plotted


def test_dynamic_text_is_escaped():
    run = make_run([1.0, None], ammeter='<script>alert("x")</script> & co')
    run["attempts"][1]["error"]["message"] = "bad <reply> & \"quotes\""
    document = render_run_report(run)

    assert "<script>" not in document and "<reply>" not in document
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; co" in document
    assert "bad &lt;reply&gt; &amp; &quot;quotes&quot;" in document
    charts(document)  # escaped tooltips keep the SVG well-formed


def test_report_states_emulator_readings_and_thresholds():
    document = render_run_report(make_run([1.0, 2.0]))
    assert "do not establish the accuracy" in document and "Thresholds: not evaluated" in document

    limited = render_run_report(make_run([1.0, None]), PerformanceLimits(max_failure_rate=0.1))
    assert "Thresholds: fail" in limited and "<td>failure_rate</td><td>fail</td>" in limited


def test_report_is_self_contained():
    document = render_run_report(make_run([1.0, None, 2.0]))
    assert "<script" not in document and "http://" not in document.replace("http://www.w3.org/2000/svg", "")
    assert "https://" not in document and "<link" not in document and "src=" not in document


def test_full_precision_stays_in_the_data_and_only_labels_are_rounded():
    value = 0.014791290164367787
    run = make_run([value])
    render_run_report(run)
    assert run["attempts"][0]["current_a"] == value  # the run is not modified
    assert "0.0147913 A" in render_run_report(run)


# --- Sample and CLI --------------------------------------------------------------------------------

def test_committed_sample_report_matches_the_committed_run():
    samples = PROJECT_ROOT / "sample_results"
    loaded_run = json.loads((samples / "greenlee_run.json").read_text(encoding="utf-8"))
    expected = (samples / "greenlee_report.html").read_text(encoding="utf-8")
    assert render_run_report(loaded_run) == expected


@pytest.fixture
def archive(tmp_path):
    return archive_with(tmp_path, variant(tmp_path, 1, outcomes=(1.0, AmmeterConnectionError(5001, "refused"), 3.0)))


def test_cli_writes_the_report_and_leaves_the_database_unchanged(capsys, archive, tmp_path, monkeypatch):
    before, rows = archive.path.read_bytes(), snapshot(archive.path)
    monkeypatch.chdir(tmp_path)

    code = archive_cli.main(["--db", str(archive.path), "plot", run_id(1), "report.html"])

    out, err = capsys.readouterr()
    assert (code, err) == (0, "") and "Wrote the report for run" in out
    document = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert document == render_run_report(archive.load_run(run_id(1)))
    assert archive.path.read_bytes() == before and snapshot(archive.path) == rows


def test_cli_never_overwrites(capsys, archive, tmp_path):
    output = tmp_path / "report.html"
    output.write_text("keep me", encoding="utf-8")

    code = archive_cli.main(["--db", str(archive.path), "plot", run_id(1), str(output)])

    assert code == 1 and "already exists" in capsys.readouterr().err
    assert output.read_text(encoding="utf-8") == "keep me"


def test_cli_limits_appear_in_the_report_without_changing_the_exit_code(capsys, archive, tmp_path):
    output = tmp_path / "report.html"
    code = archive_cli.main(["--db", str(archive.path), "plot", run_id(1), str(output), "--max-failure-rate", "0"])
    assert code == 0 and "Thresholds: fail" in output.read_text(encoding="utf-8")


def test_cli_removes_a_partial_file_after_a_write_failure(capsys, archive, tmp_path, monkeypatch):
    monkeypatch.setattr(archive_module, "_create_exclusive", FailingStream)
    output = tmp_path / "report.html"

    code = archive_cli.main(["--db", str(archive.path), "plot", run_id(1), str(output)])

    assert code == 1 and "the partial file was removed" in capsys.readouterr().err
    assert not output.exists()


def test_cli_document_is_generated_before_the_file_is_created(capsys, archive, tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("rendering failed")

    monkeypatch.setattr(archive_cli, "render_run_report", broken)
    output = tmp_path / "report.html"
    with pytest.raises(RuntimeError):
        archive_cli.main(["--db", str(archive.path), "plot", run_id(1), str(output)])
    assert not output.exists()


@pytest.mark.parametrize("argv, code, message", [
    (["plot", "not-a-uuid", "r.html"], 2, "run_id must be a UUID"),
    (["plot", run_id(1)], 2, "OUTPUT_PATH"),
    (["plot", run_id(1), "r.html", "--max-failure-rate", "2"], 2, "between 0 and 1"),
])
def test_cli_usage_errors(capsys, tmp_path, argv, code, message):
    with pytest.raises(SystemExit) as excinfo:
        archive_cli.main(["--db", str(tmp_path / "never.sqlite3"), *argv])
    assert excinfo.value.code == code and message in capsys.readouterr().err
    assert not (tmp_path / "never.sqlite3").exists()


def test_cli_missing_run_and_missing_directory(capsys, archive, tmp_path):
    assert archive_cli.main(["--db", str(archive.path), "plot", run_id(9), str(tmp_path / "r.html")]) == 1
    assert "is not archived" in capsys.readouterr().err and not (tmp_path / "r.html").exists()
    assert archive_cli.main(["--db", str(archive.path), "plot", run_id(1), str(tmp_path / "no" / "r.html")]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_real_framework_run_with_a_timeout_renders(tmp_path):
    result = variant(tmp_path, 3, outcomes=(0.5, AmmeterTimeoutError(5001, "timed out"), -0.25))
    svg = charts(render_run_report(result))
    assert len(points(svg["chart-current"], "ok")) == 2 and len(points(svg["chart-current"], "fail")) == 1
