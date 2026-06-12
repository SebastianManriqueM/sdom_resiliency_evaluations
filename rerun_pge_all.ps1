#!/usr/bin/env pwsh
# Run the 6 PG_E off-grid SOC tags against the pinned sdom, then regenerate the
# cross-tag sweep summary. Sequential (each case parallelizes 8760 LPs
# internally; no oversubscription). Outputs land under results/PG_E/.
$ErrorActionPreference = 'Continue'
$RR = $PSScriptRoot
Set-Location $RR
New-Item -ItemType Directory -Path "$RR\logs" -Force | Out-Null
$tags = @('0.5SOC', '0.6SOC', '0.7SOC', '0.8SOC', '0.9SOC', '1.0SOC')
$master = "$RR\logs\_rerun_pge_master.log"
$start = Get-Date
"Run started: $start (cwd=$RR)" | Tee-Object -FilePath $master
foreach ($tag in $tags) {
    $tagStart = Get-Date
    "[$tagStart] === $tag start ===" | Tee-Object -FilePath $master -Append
    $env:SDOM_SOC_TAG = $tag
    $logFile = "$RR\logs\rerun_pge_$tag.log"
    uv run python "$RR\run_resiliency_evaluation_pge.py" *>&1 | Tee-Object -FilePath $logFile
    $tagEnd = Get-Date
    $dur = ($tagEnd - $tagStart).TotalMinutes
    "[$tagEnd] === $tag done in $([math]::Round($dur, 2)) min (exit=$LASTEXITCODE) ===" |
        Tee-Object -FilePath $master -Append
}
$end = Get-Date
$total = ($end - $start).TotalMinutes
"Total run: $([math]::Round($total, 2)) min ($start -> $end)" | Tee-Object -FilePath $master -Append

"[$(Get-Date)] === sweep summary start ===" | Tee-Object -FilePath $master -Append
uv run python "$RR\make_sweep_summary.py" *>&1 | Tee-Object -FilePath "$RR\logs\rerun_pge_sweep_summary.log"
"[$(Get-Date)] === sweep summary done (exit=$LASTEXITCODE) ===" | Tee-Object -FilePath $master -Append
