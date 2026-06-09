# sdom_resiliency_evaluations

Reproduction package for the **MEA storage-only resiliency evaluation** built
on top of the [SDOM](https://github.com/Omar0902/SDOM) library
(`sdom.resiliency` module, PR #70 — recovery SOC slack model).

For each candidate H2 minimum-SOC fraction (`0.5` → `1.0`), the driver
fixes the CEM-Phase1 capacity design, runs an annual baseline dispatch,
and then sweeps a per-hour outage evaluation where every non-storage
resource (imports, wind, solar, balancing units, hydro, nuclear, other
renewables) **plus the Li-Ion storage** is fully outaged for 48 h followed
by a 48 h recovery window. H2 storage rides through. Loss-event metrics
(LOLP, LOLE, EUE mean/p95/p99, max unserved MW) are aggregated, plotted,
and combined across SOC tags into a sweep summary.

---

## Repo layout

```
.
├── data/MEA/
│   ├── inputs_csv/Paper_MEA 1/          # CEM previous-stage inputs (load, CFs, etc.)
│   └── outputs_CEM/For_simulations_MEA/ # CEM Phase1 outputs (capacity pin)
├── results/
│   ├── resiliency_mea_<tag>SOC/         # one folder per SOC tag (created by the driver)
│   │   ├── designed_system/summary.json
│   │   ├── baseline/timeseries.csv
│   │   ├── outage_dispatch.csv          # dispatch for loss-event anchors
│   │   ├── recovery_soc_slack.csv       # SOC slack diagnostics
│   │   ├── per_hour_metrics.csv         # 8760 rows
│   │   ├── aggregate_metrics.csv
│   │   └── plots/*.png
│   └── sweep_summary/                   # cross-tag aggregation
│       ├── sweep_aggregate_metrics.csv
│       └── {LOLP,LOLE,EUE_*,max_unserved_MW_*}.png
├── run_resiliency_evaluation.py         # per-tag driver
├── _outage_dispatch_export.py           # parallel outage-LP helper
├── make_sweep_summary.py                # cross-tag aggregator
├── pyproject.toml                       # uv project; sdom is editable from local
└── uv.lock
```

---

## Setup

Requires `uv` (≥0.11), Python 3.11–3.13, and a working Xpress license.

```pwsh
git clone git@github.com:SebastianManriqueM/sdom_resiliency_evaluations.git
cd sdom_resiliency_evaluations
```

`[tool.uv.sources]` in `pyproject.toml` points `sdom` at a sibling SDOM
checkout (`../../pySDOM/SDOM`). On a fresh machine, clone SDOM next to
this repo first:

```pwsh
# Expected layout: <root>/pySDOM/SDOM and <root>/pysdom/sdom_resiliency_evaluations
git clone git@github.com:Omar0902/SDOM.git ../../pySDOM/SDOM
git -C ../../pySDOM/SDOM checkout sm/resiliency_testing
```

Then create the venv and install everything:

```pwsh
uv sync
```

This installs `sdom 0.2.1` (editable) plus `xpress`, `pyomo`, `pandas`,
`numpy`, `matplotlib`.

---

## Run a single SOC tag

```pwsh
$env:SDOM_SOC_TAG = "0.7SOC"   # one of 0.5SOC, 0.6SOC, 0.7SOC, 0.8SOC, 0.9SOC, 1.0SOC
uv run python run_resiliency_evaluation.py
```

Outputs land under `results/resiliency_mea_<tag>/`. The H2 SOC floor is
`<tag-fraction> × h2_ref`, where `h2_ref` is the H2 reference floor
derived from the Phase1 CEM summary:

$$ h2_{\text{ref}} = \text{Cap\_E}_{\text{Phase1}} + \frac{P_{\text{dis},\text{Phase1}}}{\sqrt{\eta_{H_2}}} $$

For the MEA design at year 2030 this equals `11186.87 MWh`.

## Run the full 6-tag sweep + summary

```pwsh
foreach ($tag in @("0.5SOC","0.6SOC","0.7SOC","0.8SOC","0.9SOC","1.0SOC")) {
    $env:SDOM_SOC_TAG = $tag
    $outDir = "results\resiliency_mea_$tag"
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null
    uv run python run_resiliency_evaluation.py *> "$outDir\run.log"
}
uv run python make_sweep_summary.py
```

Each tag takes ~5 min on a 22-core machine (1 min baseline +
~4 min parallel sweep across 21 workers, 8760 anchor hours).

---

## Configuration

All knobs live at the top of `run_resiliency_evaluation.py`:

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

> **Heads-up on the slack ratio.** With the defaults (`SLACK_PENALTY =
> 10×SOC_SLACK_PENALTY`), the LP always prefers to violate the recovery
> SOC target rather than shed load. As a result the per-hour metrics
> (`EUE`, `USE_hours`, `max_unserved_MW`) can collapse to zero while the
> objective is still positive — that residual cost is paid as
> `recovery_soc_slack[s] × SOC_SLACK_PENALTY`, and is exported per anchor
> in `recovery_soc_slack.csv`. Raise `SOC_SLACK_PENALTY` (or lower
> `SLACK_PENALTY`) to make the LP prefer shedding.

---

## Reference results

`results/sweep_summary/sweep_aggregate_metrics.csv` (one row per tag):

| tag    | LOLP   | LOLE (h/event) | EUE mean (MWh) | EUE p95 | EUE p99 |
| ------ | ------ | -------------- | -------------- | ------- | ------- |
| 0.5SOC | 0.1248 | 0.974          | 90.3           | 797.8   | 1447.7  |
| 0.6SOC | 0.0591 | 0.273          | 24.2           | 109.1   | 740.6   |
| 0.7SOC | 0.0135 | 0.040          | 3.3            | 0.0     | 62.2    |
| 0.8SOC | 0.0    | 0.0            | 0.0            | 0.0     | 0.0     |
| 0.9SOC | 0.0    | 0.0            | 0.0            | 0.0     | 0.0     |
| 1.0SOC | 0.0    | 0.0            | 0.0            | 0.0     | 0.0     |

(Monotone decreasing in the H2 minimum-SOC fraction, as expected. The
collapse to zero above `0.8SOC` reflects the slack-ratio note above —
beyond that floor, the LP can always satisfy load by drawing the H2
stored energy down below the recovery target rather than shedding.)

---

## Related

* Upstream library: [Omar0902/SDOM](https://github.com/Omar0902/SDOM),
  branch `sm/resiliency_testing` (commit `a050a52` at packaging time).
* SOC-slack model: PR
  [#70](https://github.com/Omar0902/SDOM/pull/70).
* Probability-weighted outage metrics: PR
  [#71](https://github.com/Omar0902/SDOM/pull/71).
