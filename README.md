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

## Paper account #3: AGGRESSIVE: TOP-2 MOMENTUM (mom_top2_1x)
Page: https://neonpilot.github.io/opusnet-paper/crypto-aggressive/ — **paper trading, simulated money, real prices.** High risk: the backtest's max drawdown was -64%, and most of its gains came from 2021.

* Rule (the pre-registered backtest trial `mom_top2_1x`, copied exactly in `crypto_engine/aggr_signal.py`): every Monday, pick the 2 coins with the highest 90-day return (50/50). Hold them only while BTC's daily close is above its 100-day SMA, otherwise hold 100% cash. Signals use closes up to the previous day. Spot, no leverage. Declared in `config.py` (`AGGR_*`).
* Risk: the only automatic stop is a hard stop at -60% from peak (flatten, then stay in cash). There is no 24h-loss switch and no stale-data flatten. Halt manually by committing `crypto/state_aggr/HALTED` on `crypto-state`; delete that file to reset.
* `.github/workflows/crypto-paper-aggr.yml` runs at :19 and :49 every hour (a double cron, because GitHub skips scheduled runs; the step is idempotent). `python -m crypto_engine.aggr_step` books completed 1h candles into `crypto/state_aggr/` on **`crypto-state`**. Orders fill at the next hourly open after the run, never backdated. The step decides daily after the 00:00 UTC close and pushes with rebase-and-retry. It never pushes to main. `workflow_dispatch` with `init=true` opened the account once and refuses to run again.
* `python -m crypto_engine.aggr_replay` replays the past year through the same ledger code. `python -m crypto_engine.aggr_build_page <live.json>` builds `crypto-aggressive/index.html`. `python -m crypto_engine.aggr_screenshot <url>` takes screenshots.
