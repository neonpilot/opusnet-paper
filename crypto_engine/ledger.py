"""Hourly paper ledger for crypto. SIMULATED MONEY, REAL PRICES. No exchange connection, no keys.

Timing (identical in replay and live):
  * the ledger walks completed 1h Binance candles in order;
  * pending orders fill at the OPEN of the first hourly candle whose open time is >= the order's
    created_at, at that open worsened by the coin's one-way cost; sells before buys; never borrows, never shorts;
  * every hourly close is marked to market and the kill switch is checked. A breach cancels pending
    orders, queues a full liquidation at the next hourly open and blocks new entries. Two kinds of halt:
      - MANUAL halt (file HALTED): stale data (live), a HALTED file created by hand, and any rule that
        the ledger was not built to auto-re-enable (e.g. the drawdown rule of the aggressive account).
        It never clears by itself; only deleting HALTED re-enables trading.
      - AUTOMATIC halt (file AUTO_HALTED + state.halt_kind == "auto"): only for a ledger built with
        auto_reenable_hours (24h-loss rule; BTC trend account, changed 2026-10-08) and/or auto_dd_hours
        (10%-from-peak drawdown rule; BTC trend account, changed 2026-10-09), see config.py. It re-enables
        by itself at the first candle close >= state.reenable_at_utc (= halted_at_utc + cooling-off;
        halted_at_utc is the time the halt was actually acted on, never earlier than the breaching candle's
        close). The peak-equity watermark is re-based to equity at re-enable. After re-enable the strategy
        acts only at its next normal daily decision; nothing is forced. Creating HALTED by hand turns any
        halt into a manual one; deleting AUTO_HALTED by hand ends the cooling-off early (logged);
      - with rebase_peak_on_resume (BTC trend account, 2026-10-09) an operator resume (HALTED deleted)
        also re-bases the peak watermark to current equity and journals the previous peak, so a resumed
        account never starts out "in drawdown" against a stale peak;
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
                 min_trade_usd=1.0, auto_reenable_hours=None, auto_dd_hours=None, rebase_peak_on_resume=False):
        """kill_max_dd / kill_24h: None disables that rule. rebalance: "band" (original account: trade only when
        the TARGET weight moves > RESIZE_BAND) or "exact" (aggressive account: every decision is a complete
        target portfolio; holdings are traded back to it, pending orders are replaced, trades < min_trade_usd
        skipped). auto_reenable_hours: None (default; every halt is manual) or the cooling-off in hours after
        which a 24h-loss halt clears itself (BTC trend account only). auto_dd_hours: None (drawdown halts are
        manual) or the cooling-off after which a drawdown halt clears itself (BTC trend account, 2026-10-09).
        rebase_peak_on_resume: an operator resume (HALTED deleted) re-bases the peak watermark (2026-10-09)."""
        self.dir = Path(directory) if directory else None
        self.mode, self.label = mode, label
        self.kill_max_dd, self.kill_24h = kill_max_dd, kill_24h
        self.rebalance, self.min_trade_usd = rebalance, min_trade_usd
        self.auto_reenable_hours = auto_reenable_hours
        self.auto_dd_hours = auto_dd_hours
        self.rebase_peak_on_resume = rebase_peak_on_resume
        self._meta = auto_reenable_hours is not None or auto_dd_hours is not None   # records halt kind/times
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

    def auto_halted_file(self):
        return self.dir / "AUTO_HALTED" if self.dir else None

    def _auto_hours(self, reasons):
        """Cooling-off (hours) if this set of breached rules is an AUTOMATIC halt, else None (manual).
        Any reason other than the 24h-loss and drawdown rules (e.g. extra health checks) makes it manual.
        A drawdown breach that accompanies an auto 24h-loss breach follows the 24h rule (2026-10-08 behaviour)."""
        has24 = any(r.startswith("24h loss") for r in reasons)
        hasdd = any(r.startswith("drawdown") for r in reasons)
        if not reasons or any(not (r.startswith("24h loss") or r.startswith("drawdown")) for r in reasons):
            return None
        h = []
        if has24:
            if self.auto_reenable_hours is None:
                return None
            h.append(self.auto_reenable_hours)
        if hasdd:
            if self.auto_dd_hours is not None:
                h.append(self.auto_dd_hours)
            elif not has24:
                return None
        return max(h) if h else None

    def _set_halt_meta(self, kind, acted_at, breach_close=None, hours=None):
        st = self.state
        st["halt_kind"] = kind
        st["halted_at_utc"] = iso(acted_at)
        st["halt_breach_candle_close_utc"] = iso(breach_close) if breach_close is not None else None
        st["cooloff_hours"] = hours if kind == "auto" else None
        st["reenable_at_utc"] = iso(pd.Timestamp(acted_at) + pd.Timedelta(hours=hours)) if kind == "auto" else None

    def _clear_halt_meta(self):
        st = self.state
        st["halted"], st["halt_reasons"] = False, []
        for k in ("halt_kind", "halted_at_utc", "halt_breach_candle_close_utc", "reenable_at_utc", "cooloff_hours"):
            st.pop(k, None)

    def sync_manual_halt(self, now):
        """Live only: a HALTED file created by hand halts the account (flatten) and is never auto-cleared.
        If the account is already in an automatic halt, it becomes a manual one (the cooling-off no longer
        applies). Idempotent."""
        hf, st = self.halted_file(), self.state
        if hf is None or not hf.exists():
            return
        if not st["halted"]:
            self.halt(["manual halt: HALTED file created by operator"], now)
        elif st.get("halt_kind") == "auto":
            st["halt_kind"], st["reenable_at_utc"], st["cooloff_hours"] = "manual", None, None
            st["halt_reasons"] = st["halt_reasons"] + ["manual override: HALTED file created by operator"]
            af = self.auto_halted_file()
            if af is not None and af.exists():
                af.unlink()
            self.log("MANUAL_HALT_OVERRIDE", now, {"note": "HALTED file created by operator during an automatic halt; "
                                                           "the automatic re-enable is cancelled; delete HALTED to resume"})

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
        if st["halted"] and st.get("halt_kind") == "auto" and self.enforce_kill:
            ra = pd.Timestamp(st["reenable_at_utc"].rstrip("Z"))
            af = self.auto_halted_file()
            early = af is not None and not af.exists()
            if close_t >= ra or early:
                info = {"halted_at_utc": st["halted_at_utc"], "reenable_at_utc": st["reenable_at_utc"],
                        "previous_peak_equity": st["peak_equity"], "equity": eq,
                        "note": ("AUTO_HALTED file removed by operator before the cooling-off ended; " if early and close_t < ra
                                 else f"{st.get('cooloff_hours') or self.auto_reenable_hours}h cooling-off over; ")
                                + "trading re-enabled; the strategy acts at its next daily decision (nothing forced); "
                                  "peak-equity watermark re-based to current equity"}
                if decision_time is not None:
                    info["processed_at_utc"] = iso(decision_time)
                self._clear_halt_meta()
                st["peak_equity"] = eq
                if af is not None and af.exists():
                    af.unlink()
                self.log("KILL_SWITCH_AUTO_REENABLE", close_t, info)
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
            hours = self._auto_hours(hc["reasons"])
            auto = hours is not None
            st["halted"], st["halt_reasons"] = True, hc["reasons"]
            st["pending"] = []
            st["targets"] = {}
            flat = self._orders({}, eq, st["last_px"], when, "KILL SWITCH: flatten")
            st["pending"] = flat
            if not self._meta:                                # accounts without auto re-enable: unchanged behaviour
                rec = {"halted_at_utc": iso(close_t), "reasons": hc["reasons"]}
            else:
                acted = max(pd.Timestamp(when), close_t)      # never earlier than the breaching candle's close
                self._set_halt_meta("auto" if auto else "manual", acted, close_t, hours)
                rec = {"halted_at_utc": st["halted_at_utc"], "breach_candle_close_utc": iso(close_t),
                       "reasons": hc["reasons"], "kind": st["halt_kind"]}
            if auto:
                rec["reenable_at_utc"] = st["reenable_at_utc"]
                rec["cooloff_hours"] = hours
                rec["note"] = (f"automatic halt: clears itself at reenable_at_utc ({hours}h cooling-off; the peak "
                               "watermark is re-based to equity at re-enable). "
                               "Create a file named HALTED to keep it halted indefinitely.")
                if self.auto_halted_file():
                    self.auto_halted_file().write_text(json.dumps(rec))
            elif hf:
                hf.write_text(json.dumps(rec))
            meta = {} if not self._meta else \
                {"halt_kind": st["halt_kind"], "halted_at_utc": st["halted_at_utc"], "reenable_at_utc": st.get("reenable_at_utc")}
            self.log("KILL_SWITCH", close_t, {"reasons": hc["reasons"], "equity": eq, "orders": flat, **meta})
        elif st["halted"] and st.get("halt_kind") != "auto" and hf is not None and not hf.exists():
            if not self._meta and not self.rebase_peak_on_resume:   # accounts without these rules: unchanged
                st["halted"], st["halt_reasons"] = False, []
                self.log("KILL_SWITCH_RESET", close_t, {"note": "HALTED file removed by operator; trading re-enabled"})
            else:
                info = {"note": "HALTED file removed by operator; trading re-enabled",
                        "effective_from_candle_close_utc": iso(close_t)}
                if self.rebase_peak_on_resume:
                    info.update(previous_peak_equity=st["peak_equity"], new_peak_equity=eq, equity=eq)
                    info["note"] += "; peak-equity watermark re-based to current equity"
                    st["peak_equity"] = eq
                self._clear_halt_meta()
                self.log("KILL_SWITCH_RESET", decision_time if decision_time is not None else close_t, info)

        if t.hour == 23 and decide is not None:
            D = t.normalize()
            if st["last_daily_bar"] is None or D > pd.Timestamp(st["last_daily_bar"]):
                st["last_daily_bar"] = str(D.date())
                if st["halted"]:
                    self.log("DECISION", close_t, {"bar_date": str(D.date()), "action": "HALTED - no new entries",
                                                   "rationale": {"summary": "Kill switch active: " + "; ".join(st["halt_reasons"])
                                                                 + (f" (automatic halt, re-enables at {st['reenable_at_utc']})"
                                                                    if st.get("halt_kind") == "auto" else " (manual reset only)")},
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
        if self._meta:                              # BTC trend account: record the halt kind (manual)
            self._set_halt_meta("manual", ts)
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
                "halt_reasons": st["halt_reasons"], "halt_kind": st.get("halt_kind"),
                "halted_at_utc": st.get("halted_at_utc"), "reenable_at_utc": st.get("reenable_at_utc"),
                "peak_equity": st["peak_equity"], "last_hour_utc": st["last_hour"],
                "last_daily_bar": st["last_daily_bar"], "n_fills": st["n_fills"], "costs_paid": st["costs_paid"],
                "opened_at_utc": st.get("opened_at"), "health": st.get("last_health")}
