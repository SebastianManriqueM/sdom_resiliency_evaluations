"""Storage-only resiliency evaluation against the MEA paper design.

Loads the fixed-capacity design from
``data/MEA/outputs_CEM/For_simulations_MEA`` (a single CEM case that pins
capacities for every SOC-floor run), uses the matching previous-stage CEM
inputs at ``data/MEA/inputs_csv/Paper_MEA 1``, runs the baseline annual
dispatch by reusing the CEM formulations with capacities pinned, then
performs a per-hour outage evaluation where every non-storage resource is
fully outaged (imports, wind, solar, balancing units, hydro, nuclear, other
renewables), leaving only the designed storage fleet to ride through a
48 h outage + 48 h recovery window. Metrics (LOLP, LOLE, EUE, USE_hours,
max_unserved_MW) are aggregated and saved as CSV + distribution plots.

The per-run H2 SOC floor is the only thing that varies across the 6-tag
sweep; it is set via the ``SDOM_SOC_TAG`` env var (e.g. ``0.5SOC``,
``0.6SOC``, ..., ``1.0SOC``) and applied as ``MIN_SOC_FRAC * h2_ref``,
where ``h2_ref`` is the H2 reference floor derived from the Phase1 CEM
summary as ``Cap_E_Phase1 + Pdis_Phase1 / sqrt(Eff_H2)`` (Phase1 energy
capacity plus the 1-hour discharge headroom adjusted by efficiency).

Run from the repo root with the project venv active::

    python run_resiliency_evaluation.py
"""

from __future__ import annotations

import glob
import json
import logging
import math
import os
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import pyomo.environ as pyo

from sdom.resiliency import (
    VALID_COMPONENTS,
    OutageSpec,
    build_baseline_dispatch,
    load_designed_system,
    plot_metric_distribution,
    run_baseline_dispatch,
)

from _outage_dispatch_export import run_outage_evaluation_with_dispatch


# ---------------------------------------------------------------------------
# Paths and configuration
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent
SNAPSHOT_DIR = REPO_ROOT / "data" / "MEA" / "outputs_CEM" / "For_simulations_MEA"
INPUTS_DIR = REPO_ROOT / "data" / "MEA" / "inputs_csv" / "Paper_MEA 1"

YEAR = 2030
SCENARIO_ID = 1
SOC_TAG = os.environ.get("SDOM_SOC_TAG", "0.9SOC")
MIN_SOC_FRAC = float(os.environ.get("SDOM_MIN_SOC_FRAC", SOC_TAG.replace("SOC", "")))
OUTAGE_HOURS = 48
RECOVERY_HOURS = 48
SOLVER = "xpress"
SOLVER_OPTIONS: dict = {"mipgap": 0.0001}
SLACK_PENALTY = 10_000.0

OUTPUT_DIR = REPO_ROOT / "results" / f"resiliency_mea_{SOC_TAG}"


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        datefmt="%H:%M:%S",
    )


def _find_snapshot_file(snapshot_dir: Path, prefix: str, year: int) -> Path:
    """Locate ``{year}_{prefix}_*.csv`` (excluding Phase1) in ``snapshot_dir``.

    Mirrors :func:`sdom.resiliency.data_loader._find_snapshot_file` so the
    H2 reference SOC is read from the exact same file the loader will pick.
    """
    pattern = str(snapshot_dir / f"{year}_{prefix}_*.csv")
    matches = [Path(p) for p in glob.glob(pattern) if "Phase1" not in os.path.basename(p)]
    if not matches:
        raise FileNotFoundError(
            f"No {prefix} snapshot file matching '{pattern}' "
            f"(excluding Phase1 files) was found."
        )
    matches.sort(key=lambda p: (len(p.name), p.name))
    return matches[0]



def _build_storage_only_outage(designed_system, h2_floor_mwh: float) -> OutageSpec:
    """Outage every non-storage component plus Li-Ion for OUTAGE_HOURS at factor 0.

    Li-Ion is treated as outaged during the outage window (so only H2 can
    ride through), but recovers automatically when the outage window ends
    because ``OutageSpec.duration_hours`` controls outage length only —
    recovery hours leave ``delta = 1`` (no outage). H2 stays available
    throughout both windows.
    """
    non_storage = [c for c in VALID_COMPONENTS if c != "storage"]
    li_ion_techs = sorted(
        tech for tech in designed_system.storage_caps if "LI" in tech.upper().replace("-", "")
    )
    outaged: dict = {c: "all" for c in non_storage}
    if li_ion_techs:
        outaged["storage"] = li_ion_techs
    return OutageSpec(
        duration_hours=OUTAGE_HOURS,
        recovery_hours=RECOVERY_HOURS,
        outaged_assets=outaged,
        min_soc_recovery=_h2_only_soc_map(designed_system.storage_caps, h2_floor_mwh),
    )


def _find_phase1_summary(snapshot_dir: Path, year: int) -> Path:
    """Locate ``{year}_OutputSummary_Phase1_*.csv`` in ``snapshot_dir``."""
    pattern = str(snapshot_dir / f"{year}_OutputSummary_Phase1_*.csv")
    matches = [Path(p) for p in glob.glob(pattern)]
    if not matches:
        raise FileNotFoundError(
            f"No Phase1 OutputSummary file matching '{pattern}' was found."
        )
    matches.sort(key=lambda p: (len(p.name), p.name))
    return matches[0]


def _read_h2_phase1_caps(snapshot_dir: Path) -> tuple[float, float]:
    """Return ``(Cap_E_Phase1, Pdis_Phase1)`` for H2 from the Phase1 summary.

    Reads ``Energy capacity`` (MWh) and ``Discharge power capacity`` (MW)
    rows for the H2 technology out of the Phase1 ``OutputSummary``.
    """
    summary_file = _find_phase1_summary(snapshot_dir, YEAR)
    df = pd.read_csv(summary_file)
    tech = df["Technology"].astype(str).str.strip().str.upper()
    metric = df["Metric"].astype(str).str.strip()
    val = df["Optimal Value"].astype(float)

    def _pick(metric_name: str) -> float:
        mask = (metric == metric_name) & (tech == "H2")
        if not mask.any():
            raise ValueError(
                f"No '{metric_name}' row for H2 in {summary_file.name}."
            )
        return float(val[mask].iloc[0])

    return _pick("Energy capacity"), _pick("Discharge power capacity")


def _read_h2_efficiency(inputs_dir: Path, year: int) -> float:
    """Return round-trip efficiency ``Eff`` for H2 from ``StorageData_{year}.csv``."""
    path = inputs_dir / f"StorageData_{year}.csv"
    df = pd.read_csv(path, index_col=0)
    if "H2" not in df.columns or "Eff" not in df.index:
        raise ValueError(
            f"{path.name} missing H2 column or Eff row; cannot derive SOC floor."
        )
    return float(df.at["Eff", "H2"])


def _read_h2_reference_soc(snapshot_dir: Path, inputs_dir: Path) -> float:
    """Return the H2 reference floor SOC (MWh) derived from Phase1 CEM outputs.

    Formula
    -------
    ``h2_ref = Cap_E_Phase1 + Pdis_Phase1 / sqrt(Eff_H2)``

    where ``Cap_E_Phase1`` and ``Pdis_Phase1`` come from the H2 ``Energy
    capacity`` and ``Discharge power capacity`` rows of the Phase1 CEM
    ``OutputSummary``, and ``Eff_H2`` is the round-trip efficiency from
    ``StorageData_{YEAR}.csv``. The added term is the 1-hour discharge
    headroom adjusted by the one-way efficiency.
    """
    cap_e_phase1, pdis_phase1 = _read_h2_phase1_caps(snapshot_dir)
    eff_h2 = _read_h2_efficiency(inputs_dir, YEAR)
    if eff_h2 <= 0.0:
        raise ValueError(f"H2 Eff must be > 0; got {eff_h2}.")
    return cap_e_phase1 + pdis_phase1 / math.sqrt(eff_h2)


def _h2_only_soc_map(storage_caps, h2_floor_mwh: float) -> dict[str, float]:
    """Return ``min_soc_per_tech`` fractions (of ``Cap_E``) for the library.

    Only hydrogen storage gets a non-zero floor. The absolute floor in MWh is
    ``h2_floor_mwh``; this helper converts it to the equivalent fraction of
    the H2 ``Cap_E`` that the library API expects.
    """
    out: dict[str, float] = {}
    for tech, spec in storage_caps.items():
        if "H2" not in tech.upper():
            out[tech] = 0.0
            continue
        ecap = float(spec.get("Cap_E", 0.0))
        out[tech] = (h2_floor_mwh / ecap) if ecap > 0.0 else 0.0
    return out


def _summarize_metrics(per_hour_df: pd.DataFrame) -> dict[str, float]:
    """Compute aggregate resiliency metrics from the per-hour record table.

    ``expected_opex_USD`` is the sum of per-anchor objective values divided
    by the number of hours in the annual horizon (``len(df)``, normally
    8760). The per-anchor objective is the outage+recovery LP optimum
    (unserved + curtailment + soc-slack penalties + prorated FOM).
    """
    df = per_hour_df.copy()
    if "solver_status" in df.columns:
        df = df[df["solver_status"] != "error"]
    n = len(df)
    eue = df["EUE"].astype(float)
    use_hours = df["USE_hours"].astype(float)
    max_unserved = df["max_unserved_MW"].astype(float)
    has_loss = eue > 1e-6
    objective = (
        df["objective_value"].astype(float)
        if "objective_value" in df.columns
        else pd.Series(dtype=float)
    )
    obj_sum = float(objective.dropna().sum()) if not objective.empty else 0.0
    expected_opex = (obj_sum / n) if n else 0.0
    return {
        "n_anchor_hours": float(n),
        "LOLP": float(has_loss.mean()) if n else 0.0,
        "LOLE_hours_per_event": float(use_hours.mean()) if n else 0.0,
        "EUE_mean_MWh": float(eue.mean()) if n else 0.0,
        "EUE_p95_MWh": float(eue.quantile(0.95)) if n else 0.0,
        "EUE_p99_MWh": float(eue.quantile(0.99)) if n else 0.0,
        "EUE_max_MWh": float(eue.max()) if n else 0.0,
        "USE_hours_mean": float(use_hours.mean()) if n else 0.0,
        "USE_hours_p95": float(use_hours.quantile(0.95)) if n else 0.0,
        "max_unserved_MW_mean": float(max_unserved.mean()) if n else 0.0,
        "max_unserved_MW_p95": float(max_unserved.quantile(0.95)) if n else 0.0,
        "max_unserved_MW_max": float(max_unserved.max()) if n else 0.0,
        "expected_opex_USD": expected_opex,
    }


def _dump_designed_system_summary(
    designed_system,
    *,
    out_dir: Path,
    soc_floor: dict[str, float],
    outage_hours: int,
    recovery_hours: int,
    min_soc_recovery_frac: float,
    h2_ref_soc: float,
    h2_floor_mwh: float,
) -> None:
    """Write the designed-system capacities, parameters, and SOC constraint to disk.

    Outputs in ``out_dir / "designed_system"``:

    * ``summary.json`` — single JSON with system metadata, storage / thermal /
      VRE caps, SOC floor (in absolute MWh + fraction), and outage/recovery
      window settings.
    """
    ds_dir = out_dir / "designed_system"
    ds_dir.mkdir(parents=True, exist_ok=True)

    storage_rows = []
    for tech, spec in designed_system.storage_caps.items():
        ecap = float(spec.get("Cap_E", 0.0))
        frac = float(soc_floor.get(tech, spec.get("soc_min_frac", 0.0)))
        storage_rows.append(
            {
                "tech": tech,
                "Cap_Pch_MW": float(spec.get("Cap_Pch", 0.0)),
                "Cap_Pdis_MW": float(spec.get("Cap_Pdis", 0.0)),
                "Cap_E_MWh": ecap,
                "eta_ch": float(spec.get("eta_ch", 0.0)),
                "eta_dis": float(spec.get("eta_dis", 0.0)),
                "vom_USD_per_MWh": float(spec.get("vom", 0.0)),
                "soc_min_frac_applied": frac,
                "soc_min_MWh_applied": frac * ecap,
                "duration_hours_Edis": (
                    ecap / float(spec["Cap_Pdis"])
                    if spec.get("Cap_Pdis", 0.0)
                    else None
                ),
            }
        )

    thermal_rows = [
        {"plant_id": pid, **{k: float(v) for k, v in spec.items()}}
        for pid, spec in designed_system.thermal_caps.items()
    ]

    def _series_stat(s, name):
        if s is None:
            return None
        s = s.astype(float)
        return {
            "name": name,
            "length": int(s.size),
            "sum_MWh": float(s.sum()),
            "mean_MW": float(s.mean()),
            "min_MW": float(s.min()),
            "max_MW": float(s.max()),
        }

    summary = {
        "scenario_id": int(designed_system.scenario_id),
        "year": int(designed_system.year),
        "snapshot_tag": SOC_TAG,
        "counts": {
            "storage_techs": len(designed_system.storage_caps),
            "thermal_plants": len(designed_system.thermal_caps),
            "solar_plants": len(designed_system.solar_caps),
            "wind_plants": len(designed_system.wind_caps),
        },
        "storage": storage_rows,
        "thermal": thermal_rows,
        "solar": {k: float(v) for k, v in designed_system.solar_caps.items()},
        "wind": {k: float(v) for k, v in designed_system.wind_caps.items()},
        "soc_constraint": {
            "user_floor_frac": MIN_SOC_FRAC,
            "h2_reference_min_soc_MWh": h2_ref_soc,
            "h2_reference_source": "min H2 SOC in CEM OutputStorage snapshot",
            "h2_floor_MWh": h2_floor_mwh,
            "baseline_floor_frac_per_tech": {
                tech: float(soc_floor.get(tech, 0.0))
                for tech in designed_system.storage_caps
            },
            "baseline_floor_per_tech_MWh": {
                tech: float(soc_floor.get(tech, 0.0))
                * float(designed_system.storage_caps[tech].get("Cap_E", 0.0))
                for tech in designed_system.storage_caps
            },
            "outage_min_soc_recovery_MWh": {
                tech: float(soc_floor.get(tech, 0.0))
                * float(designed_system.storage_caps[tech].get("Cap_E", 0.0))
                for tech in designed_system.storage_caps
            },
            "description": (
                "H2 SOC floor for this scenario = user_floor_frac * "
                "h2_reference_min_soc_MWh (taken from the single CEM "
                "OutputStorage snapshot that pins capacities). Non-H2 "
                "storage techs receive a 0.0 floor and can cycle freely. "
                "The library API expects fractions of Cap_E; "
                "baseline_floor_frac_per_tech shows the value actually "
                "passed in. Outage recovery uses the same per-tech map."
            ),
        },
        "outage_window": {
            "duration_hours": outage_hours,
            "recovery_hours": recovery_hours,
            "outaged_components": list(VALID_COMPONENTS),
        },
        "time_series": {
            "load": _series_stat(designed_system.load, "load"),
            "nuclear": _series_stat(designed_system.nuclear, "nuclear"),
            "hydro": _series_stat(designed_system.hydro, "hydro"),
            "other_renewables": _series_stat(
                designed_system.other_renewables, "other_renewables"
            ),
            "import_cap": _series_stat(designed_system.import_cap, "import_cap"),
            "import_price": _series_stat(designed_system.import_price, "import_price"),
            "export_cap": _series_stat(designed_system.export_cap, "export_cap"),
            "export_price": _series_stat(designed_system.export_price, "export_price"),
        },
        "formulation_map": dict(designed_system.formulation_map),
    }
    with (ds_dir / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)


def _append_baseline_costs_to_summary(
    baseline_model,
    baseline_results,
    *,
    out_dir: Path,
) -> None:
    """Extract solved baseline cost components and merge into ``summary.json``.

    Reports monthly fixed/variable demand charges (``D_fix[m]``, ``D_var[m]``)
    alongside their totals and the total baseline objective.
    """
    summary_path = out_dir / "designed_system" / "summary.json"
    if not summary_path.exists():
        return

    obj_total = float(baseline_results.objective_value or 0.0)
    costs: dict[str, object] = {
        "objective_total_USD": obj_total,
        "solver_status": baseline_results.solver_status,
    }

    breakdown = dict(getattr(baseline_results, "cost_breakdown", {}) or {})
    if breakdown:
        for key, value in breakdown.items():
            costs[key] = float(value)
        sum_components = (
            breakdown.get("thermal_var_USD", 0.0)
            + breakdown.get("storage_var_USD", 0.0)
            + breakdown.get("imports_USD", 0.0)
            - breakdown.get("exports_USD", 0.0)
            + breakdown.get("demand_charges_USD", 0.0)
            + breakdown.get("curtailment_USD", 0.0)
            + breakdown.get("fom_USD", 0.0)
        )
        total = breakdown.get("total_USD", obj_total)
        tol = max(1.0, 1e-6 * abs(total))
        diff = sum_components - total
        if abs(diff) > tol:
            logging.getLogger("resiliency_mea").warning(
                "Baseline cost reconciliation drift: sum(components)=%.3f, "
                "total=%.3f, diff=%.3f (tol=%.3f).",
                sum_components,
                total,
                diff,
                tol,
            )

    breakdown = dict(getattr(baseline_results, "cost_breakdown", {}) or {})
    if breakdown:
        costs.update(breakdown)
        component_sum = (
            float(breakdown.get("thermal_var_USD", 0.0))
            + float(breakdown.get("storage_var_USD", 0.0))
            + float(breakdown.get("imports_USD", 0.0))
            - float(breakdown.get("exports_USD", 0.0))
            + float(breakdown.get("demand_charges_USD", 0.0))
            + float(breakdown.get("curtailment_USD", 0.0))
            + float(breakdown.get("fom_USD", 0.0))
        )
        total = float(breakdown.get("total_USD", obj_total))
        diff = abs(component_sum - total)
        tolerance = max(1.0, 1e-6 * abs(total))
        if diff > tolerance:
            logging.getLogger("resiliency_mea").warning(
                "Baseline cost reconciliation mismatch: sum(components)=%.6f, "
                "total=%.6f, diff=%.6f (tolerance=%.6f).",
                component_sum,
                total,
                diff,
                tolerance,
            )

    if hasattr(baseline_model, "demand_charges"):
        dc = baseline_model.demand_charges
        months = sorted(int(m) for m in dc.M)
        d_fix_by_month = {m: float(pyo.value(dc.D_fix[m])) for m in months}
        d_var_by_month = {m: float(pyo.value(dc.D_var[m])) for m in months}
        d_fix_total = sum(d_fix_by_month.values())
        d_var_total = sum(d_var_by_month.values())
        costs["demand_charges"] = {
            "D_fix_total_USD": d_fix_total,
            "D_var_total_USD": d_var_total,
            "D_fix_by_month_USD": d_fix_by_month,
            "D_var_by_month_USD": d_var_by_month,
            "share_of_objective": (
                (d_fix_total + d_var_total) / obj_total if obj_total > 0 else 0.0
            ),
            "description": (
                "D_fix[m] = max_{t in month m} phi_fix[t] * Pimp[t]; "
                "D_var[m] = max_{t in month m} phi_var[t] * Pimp[t] "
                "(USD). Tariffs sourced from fixed_dem_charges.csv and "
                "var_dem_charges.csv."
            ),
        }

    with summary_path.open("r", encoding="utf-8") as f:
        summary = json.load(f)
    summary["baseline_costs"] = costs
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)


def _save_baseline_timeseries(baseline_results, designed_system, baseline_model, out_dir: Path) -> None:
    """Write all baseline time series to a single wide CSV indexed by hour.

    VRE columns are prefixed ``Psolar_av_`` / ``Pwind_av_`` because they store
    *available* potential (capacity_factor x capacity), not dispatched power.
    Curtailed VRE is reported separately in the ``Curtailment_solar`` /
    ``Curtailment_wind`` columns; dispatched VRE = available - curtailment.
    """

    def _prefix(df: pd.DataFrame, prefix: str) -> pd.DataFrame:
        df = df.copy()
        df.columns = [f"{prefix}_{c}" for c in df.columns]
        return df

    def _as_series(s, name, index):
        if s is None:
            return pd.Series(0.0, index=index, name=name)
        s = pd.Series(s).astype(float)
        s.index = index[: len(s)] if len(s) == len(index) else s.index
        s.name = name
        return s

    soc = _prefix(baseline_results.soc_trajectory, "SOC")
    pcha = _prefix(baseline_results.pcha_trajectory, "Pcha")
    pdis = _prefix(baseline_results.pdis_trajectory, "Pdis")
    pth = _prefix(baseline_results.pthermal_trajectory, "Pthermal")
    psol = _prefix(baseline_results.psolar_trajectory, "Psolar_av")
    pwd = _prefix(baseline_results.pwind_trajectory, "Pwind_av")
    idx = soc.index

    pimp = pd.Series(baseline_results.pimp).astype(float)
    pimp.index = idx[: len(pimp)]
    pimp.name = "Pimp"
    pexp = pd.Series(baseline_results.pexp).astype(float)
    pexp.index = idx[: len(pexp)]
    pexp.name = "Pexp"

    load = _as_series(designed_system.load, "Load", idx)
    nuclear = _as_series(designed_system.nuclear, "Nuclear", idx)
    hydro = _as_series(designed_system.hydro, "Hydro", idx)
    other_re = _as_series(designed_system.other_renewables, "OtherRenewables", idx)

    hours = list(baseline_model.h)
    def _block_curt(block_name: str) -> pd.Series:
        if not hasattr(baseline_model, block_name):
            return pd.Series(0.0, index=idx, name=f"Curtailment_{block_name}")
        block = getattr(baseline_model, block_name)
        if not hasattr(block, "curtailment"):
            return pd.Series(0.0, index=idx, name=f"Curtailment_{block_name}")
        vals = [float(pyo.value(block.curtailment[h])) for h in hours]
        s = pd.Series(vals, index=idx[: len(vals)], name=f"Curtailment_{block_name}")
        return s

    curt_solar = _block_curt("pv").rename("Curtailment_solar")
    curt_wind = _block_curt("wind").rename("Curtailment_wind")
    curt_total = (curt_solar + curt_wind).rename("Curtailment_total")

    combined = pd.concat(
        [
            load, nuclear, hydro, other_re,
            psol, pwd, pth, pcha, pdis, soc, pimp, pexp,
            curt_solar, curt_wind, curt_total,
        ],
        axis=1,
    )
    combined.index.name = "hour"
    combined.to_csv(out_dir / "timeseries.csv")


def _save_plots(results, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for metric in ("EUE", "USE_hours", "max_unserved_MW"):
        for kind in ("hist", "ecdf", "exceedance"):
            fig, ax = plt.subplots(figsize=(6, 4))
            try:
                plot_metric_distribution(results, metric=metric, kind=kind, ax=ax)
            except ValueError:
                plt.close(fig)
                continue
            ax.set_title(f"{metric} - {kind}")
            ax.set_xlabel(metric)
            fig.tight_layout()
            fig.savefig(out_dir / f"{metric}_{kind}.png", dpi=120)
            plt.close(fig)


def main() -> None:
    _configure_logging()
    log = logging.getLogger("resiliency_mea")

    if not SNAPSHOT_DIR.exists():
        raise FileNotFoundError(f"Snapshot folder not found: {SNAPSHOT_DIR}")
    if not INPUTS_DIR.exists():
        raise FileNotFoundError(f"Inputs folder not found: {INPUTS_DIR}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    log.info(
        "Using CEM snapshot at %s (H2 SOC floor tag = %s).",
        SNAPSHOT_DIR,
        SOC_TAG,
    )

    log.info(
        "Loading designed system (year=%d, scenario=%d) with CEM data attached.",
        YEAR,
        SCENARIO_ID,
    )
    designed_system = load_designed_system(
        SNAPSHOT_DIR,
        inputs_dir=INPUTS_DIR,
        year=YEAR,
        scenario_id=SCENARIO_ID,
        attach_cem_data=True,
    )
    log.info(
        "Designed system: %d storage techs, %d thermal plants, "
        "%d solar plants, %d wind plants.",
        len(designed_system.storage_caps),
        len(designed_system.thermal_caps),
        len(designed_system.solar_caps),
        len(designed_system.wind_caps),
    )

    cap_e_phase1, pdis_phase1 = _read_h2_phase1_caps(SNAPSHOT_DIR)
    eff_h2 = _read_h2_efficiency(INPUTS_DIR, YEAR)
    h2_ref_soc = _read_h2_reference_soc(SNAPSHOT_DIR, INPUTS_DIR)
    h2_floor_mwh = MIN_SOC_FRAC * h2_ref_soc
    log.info(
        "H2 reference floor = Cap_E_Phase1 (%.2f MWh) + Pdis_Phase1 (%.2f MW) "
        "/ sqrt(Eff_H2 = %.4f) = %.2f MWh; H2 floor for this run = %.2f * %.2f = %.2f MWh.",
        cap_e_phase1,
        pdis_phase1,
        eff_h2,
        h2_ref_soc,
        MIN_SOC_FRAC,
        h2_ref_soc,
        h2_floor_mwh,
    )
    soc_floor = _h2_only_soc_map(designed_system.storage_caps, h2_floor_mwh)

    log.info("Writing designed-system summary for validation.")
    _dump_designed_system_summary(
        designed_system,
        out_dir=OUTPUT_DIR,
        soc_floor=soc_floor,
        outage_hours=OUTAGE_HOURS,
        recovery_hours=RECOVERY_HOURS,
        min_soc_recovery_frac=MIN_SOC_FRAC,
        h2_ref_soc=h2_ref_soc,
        h2_floor_mwh=h2_floor_mwh,
    )

    log.info("Building baseline annual dispatch (CEM-reuse, capacities fixed).")
    baseline_model = build_baseline_dispatch(
        designed_system,
        n_hours=8760,
        min_soc_per_tech=soc_floor,
    )
    log.info(
        "Solving baseline dispatch with solver=%s, options=%s.",
        SOLVER,
        SOLVER_OPTIONS,
    )
    baseline_results = run_baseline_dispatch(
        baseline_model,
        solver=SOLVER,
        solver_options=SOLVER_OPTIONS,
        tee=False,
    )
    log.info("Baseline solver status: %s.", baseline_results.solver_status)
    if baseline_results.solver_status not in ("optimal", "globallyOptimal"):
        raise RuntimeError(
            f"Baseline dispatch did not solve to optimality "
            f"(status={baseline_results.solver_status!r}); aborting."
        )

    log.info("Appending baseline cost breakdown (incl. demand charges) to summary.json.")
    _append_baseline_costs_to_summary(
        baseline_model,
        baseline_results,
        out_dir=OUTPUT_DIR,
    )

    # Persist baseline trajectories as a single consolidated time-series CSV.
    baseline_dir = OUTPUT_DIR / "baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    _save_baseline_timeseries(baseline_results, designed_system, baseline_model, baseline_dir)

    log.info(
        "Building storage-only outage spec (%dh outage + %dh recovery).",
        OUTAGE_HOURS,
        RECOVERY_HOURS,
    )
    outage_spec = _build_storage_only_outage(designed_system, h2_floor_mwh)
    outage_spec.validate(designed_system)
    log.info("Outaged components: %s.", list(outage_spec.outaged_assets.keys()))

    log.info(
        "Running per-hour outage evaluation across 8760 anchor hours "
        "(parallel workers, dispatch export)."
    )
    outage_dispatch_path = OUTPUT_DIR / "outage_dispatch.csv"
    soc_slack_path = OUTPUT_DIR / "recovery_soc_slack.csv"
    # NOTE: do NOT pass `min_soc_per_tech` here. The H2 SOC floor is a
    # planning/baseline constraint; during an outage the storage must be
    # allowed to draw down to 0 and only meet the floor again at the end
    # of the recovery window (enforced by the recovery-target constraint
    # built from `outage_min_soc_recovery_MWh`).
    results, _ = run_outage_evaluation_with_dispatch(
        baseline_results,
        outage_spec=outage_spec,
        designed_system=designed_system,
        slack_penalty=SLACK_PENALTY,
        n_hours=8760,
        solver=SOLVER,
        solver_options=SOLVER_OPTIONS,
        dispatch_csv_path=outage_dispatch_path,
        soc_slack_csv_path=soc_slack_path,
    )

    per_hour_path = OUTPUT_DIR / "per_hour_metrics.csv"
    results.per_hour.to_csv(per_hour_path)
    log.info("Per-hour metrics saved to %s.", per_hour_path)

    agg = _summarize_metrics(results.per_hour)
    agg_path = OUTPUT_DIR / "aggregate_metrics.csv"
    pd.Series(agg, name="value").to_frame().to_csv(agg_path)
    log.info("Aggregate metrics:\n%s", pd.Series(agg).to_string())
    log.info("Aggregate metrics saved to %s.", agg_path)

    plots_dir = OUTPUT_DIR / "plots"
    _save_plots(results, plots_dir)
    log.info("Distribution plots saved under %s.", plots_dir)


if __name__ == "__main__":
    main()
