"""Storage-only resiliency evaluation against the off-grid PG&E paper design.

Mirrors :mod:`run_resiliency_evaluation` (the MEA driver) but targets the
off-grid PG&E CEM snapshot at
``data/PG_E/outputs_CEM/For_simulations_PG_E`` and the matching previous-
stage inputs at ``data/PG_E/inputs_csv/Paper``.

Off-grid specifics
------------------
The PG&E case has **no grid connection**, so this driver suppresses:

* **Grid imports/exports in the baseline LP** by writing a tempdir clone
  of the inputs with ``Export_Cap_{year}.csv`` zeroed before
  ``load_designed_system`` runs (the ``Import_Cap`` CSV is already all
  zeros in the source data, but we zero it defensively as well).
* **Demand charges in the baseline LP** by passing
  ``add_demand_charges=False`` to :func:`build_baseline_dispatch`.
* **Grid imports/exports in the per-anchor outage LP** by mutating
  ``designed_system.import_cap`` and ``designed_system.export_cap`` to
  zero series after load. The outage builder reads these series directly,
  so the imports/exports blocks build with all-zero capacities.

Outage critical load
--------------------
The user-facing override is a constant **3 MW critical load** during the
outage sub-horizon (``[start_hour, start_hour + duration - 1]``); the
recovery sub-horizon continues to use the original load series so the
system replenishes toward the H2 SOC recovery target against realistic
post-outage demand. The override is plumbed through
:func:`run_outage_evaluation_with_dispatch` as ``critical_load_MW=3.0``.

Run from the repo root with the project venv active::

    python run_resiliency_evaluation_pge.py
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

import pandas as pd

from sdom.resiliency import (
    VALID_COMPONENTS,
    build_baseline_dispatch,
    load_designed_system,
    run_baseline_dispatch,
)

from _outage_dispatch_export import run_outage_evaluation_with_dispatch
from run_resiliency_evaluation import (
    _append_baseline_costs_to_summary,
    _build_storage_only_outage,
    _dump_designed_system_summary,
    _h2_only_soc_map,
    _read_h2_efficiency,
    _read_h2_phase1_caps,
    _read_h2_reference_soc,
    _save_baseline_timeseries,
    _save_plots,
    _summarize_metrics,
)


# ---------------------------------------------------------------------------
# Paths and configuration
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent
SNAPSHOT_DIR = REPO_ROOT / "data" / "PG_E" / "outputs_CEM" / "For_simulations_PG_E"
INPUTS_DIR = REPO_ROOT / "data" / "PG_E" / "inputs_csv" / "Paper"

YEAR = 2030
SCENARIO_ID = 1
SOC_TAG = os.environ.get("SDOM_SOC_TAG", "1.0SOC")
MIN_SOC_FRAC = float(os.environ.get("SDOM_MIN_SOC_FRAC", SOC_TAG.replace("SOC", "")))
OUTAGE_HOURS = 24
RECOVERY_HOURS = 24
SOLVER = "xpress"
SOLVER_OPTIONS: dict = {"mipgap": 0.0001}
SLACK_PENALTY = 10_000.0
# Constant critical load (MW) used during the outage sub-horizon only.
CRITICAL_LOAD_MW = 3.0

OUTPUT_DIR = REPO_ROOT / "results" / "PG_E" / f"resiliency_pge_{SOC_TAG}"


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        datefmt="%H:%M:%S",
    )


def _prepare_offgrid_inputs(src_inputs: Path, year: int) -> Path:
    """Mirror ``src_inputs`` to a tempdir and zero out grid Import/Export caps.

    The off-grid PG&E case must not import or export any energy across the
    annual baseline. ``load_designed_system`` reads ``Import_Cap_{year}.csv``
    and ``Export_Cap_{year}.csv`` into both ``DesignedSystem`` series and
    ``cem_data``; the CEM baseline LP enforces ``Pexp[t] <= cap[t]``, so
    rewriting the caps to 0 in a mirrored inputs dir is the cleanest way
    to forbid grid flows in the baseline without touching the source data.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="pge_offgrid_inputs_"))
    shutil.copytree(src_inputs, tmp_dir, dirs_exist_ok=True)
    for fname, header in (
        (f"Import_Cap_{year}.csv", "Imports"),
        (f"Export_Cap_{year}.csv", "Exports"),
    ):
        src_file = src_inputs / fname
        if not src_file.exists():
            raise FileNotFoundError(f"Missing required input: {src_file}")
        df = pd.read_csv(src_file)
        value_col = df.columns[1]
        df[value_col] = 0
        df.to_csv(tmp_dir / fname, index=False)
    return tmp_dir


def main() -> None:
    _configure_logging()
    log = logging.getLogger("resiliency_pge")

    if not SNAPSHOT_DIR.exists():
        raise FileNotFoundError(f"Snapshot folder not found: {SNAPSHOT_DIR}")
    if not INPUTS_DIR.exists():
        raise FileNotFoundError(f"Inputs folder not found: {INPUTS_DIR}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    log.info(
        "Using CEM snapshot at %s (H2 SOC floor tag = %s, off-grid mode).",
        SNAPSHOT_DIR,
        SOC_TAG,
    )

    offgrid_inputs_dir = _prepare_offgrid_inputs(INPUTS_DIR, YEAR)
    log.info(
        "Off-grid inputs mirrored to %s (Import_Cap & Export_Cap zeroed).",
        offgrid_inputs_dir,
    )

    log.info(
        "Loading designed system (year=%d, scenario=%d) with CEM data attached.",
        YEAR,
        SCENARIO_ID,
    )
    designed_system = load_designed_system(
        SNAPSHOT_DIR,
        inputs_dir=offgrid_inputs_dir,
        year=YEAR,
        scenario_id=SCENARIO_ID,
        attach_cem_data=True,
    )
    # Defensive: ensure the outage builder (which reads these series
    # directly off the DesignedSystem, bypassing cem_data) cannot import
    # or export either. The series are already zero after the tempdir
    # mirror, so this is idempotent.
    designed_system.import_cap = pd.Series(
        0.0, index=designed_system.import_cap.index, name=designed_system.import_cap.name
    )
    designed_system.export_cap = pd.Series(
        0.0, index=designed_system.export_cap.index, name=designed_system.export_cap.name
    )
    log.info(
        "Designed system: %d storage techs, %d thermal plants, "
        "%d solar plants, %d wind plants.",
        len(designed_system.storage_caps),
        len(designed_system.thermal_caps),
        len(designed_system.solar_caps),
        len(designed_system.wind_caps),
    )

    cap_e_phase1, pdis_phase1 = _read_h2_phase1_caps(SNAPSHOT_DIR, YEAR)
    eff_h2 = _read_h2_efficiency(INPUTS_DIR, YEAR)
    h2_ref_soc = _read_h2_reference_soc(SNAPSHOT_DIR, INPUTS_DIR, YEAR)
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
        soc_tag=SOC_TAG,
        outaged_components=tuple(VALID_COMPONENTS),
    )

    log.info(
        "Building baseline annual dispatch (off-grid, no demand charges)."
    )
    baseline_model = build_baseline_dispatch(
        designed_system,
        n_hours=8760,
        min_soc_per_tech=soc_floor,
        add_demand_charges=False,
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

    log.info("Appending baseline cost breakdown to summary.json.")
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
    outage_spec = _build_storage_only_outage(
        designed_system,
        h2_floor_mwh,
        outage_hours=OUTAGE_HOURS,
        recovery_hours=RECOVERY_HOURS,
    )
    outage_spec.validate(designed_system)
    log.info("Outaged components: %s.", list(outage_spec.outaged_assets.keys()))

    log.info(
        "Running per-hour outage evaluation across 8760 anchor hours "
        "(parallel workers, dispatch export, critical_load=%.2f MW).",
        CRITICAL_LOAD_MW,
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
        critical_load_MW=CRITICAL_LOAD_MW,
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
