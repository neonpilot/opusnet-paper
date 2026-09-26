# opusnet-paper

Public dashboard for the OPUSNET **paper-trading** research system: **simulated money, real prices.** No broker is connected.

* `index.html`, `data.json`: published once a day by the routine (`run_daily.py`, `build_dashboard.py`, `screenshot.py`, then copy and push).
* `.github/workflows/live-prices.yml` and `scripts/live_prices.py`: every 15 minutes during US market hours, a read-only job marks the paper positions to market with Yahoo prices. It writes `live.json` to the **`live`** branch, and the page polls it from raw.githubusercontent.com. The job never trades.

## OPUSNET-CRYPTO (24/7, separate system)
Page: https://neonpilot.github.io/opusnet-paper/crypto/ — **paper trading, simulated money, real prices.** No exchange keys exist and no real orders are sent.

* `crypto_engine/`: the research framework (costs, one-bar shift, deflated Sharpe, walk-forward, 1% risk / 20% cap sizing, kill switch) ported for crypto. Data: Binance public klines from `data-api.binance.vision` (keyless, reachable from US GitHub runners). It has its own trial registry, `crypto_engine/registry/crypto_trials.jsonl`, kept separate from the stock N.
  * `python -m crypto_engine.run_research`: gates plus the pre-registered approval rule, written to `results/research.json`.
  * `python -m crypto_engine.replay`: hourly-ledger replay of the past year, written to `results/replay.json`.
  * `python -m crypto_engine.build_page <live.json>`: builds `crypto/index.html`.
  * `python -m pytest crypto_engine/tests`.
* `.github/workflows/crypto-paper.yml`: runs hourly at :17 (`17 * * * *`). It books the new completed 1h candles into the paper ledger, fills orders at the next hourly open, checks the kill switch on every candle, and decides daily after the 00:00 UTC close. It commits `crypto/state/*` to the **`crypto-state`** branch (append-only, with rebase-and-retry on push). It never pushes to main.
* Kill-switch reset: delete `crypto/state/HALTED` on the `crypto-state` branch and commit.
