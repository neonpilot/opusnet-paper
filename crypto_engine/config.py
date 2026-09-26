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
KILL_MAX_DRAWDOWN = 0.10      # halt + flatten if equity < 90% of peak
KILL_24H_LOSS = 0.04          # ... or loses >4% over any rolling 24 hours
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
