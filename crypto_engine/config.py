"""OPUSNET-CRYPTO configuration. Every threshold and the APPROVAL RULE below were fixed BEFORE the
first crypto research run (pre-registration). Paper money only; no exchange keys exist anywhere."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
CACHE_DIR = ROOT / "cache"                  # local research cache (gitignored)
REGISTRY_PATH = ROOT / "registry" / "crypto_trials.jsonl"   # SEPARATE from the stock registry
RESULTS_DIR = ROOT / "results"
PAGE_DIR = REPO / "crypto"

# ---------------- data ----------------
DATA_SOURCE = "Binance public market data (data-api.binance.vision, keyless, USDT spot pairs)"
BASE_URL = "https://data-api.binance.vision/api/v3/klines"
SYMBOLS = ["BTC", "ETH", "BNB", "XRP", "ADA", "SOL", "DOGE", "LTC", "LINK"]   # quote: USDT
QUOTE = "USDT"
DATA_START = "2017-08-17"

# ---------------- costs (one-way, per unit of traded notional) ----------------
# 10 bps taker fee + slippage: 5 bps for BTC/ETH, 10 bps for the other majors.
COST_BPS = {"BTC": 15, "ETH": 15}
DEFAULT_COST_BPS = 20

# ---------------- risk (identical rules to the stock system) ----------------
RISK_PER_TRADE = 0.01
MAX_POSITION = 0.20
MAX_GROSS = 1.00
STOP_ATR_MULT = 2.0
ATR_WINDOW = 20
KILL_MAX_DRAWDOWN = 0.10      # halt + flatten if equity <= 90% of the peak watermark (BTC trend account: automatic
                              # halt with a 72h cooling-off since 2026-10-09, see below; aggressive account: own rule)
# ---- 24h-loss switch of the BTC trend account: CHANGED 2026-10-08 (Perth), AFTER SEEING A LIVE HALT ----
# Pre-registered rule (2026-09-27): halt + flatten if the account lost >= 4% over a rolling 24h, manual reset
# only. It tripped live at 2026-10-07 12:00 UTC (20:00 AWST) at -4.49% (equity ~$961) and flattened the
# basket. On 2026-10-08 the operator (Harley) replaced it with the rule below. This is a post-hoc change made
# after seeing that halt and the 2025-26 replay (where 4% tripped on 2026-08-23 and cost ~$194 vs ignoring
# it); it is declared as a new trial in registry/CHANGELOG.md. 10% and 72h were fixed BEFORE re-running the
# replay with them and must not be tuned on it.
#   New rule: if the 24h loss EXCEEDS 10%, cancel pending orders, flatten at the next hourly open (same
#   mechanics as before) and stay in cash for a 72h cooling-off; then re-enable automatically (state.json
#   records halted_at_utc / reenable_at_utc) and let the trend filter act at its next daily decision.
#   A HALTED file created by hand is a manual halt and is never auto-cleared.
KILL_24H_LOSS = 0.10              # declared 2026-10-08: halt if the rolling-24h loss exceeds 10%
KILL_24H_COOLOFF_HOURS = 72       # declared 2026-10-08: automatic re-enable 72h after the halt was acted on
KILL_24H_LOSS_ORIGINAL = 0.04     # the pre-registered value (2026-09-27 .. 2026-10-08), kept for the record
KILL_RULE_CHANGED_ON = "2026-10-08"
# ---- 10%-from-peak drawdown halt of the BTC trend account: CHANGED 2026-10-09 (Perth), AFTER A SECOND LIVE HALT ----
# Rule until 2026-10-09: halt + flatten when equity fell 10% below its peak watermark, manual reset only (delete
# crypto/state/HALTED). It tripped live on the candle closing 2026-10-08 16:00 UTC (00:00 AWST 9 Oct; acted on
# by the 21:18 UTC run, 05:18 AWST) at -11.29% from a peak of $1,012.15 and sold the 9-coin basket at the 22:00
# UTC open (06:00 AWST) into $911.44 cash. That halt was partly caused by an operator resume on 2026-10-08 that
# did NOT re-base the peak watermark (the account restarted at $957.35 cash but kept the $1,012.15 peak, so it
# began 5.4% "in drawdown"; the actual loss since the restart was -6.2%). On 2026-10-09 the operator (Harley)
# changed the rule; it is a post-hoc change, declared as trial #46 in registry/CHANGELOG.md. 10% and 72h were
# fixed BEFORE re-running the replay and must not be tuned on it.
#   New rule: drawdown <= -10% from the peak watermark -> cancel pending orders, flatten at the next hourly open,
#   72h cooling-off in cash (AUTO_HALTED + halted_at_utc / reenable_at_utc in state.json), then re-enable
#   automatically with the peak watermark re-based to current equity; the trend filter acts at its next daily
#   decision (nothing forced). The stale-data halt stays manual (HALTED). A HALTED file created by hand is a
#   manual halt and is never auto-cleared.
KILL_MAX_DD_AUTO = True           # declared 2026-10-09: the drawdown halt is an automatic (cooling-off) halt
KILL_MAX_DD_COOLOFF_HOURS = 72    # declared 2026-10-09: same 72h cooling-off as the 24h-loss switch
KILL_DD_RULE_CHANGED_ON = "2026-10-09"
# Declared 2026-10-09: EVERY resume of the BTC trend account (automatic re-enable, the operator deleting
# AUTO_HALTED early, or the operator deleting HALTED) re-bases the peak watermark to the equity at the candle
# close where the resume takes effect, and journals the previous peak. This is the fix for the 2026-10-08 bug.
REBASE_PEAK_ON_RESUME = True
STALE_HOURS = 6               # ... or the newest completed hourly candle is >6h old (live only)
RESIZE_BAND = 0.005           # ledger re-trades an asset only if its target weight moves >0.5% of equity
ORDER_DELAY_MIN = 17          # workflow runs at :17 -> decisions are made 17 min after the bar close

# ---------------- research protocol ----------------
ANN = 365
EVAL_START = "2019-01-01"
WF_TRAIN_DAYS = 730
WF_TEST_DAYS = 182
WF_MIN_FOLD_DAYS = 91
REPLAY_DAYS = 365
START_CAPITAL = 1000.0
LEAKAGE_CUTS = 8

# ---------------- gates + approval rule (pre-registered) ----------------
GATE_DSR_MIN = 0.95
GATE_OOS_SHARPE_MIN = 0.30
GATE_OOS_POS_FOLDS_MIN = 0.55
APPROVAL_RULE = ("A strategy is approved for paper trading only if it passes G1 (no look-ahead, costs on), "
                 "G2 (Deflated Sharpe >= 0.95 with N = every crypto variant ever registered) and G3 "
                 "(walk-forward OOS Sharpe >= 0.30 and >= 55% of OOS half-years profitable) AND its OOS "
                 "Sharpe beats a buy-and-hold BTC position sized by the same 1%-risk / 20%-cap rules over "
                 "the same OOS days. Among qualifiers the highest DSR wins. If none qualify: 100% cash (USD).")

# =====================================================================================================
# AGGRESSIVE PAPER ACCOUNT #3: mom_top2_1x (declared 2026-09-27, Perth time; paper only, simulated money)
# Signal copied verbatim from /workspace/aggressive/backtest.py (trial #12 of PREREGISTRATION.md):
#   every 7 days (days since 2018-01-01 divisible by 7, i.e. Mondays) pick the 2 coins with the highest
#   90-day close-to-close return (coins with >= 100 days of history), 50/50; hold them only while BTC's
#   daily close is above its 100-day SMA (checked daily); otherwise 100% cash. All inputs are closes up
#   to day t-1; the position for day t is decided after the 00:00 UTC close and, in the live ledger,
#   fills at the next hourly open after the run (never backdated). Rebalanced to exact target weights at
#   every daily decision (the backtest rebalances daily). Spot only: no leverage, no shorts, no borrowing.
# =====================================================================================================
AGGR_NAME = "mom_top2_1x"
AGGR_TITLE = "AGGRESSIVE: TOP-2 MOMENTUM, paper"
AGGR_TOP_K = 2
AGGR_LOOKBACK_DAYS = 90
AGGR_SMA_DAYS = 100
AGGR_MIN_HISTORY_DAYS = 100
AGGR_REFRESH_DAYS = 7
AGGR_REFRESH_ANCHOR = "2018-01-01"          # a Monday -> selection refreshes for Monday's position
AGGR_LEVERAGE = 1.0                         # declared rule: no leverage, ever
AGGR_MIN_TRADE_USD = 1.0                    # ignore rebalancing trades below $1 notional (dust)
# Declared risk rule: the ONLY automatic kill switch is a catastrophic-drawdown hard stop. If hourly
# marked equity falls 60% or more below its running peak, cancel pending orders, flatten everything at
# the next hourly open and stay in cash until a human deletes crypto/state_aggr/HALTED on crypto-state.
# (Backtest max drawdown 2019-2026 was -64%, so this stop would have fired once in that history.)
# Deliberately NO 24h-loss switch (a 4%/24h move is ordinary for a 2-coin book) and NO stale-data
# flatten (missed hours are caught up on the next run; stale data is flagged on the page instead).
AGGR_KILL_MAX_DRAWDOWN = 0.60
AGGR_KILL_24H_LOSS = None
AGGR_DATA_DAYS = 500                        # daily history fetched per decision (>= 90 + 100 + slack)
AGGR_STATE_SUBDIR = "crypto/state_aggr"     # on the crypto-state branch
AGGR_PAGE_DIR = REPO / "crypto-aggressive"
AGGR_BACKTEST = {"source": "/workspace/aggressive (PREREGISTRATION.md, RESULTS.md, results_full.json)",
                 "cagr_2019_on": 1.072, "cagr_2022_on": 0.302, "max_drawdown": -0.637, "year_2021": 9.432,
                 "final_1000_from_2025_09_26": 1077.46, "n_trials_total": 44}
