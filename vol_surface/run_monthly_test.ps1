# Monthly-expiry test on the NSE bhavcopy store. Run from the bhav folder:
#   powershell -ExecutionPolicy Bypass -File run_monthly_test.ps1
# Enters every trading day on the nearest MONTHLY expiry 12-45 days out (results are split by days-to-expiry).
$out = "C:\bt_monthly"
New-Item -ItemType Directory -Force -Path $out | Out-Null
python nifty_backtest.py run --store C:\nifty_bhav_store --config backtest_config_bhav_monthly.json --out $out 2>&1 | Tee-Object "$out\run.log"
Compress-Archive -Force -Path $out -DestinationPath C:\monthly_results.zip
Write-Host "Done. Send C:\monthly_results.zip"
