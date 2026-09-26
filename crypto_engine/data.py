"""Keyless Binance public klines (data-api.binance.vision works from US IPs, incl. GitHub Actions)."""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import config as C

MS = {"1h": 3_600_000, "1d": 86_400_000}


def fetch_klines(sym: str, interval: str, start_ms: int, end_ms: int | None = None, retries=4) -> pd.DataFrame:
    rows, cur = [], start_ms
    end_ms = end_ms or int(time.time() * 1000)
    while cur < end_ms:
        url = f"{C.BASE_URL}?symbol={sym}{C.QUOTE}&interval={interval}&startTime={cur}&limit=1000"
        for k in range(retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
                batch = json.loads(urllib.request.urlopen(req, timeout=30).read())
                break
            except Exception:
                if k == retries - 1:
                    raise
                time.sleep(2 * (k + 1))
        if not batch:
            break
        rows += batch
        nxt = batch[-1][0] + MS[interval]
        if nxt <= cur:
            break
        cur = nxt
        if len(batch) < 1000:
            break
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "close_time"])
    df = pd.DataFrame(rows).iloc[:, :7]
    df.columns = ["open_time", "open", "high", "low", "close", "volume", "close_time"]
    df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})
    df.index = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.tz_localize(None)
    df = df[~df.index.duplicated(keep="last")]
    now_ms = int(time.time() * 1000)
    return df[df["close_time"] < now_ms][["open", "high", "low", "close", "volume", "close_time"]]  # completed bars only


@dataclass
class Bars:
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    meta: dict

    @property
    def index(self):
        return self.close.index

    def truncate(self, end) -> "Bars":
        end = pd.Timestamp(end)
        return Bars(*(getattr(self, f).loc[:end] for f in ["open", "high", "low", "close", "volume"]),
                    meta=dict(self.meta, truncated_at=str(end)))


def build_bars(frames: dict[str, pd.DataFrame], meta: dict) -> Bars:
    idx = sorted(set().union(*(f.index for f in frames.values() if len(f))))
    idx = pd.DatetimeIndex(idx)
    g = lambda col: pd.DataFrame({s: frames[s][col].reindex(idx) for s in frames}).astype(float)
    return Bars(g("open"), g("high"), g("low"), g("close"), g("volume"), meta)


def load(interval: str, start: str | None = None, hours_back: int | None = None, cache=True, refresh=True) -> Bars:
    C.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = C.CACHE_DIR / f"bars_{interval}.pkl"
    if not refresh and path.exists():
        return pd.read_pickle(path)
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - hours_back * 3_600_000 if hours_back else int(pd.Timestamp(start or C.DATA_START).timestamp() * 1000)
    frames = {s: fetch_klines(s, interval, start_ms) for s in C.SYMBOLS}
    meta = {"source": C.DATA_SOURCE, "interval": interval,
            "fetched_utc": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
            "last_bar_open_utc": str(max(f.index[-1] for f in frames.values() if len(f)))}
    bars = build_bars(frames, meta)
    if cache:
        pd.to_pickle(bars, path)
    return bars
