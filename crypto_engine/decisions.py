"""Daily decision function: the approved strategy with its walk-forward parameters, plus a written rationale
and shadow signals of the other gate-passing strategies (information only, never traded)."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from . import config as C
from .core import atr_pct, fold_bounds, params_as_of, risk_weight
from .research import run_variants
from .strategies import BY_NAME


def load_research():
    return json.loads((C.RESULTS_DIR / "research.json").read_text())


class Decider:
    """Pre-computes every variant on the given daily bars. Decisions at date D only read row D of weights
    computed from data <= D (certified by the G1 truncation test), and the walk-forward parameter choice
    only reads the training window that ended before D's fold began."""

    def __init__(self, daily, research=None, shadows=True):
        self.b = daily
        self.R = research or load_research()
        self.approved = self.R.get("approved_strategy")
        names = [self.approved] if self.approved else []
        if shadows:
            names += [s["name"] for s in self.R["strategies"] if s["passed_all"] and s["name"] != self.approved]
        self.per = {}
        for n in names:
            bts, params, weights = run_variants(BY_NAME[n], daily)
            self.per[n] = dict(bts=bts, params=params, weights=weights)
        self.atr = atr_pct(daily)
        self.rw = risk_weight(daily, C.SYMBOLS)

    def _target(self, name, D):
        p = self.per[name]
        vid, params = params_as_of(p["bts"], p["params"], self.b.index, D)
        w = p["weights"][vid].loc[D]
        return {k: float(v) for k, v in w.items() if v > 1e-9}, params, vid

    def fold_of(self, D):
        pos = int(self.b.index.searchsorted(pd.Timestamp(D), side="right")) - 1
        fb = fold_bounds(self.b.index)
        for tr0, ts, te in fb:
            if ts <= pos < te:
                return str(self.b.index[tr0].date()), str(self.b.index[ts - 1].date()), str(self.b.index[ts].date())
        tr0, ts, te = fb[-1]
        return str(self.b.index[tr0].date()), str(self.b.index[ts - 1].date()), str(self.b.index[ts].date())

    def detail(self, name, params, D):
        c = self.b.close.loc[:D]
        if name == "c_btc_regime":
            n = params["sma"]
            sma = float(c["BTC"].rolling(n).mean().iloc[-1])
            btc = float(c["BTC"].iloc[-1])
            return {"btc_close": btc, "btc_sma": sma, "sma_days": n, "regime_on": btc > sma,
                    "btc_vs_sma": btc / sma - 1, "basket": params["basket"]}
        if name == "c_tsmom":
            L = params["lookback"]
            return {"momentum": {s: float(c[s].iloc[-1] / c[s].iloc[-1 - L] - 1) for s in C.SYMBOLS if np.isfinite(c[s].iloc[-1 - L])}}
        if name == "c_xsmom":
            L = params["lookback"]
            m = {s: float(c[s].iloc[-1] / c[s].iloc[-1 - L] - 1) for s in C.SYMBOLS if np.isfinite(c[s].iloc[-1 - L])}
            return {"momentum": dict(sorted(m.items(), key=lambda x: -x[1]))}
        return {}

    def __call__(self, D):
        D = pd.Timestamp(D)
        if D not in self.b.index:
            raise KeyError(f"daily bar {D.date()} missing")
        if not self.approved:
            return {}, {"action": "HOLD CASH", "summary": "No strategy passed the pre-registered approval rule; 100% cash (USD).",
                        "approval_rule": C.APPROVAL_RULE}
        tgt, params, vid = self._target(self.approved, D)
        det = self.detail(self.approved, params, D)
        tr0, tr1, fstart = self.fold_of(D)
        shadow = {}
        for n in self.per:
            if n != self.approved:
                t2, p2, _ = self._target(n, D)
                shadow[n] = {"targets": {k: round(v, 4) for k, v in t2.items()}, "params": p2}
        atrs = {s: round(float(self.atr[s].loc[D]), 4) for s in tgt}
        if self.approved == "c_btc_regime":
            head = (f"BTC {det['btc_close']:,.0f} is {'ABOVE' if det['regime_on'] else 'BELOW'} its {det['sma_days']}d SMA "
                    f"{det['btc_sma']:,.0f} ({det['btc_vs_sma']:+.1%}) -> regime {'ON' if det['regime_on'] else 'OFF'}")
        else:
            head = f"{self.approved} signal"
        summary = head + (f"; hold {', '.join(f'{k} {v:.1%}' for k, v in sorted(tgt.items(), key=lambda x: -x[1]))} "
                          f"(size fixed at entry: 1% equity risk to a 2xATR stop, cap 20%, gross <= 100%)"
                          if tgt else "; hold 100% cash (USD)")
        return tgt, {"action": "TARGET" if tgt else "HOLD CASH", "summary": summary, "strategy": self.approved,
                     "params": params, "variant_id": vid, "params_selected_on": f"walk-forward train {tr0}..{tr1} "
                     f"(fold starting {fstart}, best in-sample Sharpe)", "signal": det, "atr_pct": atrs,
                     "shadow_signals_not_traded": shadow}
