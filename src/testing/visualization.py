"""Self-contained HTML report with SVG charts for one run (standard library only).

``render_run_report(run, limits)`` turns a complete run (as returned by run_test or
load_run) into one HTML document: run details, the current statistics, the performance
summary, and four inline SVG charts. The document has no scripts and no external
resources, so it opens offline in any browser.

Charts
------
- Current vs. actual start offset: successful readings as circles. Failed attempts have
  no current value, so they are drawn as crosses in a separate "failed" strip below the
  plot area, at their start time; they are never plotted as 0 A.
- Start lateness and request duration (milliseconds) vs. actual start offset: every
  attempt, successful (circles) or failed (crosses). The x axes of the three charts are
  identical, so they line up.
- Histogram of the successful current values.

All charts are scatter plots or bars; no lines connect points, so nothing is drawn across
failed attempts or skipped slots. Value axes are not clipped: negative currents get a
negative axis.

Histogram bins: Sturges' rule, ``ceil(log2(n)) + 1`` equal-width bins over [min, max]
(at most MAX_HISTOGRAM_BINS). Each value is counted against the returned edges: bin i is
[edges[i], edges[i+1]), and the last bin also includes the maximum. If floating-point
rounding makes neighbouring edges equal (a range only a few float steps wide), the
duplicates are dropped and there are fewer bins. When all values are identical (including
a single value) there is one bin centred on that value.

Tiny values: a constant subnormal value gets a display range of one float step on each
side, because 10% of it rounds away. Coordinates are computed from exact differences, so
such ranges are not collapsed.

Rendering cap: at most MAX_RENDERED_POINTS attempts are drawn per chart. Larger runs are
downsampled with evenly spaced attempts: failed attempts are all kept if they fit in half
the cap (otherwise evenly spaced failures fill half), and successful attempts fill the
rest. The report says so above the charts. Statistics, the performance summary and the
histogram always use the full run.

Precision: the run data are not changed; only displayed labels are rounded. Every piece of
text from the run (device name, command, error messages, ...) is HTML-escaped.
"""
import bisect
import html
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.testing.performance import PerformanceLimits, build_performance_report

MAX_RENDERED_POINTS = 2000
MAX_HISTOGRAM_BINS = 50
MAX_FAILURE_ROWS = 100

_WIDTH, _HEIGHT = 760, 250
_LEFT, _RIGHT, _TOP, _BOTTOM = 86, 24, 34, 48
_FAIL_STRIP = 34  # extra height below the current chart for failed attempts
_OK_COLOR, _FAIL_COLOR, _BAR_COLOR = "#1f6fb4", "#c62828", "#5b8fc7"


def _e(value: Any) -> str:
    """HTML-escape any value for text or attribute use."""
    return html.escape(str(value), quote=True)


# --- Numbers, axes and ticks ------------------------------------------------------------

def _fraction(value: float, lo: float, hi: float) -> float:
    """Position of ``value`` in [lo, hi] (``lo < hi``) as 0..1.

    The plain differences are used whenever ``hi - lo`` is finite; they stay exact and
    non-zero even for subnormal ranges, where halving the ends could collapse them. Only
    a range too wide to represent (for example -1.7e308 to 1.7e308) is mapped with halved
    ends, which cannot overflow.
    """
    span = hi - lo
    if math.isfinite(span):
        return (value - lo) / span
    return (value * 0.5 - lo * 0.5) / (hi * 0.5 - lo * 0.5)


def _expand_constant(lo: float, hi: float) -> Tuple[float, float]:
    """A non-empty domain around a single value."""
    if hi > lo:
        return lo, hi
    pad = abs(lo) * 0.1 if lo != 0 else 1.0
    if lo - pad == lo or lo + pad == lo:  # 10% of a tiny (subnormal) value rounds away
        pad = math.ulp(lo)  # the spacing of floats at lo, so the range is one step each way
    left, right = lo - pad, lo + pad
    if not math.isfinite(right):  # near the largest float: extend downwards only
        left, right = lo - 2 * pad, lo
    elif not math.isfinite(left):
        left, right = lo, lo + 2 * pad
    return left, right


def _nice_step(raw: float) -> float:
    exponent = math.floor(math.log10(raw))
    base = 10.0 ** exponent
    for multiple in (1, 2, 2.5, 5, 10):
        if raw / base <= multiple:
            return multiple * base
    return 10 * base


def axis(lo: float, hi: float, target_ticks: int = 5) -> Tuple[float, float, List[float]]:
    """Expand [lo, hi] to round bounds and return (lo, hi, ticks). ``lo < hi`` is required."""
    span = hi - lo
    try:
        step = _nice_step(span / target_ticks)
        start, end = math.floor(lo / step) * step, math.ceil(hi / step) * step
        count = round((end - start) / step)
        if not (math.isfinite(start) and math.isfinite(end) and step > 0 and 1 <= count <= 4 * target_ticks):
            raise ValueError
    except (ValueError, OverflowError, ZeroDivisionError):
        middle = lo * 0.5 + hi * 0.5
        return lo, hi, [lo, middle, hi] if lo < middle < hi else [lo, hi]
    return start, end, [start + i * step for i in range(count + 1)]


def tick_labels(ticks: Sequence[float]) -> List[str]:
    """The shortest labels (3 to 15 significant digits) that keep every tick distinct."""
    for digits in range(3, 16):
        labels = [_number(tick, digits) for tick in ticks]
        if len(set(labels)) == len(labels):
            return labels
    return [repr(tick) for tick in ticks]


def _number(value: float, digits: int = 6) -> str:
    text = f"{value:.{digits}g}"
    return "0" if text in ("-0", "0") else text


# --- Data preparation ---------------------------------------------------------------------

def histogram(values: Sequence[float]) -> Dict[str, Any]:
    """Equal-width histogram (Sturges' rule); identical values give one bin. See module docs."""
    if not values:
        return {"edges": [], "counts": []}
    lo, hi = min(values), max(values)
    if lo == hi:
        left, right = _expand_constant(lo, hi)
        return {"edges": [left, right], "counts": [len(values)]}
    bins = min(MAX_HISTOGRAM_BINS, math.ceil(math.log2(len(values))) + 1)
    # A weighted average of the ends cannot overflow, even for extreme finite values.
    raw = [lo * (1 - i / bins) + hi * (i / bins) for i in range(1, bins)]
    # Rounding can make neighbouring edges equal (or, in principle, out of order) when the
    # range is only a few float steps wide. Such edges are dropped, leaving fewer bins;
    # no edge is ever moved.
    edges = [lo]
    for edge in raw:
        if edges[-1] < edge < hi:
            edges.append(edge)
    edges.append(hi)
    counts = [0] * (len(edges) - 1)
    for value in values:
        # Bin i is [edges[i], edges[i+1]); the maximum belongs to the last bin.
        index = len(counts) - 1 if value == hi else bisect.bisect_right(edges, value) - 1
        counts[index] += 1
    return {"edges": edges, "counts": counts}


def select_points(attempts: Sequence[Dict[str, Any]], cap: int = MAX_RENDERED_POINTS) -> Dict[str, Any]:
    """The attempts to draw: all of them up to ``cap``, otherwise an evenly spaced subset."""
    if len(attempts) <= cap:
        return {"attempts": list(attempts), "sampled": False}
    failed = [a for a in attempts if not a["ok"]]
    succeeded = [a for a in attempts if a["ok"]]
    kept_failed = _evenly_spaced(failed, min(len(failed), cap // 2))
    kept_ok = _evenly_spaced(succeeded, min(len(succeeded), cap - len(kept_failed)))
    chosen = sorted(kept_failed + kept_ok, key=lambda a: a["attempt"])
    return {"attempts": chosen, "sampled": True, "shown_failed": len(kept_failed), "total_failed": len(failed),
            "shown_ok": len(kept_ok), "total_ok": len(succeeded)}


def _evenly_spaced(items: Sequence[Any], count: int) -> List[Any]:
    if count <= 0:
        return []
    if count >= len(items):
        return list(items)
    if count == 1:
        return [items[0]]
    return [items[(i * (len(items) - 1)) // (count - 1)] for i in range(count)]


# --- SVG building blocks ---------------------------------------------------------------------

class _Plot:
    """Pixel mapping for one chart area."""

    def __init__(self, x_domain, y_domain, height=_HEIGHT, extra_bottom=0):
        self.x_lo, self.x_hi, self.x_ticks = x_domain
        self.y_lo, self.y_hi, self.y_ticks = y_domain
        self.height = height + extra_bottom
        self.left, self.right = _LEFT, _WIDTH - _RIGHT
        self.top, self.bottom = _TOP, height - _BOTTOM

    def x(self, value):
        return self.left + _fraction(value, self.x_lo, self.x_hi) * (self.right - self.left)

    def y(self, value):
        return self.bottom - _fraction(value, self.y_lo, self.y_hi) * (self.bottom - self.top)


def _axes(plot: _Plot, x_title: str, y_title: str, x_labels=True) -> List[str]:
    parts = [f'<rect x="{plot.left}" y="{plot.top}" width="{plot.right - plot.left}" '
             f'height="{plot.bottom - plot.top}" fill="#fafafa" stroke="#999"/>']
    for tick, label in zip(plot.y_ticks, tick_labels(plot.y_ticks)):
        y = plot.y(tick)
        parts.append(f'<line x1="{plot.left}" y1="{y:.2f}" x2="{plot.right}" y2="{y:.2f}" stroke="#e3e3e3"/>')
        parts.append(f'<text x="{plot.left - 6}" y="{y + 4:.2f}" text-anchor="end" class="tick">{_e(label)}</text>')
    for tick, label in zip(plot.x_ticks, tick_labels(plot.x_ticks)):
        x = plot.x(tick)
        parts.append(f'<line x1="{x:.2f}" y1="{plot.top}" x2="{x:.2f}" y2="{plot.bottom}" stroke="#eeeeee"/>')
        if x_labels:
            parts.append(f'<text x="{x:.2f}" y="{plot.height - 22}" text-anchor="middle" class="tick">{_e(label)}</text>')
    parts.append(f'<text x="{(plot.left + plot.right) / 2:.1f}" y="{plot.height - 5}" text-anchor="middle" '
                 f'class="axis-title">{_e(x_title)}</text>')
    parts.append(f'<text transform="translate(16 {(plot.top + plot.bottom) / 2:.1f}) rotate(-90)" '
                 f'text-anchor="middle" class="axis-title">{_e(y_title)}</text>')
    return parts


def _legend(items: Sequence[Tuple[str, str]]) -> List[str]:
    parts, x = [], _LEFT
    for kind, label in items:
        if kind == "ok":
            parts.append(f'<circle cx="{x + 6}" cy="16" r="4" fill="{_OK_COLOR}"/>')
        elif kind == "fail":
            parts.append(_cross(x + 6, 16, "legend"))
        else:
            parts.append(f'<rect x="{x + 1}" y="11" width="10" height="10" fill="{_BAR_COLOR}"/>')
        parts.append(f'<text x="{x + 16}" y="20" class="legend">{_e(label)}</text>')
        x += 24 + 7 * len(label)
    return parts


def _cross(x: float, y: float, css: str, title: str = "") -> str:
    tooltip = f"<title>{_e(title)}</title>" if title else ""
    return (f'<g class="{css}"><path d="M{x - 4:.2f} {y - 4:.2f} L{x + 4:.2f} {y + 4:.2f} '
            f'M{x - 4:.2f} {y + 4:.2f} L{x + 4:.2f} {y - 4:.2f}" stroke="{_FAIL_COLOR}" stroke-width="2"/>'
            f'{tooltip}</g>')


def _svg(chart_id: str, title: str, height: int, parts: List[str]) -> str:
    return (f'<svg id="{chart_id}" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_WIDTH} {height}" '
            f'width="100%" role="img" aria-label="{_e(title)}"><title>{_e(title)}</title>'
            + "".join(parts) + "</svg>")


def _message(plot: _Plot, text: str) -> str:
    return (f'<text x="{(plot.left + plot.right) / 2:.1f}" y="{(plot.top + plot.bottom) / 2:.1f}" '
            f'text-anchor="middle" class="empty">{_e(text)}</text>')


def _x_domain(attempts: Sequence[Dict[str, Any]], run: Dict[str, Any]):
    hi = max((a["start_offset_s"] for a in attempts), default=0.0)
    if hi <= 0:
        hi = 1.0 / run["settings"]["sampling_frequency_hz"]
    return axis(0.0, hi)


def _value_domain(values: Sequence[float], include_zero: bool):
    lo, hi = min(values), max(values)
    if include_zero:
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    return axis(*_expand_constant(lo, hi))


def _current_chart(run, shown) -> str:
    title = "Current vs. actual start offset"
    readings = [a for a in shown if a["ok"]]
    failures = [a for a in shown if not a["ok"]]
    y_domain = _value_domain([a["current_a"] for a in readings], False) if readings else axis(0.0, 1.0)
    plot = _Plot(_x_domain(shown, run), y_domain, extra_bottom=_FAIL_STRIP)
    parts = _legend([("ok", "successful reading"), ("fail", "failed attempt (no current value)")])
    parts += _axes(plot, "actual start offset (s)", "current (A)")
    strip_y = plot.bottom + 12
    parts.append(f'<rect x="{plot.left}" y="{strip_y}" width="{plot.right - plot.left}" height="{_FAIL_STRIP - 16}" '
                 f'fill="#fdecea" stroke="#e7b4b0"/>')
    parts.append(f'<text x="{plot.left - 6}" y="{strip_y + 13}" text-anchor="end" class="tick">failed</text>')
    if not shown:
        parts.append(_message(plot, "no attempts were made"))
    elif not readings:
        parts.append(_message(plot, "no successful readings"))
    for a in readings:
        parts.append(f'<circle class="ok" cx="{plot.x(a["start_offset_s"]):.2f}" cy="{plot.y(a["current_a"]):.2f}" r="3.5" '
                     f'fill="{_OK_COLOR}"><title>{_e(_attempt_tip(a))}</title></circle>')
    for a in failures:
        parts.append(_cross(plot.x(a["start_offset_s"]), strip_y + (_FAIL_STRIP - 16) / 2, "fail", _attempt_tip(a)))
    return _svg("chart-current", title, plot.height, parts)  # x tick labels sit below the failure strip


def _timing_chart(chart_id, title, y_title, key, run, shown) -> str:
    values_ms = [a[key] * 1000 for a in shown]
    y_domain = _value_domain(values_ms, True) if shown else axis(0.0, 1.0)
    plot = _Plot(_x_domain(shown, run), y_domain)
    parts = _legend([("ok", "successful attempt"), ("fail", "failed attempt")])
    parts += _axes(plot, "actual start offset (s)", y_title)
    if not shown:
        parts.append(_message(plot, "no attempts were made"))
    for a in shown:
        x, y = plot.x(a["start_offset_s"]), plot.y(a[key] * 1000)
        if a["ok"]:
            parts.append(f'<circle class="ok" cx="{x:.2f}" cy="{y:.2f}" r="3.5" fill="{_OK_COLOR}">'
                         f'<title>{_e(_attempt_tip(a))}</title></circle>')
        else:
            parts.append(_cross(x, y, "fail", _attempt_tip(a)))
    return _svg(chart_id, title, plot.height, parts)


def _histogram_chart(values) -> str:
    title = "Histogram of successful current readings"
    hist = histogram(values)
    if hist["counts"]:
        x_domain = axis(hist["edges"][0], hist["edges"][-1])
        y_domain = axis(0.0, float(max(hist["counts"])))
    else:
        x_domain, y_domain = axis(0.0, 1.0), axis(0.0, 1.0)
    plot = _Plot(x_domain, y_domain)
    parts = _legend([("bar", f"readings per bin ({len(hist['counts'])} bins, n = {len(values)})")])
    parts += _axes(plot, "current (A)", "readings")
    if not values:
        parts.append(_message(plot, "no successful readings"))
    for left, right, count in zip(hist["edges"], hist["edges"][1:], hist["counts"]):
        x0, x1, y = plot.x(left), plot.x(right), plot.y(count)
        parts.append(f'<rect class="bin" x="{x0:.2f}" y="{y:.2f}" width="{max(x1 - x0 - 1, 1):.2f}" '
                     f'height="{plot.bottom - y:.2f}" fill="{_BAR_COLOR}" data-count="{count}">'
                     f'<title>{_e(f"{_number(left)} A to {_number(right)} A: {count}")}</title></rect>')
    return _svg("chart-histogram", title, plot.height, parts)


def _attempt_tip(a: Dict[str, Any]) -> str:
    timing = (f"attempt {a['attempt']} (slot {a['slot']}), start {_number(a['start_offset_s'])} s, "
              f"lateness {_number(a['lateness_s'] * 1000)} ms, duration {_number(a['elapsed_s'] * 1000)} ms")
    if a["ok"]:
        return f"{timing}: {_number(a['current_a'])} A"
    return f"{timing}: FAILED {a['error']['type']}: {a['error']['message']}"


# --- HTML -----------------------------------------------------------------------------------

_CSS = """
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;margin:0;background:#fff;color:#222}
main{max-width:820px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:1.5rem;margin:0 0 4px} h2{font-size:1.15rem;margin:28px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px}
.sub{color:#555;margin:0 0 12px;word-break:break-all}
.note{background:#fff8e1;border:1px solid #f0d58a;padding:8px 12px;border-radius:4px;margin:12px 0}
.sampled{background:#e8f1fb;border:1px solid #a9c8ea;padding:8px 12px;border-radius:4px;margin:12px 0}
table{border-collapse:collapse;margin:6px 0;font-size:.92rem} th,td{border:1px solid #ddd;padding:4px 8px;text-align:left;vertical-align:top}
th{background:#f4f4f4;font-weight:600} td.num{text-align:right;font-variant-numeric:tabular-nums}
svg{display:block;margin:10px 0 2px;max-width:100%;height:auto}
svg text{font-family:inherit} .tick{font-size:11px;fill:#444} .axis-title{font-size:12px;fill:#222}
.legend{font-size:12px;fill:#222} .empty{font-size:14px;fill:#777}
.caption{color:#555;font-size:.88rem;margin:0 0 14px}
"""


def _table(rows: Sequence[Tuple[str, Any]]) -> str:
    return "<table>" + "".join(f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in rows) + "</table>"


def _display(value: Any, unit: str = "", scale: float = 1.0) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{_number(value * scale)}{unit}"
    return f"{value}{unit}"


def render_run_report(run: Dict[str, Any], limits: Optional[PerformanceLimits] = None, *,
                      max_points: int = MAX_RENDERED_POINTS) -> str:
    """The complete HTML document for ``run`` (see the module documentation)."""
    performance = build_performance_report(run, limits)
    attempts = run["attempts"]
    selection = select_points(attempts, max_points)
    shown = selection["attempts"]
    values = [a["current_a"] for a in attempts if a["ok"]]
    settings, counts, analysis = run["settings"], run["counts"], run["analysis"]

    run_rows = [
        ("Run ID", run["run_id"]), ("Device", run["ammeter"]),
        ("Address", f"{run['host']}:{run['port']}"), ("Command", run["command"]),
        ("Started (UTC)", run["started_at_utc"]), ("Ended (UTC)", run["ended_at_utc"]),
        ("Sampling frequency", _display(settings["sampling_frequency_hz"], " Hz")),
        ("Count limit", _display(settings["measurements_count"])),
        ("Duration limit", _display(settings["total_duration_seconds"], " s")),
        ("Request timeout", _display(settings["request_timeout_seconds"], " s")),
        ("Attempted / succeeded / failed", f"{counts['attempted']} / {counts['succeeded']} / {counts['failed']}"),
        ("Skipped slots", counts["skipped_slots"]), ("Stop reason", run["stop_reason"]),
        ("Elapsed", _display(run["elapsed_s"], " s")),
    ]
    if analysis["status"] == "ok":
        stats = _table([("Successful readings", analysis["count"]), ("Mean", _display(analysis["mean_a"], " A")),
                        ("Median", _display(analysis["median_a"], " A")),
                        ("Population std dev", _display(analysis["stddev_population_a"], " A")),
                        ("Minimum", _display(analysis["min_a"], " A")), ("Maximum", _display(analysis["max_a"], " A"))])
    elif analysis["status"] == "no_data":
        stats = "<p>No successful readings, so no current statistics.</p>"
    else:
        error = analysis["error"]
        stats = (f"<p>The statistics could not be calculated for {_e(analysis['count'])} successful readings: "
                 f"{_e(error['type'])}: {_e(error['message'])}</p>")

    late, took = performance["start_lateness_s"], performance["request_duration_s"]
    timing_rows = "".join(
        f"<tr><th>{_e(label)}</th>" + "".join(f'<td class="num">{_e(_display(summary[key], " ms", 1000))}</td>'
                                             for key in ("mean_s", "median_s", "stddev_population_s", "p95_s", "max_s"))
        + f'<td class="num">{summary["count"]}</td></tr>'
        for label, summary in (("Start lateness", late), ("Request duration", took)))
    rates = performance["rates"]
    thresholds = performance["thresholds"]
    if thresholds["status"] == "not_evaluated":
        threshold_html = "<p><strong>Thresholds: not evaluated</strong> (no limits were supplied).</p>"
    else:
        threshold_html = (f"<p><strong>Thresholds: {_e(thresholds['status'])}</strong> (user-supplied limits)</p><table>"
                          "<tr><th>Check</th><th>Status</th><th>Observed</th><th>Limit</th></tr>"
                          + "".join(f"<tr><td>{_e(c['name'])}</td><td>{_e(c['status'])}</td>"
                                    f'<td class="num">{_e(_display(c["observed"]))}</td>'
                                    f'<td class="num">{_e(_display(c["limit"]))}</td></tr>'
                                    for c in thresholds["checks"]) + "</table>")

    if selection["sampled"]:
        sampling_note = (f'<p class="sampled"><strong>Downsampled charts:</strong> the three charts below show '
                         f"{len(shown)} of {len(attempts)} attempts ({selection['shown_failed']} of "
                         f"{selection['total_failed']} failed and {selection['shown_ok']} of {selection['total_ok']} "
                         f"successful attempts, evenly spaced; at most {max_points} points per chart). "
                         "The statistics, the performance summary and the histogram use all attempts.</p>")
    else:
        sampling_note = ""

    failed = [a for a in attempts if not a["ok"]]
    if failed:
        rows = "".join(f'<tr><td class="num">{a["attempt"]}</td><td class="num">{_e(_number(a["start_offset_s"]))}</td>'
                       f"<td>{_e(a['error']['type'])}</td><td>{_e(a['error']['message'])}</td></tr>"
                       for a in failed[:MAX_FAILURE_ROWS])
        more = (f"<p>Showing the first {MAX_FAILURE_ROWS} of {len(failed)} failed attempts.</p>"
                if len(failed) > MAX_FAILURE_ROWS else "")
        failures_html = ("<table><tr><th>Attempt</th><th>Start (s)</th><th>Error</th><th>Message</th></tr>"
                         + rows + "</table>" + more)
    else:
        failures_html = "<p>No failed attempts.</p>"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ammeter run {_e(run["run_id"])}</title>
<style>{_CSS}</style>
</head>
<body>
<main>
<h1>Ammeter run report: {_e(run["ammeter"])}</h1>
<p class="sub">Run {_e(run["run_id"])}, started {_e(run["started_at_utc"])}</p>
<p class="note">These values are readings from the supplied ammeter emulators, which generate random numbers.
The charts show what was recorded; they do not establish the accuracy of any device.</p>

<h2>Run</h2>
{_table(run_rows)}

<h2>Current statistics (successful readings only)</h2>
{stats}

<h2>Performance consistency</h2>
<p>How the run was carried out, not the measured current. Failure rate {_e(_display(rates["failure_rate"]))};
skipped-slot rate {_e(_display(rates["skipped_slot_rate"]))} (skipped / (attempted + skipped)).
Timing uses every attempt, including failed ones.</p>
<table><tr><th></th><th>Mean</th><th>Median</th><th>Population std dev</th><th>p95 (nearest rank)</th><th>Max</th><th>Count</th></tr>
{timing_rows}</table>
{threshold_html}

<h2>Charts</h2>
{sampling_note}
<p class="caption">The three charts share the same horizontal axis: the actual start of each attempt, in seconds after the
run started. Points are not connected. Failed attempts are red crosses: in the current chart they sit in the separate
"failed" strip because they have no current value.</p>
{_current_chart(run, shown)}
{_timing_chart("chart-lateness", "Start lateness vs. actual start offset", "start lateness (ms)", "lateness_s", run, shown)}
{_timing_chart("chart-duration", "Request duration vs. actual start offset", "request duration (ms)", "elapsed_s", run, shown)}
{_histogram_chart(values)}
<p class="caption">Histogram: Sturges' rule, ceil(log2 n) + 1 equal-width bins between the smallest and largest reading
(at most {MAX_HISTOGRAM_BINS}); each bin includes its left edge, and the last bin also includes the maximum. Identical
readings give a single bin. Uses all {len(values)} successful readings.</p>

<h2>Failed attempts</h2>
{failures_html}
</main>
</body>
</html>
"""
