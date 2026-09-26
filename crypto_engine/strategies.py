"""Hypothesis role: crypto candidates, each with a mechanism and a small pre-declared grid.
Signals are long-only conviction flags using only data up to each daily close."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from . import config as C
from .core import size_at_entry

ALL = C.SYMBOLS


def mom(px, L):
    return px / px.shift(L) - 1


def sig_tsmom(b, lookback):
    return (mom(b.close[ALL], lookback) > 0).astype(float)


def sig_xsmom(b, lookback, top_k):
    m = mom(b.close[ALL], lookback)
    rank = m.where(m > 0).rank(axis=1, ascending=False, method="first")
    return (rank <= top_k).astype(float)


def sig_btc_regime(b, sma, basket):
    btc = b.close["BTC"]
    on = btc > btc.rolling(sma, min_periods=sma).mean()
    names = {"btc": ["BTC"], "btc_eth": ["BTC", "ETH"], "all": ALL}[basket]
    out = pd.DataFrame(0.0, index=b.index, columns=ALL)
    for s in names:
        out[s] = (on & b.close[s].notna()).astype(float)
    return out


def sig_meanrev(b, short_lb, entry_z):
    c = b.close[ALL]
    vol = c.pct_change().rolling(60, min_periods=60).std()
    z = (c / c.shift(short_lb) - 1) / (vol * np.sqrt(short_lb))
    up = c > c.rolling(200, min_periods=200).mean()
    state = pd.DataFrame(np.where((z < -entry_z) & up, 1.0, np.nan), index=c.index, columns=ALL)
    state = state.mask((z > 0) | ~up, 0.0).ffill().fillna(0.0)   # exit on recovery or trend loss
    return state


def sig_donchian(b, n):
    c = b.close[ALL]
    hi = b.high[ALL].rolling(n, min_periods=n).max().shift(1)
    lo = b.low[ALL].rolling(max(n // 2, 5), min_periods=max(n // 2, 5)).min().shift(1)
    state = pd.DataFrame(np.where(c > hi, 1.0, np.nan), index=c.index, columns=ALL)
    state = state.mask(c < lo, 0.0).ffill().fillna(0.0)
    return state


def sig_hold(b, names=("BTC",)):
    out = pd.DataFrame(0.0, index=b.index, columns=ALL)
    for s in names:
        out[s] = b.close[s].notna().astype(float)
    return out


@dataclass
class Strategy:
    name: str
    title: str
    version: str
    mechanism: str
    signal: Callable
    grid: dict
    universe: list = field(default_factory=lambda: list(ALL))

    def variants(self):
        ks = list(self.grid)
        for combo in itertools.product(*(self.grid[k] for k in ks)):
            yield dict(zip(ks, combo))

    def target_weights(self, bars, params):
        return size_at_entry(self.signal(bars, **params).reindex(bars.index).fillna(0.0), bars)


STRATEGIES = [
    Strategy("c_tsmom", "Time-series trend per coin (long/cash)", "1",
             "Crypto trends persist: slow information diffusion, reflexive flows and leverage cycles make "
             "past returns predict near-term returns (documented in Liu & Tsyvinski 2021). Hold a coin only "
             "while its own trailing return is positive.", sig_tsmom, {"lookback": [20, 60, 120, 200]}),
    Strategy("c_xsmom", "Cross-sectional momentum rotation (top-k, absolute filter)", "1",
             "Capital rotates into the strongest coins (narratives, flows); hold the top-k by trailing return "
             "but only if that return is positive.", sig_xsmom, {"lookback": [30, 90, 180], "top_k": [1, 2, 3]}),
    Strategy("c_btc_regime", "BTC trend regime filter", "1",
             "BTC is the market factor for crypto; when BTC is above its moving average, risk appetite and "
             "liquidity are expanding, so hold a basket; otherwise hold cash.", sig_btc_regime,
             {"sma": [50, 100, 200], "basket": ["btc", "btc_eth", "all"]}),
    Strategy("c_meanrev", "Short-term dip buying inside an uptrend", "1",
             "Forced liquidations overshoot; in an established uptrend (above 200d SMA) sharp multi-day drops "
             "tend to partially revert. Exit on recovery or trend loss.", sig_meanrev,
             {"short_lb": [3, 7], "entry_z": [1.0, 1.5, 2.0]}),
    Strategy("c_donchian", "Donchian channel breakout", "1",
             "New N-day highs attract momentum traders and short covering (participation breakout); exit on a "
             "break of the N/2-day low.", sig_donchian, {"n": [20, 55, 100]}),
]
BY_NAME = {s.name: s for s in STRATEGIES}
