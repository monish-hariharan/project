# 500-point condor test on the NSE bhavcopy store. Run from the bhav folder:
#   powershell -ExecutionPolicy Bypass -File run_500_test.ps1
# Run wing200: 16-delta condor (baseline) and shorts 500 pts from spot, both with 200-pt wings.
# Run wing500: the same two short-strike rules with 500-pt wings. Same entry days in both runs.
$store = "C:\nifty_bhav_store"
foreach ($x in "wing200", "wing500") {
    $out = "C:\bt_$x"
    New-Item -ItemType Directory -Force -Path $out | Out-Null
    Write-Host "=== $x -> $out ==="
    python nifty_backtest.py run --store $store --config "backtest_config_bhav_$x.json" --out $out 2>&1 | Tee-Object "$out\run.log"
}
Compress-Archive -Force -Path C:\bt_wing200, C:\bt_wing500 -DestinationPath C:\test_500_results.zip
Write-Host "Done. Send C:\test_500_results.zip"
