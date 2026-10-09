# Early-exit test on the NSE bhavcopy store. Run from the bhav folder:
#   powershell -ExecutionPolicy Bypass -File run_exit_test.ps1
# Same entry days for all three runs (nearest expiry >= 24 calendar days out); only the time exit differs.
$store = "C:\nifty_bhav_store"
foreach ($x in 3, 7, 10) {
    $out = "C:\bt_exit$x"
    New-Item -ItemType Directory -Force -Path $out | Out-Null
    Write-Host "=== exit $x trading days before expiry -> $out ==="
    python nifty_backtest.py run --store $store --config "backtest_config_bhav_exit$x.json" --out $out 2>&1 | Tee-Object "$out\run.log"
}
Compress-Archive -Force -Path C:\bt_exit3, C:\bt_exit7, C:\bt_exit10 -DestinationPath C:\exit_test_results.zip
Write-Host "Done. Send C:\exit_test_results.zip"
