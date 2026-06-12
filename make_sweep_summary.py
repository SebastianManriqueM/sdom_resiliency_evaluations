"""Aggregate per-SOC resiliency results into per-case sweep summaries.

For each case (``mea``, ``pge``, ...) present under ``results/`` as
``results/resiliency_<case>_<tag>/`` folders, reads each tag's
``aggregate_metrics.csv``, ``recovery_soc_slack.csv`` and
``designed_system/summary.json`` and emits, under
``results/sweep_summary/<case>/``:

* ``sweep_aggregate_metrics.csv`` (one row per tag)
* ``{LOLP,LOLE,EUE_mean_p95_p99,max_unserved_MW_mean_p95_p99}.png``
* ``sweep_soc_slack_metrics.csv`` (one row per tag x tech)
* ``{SOC_slack_probability,SOC_slack_MWh_mean_p95_p99,
  SOC_slack_fraction_mean,SOC_slack_cost_total_USD}.png``
* ``sweep_objective_costs.csv`` (one row per tag)
* ``objective_total_USD.png`` plus ``*_vs_cost.png`` overlays

Run from the repo root with the project venv active::

    python make_sweep_summary.py
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


REPO_ROOT = Path(__file__).resolve().parent
RESULTS_ROOT = REPO_ROOT / "results"
SWEEP_ROOT = RESULTS_ROOT / "sweep_summary"
CASE_PATTERN = re.compile(r"^resiliency_(?P<case>[a-z0-9]+)_(?P<tag>[0-9.]+SOC)$")
# Per-case subfolder under ``results/`` that holds the per-tag run
# directories. Order controls the iteration order in main().
CASE_DIRS: dict[str, str] = {
    "mea": "MEA",
    "pge": "PG_E",
}
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


def _compute_expected_opex(case_dir: Path) -> float:
    """Fallback ``expected_opex_USD`` for cases without it in aggregate_metrics.

    Reads ``per_hour_metrics.csv`` and computes
    ``sum(objective_value) / len(rows)`` (NaN-safe). Older runs produced
    before the metric was added to ``run_resiliency_evaluation.py`` lack the
    aggregate row but still carry the per-anchor objective values.
    """
    path = case_dir / "per_hour_metrics.csv"
    if not path.exists():
        return float("nan")
    df = pd.read_csv(path)
    if "objective_value" not in df.columns or df.empty:
        return float("nan")
    if "solver_status" in df.columns:
        df = df[df["solver_status"] != "error"]
    n = len(df)
    if n == 0:
        return float("nan")
    obj_sum = float(df["objective_value"].astype(float).dropna().sum())
    return obj_sum / n


def _case_root(case: str) -> Path:
    return RESULTS_ROOT / CASE_DIRS.get(case, case)


def _iter_case_dirs(case: str):
    root = _case_root(case)
    if not root.exists():
        return
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        m = CASE_PATTERN.match(entry.name)
        if not m or m.group("case") != case:
            continue
        yield entry, m.group("tag")


def _collect(case: str) -> pd.DataFrame:
    rows: list[dict[str, float | str]] = []
    for entry, tag in _iter_case_dirs(case):
        agg_file = entry / "aggregate_metrics.csv"
        if not agg_file.exists():
            continue
        soc_frac = float(tag.replace("SOC", ""))
        metrics = _read_aggregate(entry)
        rows.append(
            {
                "case": case,
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
                "expected_opex_USD": metrics.get(
                    "expected_opex_USD",
                    _compute_expected_opex(entry),
                ),
            }
        )
    df = pd.DataFrame(rows).sort_values("soc_frac").reset_index(drop=True)
    return df


def _save_plots(df: pd.DataFrame, out_dir: Path) -> None:
    x = df["soc_frac"].to_numpy()

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(x, df["LOLP"], marker="o", color="C0")
    ax.set_xlabel("H2 SOC floor fraction")
    ax.set_ylabel("LOLP")
    ax.set_title("Loss-of-load probability vs H2 SOC floor")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "LOLP.png", dpi=120)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(x, df["LOLE_hours_per_event"], marker="o", color="C1")
    ax.set_xlabel("H2 SOC floor fraction")
    ax.set_ylabel("LOLE [hours / anchor]")
    ax.set_title("Loss-of-load expectation vs H2 SOC floor")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "LOLE.png", dpi=120)
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
    fig.savefig(out_dir / "EUE_mean_p95_p99.png", dpi=120)
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
    fig.savefig(out_dir / "max_unserved_MW_mean_p95_p99.png", dpi=120)
    plt.close(fig)

    # Expected OPEX = sum(per-anchor objective) / 8760, plotted in M USD.
    fig, ax = plt.subplots(figsize=(6, 4))
    y = df["expected_opex_USD"].to_numpy() / 1e6
    ax.plot(x, y, marker="o", color="C4")
    for xi, yi in zip(x, y):
        if np.isnan(yi):
            continue
        ax.annotate(
            f"{yi:.2f}",
            xy=(xi, yi),
            xytext=(4, 4),
            textcoords="offset points",
            fontsize=8,
        )
    ax.set_xlabel("H2 SOC floor fraction")
    ax.set_ylabel("Expected OPEX [M USD / hour]")
    ax.set_title("Expected OPEX = sum(per-anchor objective) / 8760")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "expected_opex_USD.png", dpi=120)
    plt.close(fig)


def _collect_soc_slack(case: str) -> pd.DataFrame:
    rows: list[dict[str, float | str]] = []
    for entry, tag in _iter_case_dirs(case):
        slack_file = entry / "recovery_soc_slack.csv"
        if not slack_file.exists():
            continue
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
                    "case": case,
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


def _save_soc_slack_plots(slack_df: pd.DataFrame, out_dir: Path) -> None:
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
    fig.savefig(out_dir / "SOC_slack_probability.png", dpi=120)
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
    fig.savefig(out_dir / "SOC_slack_MWh_mean_p95_p99.png", dpi=120)
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
    fig.savefig(out_dir / "SOC_slack_fraction_mean.png", dpi=120)
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
    fig.savefig(out_dir / "SOC_slack_cost_total_USD.png", dpi=120)
    plt.close(fig)


_OBJECTIVE_COST_COMPONENTS: tuple[str, ...] = (
    "thermal_var_USD",
    "storage_var_USD",
    "imports_USD",
    "exports_USD",
    "demand_charges_USD",
    "curtailment_USD",
    "fom_USD",
)


def _collect_objective(case: str) -> pd.DataFrame:
    rows: list[dict[str, float | str]] = []
    for entry, tag in _iter_case_dirs(case):
        summary_file = entry / "designed_system" / "summary.json"
        if not summary_file.exists():
            continue
        with summary_file.open("r", encoding="utf-8") as fh:
            summary = json.load(fh)
        baseline = summary.get("baseline_costs", {})
        soc_frac = float(tag.replace("SOC", ""))
        row: dict[str, float | str] = {
            "case": case,
            "tag": tag,
            "soc_frac": soc_frac,
            "objective_total_USD": float(baseline.get("objective_total_USD", float("nan"))),
            "solver_status": str(baseline.get("solver_status", "")),
        }
        for comp in _OBJECTIVE_COST_COMPONENTS:
            row[comp] = float(baseline.get(comp, 0.0))
        rows.append(row)
    return pd.DataFrame(rows).sort_values("soc_frac").reset_index(drop=True)


def _save_objective_plot(df: pd.DataFrame, out_dir: Path) -> None:
    x = df["soc_frac"].to_numpy()
    obj = df["objective_total_USD"].to_numpy() / 1e6

    fig, ax = plt.subplots(figsize=(6, 4))
    bars = ax.bar(
        [f"{v:g}" for v in x],
        obj,
        color="C2",
        edgecolor="black",
        alpha=0.85,
    )
    for bar, value in zip(bars, obj):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height(),
            f"{value:.2f}",
            ha="center",
            va="bottom",
            fontsize=8,
        )
    ax.set_xlabel("H2 SOC floor fraction")
    ax.set_ylabel("Objective total [M USD]")
    ax.set_title("Designed-system objective cost vs H2 SOC floor")
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "objective_total_USD.png", dpi=120)
    plt.close(fig)


def _annotate_points(ax, x: np.ndarray, y: np.ndarray, tags: list[str]) -> None:
    for xi, yi, tag in zip(x, y, tags):
        if np.isnan(yi):
            continue
        ax.annotate(
            tag,
            xy=(xi, yi),
            xytext=(4, 4),
            textcoords="offset points",
            fontsize=8,
        )


def _save_metric_vs_cost_plots(
    metrics_df: pd.DataFrame,
    obj_df: pd.DataFrame,
    out_dir: Path,
) -> pd.DataFrame:
    merged = metrics_df.merge(
        obj_df[["tag", "objective_total_USD"]],
        on="tag",
        how="inner",
    ).sort_values("objective_total_USD").reset_index(drop=True)
    if merged.empty:
        return merged

    cost = merged["objective_total_USD"].to_numpy() / 1e6
    tags = merged["tag"].tolist()

    single_metric_specs = [
        ("LOLP", "LOLP", "C0", "Loss-of-load probability vs baseline cost", "LOLP_vs_cost.png"),
        (
            "LOLE_hours_per_event",
            "LOLE [hours / anchor]",
            "C1",
            "Loss-of-load expectation vs baseline cost",
            "LOLE_vs_cost.png",
        ),
    ]
    for column, ylabel, color, title, fname in single_metric_specs:
        y = merged[column].to_numpy()
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.plot(cost, y, marker="o", color=color)
        _annotate_points(ax, cost, y, tags)
        ax.set_xlabel("Baseline objective cost [M USD]")
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(out_dir / fname, dpi=120)
        plt.close(fig)

    multi_metric_specs = [
        (
            "EUE [MWh / anchor]",
            "EUE vs baseline cost",
            "EUE_vs_cost.png",
            [
                ("EUE_mean_MWh", "mean", "o", "-"),
                ("EUE_p95_MWh", "p95", "s", "--"),
                ("EUE_p99_MWh", "p99", "^", ":"),
            ],
        ),
        (
            "max unserved power [MW]",
            "Max unserved power vs baseline cost",
            "max_unserved_MW_vs_cost.png",
            [
                ("max_unserved_MW_mean", "mean", "o", "-"),
                ("max_unserved_MW_p95", "p95", "s", "--"),
                ("max_unserved_MW_p99", "p99", "^", ":"),
            ],
        ),
    ]
    for ylabel, title, fname, series in multi_metric_specs:
        fig, ax = plt.subplots(figsize=(6, 4))
        for column, label, marker, linestyle in series:
            y = merged[column].to_numpy()
            ax.plot(cost, y, marker=marker, linestyle=linestyle, label=label)
        annotate_y = merged[series[0][0]].to_numpy()
        _annotate_points(ax, cost, annotate_y, tags)
        ax.set_xlabel("Baseline objective cost [M USD]")
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.grid(True, alpha=0.3)
        ax.legend()
        fig.tight_layout()
        fig.savefig(out_dir / fname, dpi=120)
        plt.close(fig)

    fig, axes = plt.subplots(2, 2, figsize=(11, 8), sharex=True)
    overview_specs = [
        (axes[0, 0], "LOLP", "LOLP", "C0", None),
        (axes[0, 1], "LOLE_hours_per_event", "LOLE [hours / anchor]", "C1", None),
        (axes[1, 0], "EUE_mean_MWh", "EUE mean [MWh / anchor]", "C2", "EUE_p95_MWh"),
        (axes[1, 1], "max_unserved_MW_mean", "max unserved mean [MW]", "C3", "max_unserved_MW_p95"),
    ]
    for ax, column, ylabel, color, p95_column in overview_specs:
        y = merged[column].to_numpy()
        ax.plot(cost, y, marker="o", color=color, label="mean" if p95_column else None)
        if p95_column is not None:
            ax.plot(
                cost,
                merged[p95_column].to_numpy(),
                marker="s",
                linestyle="--",
                color=color,
                alpha=0.6,
                label="p95",
            )
            ax.legend(fontsize=8)
        _annotate_points(ax, cost, y, tags)
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.3)
    for ax in axes[1, :]:
        ax.set_xlabel("Baseline objective cost [M USD]")
    fig.suptitle("Resiliency metrics vs baseline objective cost (annotated by H2 SOC floor)")
    fig.tight_layout()
    fig.savefig(out_dir / "metrics_vs_cost_overview.png", dpi=120)
    plt.close(fig)

    return merged


def _discover_cases() -> list[str]:
    cases: list[str] = []
    for case in CASE_DIRS:
        root = _case_root(case)
        if not root.exists():
            continue
        if any(CASE_PATTERN.match(p.name) for p in root.iterdir() if p.is_dir()):
            cases.append(case)
    return cases


def _process_case(case: str, log: logging.Logger) -> None:
    out_dir = SWEEP_ROOT / case
    out_dir.mkdir(parents=True, exist_ok=True)

    df = _collect(case)
    if df.empty:
        log.warning(
            "[%s] No per-case aggregate_metrics.csv files found under %s.",
            case,
            RESULTS_ROOT,
        )
        return

    out_csv = out_dir / "sweep_aggregate_metrics.csv"
    df.to_csv(out_csv, index=False)
    log.info("[%s] Sweep aggregate metrics (%d cases) saved to %s.", case, len(df), out_csv)
    log.info("\n%s", df.to_string(index=False))

    _save_plots(df, out_dir)
    log.info("[%s] Sweep plots saved under %s.", case, out_dir)

    slack_df = _collect_soc_slack(case)
    if slack_df.empty:
        log.warning(
            "[%s] No per-case recovery_soc_slack.csv files found under %s; "
            "skipping SOC-slack sweep summary.",
            case,
            RESULTS_ROOT,
        )
    else:
        slack_csv = out_dir / "sweep_soc_slack_metrics.csv"
        slack_df.to_csv(slack_csv, index=False)
        log.info(
            "[%s] Sweep SOC-slack metrics (%d tag x tech rows) saved to %s.",
            case,
            len(slack_df),
            slack_csv,
        )
        log.info("\n%s", slack_df.to_string(index=False))
        _save_soc_slack_plots(slack_df, out_dir)
        log.info("[%s] SOC-slack sweep plots saved under %s.", case, out_dir)

    obj_df = _collect_objective(case)
    if obj_df.empty:
        log.warning(
            "[%s] No per-case designed_system/summary.json files found under %s; "
            "skipping objective-cost sweep summary.",
            case,
            RESULTS_ROOT,
        )
        return

    obj_csv = out_dir / "sweep_objective_costs.csv"
    obj_df.to_csv(obj_csv, index=False)
    log.info("[%s] Sweep objective costs (%d cases) saved to %s.", case, len(obj_df), obj_csv)
    log.info("\n%s", obj_df.to_string(index=False))

    _save_objective_plot(obj_df, out_dir)
    log.info("[%s] Objective-cost sweep plot saved under %s.", case, out_dir)

    merged = _save_metric_vs_cost_plots(df, obj_df, out_dir)
    if merged.empty:
        log.warning(
            "[%s] Could not join aggregate metrics with objective costs; "
            "skipping metrics-vs-cost plots.",
            case,
        )
    else:
        log.info(
            "[%s] Resiliency-vs-cost plots saved under %s (%d cases plotted).",
            case,
            out_dir,
            len(merged),
        )


def main() -> None:
    _configure_logging()
    log = logging.getLogger("sweep_summary")

    SWEEP_ROOT.mkdir(parents=True, exist_ok=True)
    cases = _discover_cases()
    if not cases:
        log.warning("No resiliency case directories found under %s.", RESULTS_ROOT)
        return
    log.info("Discovered cases: %s.", cases)
    for case in cases:
        _process_case(case, log)


if __name__ == "__main__":
    main()
