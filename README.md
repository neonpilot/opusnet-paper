# opusnet-paper

Public dashboard for the OPUSNET **paper-trading** research system: **simulated money, real prices.** No broker is connected.

* `index.html`, `data.json`: published once a day by the routine (`run_daily.py`, `build_dashboard.py`, `screenshot.py`, then copy and push).
* `.github/workflows/live-prices.yml` and `scripts/live_prices.py`: every 15 minutes during US market hours, a read-only job marks the paper positions to market with Yahoo prices. It writes `live.json` to the **`live`** branch, and the page polls it from raw.githubusercontent.com. The job never trades.
