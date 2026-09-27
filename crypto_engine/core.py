"""The opusnet research framework, adapted for 24/7 crypto (ported from /workspace/opusnet/opusnet:
backtest.py, metrics.py, stats.py, sizing.py, registry.py, walkforward.py). Differences: 365-day year,
uninvested cash earns 0 (USD/USDT), positions are sized ONCE at entry (1% risk to a 2xATR stop, 20% cap)
and held at that weight until exit, decisions every daily close (00:00 UTC)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from scipy import stats as st
from scipy.stats import norm

from . import config as C

# ---------------- Code role: vectorised backtest (one-bar shift, costs) ----------------

def cost_vector(cols):
    return pd.Series({c: C.COST_BPS.get(c, C.DEFAULT_COST_BPS) / 1e4 for c in cols})


def backtest(weights: pd.DataFrame, bars, execution="next_open") -> pd.DataFrame:
    W = weights.reindex(index=bars.index).fillna(0.0)
    cols = W.columns
    o, c = bars.open[cols], bars.close[cols]
    gap = (o / c.shift(1) - 1).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    intra = (c / o - 1).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    if execution == "next_open":            # decided at close t, filled at open t+1
        held, prev = W.shift(1).fillna(0.0), W.shift(2).fillna(0.0)
        ret = (prev * gap).sum(1) + (held * intra).sum(1)
    elif execution == "no_shift":           # deliberately LEAKY, diagnostics only
        held, prev = W, W.shift(1).fillna(0.0)
        ret = (held * (c / c.shift(1) - 1).fillna(0.0)).sum(1)
    else:
        raise ValueError(execution)
    trades = (held - prev).abs()
    cost = (trades * cost_vector(cols)).sum(1)
    ret = ret - cost
    return pd.DataFrame({"ret": ret, "cost": cost, "turnover": trades.sum(1), "gross": held.sum(1)})


# ---------------- metrics ----------------

def drawdown(ret):
    eq = (1 + ret).cumprod()
    return eq / eq.cummax() - 1


def summarize(bt: pd.DataFrame) -> dict:
    r = bt["ret"]
    n = len(r)
    if n < 2:
        return {"n_days": n}
    total = float((1 + r).prod() - 1)
    yrs = n / C.ANN
    cagr = float((1 + total) ** (1 / yrs) - 1) if total > -1 else -1.0
    sd = float(r.std(ddof=1))
    srd = float(r.mean() / sd) if sd > 0 else 0.0
    mdd = float(drawdown(r).min())
    return {"n_days": n, "start": str(r.index[0].date()), "end": str(r.index[-1].date()),
            "total_return": total, "cagr": cagr, "ann_vol": sd * np.sqrt(C.ANN), "sharpe": srd * np.sqrt(C.ANN),
            "sharpe_daily": srd, "skew": float(st.skew(r, bias=False)),
            "kurtosis": float(st.kurtosis(r, fisher=False, bias=False)), "max_drawdown": mdd,
            "calmar": cagr / abs(mdd) if mdd < 0 else None, "avg_gross_exposure": float(bt["gross"].mean()),
            "turnover_per_year": float(bt["turnover"].sum() / yrs), "cost_drag_per_year": float(bt["cost"].sum() / yrs),
            "pct_days_invested": float((bt["gross"] > 1e-9).mean())}


# ---------------- Statistician role: PSR / Deflated Sharpe (Bailey & Lopez de Prado) ----------------
EULER = 0.5772156649015329


def psr(sr, sr0, n, skew, kurt):
    den = np.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr ** 2, 1e-12))
    return float(norm.cdf((sr - sr0) * np.sqrt(n - 1) / den))


def expected_max_sr(n_trials, var_sr):
    if n_trials <= 1:
        return 0.0
    return float(np.sqrt(var_sr) * ((1 - EULER) * norm.ppf(1 - 1 / n_trials) + EULER * norm.ppf(1 - 1 / (n_trials * np.e))))


def deflated_sharpe(sr, n, skew, kurt, n_trials, var_sr):
    sr0 = expected_max_sr(n_trials, var_sr)
    return {"dsr": psr(sr, sr0, n, skew, kurt), "sr0_daily": sr0, "sr0_annual": sr0 * np.sqrt(C.ANN),
            "n_trials": int(n_trials), "var_sr_daily": float(var_sr)}


# ---------------- Risk role: size at entry, hold, cap, no leverage ----------------

def atr_pct(bars, window=C.ATR_WINDOW):
    h, l, c = bars.high, bars.low, bars.close
    prev = c.shift(1)
    tr = np.maximum(h - l, np.maximum((h - prev).abs(), (l - prev).abs()))
    return tr.rolling(window, min_periods=window).mean() / c


def risk_weight(bars, cols):
    return (C.RISK_PER_TRADE / (C.STOP_ATR_MULT * atr_pct(bars)[cols])).clip(upper=C.MAX_POSITION)


def size_at_entry(signal: pd.DataFrame, bars) -> pd.DataFrame:
    """Weight fixed on the entry bar (1% risk to a 2xATR stop, capped at 20%) and held until the signal ends."""
    sig = (signal.fillna(0.0) > 0).astype(float)
    rw = risk_weight(bars, sig.columns).reindex(sig.index).fillna(0.0)   # no ATR yet -> no position this episode
    entry = (sig > 0) & (sig.shift(1).fillna(0.0) == 0)
    w = rw.where(entry).ffill().fillna(0.0) * sig
    w = w.where(np.isfinite(w), 0.0)
    gross = w.sum(1)
    return w.mul((C.MAX_GROSS / gross).where(gross > C.MAX_GROSS, 1.0), axis=0)


_DEFAULT = object()


def health_check(equity: pd.Series, hours_per_day=24, max_dd=_DEFAULT, loss_24h=_DEFAULT) -> dict:
    """max_dd / loss_24h default to the config limits; pass None to disable a rule (aggressive account)."""
    max_dd = C.KILL_MAX_DRAWDOWN if max_dd is _DEFAULT else max_dd
    loss_24h = C.KILL_24H_LOSS if loss_24h is _DEFAULT else loss_24h
    eq = equity.dropna()
    reasons = []
    dd = float(eq.iloc[-1] / eq.cummax().iloc[-1] - 1) if len(eq) else 0.0
    l24 = float(eq.iloc[-1] / eq.iloc[-1 - hours_per_day] - 1) if len(eq) > hours_per_day else \
        (float(eq.iloc[-1] / eq.iloc[0] - 1) if len(eq) > 1 else 0.0)
    if max_dd is not None and dd <= -max_dd:
        reasons.append(f"drawdown {dd:.2%} breached the -{max_dd:.0%} limit")
    if loss_24h is not None and l24 <= -loss_24h:
        reasons.append(f"24h loss {l24:.2%} breached the -{loss_24h:.0%} limit")
    return {"ok": not reasons, "reasons": reasons, "drawdown": dd, "loss_24h": l24}


# ---------------- trial registry (crypto-only, append-only) ----------------

def variant_id(strategy, version, params):
    return hashlib.sha1(json.dumps({"s": strategy, "v": version, "p": params, "u": "crypto"}, sort_keys=True).encode()).hexdigest()[:12]


class Registry:
    def __init__(self, path=None):
        self.path = path or C.REGISTRY_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, strategy, version, params, m, run_id, note=""):
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "run_id": run_id,
               "variant_id": variant_id(strategy, version, params), "strategy": strategy, "version": version,
               "params": params, "note": note,
               "metrics": {k: m.get(k) for k in ["start", "end", "n_days", "sharpe", "sharpe_daily", "cagr",
                                                 "max_drawdown", "skew", "kurtosis"]}}
        with open(self.path, "a") as f:
            f.write(json.dumps(rec, default=float) + "\n")

    def load(self):
        return [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()] if self.path.exists() else []

    def unique(self):
        u = {}
        for r in self.load():
            u[r["variant_id"]] = r
        return u

    def n_trials(self):
        return len(self.unique())

    def daily_sharpes(self):
        return [r["metrics"]["sharpe_daily"] for r in self.unique().values() if r["metrics"].get("sharpe_daily") is not None]

    def n_runs(self):
        return len({r["run_id"] for r in self.load()})


# ---------------- walk-forward (2y train -> 6m untouched test, rolled every 6m) ----------------

def _sharpe(r):
    sd = r.std(ddof=1)
    return float(r.mean() / sd * np.sqrt(C.ANN)) if sd and sd > 0 else -np.inf


def fold_bounds(index):
    p0 = int(index.searchsorted(pd.Timestamp(C.EVAL_START)))
    out, k = [], 0
    while True:
        ts = p0 + C.WF_TRAIN_DAYS + k * C.WF_TEST_DAYS
        if ts >= len(index):
            break
        out.append((ts - C.WF_TRAIN_DAYS, ts, min(ts + C.WF_TEST_DAYS, len(index))))
        k += 1
    return out


def select(bts, idx):
    sc = {v: _sharpe(bt["ret"].reindex(idx)) for v, bt in bts.items()}
    b = max(sc, key=sc.get)
    return b, sc[b]


def walk_forward(bts, params, index):
    folds, pieces = [], []
    for tr0, ts, te in fold_bounds(index):
        tri, tei = index[tr0:ts], index[ts:te]
        vid, is_sr = select(bts, tri)
        test = bts[vid].reindex(tei)
        pieces.append(test)
        tsr = _sharpe(test["ret"])
        folds.append({"train_start": str(tri[0].date()), "train_end": str(tri[-1].date()),
                      "test_start": str(tei[0].date()), "test_end": str(tei[-1].date()), "variant_id": vid,
                      "params": params[vid], "train_sharpe": is_sr, "test_sharpe": tsr if np.isfinite(tsr) else None,
                      "test_return": float((1 + test["ret"]).prod() - 1), "n_days": len(tei)})
    oos = pd.concat(pieces)
    counted = [f for f in folds if f["n_days"] >= C.WF_MIN_FOLD_DAYS]
    pos = [f["test_return"] > 0 for f in counted]
    return {"folds": folds, "oos": oos, "oos_metrics": summarize(oos),
            "pos_fold_fraction": float(np.mean(pos)) if pos else 0.0, "n_folds_counted": len(counted),
            "mean_train_sharpe": float(np.mean([f["train_sharpe"] for f in folds]))}


def params_as_of(bts, params, index, date):
    pos = int(index.searchsorted(pd.Timestamp(date), side="right")) - 1
    for tr0, ts, te in fold_bounds(index):
        if ts <= pos < te:
            vid, _ = select(bts, index[tr0:ts])
            return vid, params[vid]
    # beyond the last fold boundary (live): the fold that would start at the next boundary isn't open yet,
    # so the most recent boundary's selection stays in force
    fb = fold_bounds(index)
    tr0, ts, te = fb[-1]
    vid, _ = select(bts, index[tr0:ts])
    return vid, params[vid]
