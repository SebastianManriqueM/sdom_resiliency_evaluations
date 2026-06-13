# sdom_resiliency_evaluations

Reproduction package for the **MEA** (grid-connected) and **PG_E**
(off-grid) storage-only resiliency evaluations built on top of the
[SDOM](https://github.com/Omar0902/SDOM) library (`sdom.resiliency`
module — recovery SOC slack model + per-anchor outage LPs).

For each candidate H2 minimum-SOC fraction (`0.5` → `1.0`), the driver
fixes the CEM-Phase1 capacity design, runs an annual baseline dispatch,
and then sweeps a per-hour outage evaluation where every non-storage
resource (imports, wind, solar, balancing units, hydro, nuclear, other
renewables) **plus the Li-Ion storage** is fully outaged for 48 h followed
by a 48 h recovery window. H2 storage rides through. Loss-event metrics
(LOLP, LOLE, EUE mean/p95/p99, max unserved MW, expected OPEX) are
aggregated, plotted, and combined across SOC tags into a per-case sweep
summary.

The PG_E case is the **off-grid** variant: import/export caps are zeroed
in both the baseline dispatch and the outage LPs, demand charges are
disabled, and the outage window uses a constant **3 MW critical load**.

---

## Repo layout

```
.
├── data/
│   ├── MEA/
│   │   ├── inputs_csv/Paper_MEA 1/           # CEM previous-stage inputs
│   │   └── outputs_CEM/For_simulations_MEA/  # CEM Phase1 outputs (capacity pin)
│   └── PG_E/
│       ├── inputs_csv/Paper/                 # CEM previous-stage inputs
│       └── outputs_CEM/For_simulations_PG_E/ # CEM Phase1 outputs (capacity pin)
│
├── results/
│   ├── MEA/
│   │   └── resiliency_mea_<tag>/             # one folder per SOC tag (created by the driver)
│   │       ├── designed_system/summary.json
│   │       ├── baseline/timeseries.csv
│   │       ├── outage_dispatch.csv           # dispatch for loss-event anchors
│   │       ├── recovery_soc_slack.csv        # SOC slack diagnostics
│   │       ├── per_hour_metrics.csv          # 8760 rows
│   │       ├── aggregate_metrics.csv
│   │       └── plots/*.png
│   ├── PG_E/
│   │   └── resiliency_pge_<tag>/             # same shape as the MEA per-tag folder
│   └── sweep_summary/
│       ├── mea/                              # cross-tag aggregation for MEA
│       │   ├── sweep_aggregate_metrics.csv
│       │   ├── sweep_objective_costs.csv
│       │   ├── sweep_soc_slack_metrics.csv
│       │   └── *.png
│       └── pge/                              # cross-tag aggregation for PG_E
│
├── run_resiliency_evaluation.py              # MEA driver (per-tag)
├── run_resiliency_evaluation_pge.py          # PG_E off-grid driver (per-tag)
├── _outage_dispatch_export.py                # parallel outage-LP helper (shared)
├── _pge_smoke_test.py                        # 3-anchor smoke harness for PG_E
├── make_sweep_summary.py                     # cross-tag aggregator (case-aware)
├── rerun_all.ps1                             # MEA: loop 6 tags + sweep summary
├── rerun_pge_all.ps1                         # PG_E: loop 6 tags + sweep summary
├── pyproject.toml                            # uv project; pins sdom[xpress]==0.2.3
└── uv.lock
```

> **Per-case isolation.** MEA and PG_E outputs live in separate
> subfolders under `results/`; the case-aware sweep summary writes to
> `results/sweep_summary/<case>/`. Running one case never overwrites the
> other.

---

## Setup

### Prerequisites

| Tool          | Version             | Notes                                              |
| ------------- | ------------------- | -------------------------------------------------- |
| Python        | 3.11 – 3.13         | Managed by `uv` (no need to install separately).   |
| [`uv`](https://docs.astral.sh/uv/) | ≥ 0.11 | Project + virtualenv manager.                       |
| Xpress        | 9.x with a valid license file on `PATH` | Bundled via `sdom[xpress]==0.2.3` — solver itself needs the runtime license. |
| Git           | any                 | For cloning the repo.                              |
| PowerShell    | 7+                  | Required only for the `rerun_*.ps1` launchers.     |

### One-time setup

```pwsh
git clone git@github.com:SebastianManriqueM/sdom_resiliency_evaluations.git
cd sdom_resiliency_evaluations
uv sync
```

`uv sync` reads `pyproject.toml` + `uv.lock` and creates `.venv/` with
`sdom 0.2.3` (from PyPI), `xpress`, `pyomo`, `pandas`, `numpy`,
`matplotlib`. There is no editable SDOM dependency — the version is
pinned in the lockfile.

> The `sdom` package does not expose `__version__`; verify the install
> via `uv pip show sdom`.

### Data placement

The input data is tracked in the repo under `data/MEA/` and
`data/PG_E/`. Both drivers resolve their paths relative to the repo
root:

```python
# MEA (run_resiliency_evaluation.py)
SNAPSHOT_DIR = REPO_ROOT / "data" / "MEA"  / "outputs_CEM" / "For_simulations_MEA"
INPUTS_DIR   = REPO_ROOT / "data" / "MEA"  / "inputs_csv"  / "Paper_MEA 1"

# PG_E (run_resiliency_evaluation_pge.py)
SNAPSHOT_DIR = REPO_ROOT / "data" / "PG_E" / "outputs_CEM" / "For_simulations_PG_E"
INPUTS_DIR   = REPO_ROOT / "data" / "PG_E" / "inputs_csv"  / "Paper"
```

If your CEM outputs / inputs live elsewhere, either symlink them into
those paths or edit the four constants above. **Do not modify files
under `data/` in place** — both drivers mirror the inputs to a tempdir
before the SDOM loader sees them.

---

## Run a single SOC tag

Pick the H2 SOC floor fraction via the `SDOM_SOC_TAG` environment
variable (one of `0.5SOC`, `0.6SOC`, `0.7SOC`, `0.8SOC`, `0.9SOC`,
`1.0SOC`):

```pwsh
# MEA (grid-connected)
$env:SDOM_SOC_TAG = "0.7SOC"
uv run python run_resiliency_evaluation.py

# PG_E (off-grid, constant 3 MW critical load)
$env:SDOM_SOC_TAG = "0.7SOC"
uv run python run_resiliency_evaluation_pge.py
```

Outputs land under `results/MEA/resiliency_mea_<tag>/` or
`results/PG_E/resiliency_pge_<tag>/`.

The H2 SOC floor is `<tag-fraction> × h2_ref`, where `h2_ref` is the H2
reference floor derived from the Phase1 CEM summary:

$$ h2_{\text{ref}} = \text{Cap\_E}_{\text{Phase1}} + \frac{P_{\text{dis,Phase1}}}{\sqrt{\eta_{H_2}}} $$

For the MEA design at year 2030 this equals `11186.87 MWh`; for the PG_E
design `113.84 MWh`.

### Smoke-test the PG_E driver (3 anchors, ~30 s)

```pwsh
uv run python _pge_smoke_test.py
```

Useful when validating off-grid plumbing without paying for the full
8760-hour sweep. Output is cleaned up automatically.

---

## Run the full 6-tag sweep + summary

Use the per-case launchers. Each script loops the 6 SOC tags
sequentially (each tag parallelizes 8760 LPs internally across 21
workers — no oversubscription), then runs the cross-tag aggregator.

```pwsh
# MEA
pwsh -NoProfile -ExecutionPolicy Bypass -File .\rerun_all.ps1

# PG_E
pwsh -NoProfile -ExecutionPolicy Bypass -File .\rerun_pge_all.ps1
```

Per-tag logs land in `logs/rerun_<tag>.log` (or `logs/rerun_pge_<tag>.log`);
master logs in `logs/_rerun_master.log` and `logs/_rerun_pge_master.log`.

Expected wall time on a 22-core Windows machine:

| Case  | Per-tag         | 6-tag sweep + summary |
| ----- | --------------- | --------------------- |
| MEA   | ~3.5 – 5 min    | ~24 min               |
| PG_E  | ~4.5 – 5.5 min  | ~30 min               |

### Running the cross-tag aggregator on its own

`make_sweep_summary.py` scans `results/<CASE_DIR>/resiliency_<case>_<tag>/`
for every case it knows about (`CASE_DIRS = {"mea": "MEA", "pge": "PG_E"}`)
and writes per-case CSVs + PNGs to `results/sweep_summary/<case>/`. Run
it whenever per-tag results have changed:

```pwsh
uv run python make_sweep_summary.py
```

---

## Configuration

All knobs live at the top of each driver. Common to both:

| Variable             | Default                | Meaning                                  |
| -------------------- | ---------------------- | ---------------------------------------- |
| `YEAR`               | `2030`                 | Snapshot year                            |
| `SCENARIO_ID`        | `1`                    | Scenario id selected from the CEM design |
| `SOC_TAG`            | env `SDOM_SOC_TAG`     | H2 SOC fraction tag (e.g. `0.7SOC`)      |
| `OUTAGE_HOURS`       | `48`                   | Outage window length                     |
| `RECOVERY_HOURS`     | `48`                   | Recovery window length                   |
| `SOLVER`             | `"xpress"`             | Pyomo solver name                        |
| `SOLVER_OPTIONS`     | `{"mipgap": 0.0001}`   | Solver options                           |
| `SLACK_PENALTY`      | `10_000` USD/MWh       | Unserved-load penalty                    |
| `SOC_SLACK_PENALTY`  | `1_000` USD/MWh        | Recovery-target SOC slack penalty        |
| `OUTPUT_DIR`         | `results/<CASE>/resiliency_<case>_<tag>/` | Per-tag output folder         |

PG_E-only:

| Variable             | Default                | Meaning                                          |
| -------------------- | ---------------------- | ------------------------------------------------ |
| `CRITICAL_LOAD_MW`   | `3.0`                  | Constant critical load enforced during outage    |

> **Heads-up on the slack ratio.** With the defaults (`SLACK_PENALTY =
> 10×SOC_SLACK_PENALTY`), the LP always prefers to violate the recovery
> SOC target rather than shed load. As a result the per-hour metrics
> (`EUE`, `USE_hours`, `max_unserved_MW`) can collapse to zero while the
> objective is still positive — that residual cost is paid as
> `recovery_soc_slack[s] × SOC_SLACK_PENALTY`, and is exported per
> anchor in `recovery_soc_slack.csv`. Raise `SOC_SLACK_PENALTY` (or
> lower `SLACK_PENALTY`) to make the LP prefer shedding.

---

## Reference results (sdom 0.2.3)

`results/sweep_summary/mea/sweep_aggregate_metrics.csv`:

| tag    | LOLP   | LOLE (h/event) | EUE mean (MWh) | EUE p95 | EUE p99  |
| ------ | ------ | -------------- | -------------- | ------- | -------- |
| 0.5SOC | 0.1357 | 1.117          | 104.30         | 905.26  | 1561.01  |
| 0.6SOC | 0.0726 | 0.351          | 31.38          | 206.23  | 847.94   |
| 0.7SOC | 0.0220 | 0.066          | 5.42           | 0.0     | 160.95   |
| 0.8SOC | 0.0    | 0.0            | 0.0            | 0.0     | 0.0      |
| 0.9SOC | 0.0    | 0.0            | 0.0            | 0.0     | 0.0      |
| 1.0SOC | 0.0    | 0.0            | 0.0            | 0.0     | 0.0      |

`results/sweep_summary/pge/sweep_aggregate_metrics.csv`:

| tag    | LOLP   | LOLE (h/event) | EUE mean (MWh) | EUE p95 | EUE p99 | exp. OPEX (USD) |
| ------ | ------ | -------------- | -------------- | ------- | ------- | --------------- |
| 0.5SOC | 0.2824 | 5.84           | 15.4           | 91.0    | 111.7   | 170,117         |
| 0.6SOC | 0.3334 | 6.19           | 16.3           | 86.4    | 106.4   | 180,287         |
| 0.7SOC | 0.1705 | 3.36           | 8.4            | 68.0    | 98.8    | 101,312         |
| 0.8SOC | 0.1595 | 2.99           | 7.4            | 62.5    | 94.6    | 92,829          |
| 0.9SOC | 0.1588 | 2.78           | 6.8            | 57.7    | 88.8    | 88,180          |
| 1.0SOC | 0.1451 | 2.47           | 6.0            | 51.4    | 82.8    | 81,454          |

The MEA series is monotone decreasing in the H2 SOC floor (collapsing to
zero above `0.8SOC` because of the slack-ratio note above). The PG_E
series is **not** monotone between `0.5SOC` and `0.6SOC` because the
constant 3 MW critical-load override is identical across tags while the
baseline pre-outage SOC trajectories differ.

---

## Troubleshooting

- **`WARNING Failed to create solver with name 'appsi_xpress'`** — benign.
  Pyomo's `SolverFactory` tries the APPSI shim first, then falls back to
  the legacy `xpress` interface. The next log line will confirm
  `Solving ... with solver='xpress'`.
- **`BrokenProcessPool` / `OSError: handle is closed`** — seen
  occasionally on Windows after ~30 s of a sweep. Not deterministic;
  rerun the affected tag standalone (`$env:SDOM_SOC_TAG = '<tag>';
  uv run python run_resiliency_evaluation.py`). If it recurs across
  multiple tags, reduce `n_workers` in
  [`_outage_dispatch_export.py`](_outage_dispatch_export.py).
- **VS Code terminal eats `cd` / `Set-Location`** in chained commands
  when the cwd differs from the target. Workaround: invoke a `.ps1`
  script via `pwsh -NoProfile -ExecutionPolicy Bypass -File <abs path>`,
  or use `uv run --directory <abs path>`.

---

## Related

* Upstream library: [Omar0902/SDOM](https://github.com/Omar0902/SDOM)
  — pinned to PyPI release `sdom[xpress]==0.2.3`.
* SOC-slack model: PR
  [#70](https://github.com/Omar0902/SDOM/pull/70).
* Probability-weighted outage metrics: PR
  [#71](https://github.com/Omar0902/SDOM/pull/71).
