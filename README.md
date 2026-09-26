# crypto-state (OPUSNET-CRYPTO paper ledger)
Append-only state of the 24/7 **paper** crypto account, written hourly by the `crypto-paper` workflow.
SIMULATED MONEY, REAL PRICES (Binance public klines). No exchange keys, no real orders.
- `crypto/state/state.json`   cash, positions, pending orders, peak, kill-switch status
- `crypto/state/journal.jsonl` every decision (with rationale), fill and kill-switch event
- `crypto/state/equity.csv`   hourly mark-to-market
- `crypto/state/live.json`    snapshot read by https://neonpilot.github.io/opusnet-paper/crypto/
- `crypto/state/HALTED`       exists only while the kill switch is active; delete it (commit) to re-enable trading
