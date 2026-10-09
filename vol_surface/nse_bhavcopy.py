#!/usr/bin/env python3
"""
Download NSE F&O bhavcopies and turn NIFTY options into a backtest store. Run on your machine.

  1. download     python nse_bhavcopy.py download --start 2016-01-01 --end 2026-10-09 --out D:/bhav
                  Saves one zip per trading day (holidays/weekends are skipped automatically).
                  Re-running resumes: days already downloaded are not fetched again.

  2. build-store  python nse_bhavcopy.py build-store --raw D:/bhav --out D:/nifty_bhav_store
                  Writes date=YYYY-MM-DD/part.parquet folders that nifty_backtest.py reads.

  3. backtest     python nifty_backtest.py run --store D:/nifty_bhav_store --config backtest_config_bhav.json --out D:/bt_bhav

What is stored per NIFTY option contract per day (real exchange data, end of day):
  ltp         closing price (only contracts that traded that day; untraded rows are dropped
              unless --keep-untraded, so every price is a real trade)
  oi          open interest (units)
  volume      contracts traded
  underlying  NIFTY spot: UndrlygPric in the new format; for the old format it is implied from
              put-call parity at the most liquid near-the-money strike of the nearest expiry,
              or taken from --spot-csv (date,close) if you pass one
  lot         market lot (NewBrdLotQty in the new format; otherwise from backtest_config lot schedule)

File formats handled
  old (to 2024-07-05): content/historical/DERIVATIVES/YYYY/MON/foDDMONYYYYbhav.csv.zip
  new (from 2024-07-08, UDiFF): content/fo/BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip
"""
from __future__ import annotations

import argparse
import io
import random
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

UDIFF_START = date(2024, 7, 8)
HOSTS = ["https://nsearchives.nseindia.com", "https://archives.nseindia.com"]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0 Safari/537.36",
    "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.nseindia.com/",
}


# ----------------------------------------------------------------- download

def _paths(d: date):
    mon = d.strftime("%b").upper()
    old = f"/content/historical/DERIVATIVES/{d:%Y}/{mon}/fo{d:%d}{mon}{d:%Y}bhav.csv.zip"
    new = f"/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
    return [new, old] if d >= UDIFF_START else [old, new]


def download(start, end, out, pause=1.2):
    import requests
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get("https://www.nseindia.com", timeout=15)        # sets the cookies NSE expects
    except Exception as e:                                    # noqa: BLE001
        print(f"warning: could not open nseindia.com first ({e}); continuing")
    d, got, missing, failed = start, 0, 0, []
    while d <= end:
        if d.weekday() >= 5:
            d += timedelta(days=1)
            continue
        target = out / f"{d.isoformat()}.zip"
        if target.exists() and target.stat().st_size > 1000:
            d += timedelta(days=1)
            continue
        ok = False
        for path in _paths(d):
            for host in HOSTS:
                for attempt in range(3):
                    try:
                        r = s.get(host + path, timeout=30)
                    except Exception:                         # noqa: BLE001
                        time.sleep(2 * (attempt + 1))
                        continue
                    if r.status_code == 200 and r.content[:2] == b"PK":
                        target.write_bytes(r.content)
                        ok = True
                    elif r.status_code in (403, 429):
                        time.sleep(5 * (attempt + 1))
                        continue
                    break
                if ok:
                    break
            if ok:
                break
        if ok:
            got += 1
        else:
            missing += 1                                      # holiday or not published
            failed.append(d.isoformat())
        print(f"{d}: {'ok' if ok else 'not found (holiday?)'}  [{got} saved, {missing} missing]", end="\r")
        time.sleep(pause + random.random() * 0.5)
        d += timedelta(days=1)
    (out / "missing_days.txt").write_text("\n".join(failed))
    print(f"\nDone: {got} files saved to {out}; {missing} weekdays not found (listed in missing_days.txt).")
    print("A few dozen missing days per year are normal (exchange holidays). If almost every day is "
          "missing, NSE is blocking the requests: open nseindia.com in your browser once and retry.")


# --------------------------------------------------------------- parse

def _read_zip(p):
    with zipfile.ZipFile(p) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        return pd.read_csv(io.BytesIO(z.read(name)), low_memory=False)


def parse(p, symbol="NIFTY"):
    """Return NIFTY index options for one day in a common layout."""
    df = _read_zip(p)
    df.columns = [c.strip() for c in df.columns]
    if "TckrSymb" in df.columns:                              # UDiFF (new)
        df = df[(df["FinInstrmTp"].astype(str).str.strip() == "IDO") &
                (df["TckrSymb"].astype(str).str.strip() == symbol)]
        out = pd.DataFrame({
            "date": pd.to_datetime(df["TradDt"]).dt.date.astype(str),
            "expiry": pd.to_datetime(df["XpryDt"]).dt.date.astype(str),
            "strike": pd.to_numeric(df["StrkPric"], errors="coerce"),
            "type": df["OptnTp"].astype(str).str.strip().str.upper(),
            "ltp": pd.to_numeric(df["ClsPric"], errors="coerce"),
            "settle": pd.to_numeric(df.get("SttlmPric"), errors="coerce"),
            "oi": pd.to_numeric(df["OpnIntrst"], errors="coerce"),
            "volume": pd.to_numeric(df["TtlTradgVol"], errors="coerce"),
            "underlying": pd.to_numeric(df.get("UndrlygPric"), errors="coerce"),
            "lot": pd.to_numeric(df.get("NewBrdLotQty"), errors="coerce"),
        })
    else:                                                     # old format
        df = df[(df["INSTRUMENT"].astype(str).str.strip() == "OPTIDX") &
                (df["SYMBOL"].astype(str).str.strip() == symbol)]
        out = pd.DataFrame({
            "date": pd.to_datetime(df["TIMESTAMP"], format="%d-%b-%Y").dt.date.astype(str),
            "expiry": pd.to_datetime(df["EXPIRY_DT"], format="%d-%b-%Y").dt.date.astype(str),
            "strike": pd.to_numeric(df["STRIKE_PR"], errors="coerce"),
            "type": df["OPTION_TYP"].astype(str).str.strip().str.upper(),
            "ltp": pd.to_numeric(df["CLOSE"], errors="coerce"),
            "settle": pd.to_numeric(df["SETTLE_PR"], errors="coerce"),
            "oi": pd.to_numeric(df["OPEN_INT"], errors="coerce"),
            "volume": pd.to_numeric(df["CONTRACTS"], errors="coerce"),
            "underlying": np.nan,
            "lot": np.nan,
        })
    return out[out["type"].isin(["CE", "PE"])].dropna(subset=["strike", "ltp"])


def implied_spot(day):
    """Put-call parity at the most-traded near-ATM strike of the nearest expiry (≈ forward)."""
    for e in sorted(day["expiry"].unique()):
        x = day[day["expiry"] == e]
        ce = x[x["type"] == "CE"].set_index("strike")
        pe = x[x["type"] == "PE"].set_index("strike")
        both = ce.join(pe, lsuffix="_c", rsuffix="_p", how="inner")
        both = both[(both["volume_c"] > 0) & (both["volume_p"] > 0)]
        if both.empty:
            continue
        both = both.assign(diff=(both["ltp_c"] - both["ltp_p"]).abs())
        k = both.nsmallest(3, "diff")
        k = k.loc[(k["volume_c"] + k["volume_p"]).idxmax()]
        return float(k.name + k["ltp_c"] - k["ltp_p"])
    return None


def build_store(raw, out, symbol="NIFTY", keep_untraded=False, spot_csv=None):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    spot = {}
    if spot_csv:
        s = pd.read_csv(spot_csv)
        spot = dict(zip(pd.to_datetime(s.iloc[:, 0]).dt.date.astype(str), s["close"]))
    files = sorted(Path(raw).glob("*.zip"))
    n_days, n_rows, src = 0, 0, {"bhavcopy": 0, "spot_csv": 0, "parity": 0}
    for f in files:
        try:
            day = parse(f, symbol)
        except Exception as e:                                # noqa: BLE001
            print(f"\nskip {f.name}: {e}")
            continue
        if day.empty:
            continue
        d = day["date"].iloc[0]
        u = day["underlying"].dropna()
        if len(u) and u.iloc[0] > 0:
            S = float(u.iloc[0]); src["bhavcopy"] += 1
        elif d in spot:
            S = float(spot[d]); src["spot_csv"] += 1
        else:
            S = implied_spot(day); src["parity"] += 1
        if not S:
            continue
        if not keep_untraded:
            day = day[day["volume"] > 0]
        day = day.assign(underlying=S, timestamp=pd.Timestamp(d) + pd.Timedelta(hours=15, minutes=30))
        ddir = out / f"date={d}"
        ddir.mkdir(exist_ok=True)
        day.drop(columns=["date"]).to_parquet(ddir / "part.parquet", index=False)
        n_days += 1
        n_rows += len(day)
        print(f"{d}: {len(day)} contracts", end="\r")
    print(f"\nStore ready: {n_days} days, {n_rows:,} option rows in {out}. Spot source: {src}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--start", required=True)
    d.add_argument("--end", default=date.today().isoformat())
    d.add_argument("--out", required=True)
    d.add_argument("--pause", type=float, default=1.2, help="seconds between requests")
    b = sub.add_parser("build-store")
    b.add_argument("--raw", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--symbol", default="NIFTY")
    b.add_argument("--keep-untraded", action="store_true")
    b.add_argument("--spot-csv", help="optional NIFTY daily closes: date,close")
    a = ap.parse_args()
    if a.cmd == "download":
        download(date.fromisoformat(a.start), date.fromisoformat(a.end), a.out, a.pause)
    else:
        build_store(a.raw, a.out, a.symbol, a.keep_untraded, a.spot_csv)


if __name__ == "__main__":
    main()
