"""Aggregate per-SOC resiliency results into a sweep summary.

Reads each ``results/resiliency_mea_<tag>/aggregate_metrics.csv`` and emits:

* ``results/sweep_summary/sweep_aggregate_metrics.csv`` (one row per tag)
* ``results/sweep_summary/{LOLP,LOLE,EUE_mean_p95_p99,max_unserved_MW_mean_p95_p99}.png``

Run from the repo root with the project venv active::

    python ___sweep_summary.py
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


REPO_ROOT = Path(__file__).resolve().parent
RESULTS_ROOT = REPO_ROOT / "results"
OUT_DIR = RESULTS_ROOT / "sweep_summary"
TAG_PATTERN = re.compile(r"^resiliency_mea_(?P<tag>[0-9.]+SOC)$")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        datefmt="%H:%M:%S",
    )


def _read_aggregate(case_dir: Path) -> dict[str, float]:
    df = pd.read_csv(case_dir / "aggregate_metrics.csv", index_col=0)
    col = df.columns[0]
    return {idx: float(val) for idx, val in df[col].items()}


def _collect() -> pd.DataFrame:
    rows: list[dict[str, float | str]] = []
    for entry in sorted(RESULTS_ROOT.iterdir()):
        if not entry.is_dir():
            continue
        m = TAG_PATTERN.match(entry.name)
        if not m:
            continue
        agg_file = entry / "aggregate_metrics.csv"
        if not agg_file.exists():
            continue
        tag = m.group("tag")
        soc_frac = float(tag.replace("SOC", ""))
        metrics = _read_aggregate(entry)
        rows.append(
            {
                "tag": tag,
                "soc_frac": soc_frac,
                "LOLP": metrics.get("LOLP", 0.0),
                "LOLE_hours_per_event": metrics.get("LOLE_hours_per_event", 0.0),
                "EUE_mean_MWh": metrics.get("EUE_mean_MWh", 0.0),
                "EUE_p95_MWh": metrics.get("EUE_p95_MWh", 0.0),
                "EUE_p99_MWh": metrics.get("EUE_p99_MWh", 0.0),
                "max_unserved_MW_mean": metrics.get("max_unserved_MW_mean", 0.0),
                "max_unserved_MW_p95": metrics.get("max_unserved_MW_p95", 0.0),
                "max_unserved_MW_p99": metrics.get("max_unserved_MW_p99", 0.0),
            }
        )
    df = pd.DataFrame(rows).sort_values("soc_frac").reset_index(drop=True)
    return df


def _save_plots(df: pd.DataFrame) -> None:
    x = df["soc_frac"].to_numpy()

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(x, df["LOLP"], marker="o", color="C0")
    ax.set_xlabel("H2 SOC floor fraction")
    ax.set_ylabel("LOLP")
    ax.set_title("Loss-of-load probability vs H2 SOC floor")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "LOLP.png", dpi=120)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(x, df["LOLE_hours_per_event"], marker="o", color="C1")
    ax.set_xlabel("H2 SOC floor fraction")
    ax.set_ylabel("LOLE [hours / anchor]")
    ax.set_title("Loss-of-load expectation vs H2 SOC floor")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "LOLE.png", dpi=120)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(x, df["EUE_mean_MWh"], marker="o", label="mean")
    ax.plot(x, df["EUE_p95_MWh"], marker="s", label="p95")
    ax.plot(x, df["EUE_p99_MWh"], marker="^", label="p99")
    ax.set_xlabel("H2 SOC floor fraction")
    ax.set_ylabel("EUE [MWh / anchor]")
    ax.set_title("EUE distribution vs H2 SOC floor")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUT_DIR / "EUE_mean_p95_p99.png", dpi=120)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(x, df["max_unserved_MW_mean"], marker="o", label="mean")
    ax.plot(x, df["max_unserved_MW_p95"], marker="s", label="p95")
    ax.plot(x, df["max_unserved_MW_p99"], marker="^", label="p99")
    ax.set_xlabel("H2 SOC floor fraction")
    ax.set_ylabel("max unserved power [MW]")
    ax.set_title("Max unserved power vs H2 SOC floor")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUT_DIR / "max_unserved_MW_mean_p95_p99.png", dpi=120)
    plt.close(fig)


def main() -> None:
    _configure_logging()
    log = logging.getLogger("sweep_summary")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = _collect()
    if df.empty:
        log.warning("No per-case aggregate_metrics.csv files found under %s.", RESULTS_ROOT)
        return

    out_csv = OUT_DIR / "sweep_aggregate_metrics.csv"
    df.to_csv(out_csv, index=False)
    log.info("Sweep aggregate metrics (%d cases) saved to %s.", len(df), out_csv)
    log.info("\n%s", df.to_string(index=False))

    _save_plots(df)
    log.info("Sweep plots saved under %s.", OUT_DIR)


if __name__ == "__main__":
    main()
