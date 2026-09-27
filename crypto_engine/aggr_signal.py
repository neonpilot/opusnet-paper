"""mom_top2_1x signal: a verbatim port of w_mom(k=2) and `regime` from /workspace/aggressive/backtest.py, so the
live paper account trades exactly the backtested rule. Weights for day t read closes up to day t-1 only."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config as C


def mom_weights(close: pd.DataFrame, k=C.AGGR_TOP_K, lookback=C.AGGR_LOOKBACK_DAYS, sma=C.AGGR_SMA_DAYS,
                min_age=C.AGGR_MIN_HISTORY_DAYS, refresh_days=C.AGGR_REFRESH_DAYS, anchor=C.AGGR_REFRESH_ANCHOR):
    """Row t = target weights held during day t (set at the open of day t). Identical maths to backtest.py."""
    Cl = close
    idx = Cl.index
    btc = Cl["BTC"]
    regime = (btc > btc.rolling(sma, min_periods=sma).mean()).shift(1).fillna(False).astype(bool)
    age = Cl.notna().cumsum()
    eligible = (age >= min_age).shift(1).fillna(False).astype(bool)
    m90 = (Cl / Cl.shift(lookback) - 1).shift(1)
    m90 = m90.where(eligible & m90.notna())
    refresh = ((idx - pd.Timestamp(anchor)).days % refresh_days) == 0
    sel = pd.DataFrame(np.nan, index=idx, columns=Cl.columns)
    rk = m90.rank(axis=1, ascending=False, method="first")
    sel.loc[refresh] = ((rk.loc[refresh] <= k).astype(float) / k).values
    sel = sel.ffill().fillna(0.0)
    return sel.mul(regime.astype(float), axis=0) * C.AGGR_LEVERAGE, dict(regime=regime, m90=m90, refresh=refresh)


def is_refresh_day(t) -> bool:
    return (pd.Timestamp(t) - pd.Timestamp(C.AGGR_REFRESH_ANCHOR)).days % C.AGGR_REFRESH_DAYS == 0


def target_after_close(close: pd.DataFrame, D) -> tuple[dict, dict]:
    """Decision made after the daily candle D has closed: the weights for day t = D + 1, from closes <= D.
    Rows after D are dropped first, so no later data can leak in."""
    D = pd.Timestamp(D)
    c = close.loc[:D, C.SYMBOLS]
    if c.index[-1] != D:
        raise KeyError(f"daily close {D.date()} missing")
    t = D + pd.Timedelta(days=1)
    ext = pd.concat([c, pd.DataFrame(np.nan, index=[t], columns=c.columns)])
    W, aux = mom_weights(ext)
    w = W.loc[t]
    tgt = {s: float(v) for s, v in w.items() if v > 1e-12}
    # ---- rationale (all from closes <= D) ----
    btc = c["BTC"]
    sma = float(btc.rolling(C.AGGR_SMA_DAYS).mean().iloc[-1])
    btc_c = float(btc.iloc[-1])
    regime_on = bool(aux["regime"].loc[t])
    ref_days = [x for x in ext.index[aux["refresh"]] if x <= t]
    r = ref_days[-1] if ref_days else None
    sel_rank = aux["m90"].loc[r].dropna().sort_values(ascending=False) if r is not None else pd.Series(dtype=float)
    selection = list(sel_rank.index[:C.AGGR_TOP_K])
    now_rank = aux["m90"].loc[t].dropna().sort_values(ascending=False)
    nxt = t + pd.Timedelta(days=(C.AGGR_REFRESH_DAYS - (t - pd.Timestamp(C.AGGR_REFRESH_ANCHOR)).days % C.AGGR_REFRESH_DAYS)
                           % C.AGGR_REFRESH_DAYS or C.AGGR_REFRESH_DAYS)
    head = (f"BTC {btc_c:,.0f} is {'ABOVE' if regime_on else 'BELOW'} its {C.AGGR_SMA_DAYS}d SMA {sma:,.0f} "
            f"({btc_c / sma - 1:+.1%}) -> regime {'ON' if regime_on else 'OFF'}")
    rk_txt = ", ".join(f"{s} {v:+.0%}" for s, v in sel_rank.iloc[:4].items())
    if regime_on:
        summary = (f"{head}; hold {' + '.join(f'{s} {tgt.get(s, 0):.0%}' for s in selection)} "
                   f"(top-2 by 90d return, selection refreshed {r.date()} from closes to {(r - pd.Timedelta(days=1)).date()}: {rk_txt})")
    else:
        summary = f"{head}; hold 100% cash (USD). Top-2 selection of {r.date() if r is not None else '-'} ({rk_txt}) is parked."
    rat = {"action": "TARGET" if tgt else "HOLD CASH", "summary": summary, "strategy": C.AGGR_NAME,
           "position_day": str(t.date()), "decided_on_close_of": str(D.date()),
           "signal": {"btc_close": btc_c, "btc_sma": sma, "sma_days": C.AGGR_SMA_DAYS, "regime_on": regime_on,
                      "btc_vs_sma": btc_c / sma - 1,
                      "selection_refresh_day": str(r.date()) if r is not None else None,
                      "selection": selection, "is_refresh_day": bool(is_refresh_day(t)),
                      "selection_ranking_90d": {s: round(float(v), 4) for s, v in sel_rank.items()},
                      "ranking_90d_now": {s: round(float(v), 4) for s, v in now_rank.items()},
                      "next_refresh_position_day": str(nxt.date())}}
    return tgt, rat


def vectorised_returns(opens: pd.DataFrame, close: pd.DataFrame) -> pd.DataFrame:
    """Daily net returns of the 1x rule exactly as backtest.py daily_returns(w, 1): position set at the open of
    day t, day return = open(t+1)/open(t) - 1, one-way cost on all traded notional (daily rebalance)."""
    w, _ = mom_weights(close[C.SYMBOLS])
    O = opens[C.SYMBOLS]
    NO = O.shift(-1).fillna(close[C.SYMBOLS])
    R = (NO / O - 1).fillna(0.0)
    cost = pd.Series({s: C.COST_BPS.get(s, C.DEFAULT_COST_BPS) / 1e4 for s in C.SYMBOLS})
    Rn = R.where(w > 0, 0.0)
    gross = (w * Rn).sum(1)
    drift = (w * (1 + Rn)).div(1 + gross, axis=0).shift(1).fillna(0.0)
    turnover = (w - drift).abs()
    c = (turnover * cost).sum(1)
    return pd.DataFrame({"ret": gross - c, "gross": gross, "cost": c, "exposure": w.sum(1)})
