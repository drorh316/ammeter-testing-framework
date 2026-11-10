"""Accuracy-assessment demo with small, deterministic SYNTHETIC datasets.

Run from the project root:

    python -m examples.accuracy_demo [--output PATH]

Three made-up datasets ("profiles") are assessed against one known reference of 2.000 A.
They are synthetic numbers chosen to show the difference between bias, spread and RMSE.
They are NOT readings of, or estimates for, the Greenlee, ENTES or CIRCUTOR emulators,
whose random outputs have no known true current and cannot be ranked by accuracy.

The demo prints a table and the ordering by observed RMSE. With ``--output`` it also writes
the complete result as JSON to a new file (never overwritten); the committed
``sample_results/accuracy_synthetic.json`` was written this way.

Exit codes: 0 success; 1 if the output file cannot be written; 2 for invalid usage.
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from src.testing.accuracy import assess_accuracy, rank_by_rmse
from src.testing.archive import ArchiveError, write_new_file

REFERENCE_A = 2.0
REFERENCE_SOURCE = "synthetic: a fixed 2.000 A reference chosen for this demo (no device, no measurement)"

PROFILES = {
    "synthetic-biased-precise": {
        "description": "every reading about 0.1 A high, with very little spread",
        "readings_a": [2.10, 2.11, 2.09, 2.10, 2.10],
    },
    "synthetic-unbiased-noisy": {
        "description": "correct on average, but individual readings scatter widely",
        "readings_a": [1.80, 2.20, 1.90, 2.10, 2.00],
    },
    "synthetic-accurate": {
        "description": "correct on average, with little spread",
        "readings_a": [2.01, 1.99, 2.00, 2.02, 1.98],
    },
}


def build_demo() -> Dict[str, Any]:
    """The complete, deterministic demo result (JSON-compatible)."""
    assessments = {name: assess_accuracy(profile["readings_a"], REFERENCE_A, reference_source=REFERENCE_SOURCE)
                   for name, profile in PROFILES.items()}
    return {
        "synthetic": True,
        "description": ("Synthetic datasets sharing one known reference, to illustrate bias, spread and RMSE. "
                        "Not data from, or a ranking of, the supplied Greenlee, ENTES or CIRCUTOR emulators."),
        "reference": {"current_a": REFERENCE_A, "source": REFERENCE_SOURCE},
        "profiles": {name: {"description": profile["description"], "readings_a": profile["readings_a"],
                            "assessment": assessments[name]} for name, profile in PROFILES.items()},
        "ordering_by_rmse": rank_by_rmse(assessments),
        "ordering_note": ("Ordered by observed RMSE against the shared synthetic reference. This does not identify a "
                          "most reliable device and makes no claim of statistical significance; comparable "
                          "measurement conditions are the caller's responsibility. Equal RMSE values share a rank."),
    }


def _signed(value: float) -> str:
    """4 decimals with a sign; a value that rounds to zero (e.g. 4e-17 of float residue) shows as 0.0000."""
    rounded = round(value, 4)
    return "0.0000" if rounded == 0 else f"{rounded:+.4f}"


def table_lines(demo: Dict[str, Any]) -> List[str]:
    lines = [f"SYNTHETIC data, reference {demo['reference']['current_a']} A ({demo['reference']['source']})",
             "",
             f"{'profile':<26}{'bias (A)':>11}{'MAE (A)':>10}{'spread (A)':>12}{'RMSE (A)':>10}"]
    for name, profile in demo["profiles"].items():
        a = profile["assessment"]
        lines.append(f"{name:<26}{_signed(a['bias_a']):>11}{a['mean_absolute_error_a']:>10.4f}"
                     f"{a['stddev_population_a']:>12.4f}{a['rmse_a']:>10.4f}")
    lines += ["", "spread = population standard deviation of the readings (not accuracy); "
                  "RMSE^2 = bias^2 + spread^2", "", "ordering by observed RMSE (lowest first):"]
    lines += [f"  {entry['rank']}. {entry['name']} (RMSE {entry['rmse_a']:.4f} A)" for entry in demo["ordering_by_rmse"]]
    lines += ["", demo["ordering_note"]]
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m examples.accuracy_demo",
                                     description="Accuracy metrics on synthetic datasets with a known reference.")
    parser.add_argument("--output", type=Path, metavar="PATH",
                        help="also write the result as JSON to this new file (never overwritten)")
    args = parser.parse_args(argv)
    demo = build_demo()
    print("\n".join(table_lines(demo)))
    if args.output is not None:
        try:
            written = write_new_file(args.output,
                                     (json.dumps(demo, indent=2, allow_nan=False) + "\n").encode("utf-8"))
        except ArchiveError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"\nwrote {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
