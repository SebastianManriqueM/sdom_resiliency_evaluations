"""Aggregate per-SOC resiliency results into a sweep summary.

Reads each ``results/resiliency_mea_<tag>/aggregate_metrics.csv`` and
``results/resiliency_mea_<tag>/recovery_soc_slack.csv`` and emits:

* ``results/sweep_summary/sweep_aggregate_metrics.csv`` (one row per tag)
* ``results/sweep_summary/{LOLP,LOLE,EUE_mean_p95_p99,max_unserved_MW_mean_p95_p99}.png``
* ``results/sweep_summary/sweep_soc_slack_metrics.csv`` (one row per tag x tech)
* ``results/sweep_summary/{SOC_slack_probability,SOC_slack_MWh_mean_p95_p99,
  SOC_slack_fraction_mean,SOC_slack_cost_total_USD}.png``

Run from the repo root with the project venv active::

    python ___sweep_summary.py
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


REPO_ROOT = Path(__file__).resolve().parent
RESULTS_ROOT = REPO_ROOT / "results"
OUT_DIR = RESULTS_ROOT / "sweep_summary"
TAG_PATTERN = re.compile(r"^resiliency_mea_(?P<tag>[0-9.]+SOC)$")
SLACK_EPS = 1e-6


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


def _collect_soc_slack() -> pd.DataFrame:
    rows: list[dict[str, float | str]] = []
    for entry in sorted(RESULTS_ROOT.iterdir()):
        if not entry.is_dir():
            continue
        m = TAG_PATTERN.match(entry.name)
        if not m:
            continue
        slack_file = entry / "recovery_soc_slack.csv"
        if not slack_file.exists():
            continue
        tag = m.group("tag")
        soc_frac = float(tag.replace("SOC", ""))
        df = pd.read_csv(slack_file)
        for tech, group in df.groupby("tech", sort=True):
            slack_mwh = group["recovery_soc_slack_MWh"].to_numpy()
            target_mwh = group["recovery_target_MWh"].to_numpy()
            cost_usd = group["soc_slack_cost_USD"].to_numpy()
            mask_positive_target = target_mwh > SLACK_EPS
            if mask_positive_target.any():
                frac = slack_mwh[mask_positive_target] / target_mwh[mask_positive_target]
                frac_mean = float(np.mean(frac))
                frac_p95 = float(np.percentile(frac, 95))
            else:
                frac_mean = float("nan")
                frac_p95 = float("nan")
            rows.append(
                {
                    "tag": tag,
                    "soc_frac": soc_frac,
                    "tech": str(tech),
                    "n_anchors": int(slack_mwh.size),
                    "slack_probability": float(np.mean(slack_mwh > SLACK_EPS)),
                    "slack_MWh_mean": float(np.mean(slack_mwh)),
                    "slack_MWh_p95": float(np.percentile(slack_mwh, 95)),
                    "slack_MWh_p99": float(np.percentile(slack_mwh, 99)),
                    "slack_MWh_max": float(np.max(slack_mwh)),
                    "slack_fraction_mean": frac_mean,
                    "slack_fraction_p95": frac_p95,
                    "slack_cost_total_USD": float(np.sum(cost_usd)),
                }
            )
    return (
        pd.DataFrame(rows)
        .sort_values(["tech", "soc_frac"])
        .reset_index(drop=True)
    )


def _save_soc_slack_plots(slack_df: pd.DataFrame) -> None:
    techs = list(dict.fromkeys(slack_df["tech"].tolist()))
    markers = ["o", "s", "^", "D", "v", "P"]
    tech_style = {tech: markers[i % len(markers)] for i, tech in enumerate(techs)}

    fig, ax = plt.subplots(figsize=(6, 4))
    for tech in techs:
        sub = slack_df[slack_df["tech"] == tech].sort_values("soc_frac")
        ax.plot(sub["soc_frac"], sub["slack_probability"], marker=tech_style[tech], label=tech)
    ax.set_xlabel("H2 SOC floor fraction")
    ax.set_ylabel("P(recovery slack > 0)")
    ax.set_title("Recovery-SOC slack probability vs H2 SOC floor")
    ax.grid(True, alpha=0.3)
    ax.legend(title="tech")
    fig.tight_layout()
    fig.savefig(OUT_DIR / "SOC_slack_probability.png", dpi=120)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6, 4))
    for tech in techs:
        sub = slack_df[slack_df["tech"] == tech].sort_values("soc_frac")
        ax.plot(sub["soc_frac"], sub["slack_MWh_mean"], marker="o", label=f"{tech} mean")
        ax.plot(sub["soc_frac"], sub["slack_MWh_p95"], marker="s", linestyle="--", label=f"{tech} p95")
        ax.plot(sub["soc_frac"], sub["slack_MWh_p99"], marker="^", linestyle=":", label=f"{tech} p99")
    ax.set_xlabel("H2 SOC floor fraction")
    ax.set_ylabel("Recovery slack [MWh / anchor]")
    ax.set_title("Recovery-SOC slack magnitude vs H2 SOC floor")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "SOC_slack_MWh_mean_p95_p99.png", dpi=120)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6, 4))
    for tech in techs:
        sub = slack_df[slack_df["tech"] == tech].sort_values("soc_frac")
        ax.plot(sub["soc_frac"], sub["slack_fraction_mean"], marker=tech_style[tech], label=tech)
    ax.set_xlabel("H2 SOC floor fraction")
    ax.set_ylabel("mean(slack / recovery_target)")
    ax.set_title("Mean SOC-slack shortfall fraction vs H2 SOC floor")
    ax.grid(True, alpha=0.3)
    ax.legend(title="tech")
    fig.tight_layout()
    fig.savefig(OUT_DIR / "SOC_slack_fraction_mean.png", dpi=120)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6, 4))
    for tech in techs:
        sub = slack_df[slack_df["tech"] == tech].sort_values("soc_frac")
        ax.plot(sub["soc_frac"], sub["slack_cost_total_USD"] / 1e6, marker=tech_style[tech], label=tech)
    ax.set_xlabel("H2 SOC floor fraction")
    ax.set_ylabel("Total recovery-slack cost [M USD]")
    ax.set_title("Annual SOC-slack cost vs H2 SOC floor")
    ax.grid(True, alpha=0.3)
    ax.legend(title="tech")
    fig.tight_layout()
    fig.savefig(OUT_DIR / "SOC_slack_cost_total_USD.png", dpi=120)
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

    slack_df = _collect_soc_slack()
    if slack_df.empty:
        log.warning(
            "No per-case recovery_soc_slack.csv files found under %s; "
            "skipping SOC-slack sweep summary.",
            RESULTS_ROOT,
        )
        return

    slack_csv = OUT_DIR / "sweep_soc_slack_metrics.csv"
    slack_df.to_csv(slack_csv, index=False)
    log.info(
        "Sweep SOC-slack metrics (%d tag x tech rows) saved to %s.",
        len(slack_df),
        slack_csv,
    )
    log.info("\n%s", slack_df.to_string(index=False))

    _save_soc_slack_plots(slack_df)
    log.info("SOC-slack sweep plots saved under %s.", OUT_DIR)


if __name__ == "__main__":
    main()
