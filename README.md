# CXMT_data - ChangXin Memory Technologies pre-IPO perp dataset

Continuous collection of market data for the **CXMT** (ChangXin Memory
Technologies) pre-IPO perpetual on Hyperliquid. Companion to
[SPCX_data](https://github.com/marsierz-ui/SPCX_data) and
[CBRS_data](https://github.com/marsierz-ui/CBRS_data): poll a public API on a
schedule, append into ever-growing master CSVs, commit.

ChangXin Memory is a private Chinese DRAM manufacturer with no public listing,
so the perp is the only continuous price series that exists for it. That makes
the archive use-it-or-lose-it: the API forgets old candles (see below) and
there is no exchange tape to reconstruct them from.

## Which market

| market | venue | first candle | note |
| --- | --- | --- | --- |
| `xyz:CXMT` | trade[XYZ] (HIP-3 dex on Hyperliquid) | 2026-07-15 01:30 UTC @ $6.00 | Pre-IPO Perpetual (IPOP), isolated margin, max 10x |

Checked and empty: every other Hyperliquid perp dex (`vntl`/Ventuals, `flx`,
`hyna`, `km`, `mkts`, `para`, `cash`, `abcd`, core HL) - all 491 listed perps
were scanned and `xyz:CXMT` is the only CXMT market. Delisted assets stay in
the `meta` universe, so a settled CXMT market elsewhere on Hyperliquid would
still show up.

## History horizon (why several intervals)

`candleSnapshot` serves roughly the most recent **5000 candles per interval**.
Older data is gone permanently, so the finest interval that still reaches the
2026-07-15 inception shrinks over time:

| interval | reach | covers the 2026-07-15 inception? |
| --- | --- | --- |
| 1m | ~3.5 d | no (lost since ~2026-07-18) |
| 5m | ~17.4 d | no (lost since ~2026-08-01) |
| 15m | ~52 d | **yes, until ~2026-09-05** |
| 30m | ~104 d | yes, until ~2026-10-27 |
| 1h | ~208 d | yes, until ~2027-02-08 |
| 1d | ~5000 d | yes |

**15m is therefore the smallest granularity available for the full series
since trading began.** The archive under `out/` holds it complete
(3026 candles, 2026-07-15 01:30 .. now, zero gaps), captured 2026-08-15. After
~2026-09-05 the API can no longer reproduce that, so keep the collector
running.

The 1m and 5m series start where the API's window began when collection
started (2026-08-12 and 2026-07-29 respectively) and grow forward from there.

## Usage

```bash
python cxmt_collector.py                                   # one-shot, all intervals
python cxmt_collector.py --loop --poll-sec 60              # run forever
python cxmt_collector.py --coins xyz:CXMT --intervals 1m   # narrow it
```

Output: `out/xyz_CXMT_<interval>_master.csv` with columns
`ts,open,high,low,close,volume,trades` (ts = candle open, UTC).

Each run re-fetches the last 5 candles and dedups on open time, because the
most recent candle is still forming and its OHLCV keeps changing until it
closes. Writes are atomic (temp file + `os.replace`), so an interrupted run
cannot corrupt an archive.

`cxmt_hl_api.py` is the one-shot pull for ad-hoc windows, plus optional
alignment of the perp against a reference price CSV (`--stock`), writing
`out/merged_basis.csv` and `out/comparison.png`.

`.github/workflows/collect.yml` runs the collector every 15 minutes and commits
the CSVs. GitHub only fires scheduled workflows from the default branch, and
you can trigger a run by hand from the Actions tab.

## No stock side

Unlike SPCX_data and CBRS_data, there is no `CXMT_import.py`. ChangXin Memory
has not listed - there is no Nasdaq/Yahoo ticker and no LSEG RIC to pull, so
the perp is the whole dataset. If the company lists (its filings target the
Shanghai STAR Market), add a stock collector then; `CBRS_import.py` in
CBRS_data is the template.
