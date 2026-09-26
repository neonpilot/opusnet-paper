"""Official crypto research run. Usage: python -m crypto_engine.run_research [--no-refresh]"""
import json, sys
from . import config as C
from .data import load
from .research import run_research


def main():
    b = load("1d", refresh="--no-refresh" not in sys.argv)
    out, _ = run_research(b, note="official crypto research run v1")
    C.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (C.RESULTS_DIR / "research.json").write_text(json.dumps(out, indent=1, default=float))
    print("N trials:", out["n_trials_total"], "runs:", out["n_research_runs"], "engine:", out["engine_check"])
    for s in out["strategies"]:
        g = s["gates"]; o = s["oos"]; f = s["full_sample"]; bs = s["benchmark_btc_sized_oos"]; br = s["benchmark_btc_raw_oos"]
        print(f"{s['name']:13s} full SR {f['sharpe']:.2f} CAGR {f['cagr']:.1%} DD {f['max_drawdown']:.1%} | DSR {g['g2_dsr']['dsr']:.3f} "
              f"| OOS SR {o['sharpe']:.2f} CAGR {o['cagr']:.1%} DD {o['max_drawdown']:.1%} pos {g['g3_walkforward']['pos_fold_fraction']:.0%} "
              f"| G1 {g['g1_leakage']['pass']} G2 {g['g2_dsr']['pass']} G3 {g['g3_walkforward']['pass']} beatsBTC {s['beats_sized_btc']} "
              f"| BTC sized SR {bs['sharpe']:.2f} raw SR {br['sharpe']:.2f} CAGR {br['cagr']:.1%} DD {br['max_drawdown']:.1%}")
    print("APPROVED:", out["approved_strategy"])


if __name__ == "__main__":
    main()
