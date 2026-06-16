# SDOM Resiliency Agent Memory

This file is read at the start of every task and updated at the end with new learnings.
Keep entries terse (one-line bullets when possible). Update or remove entries that turn out to be wrong.

## Repository Layout

- `run_resiliency_evaluation.py` - MEA driver: builds baseline, runs per-anchor outage sweep, aggregates metrics + plots for one SOC tag. Its helpers (`_build_storage_only_outage`, `_read_h2_*`, `_dump_designed_system_summary`, `_save_baseline_timeseries`, `_save_plots`, `_summarize_metrics`, `_h2_only_soc_map`, `_append_baseline_costs_to_summary`) are reusable: they all take year/window/tag/components as params (no MEA-specific module globals leak).
- `run_resiliency_evaluation_pge.py` - PG_E off-grid driver: reuses the MEA helpers, zeroes Import_Cap/Export_Cap via a tempdir mirror + mutates `designed_system.{import,export}_cap` to zeros, calls `build_baseline_dispatch(..., add_demand_charges=False)` and `run_outage_evaluation_with_dispatch(..., critical_load_MW=3.0)`.
- `_outage_dispatch_export.py` - driver-side wrapper around `sdom.resiliency.build_outage_dispatch` that also persists per-hour dispatch trajectories for loss-event anchors. Now plumbs `critical_load_MW` through to the outage builder.
- `_pge_smoke_test.py` - 3-hour smoke harness for the PG_E driver (baseline + outage hours `[1, 2000, 5000]`, serial). Use when validating off-grid plumbing without paying for the full 8760-hour sweep.
- `make_sweep_summary.py` - cross-tag aggregator. Case-aware via `CASE_DIRS = {"mea": "MEA", "pge": "PG_E"}`: scans `results/<CASE_DIR>/resiliency_<case>_<tag>/` and writes per-case summaries under `results/sweep_summary/<case>/`.
- `rerun_all.ps1`, `rerun_pge_all.ps1` - per-case sweep launchers (loop 6 SOC tags, log per-tag + master log, then invoke `make_sweep_summary.py`).
- `data/MEA/`, `data/PG_E/` - CEM snapshots + previous-stage inputs (never modify in place).
- `results/MEA/resiliency_mea_<tag>/`, `results/PG_E/resiliency_pge_<tag>/` - one folder per case x SOC floor sweep tag (case-segregated).
- `results/sweep_summary/<case>/` - cross-tag aggregated CSVs + PNGs per case.

## Pinned Stack

- `sdom[xpress]==0.2.3` (PyPI release, no editable install).
- Solver = `xpress`, `mipgap = 1e-4`.
- 6 SOC tags: `0.5SOC`, `0.6SOC`, `0.7SOC`, `0.8SOC`, `0.9SOC`, `1.0SOC` (H2 floor fraction).

## Key Conventions (v0.2.2)

- `load_designed_system(..., attach_cem_data=True)` is mandatory before `build_baseline_dispatch` (the baseline LP needs `cem_data`).
- `OutageSpec.min_soc_recovery` is keyed by tech and expressed as a fraction of `Cap_E`. The driver converts an MWh floor via `floor_mwh / Cap_E`.
- `objective_value` in the per-anchor outage CSV is the **LP optimum** for the 48 h outage + 48 h recovery window. It includes unserved/curtailment/soc-slack penalties **and** the prorated FOM constant added in v0.2.2.
- `expected_opex_USD = sum(objective_value) / 8760` (one row per anchor, all 8760 anchors).

## Known Gotchas

- Pyomo emits a benign `WARNING Failed to create solver with name 'appsi_xpress'` once per process: the SolverFactory tries the APPSI shim first, then falls back to the legacy `xpress` interface. Safe to ignore - the next log line confirms `Solving ... with solver='xpress'`.
- VS Code terminal wrapper strips leading `cd`/`Set-Location` from chained commands when cwd != target. Workaround: save a `.ps1` script and invoke via `pwsh -NoProfile -ExecutionPolicy Bypass -File <abs path>`, or use `uv run --directory <abs path>`.
- `sdom` package does not export `__version__`; query install metadata via `uv pip show sdom` (or `importlib.metadata.version("sdom")`).
- The 8760-anchor ProcessPool occasionally dies with `BrokenProcessPool` / `OSError: handle is closed` on Windows (seen once on 0.9SOC, ~30 s in). Not deterministic - rerunning that single tag worked on the next attempt. If it recurs across multiple tags, reduce `n_workers` in [_outage_dispatch_export.py](_outage_dispatch_export.py).

## Anti-Patterns

- Do **not** call `Var.fix(SOC[s, start_hour])` after building the outage model - v0.2.2 seeds it via `block.SOC_init` and the `_soc_dynamics` rule covers `start_hour`.
- Do **not** edit files under `data/MEA/` in place; `load_cem_data` mirrors them to a tempdir.

## Recent Decisions / Changes

- 2026-06-16 - added `EUE_total_MWh.png` and `EUE_cost_total_USD.png` per case under `results/sweep_summary/<case>/` (mirrors `SOC_slack_cost_total_USD.png`). Cost = `sum(EUE_per_anchor) * 10_000 USD/MWh` where 10k matches `SLACK_PENALTY` in both drivers; constant lives in `make_sweep_summary.UNSERVED_ENERGY_PENALTY_USD_PER_MWH`. New columns `n_anchor_hours`, `EUE_total_MWh`, `EUE_cost_total_USD` added to `sweep_aggregate_metrics.csv`. PGE 1.0SOC -> 4.32 M USD; MEA 1.0SOC -> 0 USD (zero unserved at >=0.8SOC).
- 2026-06-16 - shortened PG_E outage horizons from 48+48 h to **24 h outage + 24 h recovery** in [run_resiliency_evaluation_pge.py](run_resiliency_evaluation_pge.py) (user edit). MEA driver still uses 48+48. Reran full PG_E sweep via `rerun_pge_all.ps1` (37.8 min wall, 6 tags, 21 workers, all `exit=0`). Headline metrics now monotonic in SOC floor: LOLP 0.097 (0.5SOC) -> 0.016 (1.0SOC); EUE_mean 1.74 -> 0.05 MWh; expected_opex_USD 26.0k -> 10.7k. Loss-event count drops 446 (0.7SOC) -> 136 (1.0SOC) anchors. Recovery slack still nonzero at 1.0SOC (870 / 17520 rows, $41.8M total) because the H2 floor = 113.84 MWh is ~9.4% of `Cap_E` and the 24 h recovery window cannot always refill against PG&E's 16 MW PV / 16 MW wind under realistic load. Baseline objective unchanged (995,860 USD = pure FOM). `make_sweep_summary.py` regenerated under `results/sweep_summary/{mea,pge}/`.
- 2026-06-12 - ran full PG_E sweep via `rerun_pge_all.ps1` with 48+48 h horizons (29.8 min wall, 6 tags). LOLP ranged 0.145 (1.0SOC) -> 0.333 (0.6SOC) - non-monotonic at 48 h because storage cannot serve 48 h x 3 MW off-grid regardless of SOC tag. Superseded by the 24+24 h rerun on 2026-06-16.
- 2026-06-12 - reorganized `results/` so MEA and PG_E outputs are case-segregated: `results/MEA/resiliency_mea_<tag>/` and `results/PG_E/resiliency_pge_<tag>/`. Both drivers' `OUTPUT_DIR` constants updated. `make_sweep_summary.py` gained `CASE_DIRS = {"mea": "MEA", "pge": "PG_E"}` and helpers `_case_root(case)`/`_iter_case_dirs(case)` to walk the new layout.
- 2026-06-12 - added PG_E off-grid driver `run_resiliency_evaluation_pge.py`. Off-grid is enforced by (a) mirroring `data/PG_E/inputs_csv/Paper` to a tempdir with `Import_Cap_2030.csv`/`Export_Cap_2030.csv` rewritten to zeros (kills baseline grid flows that go through cem_data), (b) overwriting `designed_system.import_cap`/`export_cap` to zero series after load (kills outage-LP grid flows that read these directly), and (c) passing `add_demand_charges=False` to `build_baseline_dispatch`. Critical outage load is the new `critical_load_MW` kwarg (=3.0 MW for PG_E), now plumbed through `_outage_dispatch_export.run_outage_evaluation_with_dispatch` -> `build_outage_dispatch`. Smoke test (3 anchors) passes; full sweep is the user's call.
- 2026-06-12 - made MEA helpers parameter-driven so the PG_E driver can reuse them: `_read_h2_phase1_caps(snapshot_dir, year)`, `_read_h2_reference_soc(snapshot_dir, inputs_dir, year)`, `_build_storage_only_outage(..., outage_hours, recovery_hours)`, `_dump_designed_system_summary(..., soc_tag, outaged_components)`. Defaults preserve MEA behaviour.
- 2026-06-12 - generalized `make_sweep_summary.py` to scan all `resiliency_<case>_<tag>` directories. New `CASE_PATTERN = r"^resiliency_(?P<case>[a-z0-9]+)_(?P<tag>[0-9.]+SOC)$"`. Per-case CSVs/plots land in `results/sweep_summary/<case>/`. Existing MEA outputs that lived directly under `results/sweep_summary/` are now stale - rerun to regenerate under `results/sweep_summary/mea/`.
- 2026-06-12 - reran full MEA sweep against sdom 0.2.3 via [rerun_all.ps1](rerun_all.ps1) (now also invokes `make_sweep_summary.py`). Total wall = 24.0 min for 6 tags + 0.7 min retry. 0.9SOC hit a one-off `BrokenProcessPool` on the first pass; standalone rerun succeeded. Aggregate metrics vs 0.2.2 are essentially unchanged.
- 2026-06-12 - bumped pinned dep to `sdom[xpress]==0.2.3` (latest PyPI, released same day) and `uv sync`ed `.venv`. `sdom` package does not expose `__version__`; verify via `uv pip show sdom`.
- 2026-06-12 - switched repo dependency from editable `../SDOM` to pinned `sdom[xpress]==0.2.2` (PyPI).

## Update Protocol (end of each task)

1. Summarize the change/learning in 1-3 bullets under "Recent Decisions / Changes" (newest first, dated).
2. If a new gotcha was discovered, add a one-liner under "Known Gotchas".
3. If an existing entry is now wrong, edit or delete it - do not stack contradictory notes.
4. Keep total file length under ~150 lines; prune the oldest "Recent Decisions" entries when needed.
