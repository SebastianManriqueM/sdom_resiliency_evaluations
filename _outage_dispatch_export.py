"""Per-anchor outage evaluation that also exports dispatch trajectories.

Wraps the SDOM resiliency outage builder/solver so it returns:

1. a :class:`~sdom.resiliency.ResiliencyResults` container with the same
   per-hour metrics columns as :func:`sdom.resiliency.run_resiliency_evaluation`
   (``EUE``, ``USE_hours``, ``max_unserved_MW``, ``objective_value``,
   ``solver_status``, ``solve_time_s``, ``truncated``, ``error_message``);
2. a long-format dispatch DataFrame containing the per-hour outage
   trajectories (one row per ``(start_hour, hour)``), filtered to anchor
   hours with strictly positive total unserved energy (the "loss-event
   only" filter described in
   ``dev_guidelines/resiliency evaluation/metrics_and_plots.md``).

The helper is intentionally kept outside the public ``sdom.resiliency``
package because it is a driver-side utility tied to the columns expected
by the MEA paper post-processing scripts. The library-side runner
(``run_resiliency_evaluation``) returns only metrics.

Per-hour outage LPs are independent and solved in parallel via
:class:`concurrent.futures.ProcessPoolExecutor` (one process per worker).
"""

from __future__ import annotations

import logging
import os
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import pandas as pd
import pyomo.environ as pyo

from sdom.resiliency import (
    BaselineDispatchResults,
    DesignedSystem,
    OutageSpec,
    ResiliencyResults,
    build_outage_dispatch,
)
from sdom.resiliency.runner import (
    _PER_HOUR_COLUMNS,
    _USE_EPS,
    _compute_truncation,
    _resolve_solver,
)


__all__ = ["run_outage_evaluation_with_dispatch"]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
def _solve_one_hour_with_dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    """Build, solve and capture trajectories for a single anchor hour.

    Module-level so it is picklable for ``ProcessPoolExecutor`` workers on
    Windows ``spawn``. Returns a dict with the per-hour record plus a list
    of per-(start_hour, hour) dispatch rows.
    """
    start_hour = int(payload["start_hour"])
    n_hours = int(payload["n_hours"])
    outage_spec: OutageSpec = payload["outage_spec"]
    designed_system: DesignedSystem = payload["designed_system"]

    duration_hours = int(outage_spec.duration_hours)
    recovery_per_tech = outage_spec.resolve_recovery_hours(designed_system)
    max_recovery = max(recovery_per_tech.values()) if recovery_per_tech else 0
    truncated = _compute_truncation(
        start_hour=start_hour,
        duration_hours=duration_hours,
        max_recovery=max_recovery,
        n_hours=n_hours,
    )

    record: dict[str, Any] = {
        "start_hour": start_hour,
        "EUE": 0.0,
        "USE_hours": 0,
        "max_unserved_MW": 0.0,
        "objective_value": float("nan"),
        "solver_status": "error",
        "solve_time_s": 0.0,
        "truncated": bool(truncated),
        "error_message": "",
        "dispatch_rows": [],
        "soc_slack_rows": [],
    }

    t0 = time.perf_counter()
    try:
        model = build_outage_dispatch(
            payload["baseline_results"],
            start_hour=start_hour,
            outage_spec=outage_spec,
            designed_system=designed_system,
            slack_penalty=float(payload["slack_penalty"]),
            curtailment_penalty=float(payload["curtailment_penalty"]),
            soc_slack_penalty=float(payload.get("soc_slack_penalty", 1_000.0)),
            min_soc_per_tech=payload.get("min_soc_per_tech"),
            n_hours=n_hours,
        )
        solver = _resolve_solver(str(payload["solver"]))
        solver_options = payload.get("solver_options") or {}
        res = solver.solve(model, options=solver_options)
        status = str(res.solver.termination_condition)

        hours = list(model.h)
        u_values = [float(pyo.value(model.u[t])) for t in hours]
        eue = float(sum(u_values))
        use_hours = int(sum(1 for v in u_values if v > _USE_EPS))
        max_unserved = float(max(u_values)) if u_values else 0.0
        obj = float(pyo.value(model.objective))

        record.update(
            EUE=eue,
            USE_hours=use_hours,
            max_unserved_MW=max_unserved,
            objective_value=obj,
            solver_status=status,
            error_message="",
        )

        # SOC recovery slack is captured for every anchor (one row per
        # storage tech) -- the slack burn often dominates the objective
        # without any unserved energy, so a sidecar is essential for
        # diagnostics. See dispatch_csv gating below for trajectories.
        soc_slack_pen = float(payload.get("soc_slack_penalty", 1_000.0))
        record["soc_slack_rows"] = _extract_soc_slack_rows(
            model=model,
            start_hour=start_hour,
            soc_slack_penalty=soc_slack_pen,
        )
        slack_by_tech = {
            row["tech"]: row["recovery_soc_slack_MWh"]
            for row in record["soc_slack_rows"]
        }

        # Only capture trajectories for loss-event anchors to keep the
        # output CSV small (matches the original driver behaviour).
        if eue > _USE_EPS:
            record["dispatch_rows"] = _extract_dispatch_rows(
                model=model,
                hours=hours,
                start_hour=start_hour,
                duration_hours=duration_hours,
                designed_system=designed_system,
                u_values=u_values,
                slack_by_tech=slack_by_tech,
            )
    except Exception as exc:  # noqa: BLE001 - failure isolation by design
        record["solver_status"] = "error"
        record["error_message"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
    finally:
        record["solve_time_s"] = float(time.perf_counter() - t0)

    return record


def _extract_soc_slack_rows(
    *,
    model,
    start_hour: int,
    soc_slack_penalty: float,
) -> list[dict[str, Any]]:
    """Return one row per storage tech with recovery-target SOC slack info.

    ``model.recovery_soc_slack[s]`` is the per-tech non-negative variable
    added by the resiliency PR #70 that softens the end-of-recovery SOC
    target. The slack is paid at ``soc_slack_penalty`` USD/MWh.
    """
    meta = getattr(model, "_sdom_outage_meta", {}) or {}
    recovery_end_hour = meta.get("recovery_end_hour", {}) or {}
    recovery_target = meta.get("recovery_target_MWh", {}) or {}
    storage = model.storage

    rows: list[dict[str, Any]] = []
    for s in list(storage.S):
        slack_mwh = float(pyo.value(model.recovery_soc_slack[s]))
        t_end = int(recovery_end_hour.get(s, -1))
        soc_end_mwh = (
            float(pyo.value(storage.SOC[s, t_end])) if t_end >= start_hour else float("nan")
        )
        target_mwh = float(recovery_target.get(s, 0.0))
        rows.append(
            {
                "start_hour": int(start_hour),
                "tech": str(s),
                "recovery_end_hour": int(t_end),
                "recovery_target_MWh": target_mwh,
                "soc_at_recovery_end_MWh": soc_end_mwh,
                "recovery_soc_slack_MWh": slack_mwh,
                "soc_slack_cost_USD": slack_mwh * float(soc_slack_penalty),
            }
        )
    return rows


def _extract_dispatch_rows(
    *,
    model,
    hours: list[int],
    start_hour: int,
    duration_hours: int,
    designed_system: DesignedSystem,
    u_values: list[float],
    slack_by_tech: dict[str, float],
) -> list[dict[str, Any]]:
    """Pull per-hour trajectories out of a solved outage model."""
    storage = model.storage
    pv = model.pv if hasattr(model, "pv") else getattr(model, "solar", None)
    wind = model.wind
    imports = model.imports
    exports = model.exports

    storage_techs = list(storage.S)
    solar_plants = list(pv.K) if pv is not None else []
    wind_plants = list(wind.K)

    # The outage builder stores per-(asset,t) delta multipliers on
    # ``model._sdom_outage_meta`` and per-(plant,t) capacity factors on
    # the VRE blocks. Use both to compute the *available* (potential) VRE
    # power = delta * cap * cf.
    meta = getattr(model, "_sdom_outage_meta", {}) or {}
    delta_solar = meta.get("delta_solar", {})
    delta_wind = meta.get("delta_wind", {})

    rows: list[dict[str, Any]] = []
    for idx, t in enumerate(hours):
        hour_in_window = t - start_hour + 1
        phase = "outage" if hour_in_window <= duration_hours else "recovery"

        row: dict[str, Any] = {
            "start_hour": start_hour,
            "hour": int(t),
            "hour_in_window": int(hour_in_window),
            "phase": phase,
            "Load": float(pyo.value(model.load_param[t])),
            "Nuclear": float(pyo.value(model.nuclear_eff_param[t])),
            "Hydro": float(pyo.value(model.hydro_eff_param[t])),
            "OtherRenewables": float(pyo.value(model.other_ren_eff_param[t])),
        }

        curt_solar = 0.0
        for k in solar_plants:
            cap = float(pv.cap[k])
            cf_val = float(pyo.value(pv.cf[k, t]))
            delta = float(delta_solar.get((k, t), 1.0))
            av = delta * cap * cf_val
            disp = float(pyo.value(pv.Psolar[k, t]))
            row[f"Psolar_av_{k}"] = av
            row[f"Psolar_disp_{k}"] = disp
            curt_solar += max(av - disp, 0.0)

        curt_wind = 0.0
        for k in wind_plants:
            cap = float(wind.cap[k])
            cf_val = float(pyo.value(wind.cf[k, t]))
            delta = float(delta_wind.get((k, t), 1.0))
            av = delta * cap * cf_val
            disp = float(pyo.value(wind.Pwind[k, t]))
            row[f"Pwind_av_{k}"] = av
            row[f"Pwind_disp_{k}"] = disp
            curt_wind += max(av - disp, 0.0)

        for s in storage_techs:
            row[f"Pcha_{s}"] = float(pyo.value(storage.Pcha[s, t]))
            row[f"Pdis_{s}"] = float(pyo.value(storage.Pdis[s, t]))
            row[f"SOC_{s}"] = float(pyo.value(storage.SOC[s, t]))
            row[f"RecoverySocSlack_{s}"] = float(slack_by_tech.get(s, 0.0))

        row["Pimp"] = float(pyo.value(imports.Pimp[t]))
        row["Pexp"] = float(pyo.value(exports.Pexp[t]))
        row["Unserved"] = float(u_values[idx])
        row["Curtailment_solar"] = curt_solar
        row["Curtailment_wind"] = curt_wind
        row["Curtailment_total"] = curt_solar + curt_wind

        rows.append(row)

    return rows


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def _resolve_n_workers(n_workers: int | None, n_payloads: int) -> int:
    if n_workers is None:
        cpu = os.cpu_count() or 1
        resolved = max(1, cpu - 1)
    else:
        resolved = int(n_workers)
        if resolved < 1:
            raise ValueError("n_workers must be >= 1.")
    return max(1, min(resolved, max(1, n_payloads)))


def run_outage_evaluation_with_dispatch(
    baseline_results,
    *,
    outage_spec,
    designed_system,
    slack_penalty=10_000.0,
    curtailment_penalty=0.0,
    soc_slack_penalty=1_000.0,
    min_soc_per_tech=None,
    n_hours=8760,
    hours=None,
    n_workers=None,
    solver="highs",
    solver_options=None,
    dispatch_csv_path=None,
    soc_slack_csv_path=None,
):
    """Run the per-anchor outage LP sweep and persist loss-event trajectories.

    Parameters
    ----------
    baseline_results : BaselineDispatchResults
        Baseline annual dispatch (capacities fixed). Provides initial SOC
        and the design carried in ``metadata['designed_system']``.
    outage_spec : OutageSpec
        Outage / de-rating specification, broadcast to every anchor hour.
    designed_system : DesignedSystem
        Source of truth for capacities and time series.
    slack_penalty, curtailment_penalty, soc_slack_penalty : float, optional
        Forwarded to :func:`sdom.resiliency.build_outage_dispatch`.
    min_soc_per_tech : dict, optional
        Operational SOC floor per storage tech (fraction of ``Cap_E``).
    n_hours : int, optional
        Length of the baseline horizon (used for end-of-year clipping).
    hours : iterable of int, optional
        Anchor hours to evaluate. ``None`` -> ``1..n_hours``.
    n_workers : int, optional
        Worker pool size. ``None`` -> ``max(1, os.cpu_count() - 1)``.
    solver : str, optional
        Pyomo solver name.
    solver_options : dict, optional
        Options forwarded to ``solver.solve(...)``.
    dispatch_csv_path : str or os.PathLike, optional
        If given, the loss-event dispatch DataFrame is written to this path.
    soc_slack_csv_path : str or os.PathLike, optional
        If given, a sidecar DataFrame with one row per
        ``(start_hour, tech)`` is written. Carries
        ``recovery_end_hour``, ``recovery_target_MWh``,
        ``soc_at_recovery_end_MWh``, ``recovery_soc_slack_MWh`` and
        ``soc_slack_cost_USD``. Captured for every anchor (not gated on
        loss events) so the slack burn is auditable even when EUE=0.

    Returns
    -------
    tuple of (ResiliencyResults, pandas.DataFrame)
        ``(results, dispatch_df)`` where ``dispatch_df`` carries
        per-(start_hour, hour) trajectories for loss-event anchors only.
    """
    if not isinstance(baseline_results, BaselineDispatchResults):
        raise TypeError("baseline_results must be a BaselineDispatchResults instance.")
    if not isinstance(outage_spec, OutageSpec):
        raise TypeError("outage_spec must be an OutageSpec instance.")
    if not isinstance(designed_system, DesignedSystem):
        raise TypeError("designed_system must be a DesignedSystem instance.")

    n_hours = int(n_hours)
    if n_hours <= 0:
        raise ValueError("n_hours must be a positive integer.")

    if hours is None:
        hour_list = list(range(1, n_hours + 1))
    else:
        hour_list = sorted({int(h) for h in hours})
    for h in hour_list:
        if not (1 <= h <= n_hours):
            raise ValueError(f"hours contains {h}, outside [1, {n_hours}].")

    n_workers_used = _resolve_n_workers(n_workers, len(hour_list))
    logger.info(
        "Running outage+dispatch evaluation: %d anchor hour(s), n_workers=%d, solver=%r.",
        len(hour_list),
        n_workers_used,
        solver,
    )

    payloads = [
        {
            "baseline_results": baseline_results,
            "outage_spec": outage_spec,
            "designed_system": designed_system,
            "start_hour": h,
            "slack_penalty": float(slack_penalty),
            "curtailment_penalty": float(curtailment_penalty),
            "soc_slack_penalty": float(soc_slack_penalty),
            "min_soc_per_tech": min_soc_per_tech,
            "n_hours": n_hours,
            "solver": solver,
            "solver_options": dict(solver_options) if solver_options else {},
        }
        for h in hour_list
    ]

    if n_workers_used == 1 or len(payloads) <= 1:
        records = [_solve_one_hour_with_dispatch(p) for p in payloads]
    else:
        with ProcessPoolExecutor(max_workers=n_workers_used) as pool:
            records = list(pool.map(_solve_one_hour_with_dispatch, payloads))

    records.sort(key=lambda r: int(r["start_hour"]))

    dispatch_rows: list[dict[str, Any]] = []
    slack_rows: list[dict[str, Any]] = []
    loss_anchors = 0
    for rec in records:
        rows = rec.pop("dispatch_rows", []) or []
        if rows:
            loss_anchors += 1
        dispatch_rows.extend(rows)
        slack_rows.extend(rec.pop("soc_slack_rows", []) or [])

    if records:
        df_metrics = pd.DataFrame(records).set_index("start_hour")
        df_metrics.index.name = "hour"
        for col in _PER_HOUR_COLUMNS:
            if col not in df_metrics.columns:
                df_metrics[col] = pd.NA
        df_metrics = df_metrics[_PER_HOUR_COLUMNS]
    else:
        df_metrics = pd.DataFrame(columns=_PER_HOUR_COLUMNS)
        df_metrics.index.name = "hour"

    n_errors = (
        int((df_metrics["solver_status"] == "error").sum())
        if "solver_status" in df_metrics.columns and not df_metrics.empty
        else 0
    )
    logger.info(
        "Outage+dispatch evaluation complete: %d hour(s) processed, %d worker error(s).",
        len(hour_list),
        n_errors,
    )

    dispatch_df = pd.DataFrame(dispatch_rows)
    slack_df = pd.DataFrame(slack_rows)

    if dispatch_csv_path is not None:
        out_path = Path(dispatch_csv_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        dispatch_df.to_csv(out_path, index=False)
        logger.info(
            "Outage dispatch trajectories (loss events only: %d / %d anchors) saved to %s.",
            loss_anchors,
            len(hour_list),
            out_path,
        )

    if soc_slack_csv_path is not None:
        slack_path = Path(soc_slack_csv_path)
        slack_path.parent.mkdir(parents=True, exist_ok=True)
        slack_df.to_csv(slack_path, index=False)
        if not slack_df.empty:
            nz = int((slack_df["recovery_soc_slack_MWh"] > _USE_EPS).sum())
            total_cost = float(slack_df["soc_slack_cost_USD"].sum())
            logger.info(
                "Recovery SOC slack sidecar saved to %s (%d rows, %d non-zero, "
                "total slack cost = %.2f USD).",
                slack_path,
                len(slack_df),
                nz,
                total_cost,
            )
        else:
            logger.info("Recovery SOC slack sidecar saved to %s (empty).", slack_path)

    metadata = {
        "n_workers_used": int(n_workers_used),
        "outage_spec": outage_spec,
        "n_hours": n_hours,
        "solver": solver,
        "n_hours_evaluated": len(hour_list),
        "n_loss_event_anchors": loss_anchors,
    }
    results = ResiliencyResults(per_hour=df_metrics, metadata=metadata)
    return results, dispatch_df
