#!/usr/bin/env python3
"""
Describe a folder of market-data files (CSV and Parquet) without loading them fully.

    python inspect_data.py /path/to/your/data            # writes data_report.json + data_report.txt

For every file it reports size, columns, data types, a few sample rows, an estimated row count,
the date/time range, and what the columns look like (option chain, spot/futures, VIX, bid/ask,
tick vs minute vs daily). Share data_report.txt (a few KB); none of your raw data is uploaded.
Needs: pandas, pyarrow (pip install pandas pyarrow).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

SAMPLE_ROWS = 5000
HINTS = {
    "option": ["strike", "option_type", "opt_type", "call_put", "cp", "right", "ce", "pe", "optiontype"],
    "expiry": ["expiry", "expiry_date", "exp", "maturity"],
    "bid_ask": ["bid", "ask", "best_bid", "best_ask", "bid_price", "ask_price", "bidprice", "askprice"],
    "price": ["ltp", "close", "last", "last_price", "price", "settle"],
    "oi": ["oi", "open_interest", "openinterest"],
    "volume": ["volume", "vol", "qty", "traded_qty", "contracts"],
    "iv": ["iv", "implied_volatility", "impliedvol"],
    "time": ["timestamp", "datetime", "date", "time", "trade_time", "ts"],
    "symbol": ["symbol", "tradingsymbol", "instrument", "underlying", "ticker", "name"],
}


def classify(cols):
    low = [c.lower() for c in cols]
    found = {k: [c for c, l in zip(cols, low) if any(h == l or h in l for h in hs)] for k, hs in HINTS.items()}
    return {k: v for k, v in found.items() if v}


def time_info(df, cols):
    out = {}
    for c in cols:
        try:
            t = pd.to_datetime(df[c], errors="coerce")
        except Exception:
            continue
        t = t.dropna()
        if len(t) < 2:
            continue
        d = t.sort_values().diff().dropna()
        d = d[d > pd.Timedelta(0)]
        step = d.median() if len(d) else None
        out[c] = dict(first=str(t.min()), last=str(t.max()),
                      typical_step=str(step) if step is not None else None)
    return out


def granularity(ti):
    for v in ti.values():
        s = v.get("typical_step")
        if not s:
            continue
        td = pd.Timedelta(s)
        if td < pd.Timedelta(seconds=30):
            return "tick / sub-minute"
        if td <= pd.Timedelta(minutes=1):
            return "1-minute"
        if td < pd.Timedelta(hours=1):
            return f"{int(td.total_seconds() // 60)}-minute"
        if td < pd.Timedelta(days=1):
            return "hourly"
        return "daily"
    return "unknown"


def inspect(path: Path):
    info = dict(file=str(path), size_mb=round(path.stat().st_size / 1e6, 1))
    try:
        if path.suffix.lower() == ".parquet":
            import pyarrow.parquet as pq
            pf = pq.ParquetFile(path)
            info["rows"] = pf.metadata.num_rows
            df = next(pf.iter_batches(batch_size=SAMPLE_ROWS)).to_pandas()
        else:
            df = pd.read_csv(path, nrows=SAMPLE_ROWS, low_memory=False)
            sample_bytes = len(df.to_csv(index=False).encode())
            info["rows_estimate"] = int(path.stat().st_size / max(sample_bytes / max(len(df), 1), 1))
    except Exception as e:                         # noqa: BLE001
        info["error"] = f"{type(e).__name__}: {e}"
        return info
    info["columns"] = {c: str(t) for c, t in df.dtypes.items()}
    info["looks_like"] = classify(list(df.columns))
    info["time"] = time_info(df, info["looks_like"].get("time", []))
    info["granularity_in_sample"] = granularity(info["time"])
    info["has_bid_ask"] = bool(info["looks_like"].get("bid_ask"))
    for k in ("symbol", "option", "expiry"):
        for c in info["looks_like"].get(k, [])[:2]:
            info.setdefault("distinct_values", {})[c] = [str(v) for v in df[c].dropna().unique()[:12]]
    info["head"] = df.head(3).astype(str).to_dict(orient="records")
    return info


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    files = sorted(p for p in root.rglob("*") if p.suffix.lower() in (".csv", ".parquet"))
    if not files:
        sys.exit(f"No .csv or .parquet files under {root}")
    by_layout, report = {}, []
    for p in files:
        r = inspect(p)
        key = tuple(r.get("columns", {}).keys())
        by_layout.setdefault(key, []).append(r)
    # one full description per distinct column layout, plus the file list
    for key, rs in by_layout.items():
        first = rs[0]
        report.append(dict(layout_files=len(rs), total_size_mb=round(sum(r["size_mb"] for r in rs), 1),
                           example=first, all_files=[r["file"] for r in rs][:200]))
    out = dict(root=str(root), files=len(files),
               total_size_gb=round(sum(p.stat().st_size for p in files) / 1e9, 2), layouts=report)
    Path("data_report.json").write_text(json.dumps(out, indent=2, default=str))
    lines = [f"{out['files']} files, {out['total_size_gb']} GB under {root}", ""]
    for i, L in enumerate(report, 1):
        e = L["example"]
        lines += [f"=== Layout {i}: {L['layout_files']} files, {L['total_size_mb']} MB ===",
                  f"example: {e['file']}",
                  f"rows: {e.get('rows', e.get('rows_estimate', '?'))}"
                  + (" (estimated)" if "rows_estimate" in e else ""),
                  f"granularity (sample): {e.get('granularity_in_sample')}",
                  f"bid/ask columns: {e.get('has_bid_ask')}",
                  f"looks like: {e.get('looks_like')}",
                  f"time range in sample: {e.get('time')}",
                  f"columns: {e.get('columns')}",
                  f"distinct values: {e.get('distinct_values')}",
                  f"first rows: {e.get('head')}",
                  e.get("error", ""), ""]
    Path("data_report.txt").write_text("\n".join(lines))
    print("\n".join(lines[:60]))
    print("Wrote data_report.txt and data_report.json — share data_report.txt.")


if __name__ == "__main__":
    main()
