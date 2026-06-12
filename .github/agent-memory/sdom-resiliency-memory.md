# SDOM Resiliency Agent Memory

This file is read at the start of every task and updated at the end with new learnings.
Keep entries terse (one-line bullets when possible). Update or remove entries that turn out to be wrong.

## Repository Layout

- `run_resiliency_evaluation.py` - main driver: builds baseline, runs per-anchor outage sweep, aggregates metrics + plots for one SOC tag.
- `_outage_dispatch_export.py` - driver-side wrapper around `sdom.resiliency.build_outage_dispatch` that also persists per-hour dispatch trajectories for loss-event anchors.
- `make_sweep_summary.py` - cross-tag aggregator over `results/resiliency_mea_<tag>/`.
- `data/MEA/` - CEM snapshot + previous-stage inputs (never modify in place).
- `results/resiliency_mea_<tag>/` - one folder per H2 SOC floor sweep tag.
- `results/sweep_summary/` - cross-tag aggregated CSVs + PNGs.

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

## Anti-Patterns

- Do **not** call `Var.fix(SOC[s, start_hour])` after building the outage model - v0.2.2 seeds it via `block.SOC_init` and the `_soc_dynamics` rule covers `start_hour`.
- Do **not** edit files under `data/MEA/` in place; `load_cem_data` mirrors them to a tempdir.

## Recent Decisions / Changes

- 2026-06-12 - bumped pinned dep to `sdom[xpress]==0.2.3` (latest PyPI, released same day) and `uv sync`ed `.venv`. Note: `sdom` package does not expose `__version__`; verify via `uv pip show sdom`. Existing `results/` were generated against 0.2.2 - rerun the sweep if comparing apples-to-apples.
- 2026-06-12 - switched repo dependency from editable `../SDOM` to pinned `sdom[xpress]==0.2.2` (PyPI).
- 2026-06-12 - added `expected_opex_USD` metric to per-run aggregate + sweep summary. Total wall = 31.14 min (0.5SOC: 7.15 min, others 3.8-5.1 min). All exit=0.
- 2026-06-12 - sweep summary regenerated. New `expected_opex_USD` ranges 1.53M (0.7SOC) to 2.20M (0.5SOC) USD/hr. LOLP drops to 0 at >=0.8SOC. Objective totals 55.7M-57.9M USD; FOM constant 22.81M across all tags as expected.
- 2026-06-12 - re-ran all 6 MEA cases against released sdom 0.2.2 (anchor-hour SOC fix + FOM cost in objective).

## Update Protocol (end of each task)

1. Summarize the change/learning in 1-3 bullets under "Recent Decisions / Changes" (newest first, dated).
2. If a new gotcha was discovered, add a one-liner under "Known Gotchas".
3. If an existing entry is now wrong, edit or delete it - do not stack contradictory notes.
4. Keep total file length under ~150 lines; prune the oldest "Recent Decisions" entries when needed.
