"""Hourly paper ledger for crypto. SIMULATED MONEY, REAL PRICES. No exchange connection, no keys.

Timing (identical in replay and live):
  * the ledger walks completed 1h Binance candles in order;
  * pending orders fill at the OPEN of the first hourly candle whose open time is >= the order's
    created_at, at that open worsened by the coin's one-way cost; sells before buys; never borrows, never shorts;
  * every hourly close is marked to market and the kill switch is checked (10% drawdown from peak,
    4% rolling-24h loss, stale data > 6h in live). A breach cancels pending orders, queues a full
    liquidation and blocks new entries until a human deletes crypto/state/HALTED;
  * when the 23:00 UTC hourly candle is processed the daily candle is complete, so the approved
    strategy decides on it. Orders are stamped created_at = run time (live) or daily close + 17 min
    (replay, mirroring the :17 cron), so they fill at the next hourly open after that (~01:00 UTC).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C
from .core import health_check

H = pd.Timedelta(hours=1)


def iso(t) -> str:
    return pd.Timestamp(t).strftime("%Y-%m-%dT%H:%M:%SZ")


def awst(t) -> str:
    return (pd.Timestamp(t).tz_localize("UTC") if pd.Timestamp(t).tzinfo is None else pd.Timestamp(t)) \
        .tz_convert("Australia/Perth").strftime("%Y-%m-%d %H:%M")


def cost(sym):
    return C.COST_BPS.get(sym, C.DEFAULT_COST_BPS) / 1e4


class CryptoLedger:
    def __init__(self, directory: Path | None = None, mode="live", label="", start_capital=C.START_CAPITAL,
                 opened_at=None, kill_max_dd=C.KILL_MAX_DRAWDOWN, kill_24h=C.KILL_24H_LOSS, rebalance="band",
                 min_trade_usd=1.0):
        """kill_max_dd / kill_24h: None disables that rule. rebalance: "band" (original account: trade only when
        the TARGET weight moves > RESIZE_BAND) or "exact" (aggressive account: every decision is a complete
        target portfolio; holdings are traded back to it, pending orders are replaced, trades < min_trade_usd
        skipped)."""
        self.dir = Path(directory) if directory else None
        self.mode, self.label = mode, label
        self.kill_max_dd, self.kill_24h = kill_max_dd, kill_24h
        self.rebalance, self.min_trade_usd = rebalance, min_trade_usd
        self.new_journal, self.new_equity = [], []
        if self.dir and (self.dir / "state.json").exists():
            self.state = json.loads((self.dir / "state.json").read_text())
            eqp = self.dir / "equity.csv"
            self.eq_hist = pd.read_csv(eqp, index_col=0, parse_dates=True)["equity"].iloc[-200:] if eqp.exists() \
                else pd.Series(dtype=float)
        else:
            self.state = {"label": label, "mode": mode, "start_capital": start_capital, "cash": start_capital,
                          "positions": {}, "targets": {}, "pending": [], "peak_equity": start_capital,
                          "halted": False, "halt_reasons": [], "last_hour": None, "last_daily_bar": None,
                          "last_px": {}, "opened_at": iso(opened_at) if opened_at is not None else None,
                          "n_fills": 0, "costs_paid": 0.0, "seq": 0, "last_equity": start_capital}
            self.eq_hist = pd.Series(dtype=float)
        self.journal = []          # full in-memory journal (replay)
        self.enforce_kill = True   # False only for a clearly-labelled research shadow replay

    # ---------------- journal / persistence ----------------
    def log(self, kind, ts, payload):
        self.state["seq"] += 1
        rec = {"seq": self.state["seq"], "ts_utc": iso(ts), "ts_awst": awst(ts), "mode": self.mode,
               "account": self.label, "kind": kind, **payload}
        self.journal.append(rec)
        self.new_journal.append(rec)
        return rec

    def save(self):
        if not self.dir:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "state.json").write_text(json.dumps(self.state, indent=1, default=float))
        if self.new_journal:
            with open(self.dir / "journal.jsonl", "a") as f:
                for r in self.new_journal:
                    f.write(json.dumps(r, default=float) + "\n")
        if self.new_equity:
            p = self.dir / "equity.csv"
            new = not p.exists()
            with open(p, "a") as f:
                if new:
                    f.write("hour_utc,equity,cash,invested\n")
                for t, e, c, i in self.new_equity:
                    f.write(f"{iso(t)},{e:.4f},{c:.4f},{i:.4f}\n")
        self.new_journal, self.new_equity = [], []

    # ---------------- mechanics ----------------
    def value(self, px: dict):
        inv = sum(q * float(px.get(s, np.nan)) for s, q in self.state["positions"].items())
        return self.state["cash"] + inv, inv

    def halted_file(self):
        return self.dir / "HALTED" if self.dir else None

    def _fill(self, t, opens: dict):
        st, still = self.state, []
        orders = sorted(st["pending"], key=lambda o: 0 if o["side"] == "SELL" else 1)
        for o in orders:
            if pd.Timestamp(o["created_at"].rstrip("Z")) > t:
                still.append(o)
                continue
            s = o["sym"]
            px = opens.get(s)
            if px is None or not np.isfinite(px) or px <= 0:
                if t - pd.Timestamp(o["created_at"].rstrip("Z")) > pd.Timedelta(hours=24):
                    self.log("ORDER_CANCELLED", t, {"order": o, "reason": "no valid open price for 24h"})
                else:
                    still.append(o)
                continue
            c = cost(s)
            held = st["positions"].get(s, 0.0)
            if o["side"] == "SELL":
                qty = held if o.get("close_all") else min(o["qty"], held)
                if qty <= 0:
                    continue
                fill = px * (1 - c)
                st["cash"] += qty * fill
                new = held - qty
            else:
                fill = px * (1 + c)
                qty = o["qty"]
                if qty * fill > st["cash"]:                          # never borrow
                    qty = max(st["cash"], 0) / fill * (1 - 1e-9)
                if qty * px < 1.0:                                   # < $1 notional: skip dust
                    self.log("ORDER_CANCELLED", t, {"order": o, "reason": "insufficient cash"})
                    continue
                st["cash"] -= qty * fill
                new = held + qty
            if new * px < 0.01:
                st["positions"].pop(s, None)
            else:
                st["positions"][s] = new
            st["n_fills"] += 1
            st["costs_paid"] += qty * px * c
            self.log("FILL", t, {"sym": s, "side": o["side"], "qty": qty, "ref_open": px, "fill_price": fill,
                                 "notional": qty * fill, "cost": qty * px * c, "fill_bar_open_utc": iso(t),
                                 "order_created_utc": o["created_at"], "order_reason": o.get("reason", "")})
        st["pending"] = still

    def _orders(self, target: dict, eq: float, px: dict, created_at, reason: str):
        if self.rebalance == "exact":
            return self._orders_exact(target, eq, px, created_at, reason)
        st, orders = self.state, []
        prev = st["targets"]
        for s in sorted(set(target) | set(prev) | set(st["positions"])):
            tw, pw = float(target.get(s, 0.0)), float(prev.get(s, 0.0))
            held = st["positions"].get(s, 0.0)
            p = float(px.get(s, np.nan))
            if not np.isfinite(p) or p <= 0:
                continue
            if tw <= 0 and held > 0:
                orders.append({"sym": s, "side": "SELL", "qty": held, "close_all": True, "target_weight": 0.0,
                               "ref_close": p, "created_at": iso(created_at), "reason": reason})
            elif tw > 0 and (held <= 0 or pw <= 0):
                orders.append({"sym": s, "side": "BUY", "qty": tw * eq / p, "target_weight": tw, "ref_close": p,
                               "created_at": iso(created_at), "reason": reason})
            elif tw > 0 and abs(tw - pw) > C.RESIZE_BAND:
                dq = tw * eq / p - held
                if abs(dq * p) >= 1.0:
                    orders.append({"sym": s, "side": "BUY" if dq > 0 else "SELL", "qty": abs(dq), "target_weight": tw,
                                   "ref_close": p, "created_at": iso(created_at), "reason": reason + " (resize)"})
        return orders

    def _orders_exact(self, target: dict, eq: float, px: dict, created_at, reason: str):
        st, orders = self.state, []
        for s in sorted(set(target) | set(st["positions"])):
            tw = float(target.get(s, 0.0))
            held = st["positions"].get(s, 0.0)
            p = float(px.get(s, np.nan))
            if not np.isfinite(p) or p <= 0:
                continue
            base = {"target_weight": tw, "ref_close": p, "created_at": iso(created_at)}
            if tw <= 0:
                if held > 0:
                    orders.append({"sym": s, "side": "SELL", "qty": held, "close_all": True, **base, "reason": reason})
                continue
            dq = tw * eq / p - held
            if abs(dq * p) < self.min_trade_usd:
                continue
            orders.append({"sym": s, "side": "BUY" if dq > 0 else "SELL", "qty": abs(dq), **base,
                           "reason": reason + (" (rebalance)" if held > 0 else "")})
        return orders

    def process_hour(self, t, opens: dict, closes: dict, decide=None, decision_time=None, extra_health=None):
        """t = open time (UTC-naive) of a COMPLETED hourly candle."""
        st = self.state
        t = pd.Timestamp(t)
        if st["last_hour"] is not None and t <= pd.Timestamp(st["last_hour"].rstrip("Z")):
            return None                                                   # idempotent: already processed
        self._fill(t, opens)
        for s, v in closes.items():
            if v is not None and np.isfinite(v):
                st["last_px"][s] = float(v)
        eq, inv = self.value(st["last_px"])
        close_t = t + H
        self.new_equity.append((t, eq, st["cash"], inv))
        self.eq_hist = pd.concat([self.eq_hist, pd.Series([eq], index=[t])]).iloc[-200:]
        st["peak_equity"] = max(st["peak_equity"], eq)
        st["last_equity"] = eq
        hist = self.eq_hist if len(self.eq_hist) > 24 else \
            pd.concat([pd.Series([st["start_capital"]]), self.eq_hist]).reset_index(drop=True)
        hc = health_check(hist, max_dd=self.kill_max_dd, loss_24h=self.kill_24h)
        dd = eq / st["peak_equity"] - 1
        hc["drawdown"] = dd
        hc["reasons"] = [r for r in hc["reasons"] if not r.startswith("drawdown")]
        if self.kill_max_dd is not None and dd <= -self.kill_max_dd:
            hc["reasons"].insert(0, f"drawdown {dd:.2%} from peak ${st['peak_equity']:.2f} breached the -{self.kill_max_dd:.0%} limit")
        if extra_health:
            hc["reasons"] += extra_health
        hc["ok"] = not hc["reasons"]
        st["last_health"] = hc
        st["last_hour"] = iso(t)
        when = decision_time if decision_time is not None else close_t + pd.Timedelta(minutes=C.ORDER_DELAY_MIN)
        hf = self.halted_file()
        if not hc["ok"] and not st["halted"] and not self.enforce_kill:
            if not st.get("_shadow_breach"):
                self.log("KILL_SWITCH_IGNORED", close_t, {"reasons": hc["reasons"], "equity": eq,
                                                          "note": "research shadow: breach logged, not enforced"})
            st["_shadow_breach"] = True
        elif not hc["ok"] and not st["halted"]:
            st["halted"], st["halt_reasons"] = True, hc["reasons"]
            st["pending"] = []
            st["targets"] = {}
            flat = self._orders({}, eq, st["last_px"], when, "KILL SWITCH: flatten")
            st["pending"] = flat
            if hf:
                hf.write_text(json.dumps({"halted_at_utc": iso(close_t), "reasons": hc["reasons"]}))
            self.log("KILL_SWITCH", close_t, {"reasons": hc["reasons"], "equity": eq, "orders": flat})
        elif st["halted"] and hf is not None and not hf.exists():
            st["halted"], st["halt_reasons"] = False, []
            self.log("KILL_SWITCH_RESET", close_t, {"note": "HALTED file removed by operator; trading re-enabled"})

        if t.hour == 23 and decide is not None:
            D = t.normalize()
            if st["last_daily_bar"] is None or D > pd.Timestamp(st["last_daily_bar"]):
                st["last_daily_bar"] = str(D.date())
                if st["halted"]:
                    self.log("DECISION", close_t, {"bar_date": str(D.date()), "action": "HALTED - no new entries",
                                                   "rationale": {"summary": "Kill switch active: " + "; ".join(st["halt_reasons"])},
                                                   "equity": eq})
                else:
                    target, rat = decide(D)
                    orders = self._orders(target, eq, st["last_px"], when, rat.get("summary", ""))
                    if self.rebalance == "exact":      # a decision is a complete portfolio: replace all pending
                        st["pending"] = orders
                    else:
                        st["pending"] = [o for o in st["pending"] if o["sym"] not in {x["sym"] for x in orders}] + orders
                    st["targets"] = {k: float(v) for k, v in target.items() if v > 0}
                    self.log("DECISION", close_t, {"bar_date": str(D.date()), "action": rat.get("action"),
                                                   "target_weights": {k: round(float(v), 4) for k, v in target.items() if v > 0},
                                                   "orders": orders, "equity": eq, "cash": st["cash"],
                                                   "health": {k: hc[k] for k in ["ok", "drawdown", "loss_24h", "reasons"]},
                                                   "rationale": rat})
        return eq

    def halt(self, reasons, ts):
        """Kill switch triggered outside an hourly bar (e.g. stale data in live)."""
        st = self.state
        if st["halted"]:
            return
        st["halted"], st["halt_reasons"], st["targets"] = True, reasons, {}
        eq, _ = self.value(st["last_px"])
        st["pending"] = self._orders({}, eq, st["last_px"], ts, "KILL SWITCH: flatten")
        if self.halted_file():
            self.halted_file().write_text(json.dumps({"halted_at_utc": iso(ts), "reasons": reasons}))
        self.log("KILL_SWITCH", ts, {"reasons": reasons, "equity": eq, "orders": st["pending"]})

    def summary(self):
        st = self.state
        px = st["last_px"]
        eq, inv = self.value(px)
        return {"label": st["label"], "mode": st["mode"], "start_capital": st["start_capital"], "equity": eq,
                "cash": st["cash"], "invested": inv, "net_pnl": eq - st["start_capital"],
                "positions": [{"sym": s, "qty": q, "price": px.get(s), "value": q * px.get(s, np.nan),
                               "weight": q * px.get(s, np.nan) / eq if eq else 0} for s, q in st["positions"].items()],
                "pending": st["pending"], "targets": st["targets"], "halted": st["halted"],
                "halt_reasons": st["halt_reasons"], "peak_equity": st["peak_equity"], "last_hour_utc": st["last_hour"],
                "last_daily_bar": st["last_daily_bar"], "n_fills": st["n_fills"], "costs_paid": st["costs_paid"],
                "opened_at_utc": st.get("opened_at"), "health": st.get("last_health")}
