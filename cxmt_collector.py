#!/usr/bin/env python3
"""
Incremental CXMT perp candle collector for Hyperliquid.

Maintains one ever-growing master CSV per (coin, interval). Each run fetches
only the candles since the last stored one, dedups on candle open-time, and
re-fetches the last few candles (the most recent candle is still forming and
its o/h/l/c/v/n keep updating until it closes). Writes are atomic (temp file
+ os.replace).

Why incremental matters: candleSnapshot only serves roughly the most recent
5000 candles PER INTERVAL. That is ~3.5 days at 1m, ~17 days at 5m, ~52 days
at 15m, ~104 days at 30m, ~208 days at 1h. Anything older is unrecoverable,
so you must poll regularly and append into your own archive.

Why several intervals: xyz:CXMT (ChangXin Memory Technologies, a pre-IPO
perpetual) started trading 2026-07-15 01:30 UTC. As of 2026-08-15 the finest
interval that still reaches inception is 15m -- 1m only reaches back ~3.5d and
5m ~17d. The 15m horizon crosses inception around 2026-09-05, so the archive
has to capture the full 15m series before then. Collecting 1m + 5m + 15m + 30m
+ 1h + 1d keeps the finest live data AND locks the launch history in.

MODES
    python cxmt_collector.py                      # one-shot: fetch new candles, append, exit
    python cxmt_collector.py --loop               # run forever, poll every --poll-sec (default 60)
    python cxmt_collector.py --coins xyz:CXMT     # skip discovery (also auto-cached after first run)
    python cxmt_collector.py --intervals 1m,15m   # override the interval set

Designed to be safe to call repeatedly from Windows Task Scheduler or GitHub
Actions (idempotent). Output: out/<coin>_<interval>_master.csv
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd

API = "https://api.hyperliquid.xyz/info"
SYMBOL = "CXMT"
INTERVALS = ["1m", "5m", "15m", "30m", "1h", "1d"]
UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000,
           "w": 604_800_000, "M": 30 * 86_400_000}
MAX_CANDLES = 5000          # endpoint per-request / recency limit
OVERLAP = 5                 # re-fetch this many trailing candles each poll
COLUMNS = ["ts", "open", "high", "low", "close", "volume", "trades"]

OUTDIR = Path("out")
COIN_CACHE = OUTDIR / ".coin_cache.json"


def post(payload, retries=5):
    """POST with exponential backoff on 429 / transient network errors."""
    data = json.dumps(payload).encode()
    for attempt in range(retries):
        req = urllib.request.Request(
            API, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = 2 ** attempt
                print(f"[http] 429 rate limited, backing off {wait}s")
                time.sleep(wait)
                continue
            sys.exit(f"[http] {e.code} {e.reason}: "
                     f"{e.read().decode(errors='replace')}")
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                wait = 2 ** attempt
                print(f"[http] network error ({e.reason}), retry in {wait}s")
                time.sleep(wait)
                continue
            sys.exit(f"[http] request failed: {e.reason}")


def interval_ms(interval):
    return int(interval[:-1]) * UNIT_MS[interval[-1]]


def slug(coin):
    """xyz:CXMT -> xyz_CXMT (':' is not a legal filename char on Windows)."""
    return coin.replace(":", "_")


def discover_coins(symbol):
    """All {dex}:{coin} ids whose name contains `symbol`, across every perp dex."""
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
        print(f"[discover] No coin matched '{symbol}'. Sample of coins:")
        for s in sorted(set(seen))[:40]:
            print("   ", s)
        sys.exit("Pass --coins with the exact id(s).")
    print(f"[discover] {symbol} -> {hits}")
    return hits


def resolve_coins(symbol, coins_arg):
    """Use --coins, else cached coins, else discover and cache them."""
    if coins_arg:
        return [c.strip() for c in coins_arg.split(",") if c.strip()]
    if COIN_CACHE.exists():
        cached = json.loads(COIN_CACHE.read_text()).get(symbol)
        if cached:
            return cached
    coins = discover_coins(symbol)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    COIN_CACHE.write_text(json.dumps({symbol: coins}))
    print("[discover] cached")
    return coins


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
        lo = hi
        time.sleep(0.1)
    return out


def candles_to_frame(candles_by_t):
    """{t: hyperliquid candle} -> tidy DataFrame keyed by ts."""
    if not candles_by_t:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.DataFrame([candles_by_t[t] for t in sorted(candles_by_t)])
    df["ts"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    ren = {"o": "open", "h": "high", "l": "low", "c": "close",
           "v": "volume", "n": "trades"}
    for src, std in ren.items():
        df[std] = pd.to_numeric(df[src], errors="coerce")
    return df[COLUMNS]


def load_master(path):
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(path, parse_dates=["ts"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def atomic_write_csv(df, path):
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def poll_once(coin, interval, master_path, backfill_days=None):
    """Fetch new candles, merge into master, write back. Returns row count."""
    master = load_master(master_path)
    now_ms = int(pd.Timestamp.now(tz="UTC").value // 1e6)
    ims = interval_ms(interval)
    window_days = MAX_CANDLES * ims / 86_400_000

    if master.empty:
        # Fresh start: grab the whole window the endpoint will serve.
        span = min(backfill_days, window_days) if backfill_days else window_days
        start_ms = now_ms - int(span * 86_400_000)
        print(f"[{coin} {interval}] cold start, backfilling ~{span:.1f}d")
    else:
        last_ms = int(master["ts"].max().value // 1e6)
        start_ms = last_ms - OVERLAP * ims          # re-fetch trailing candles
        gap_days = (now_ms - last_ms) / 86_400_000
        if gap_days > window_days:
            print(f"[{coin} {interval}] WARNING gap of {gap_days:.1f}d exceeds the "
                  f"~{window_days:.1f}d recovery window; older candles are lost.")

    fetched = fetch_candles(coin, interval, start_ms, now_ms)
    new = candles_to_frame(fetched)
    if new.empty:
        print(f"[{coin} {interval}] no candles returned "
              f"(master has {len(master)} rows)")
        return len(master)

    stack = new if master.empty else pd.concat([master, new])
    combined = (stack
                .drop_duplicates(subset="ts", keep="last")   # keep updated last candle
                .sort_values("ts")
                .reset_index(drop=True))
    added = len(combined) - len(master)
    atomic_write_csv(combined, master_path)
    print(f"[{coin} {interval}] +{added} new, {len(combined)} total, "
          f"{combined['ts'].iloc[0]} .. {combined['ts'].iloc[-1]}")
    return len(combined)


def poll_all(coins, intervals, backfill_days):
    for coin in coins:
        for interval in intervals:
            path = OUTDIR / f"{slug(coin)}_{interval}_master.csv"
            poll_once(coin, interval, path, backfill_days)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--intervals", default=",".join(INTERVALS),
                    help="comma-separated, e.g. 1m,5m,15m,30m,1h,1d")
    ap.add_argument("--symbol", default=SYMBOL)
    ap.add_argument("--coins", default=None,
                    help="comma-separated exact {dex}:{coin} ids; skips discovery")
    ap.add_argument("--loop", action="store_true", help="run forever")
    ap.add_argument("--poll-sec", type=float, default=60.0)
    ap.add_argument("--backfill-days", type=float, default=None,
                    help="cold-start lookback; default is the full endpoint window")
    ap.add_argument("--outdir", default="out", help="output directory")
    args = ap.parse_args()

    global OUTDIR, COIN_CACHE
    OUTDIR = Path(args.outdir)
    COIN_CACHE = OUTDIR / ".coin_cache.json"
    OUTDIR.mkdir(parents=True, exist_ok=True)
    coins = resolve_coins(args.symbol, args.coins)
    intervals = [i.strip() for i in args.intervals.split(",") if i.strip()]
    print(f"=== collector coins={coins} intervals={intervals} -> {OUTDIR} ===")

    if not args.loop:
        poll_all(coins, intervals, args.backfill_days)
        return

    print(f"[loop] polling every {args.poll_sec:.0f}s; Ctrl-C to stop")
    while True:
        try:
            poll_all(coins, intervals, args.backfill_days)
        except SystemExit:
            raise
        except Exception as e:                       # noqa: BLE001
            print(f"[loop] poll error: {e}")
        time.sleep(args.poll_sec)


if __name__ == "__main__":
    main()
