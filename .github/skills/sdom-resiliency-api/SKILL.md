---
name: sdom-resiliency-api
description: "Quick reference for the public sdom.resiliency API (v0.2.2): how to load a CEM design, build/solve the baseline annual dispatch, run per-anchor outage LPs, and post-process metrics. Use whenever working with resiliency evaluation scripts or extending the per-anchor outage workflow."
argument-hint: "Describe the resiliency task and which API entry points are involved"
user-invocable: false
---

# SDOM Resiliency API Summary (v0.2.2)

The `sdom.resiliency` package exposes a small, focused API for evaluating storage-anchored resiliency on a fixed-capacity SDOM design. The data flow is:

```
CEM snapshot (capacities + previous-stage inputs)
   -> load_designed_system        (DesignedSystem)
   -> build_baseline_dispatch     (Pyomo annual LP)
   -> run_baseline_dispatch       (BaselineDispatchResults with SOC trajectory + cem_data)
   -> per-anchor outage sweep:
        build_outage_dispatch     (Pyomo LP for one start_hour)
        + solver
      OR
        run_resiliency_evaluation (parallel sweep of all 8760 anchors)
        OR
        evaluate_resiliency       (one-call wrapper: design -> baseline -> sweep)
   -> ResiliencyResults (per-hour metrics + aggregate)
```

## Public Names

All importable from `sdom.resiliency`:

| Name | Kind | Purpose |
|---|---|---|
| `load_designed_system` | function | Load fixed-capacity design from CEM snapshot + previous-stage inputs. |
| `load_cem_data` | function | Build the CEM-shaped dict that `build_*_dispatch` consumes. |
| `build_baseline_dispatch` | function | Build the annual baseline Pyomo LP (capacities pinned). |
| `run_baseline_dispatch` | function | Solve baseline model -> `BaselineDispatchResults`. |
| `build_outage_dispatch` | function | Build a per-anchor outage LP for one `start_hour`. |
| `run_resiliency_evaluation` | function | Parallel sweep of outage LPs over many anchors. |
| `evaluate_resiliency` | function | One-call wrapper: load design -> baseline -> outage sweep. |
| `plot_metric_distribution` | function | Histogram / ECDF helper for a metric in `ResiliencyResults`. |
| `DesignedSystem` | dataclass | Fixed-capacity design (storage, thermal, VRE, time series, `cem_data`). |
| `OutageSpec` | dataclass | Outage configuration (duration, recovery, outaged assets, SOC recovery target). |
| `BaselineDispatchResults` | dataclass | Baseline solver output (SOC trajectory, objective, metadata). |
| `ResiliencyResults` | dataclass | Per-anchor metrics + aggregate metrics. |
| `VALID_COMPONENTS` | tuple | `('imports', 'wind', 'solar', 'balancing_units', 'hydro', 'nuclear', 'other_renewables', 'storage')` |
| `MUST_RUN_COMPONENTS` | tuple | `('hydro', 'nuclear', 'other_renewables')` - cannot be outaged via `outaged_assets`. |
| `add_imports_with_demand_charges` | function | Lower-level builder used by the baseline LP. |

## Key Signatures

```python
load_designed_system(snapshot_dir, *, inputs_dir, year=2030, scenario_id=1,
                     formulation_overrides=None, attach_cem_data=True)

load_cem_data(inputs_dir, *, formulations_overrides=None)

build_baseline_dispatch(designed_system, *, n_hours=8760,
                        min_soc_per_tech=None, curtailment_penalty=0.0,
                        add_demand_charges=True,
                        model_name='SDOM_BaselineDispatch', profile=False)

run_baseline_dispatch(model, *, solver='highs', solver_options=None,
                      tee=False, profile=False)

build_outage_dispatch(baseline_results, *, start_hour,
                      outage_spec, designed_system=None,
                      slack_penalty=10000.0, curtailment_penalty=0.0,
                      soc_slack_penalty=1000.0,
                      min_soc_per_tech=None, n_hours=8760,
                      model_name='SDOM_OutageDispatch', profile=False)

run_resiliency_evaluation(baseline_results, *, outage_spec,
                          designed_system=None, hours=None,
                          slack_penalty=10000.0, curtailment_penalty=0.0,
                          soc_slack_penalty=1000.0,
                          min_soc_per_tech=None, n_hours=8760,
                          n_workers=None, solver='highs',
                          solver_options=None, profile_outages=False)

evaluate_resiliency(snapshot_dir, *, inputs_dir, outage_spec, year=2030,
                    scenario_id=1, n_hours=8760, hours=None,
                    min_soc_per_tech=None, slack_penalty=10000.0,
                    curtailment_penalty=0.0, soc_slack_penalty=1000.0,
                    formulation_overrides=None, n_workers=None,
                    solver='highs', solver_options=None,
                    profile_baseline=False, profile_outages=False)

OutageSpec(duration_hours: int,
           recovery_hours: int | dict[str, int],
           outaged_assets: dict[str, str | Iterable],
           derating_factors: dict[tuple[str, str], float] = {},
           min_soc_recovery: dict[str, float] | None = None,
           per_asset_durations: dict[tuple[str, str], int] = {})
```

## Conventions

- **`load_designed_system` must be called with `attach_cem_data=True`** (default) before `build_baseline_dispatch`; the baseline builder requires `DesignedSystem.cem_data`. The kwarg is `attach_cem_data`, not `load_cem_data`.
- **`outaged_assets`** keys must be in `VALID_COMPONENTS`. Members of `MUST_RUN_COMPONENTS` (`hydro`, `nuclear`, `other_renewables`) cannot be outaged via this dict.
- **`min_soc_per_tech`** is a fraction of each storage tech's `Cap_E`. The library converts MWh floors to fractions internally (see `OutageSpec.min_soc_recovery`).
- **`recovery_hours`** can be int (all techs) or `{tech: hours}` (per-tech overrides).
- **Per-anchor outage LP is independent**; `run_resiliency_evaluation` parallelises across anchors with `ProcessPoolExecutor`. Default `n_workers = max(1, cpu_count() - 1)`.
- **Anchor-hour SOC dynamics fix (v0.2.2)**: SOC at `t == start_hour` is now seeded via `block.SOC_init[s]` and the `_soc_dynamics` rule covers every hour including `start_hour`. Older drivers that fixed `SOC[s, start_hour]` directly are no longer needed.
- **FOM cost** (added in v0.2.2): the outage objective includes a prorated FOM term `Σ(Cap × FOM × MW_TO_KW) × (horizon_hours / 8760)`. This is a constant for a given design - it shifts the reported objective but does not change the LP optimum.
- **Per-anchor `objective_value` semantics**: the LP minimizes `(unserved penalty + curtailment penalty + soc_slack penalty + FOM cost)` over the outage+recovery window. It is **not** an OPEX figure for the planning horizon. Driver-side post-processing decides how to interpret/aggregate it.
- **`build_outage_dispatch` exposes `model._sdom_outage_meta`** with `delta_solar`, `delta_wind`, `recovery_end_hour`, `recovery_target_MWh` for driver-side trajectory extraction.

## Typical Driver Pattern

```python
from sdom.resiliency import (
    load_designed_system, build_baseline_dispatch, run_baseline_dispatch,
    run_resiliency_evaluation, OutageSpec, VALID_COMPONENTS,
)

ds = load_designed_system(snapshot_dir, inputs_dir=inputs_dir,
                          year=2030, scenario_id=1)

baseline_model = build_baseline_dispatch(ds, n_hours=8760,
                                          min_soc_per_tech=min_soc_per_tech)
baseline = run_baseline_dispatch(baseline_model, solver="xpress",
                                 solver_options={"mipgap": 1e-4})

outage_spec = OutageSpec(
    duration_hours=48, recovery_hours=48,
    outaged_assets={c: "all" for c in VALID_COMPONENTS if c != "storage"},
    min_soc_recovery=min_soc_recovery,
)

results = run_resiliency_evaluation(
    baseline, outage_spec=outage_spec, designed_system=ds,
    n_hours=8760, n_workers=None, solver="xpress",
    solver_options={"mipgap": 1e-4},
)

results.per_hour.to_csv("per_hour_metrics.csv")
results.aggregate_metrics  # dict[str, float]
```

## Where to Find More

- Math model: `docs/source/user_guide/resiliency_math.md` (in the SDOM repo).
- User guide: `docs/source/user_guide/resiliency.md`.
- Reference scripts: `run_resiliency_evaluation.py`, `_outage_dispatch_export.py` (this repo).
