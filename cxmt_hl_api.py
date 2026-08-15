#!/usr/bin/env python3
"""
CXMT perp candles from the Hyperliquid public info API (one-shot pull).

No AWS, no credentials. Plain HTTPS against https://api.hyperliquid.xyz/info .
The candleSnapshot endpoint only serves roughly the most recent 5000 candles
per interval, so at 1m you get ~3.5 days of history, at 5m ~17 days, at 15m
~52 days, at 30m ~104 days, at 1h ~208 days. xyz:CXMT started trading
2026-07-15 01:30 UTC; to reach that far back use 15m or coarser.

HIP-3 builder-deployed perps are named {dex}:{coin} (e.g. "xyz:CXMT"). The dex
prefix isn't hardcoded here -- the script queries perpDexs/meta and finds the
coin whose name contains your --symbol. Override with --coin if you know it.

USAGE
    python cxmt_hl_api.py --interval 15m --start 2026-07-15
    python cxmt_hl_api.py --coin xyz:CXMT --interval 1m
    python cxmt_hl_api.py --interval 15m --stock my_reference_series.csv \
        --stock-time-col timestamp --stock-price-col close

Outputs go to ./out/ .
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd

API = "https://api.hyperliquid.xyz/info"
SYMBOL = "CXMT"
INTERVAL_VALID = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h",
                  "12h", "1d", "3d", "1w", "1M"}
UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000,
           "w": 604_800_000, "M": 30 * 86_400_000}
MAX_CANDLES = 5000             # endpoint's per-request / recency limit
INCEPTION = "2026-07-15"       # first xyz:CXMT candle: 2026-07-15 01:30 UTC


def post(payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(API, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"[http] {e.code} {e.reason}: {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"[http] request failed: {e.reason}")


def interval_ms(interval):
    return int(interval[:-1]) * UNIT_MS[interval[-1]]


def discover_coin(symbol):
    """Find the full {dex}:{coin} id whose name contains `symbol`."""
    dexs = post({"type": "perpDexs"}) or []
    seen, hits = [], []
    for d in dexs:
        dex = "" if d is None else (d.get("name", "") if isinstance(d, dict) else "")
        meta = post({"type": "meta", "dex": dex} if dex else {"type": "meta"})
        for a in (meta or {}).get("universe", []):
            nm = a.get("name", "")
            full = nm if ":" in nm else (f"{dex}:{nm}" if dex else nm)
            seen.append(full)
            if symbol.lower() in full.lower():
                hits.append(full)
        time.sleep(0.05)
    hits = sorted(set(hits))
    if not hits:
        print(f"[discover] No coin matched '{symbol}'. Sample of available coins:")
        for s in sorted(set(seen))[:40]:
            print("   ", s)
        sys.exit("Pass --coin with the exact id from the list above.")
    if len(hits) > 1:
        print(f"[discover] Multiple matches for '{symbol}': {hits}")
        print(f"[discover] Using '{hits[0]}'. Override with --coin if wrong.")
    else:
        print(f"[discover] coin = {hits[0]}")
    return hits[0]


def fetch_candles(coin, interval, start_ms, end_ms):
    """Page through [start, end] in <=5000-candle chunks; dedup on open time."""
    step = interval_ms(interval) * MAX_CANDLES
    out, lo = {}, start_ms
    while lo < end_ms:
        hi = min(lo + step, end_ms)
        batch = post({"type": "candleSnapshot",
                      "req": {"coin": coin, "interval": interval,
                              "startTime": lo, "endTime": hi}}) or []
        for c in batch:
            out[c["t"]] = c
        print(f"[fetch] {pd.to_datetime(lo, unit='ms', utc=True)} "
              f"-> {len(batch)} candles (total {len(out)})")
        lo = hi
        time.sleep(0.1)
    return [out[t] for t in sorted(out)]


def to_frame(candles):
    """Hyperliquid candle: t/T open/close ms, o/h/l/c price, v volume, n trades."""
    df = pd.DataFrame(candles)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    ren = {"o": "open", "h": "high", "l": "low", "c": "close",
           "v": "volume", "n": "trades"}
    for src, std in ren.items():
        if src in df:
            df[std] = pd.to_numeric(df[src], errors="coerce")
    keep = ["ts"] + [v for v in ren.values() if v in df]
    return df[keep].sort_values("ts").reset_index(drop=True)


# --- optional reference-series alignment -----------------------------------------
TIME_HINTS = ["time", "timestamp", "ts", "t", "open_time", "start", "bucket"]


def pick(cols, hints):
    low = {c.lower(): c for c in cols}
    for h in hints:
        if h in low:
            return low[h]
    for c in cols:
        if any(h in c.lower() for h in hints):
            return c
    return None


def load_stock(path, tcol, pcol):
    raw = pd.read_csv(path)
    if tcol is None:
        tcol = pick(list(raw.columns), TIME_HINTS) or raw.columns[0]
    if pcol is None:
        pcol = pick(list(raw.columns), ["close", "c", "price", "px", "last"]) \
            or raw.columns[-1]
    ts = (pd.to_datetime(raw[tcol], unit="ms", utc=True)
          if pd.api.types.is_numeric_dtype(raw[tcol])
          else pd.to_datetime(raw[tcol], utc=True))
    out = pd.DataFrame({"ts": ts,
                        "stock_close": pd.to_numeric(raw[pcol], errors="coerce")})
    print(f"[stock] using time='{tcol}', price='{pcol}', {len(out)} rows")
    return out.dropna().sort_values("ts").reset_index(drop=True)


def merge_and_chart(perp, stock, tol_sec, outdir):
    perp_close = perp[["ts", "close"]].rename(columns={"close": "perp_close"})
    perp_close = perp_close.copy()
    stock = stock.copy()
    perp_close["ts"] = perp_close["ts"].astype("datetime64[ns, UTC]")
    stock["ts"] = stock["ts"].astype("datetime64[ns, UTC]")
    merged = pd.merge_asof(
        perp_close.sort_values("ts"), stock.sort_values("ts"), on="ts",
        direction="nearest", tolerance=pd.Timedelta(seconds=tol_sec))
    both = merged.dropna()
    if not both.empty:
        both = both.assign(
            basis_abs=both.perp_close - both.stock_close,
            basis_pct=(both.perp_close / both.stock_close - 1) * 100)
        both.to_csv(outdir / "merged_basis.csv", index=False)
        print(f"[merge] {len(both)} overlapping points -> out/merged_basis.csv")
        print(f"[merge] mean perp premium vs reference: {both.basis_pct.mean():+.3f}%")
    merged.to_csv(outdir / "merged.csv", index=False)
    try:
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(13, 6))
        ax.plot(perp_close.ts, perp_close.perp_close, lw=0.8, label="Perp CXMT")
        ax.plot(stock.ts, stock.stock_close, lw=1.1, label="Reference series")
        ax.set_title("CXMT: pre-IPO perp vs reference series")
        ax.set_xlabel("UTC"); ax.set_ylabel("Price")
        ax.legend(); ax.grid(alpha=0.3); fig.tight_layout()
        fig.savefig(outdir / "comparison.png", dpi=130)
        print("[chart] -> out/comparison.png")
    except Exception as e:                            # noqa: BLE001
        print(f"[chart] skipped ({e})")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", default="15m", choices=sorted(INTERVAL_VALID))
    ap.add_argument("--symbol", default=SYMBOL, help="symbol to discover (default CXMT)")
    ap.add_argument("--coin", default=None, help="exact {dex}:{coin} id; skips discovery")
    ap.add_argument("--start", default=INCEPTION, help="UTC YYYY-MM-DD (market inception)")
    ap.add_argument("--end", default=None, help="UTC YYYY-MM-DD (default now)")
    ap.add_argument("--stock", default=None, help="path to a reference price CSV (optional)")
    ap.add_argument("--stock-time-col", default=None)
    ap.add_argument("--stock-price-col", default=None)
    args = ap.parse_args()

    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC") if args.end else pd.Timestamp.now(tz="UTC")
    start_ms, end_ms = int(start.value // 1e6), int(end.value // 1e6)
    outdir = Path("out"); outdir.mkdir(exist_ok=True)

    coin = args.coin or discover_coin(args.symbol)
    span_days = MAX_CANDLES * interval_ms(args.interval) / 86_400_000
    print(f"=== Hyperliquid candles: coin={coin} interval={args.interval} "
          f"{start.date()}..{end.date()} (endpoint serves ~{span_days:.1f}d back) ===")

    candles = fetch_candles(coin, args.interval, start_ms, end_ms)
    df = to_frame(candles)
    df = df[(df.ts >= start) & (df.ts <= end)].reset_index(drop=True)
    if df.empty:
        sys.exit("No candles returned. The window is probably older than the "
                 f"~{span_days:.1f}d recency limit for {args.interval}, or the coin "
                 "id is wrong.")
    perp_path = outdir / f"perp_{args.interval}.csv"
    df.to_csv(perp_path, index=False)
    print(f"[perp] {len(df)} rows -> {perp_path}")
    print(df.head(3).to_string(index=False))

    if args.stock:
        stock = load_stock(args.stock, args.stock_time_col, args.stock_price_col)
        merge_and_chart(df, stock, interval_ms(args.interval) / 1000, outdir)

    print("\nDone. Files in ./out/")


if __name__ == "__main__":
    main()
