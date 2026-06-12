"""Smoke-test the PG_E off-grid resiliency driver on a single anchor hour.

Builds the baseline LP exactly as ``run_resiliency_evaluation_pge.main()``
does, then runs the per-anchor outage LP for ``start_hour=1`` only. This
avoids the 8760-hour parallel sweep so the pipeline plumbing
(critical_load_MW override, off-grid input zeroing, demand-charge bypass)
can be validated in a few minutes.

Writes outputs under ``results/resiliency_pge_<tag>/`` just like the real
driver, but with a single per-hour row.
"""

from __future__ import annotations

import logging
import os
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
    _summarize_metrics,
)
from run_resiliency_evaluation_pge import (
    CRITICAL_LOAD_MW,
    INPUTS_DIR,
    MIN_SOC_FRAC,
    OUTAGE_HOURS,
    OUTPUT_DIR,
    RECOVERY_HOURS,
    SCENARIO_ID,
    SLACK_PENALTY,
    SNAPSHOT_DIR,
    SOC_TAG,
    SOLVER,
    SOLVER_OPTIONS,
    YEAR,
    _prepare_offgrid_inputs,
    _configure_logging,
)


def main() -> None:
    _configure_logging()
    log = logging.getLogger("resiliency_pge_smoke")

    smoke_dir = OUTPUT_DIR.parent / f"resiliency_pge_{SOC_TAG}_smoke"
    smoke_dir.mkdir(parents=True, exist_ok=True)

    offgrid_inputs_dir = _prepare_offgrid_inputs(INPUTS_DIR, YEAR)
    log.info("Off-grid tempdir inputs: %s.", offgrid_inputs_dir)

    designed_system = load_designed_system(
        SNAPSHOT_DIR,
        inputs_dir=offgrid_inputs_dir,
        year=YEAR,
        scenario_id=SCENARIO_ID,
        attach_cem_data=True,
    )
    designed_system.import_cap = pd.Series(
        0.0, index=designed_system.import_cap.index, name=designed_system.import_cap.name
    )
    designed_system.export_cap = pd.Series(
        0.0, index=designed_system.export_cap.index, name=designed_system.export_cap.name
    )

    h2_ref_soc = _read_h2_reference_soc(SNAPSHOT_DIR, INPUTS_DIR, YEAR)
    h2_floor_mwh = MIN_SOC_FRAC * h2_ref_soc
    cap_e_phase1, pdis_phase1 = _read_h2_phase1_caps(SNAPSHOT_DIR, YEAR)
    eff_h2 = _read_h2_efficiency(INPUTS_DIR, YEAR)
    log.info(
        "H2 ref floor=%.2f MWh (Cap_E_Phase1=%.2f MWh, Pdis_Phase1=%.2f MW, Eff=%.4f); "
        "tag=%s -> h2_floor=%.2f MWh.",
        h2_ref_soc,
        cap_e_phase1,
        pdis_phase1,
        eff_h2,
        SOC_TAG,
        h2_floor_mwh,
    )
    soc_floor = _h2_only_soc_map(designed_system.storage_caps, h2_floor_mwh)

    _dump_designed_system_summary(
        designed_system,
        out_dir=smoke_dir,
        soc_floor=soc_floor,
        outage_hours=OUTAGE_HOURS,
        recovery_hours=RECOVERY_HOURS,
        min_soc_recovery_frac=MIN_SOC_FRAC,
        h2_ref_soc=h2_ref_soc,
        h2_floor_mwh=h2_floor_mwh,
        soc_tag=SOC_TAG,
        outaged_components=tuple(VALID_COMPONENTS),
    )

    log.info("Building baseline (no demand charges, off-grid).")
    baseline_model = build_baseline_dispatch(
        designed_system,
        n_hours=8760,
        min_soc_per_tech=soc_floor,
        add_demand_charges=False,
    )
    baseline_results = run_baseline_dispatch(
        baseline_model,
        solver=SOLVER,
        solver_options=SOLVER_OPTIONS,
        tee=False,
    )
    log.info("Baseline solver status: %s.", baseline_results.solver_status)
    if baseline_results.solver_status not in ("optimal", "globallyOptimal"):
        raise SystemExit(f"Baseline non-optimal: {baseline_results.solver_status!r}.")

    _append_baseline_costs_to_summary(baseline_model, baseline_results, out_dir=smoke_dir)
    baseline_dir = smoke_dir / "baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    _save_baseline_timeseries(baseline_results, designed_system, baseline_model, baseline_dir)

    outage_spec = _build_storage_only_outage(
        designed_system,
        h2_floor_mwh,
        outage_hours=OUTAGE_HOURS,
        recovery_hours=RECOVERY_HOURS,
    )
    outage_spec.validate(designed_system)

    log.info(
        "Running outage LP for hours=[1, 2000, 5000] with critical_load=%.2f MW.",
        CRITICAL_LOAD_MW,
    )
    results, dispatch_df = run_outage_evaluation_with_dispatch(
        baseline_results,
        outage_spec=outage_spec,
        designed_system=designed_system,
        slack_penalty=SLACK_PENALTY,
        critical_load_MW=CRITICAL_LOAD_MW,
        n_hours=8760,
        hours=[1, 2000, 5000],
        n_workers=1,
        solver=SOLVER,
        solver_options=SOLVER_OPTIONS,
        dispatch_csv_path=smoke_dir / "outage_dispatch.csv",
        soc_slack_csv_path=smoke_dir / "recovery_soc_slack.csv",
    )

    results.per_hour.to_csv(smoke_dir / "per_hour_metrics.csv")
    agg = _summarize_metrics(results.per_hour)
    pd.Series(agg, name="value").to_frame().to_csv(smoke_dir / "aggregate_metrics.csv")
    log.info("Per-hour metrics:\n%s", results.per_hour.to_string())
    log.info("Aggregate metrics:\n%s", pd.Series(agg).to_string())
    log.info("Smoke dispatch rows: %d.", len(dispatch_df))


if __name__ == "__main__":
    main()
