# OPUSNET-CRYPTO rule changelog (dated, append-only)

Times are AWST (Perth, UTC+8) unless marked UTC. Paper money only.

## 2026-10-08: BTC trend account (`crypto-paper`, `c_btc_regime`), 24h-loss kill switch changed. Declared trial #45

**Honest disclosure:** this is a post-hoc change. It was made **after seeing a live halt** and after seeing the
2025-26 replay, so it is not pre-registered. Count it as an additional tested variant: 31 original crypto research
variants (`crypto_trials.jsonl`), plus 13 aggressive variants (`/workspace/aggressive/PREREGISTRATION.md`), plus this
one, gives **45** crypto trials in total. It changes a risk rule, not a signal, so it has no Sharpe series of its
own. That is why it is recorded here and not as a row in `crypto_trials.jsonl`, which feeds the Deflated-Sharpe N
of the strategy research. That N stays at 31 for the original research run.

* **What triggered it:** the live account halted at 2026-10-07 20:00 AWST (12:00 UTC candle close) with a 24h loss
  of -4.49% under the pre-registered rule (24h loss >= 4%, manual reset only), at equity of about $961.29. The halt
  cancelled pending orders and **sold the whole 9-coin basket** at the next hourly open (21:00 AWST, 9 SELL fills,
  $1.80 of costs), leaving $957.35 cash. It then blocked new entries until a human deleted `crypto/state/HALTED`.
* **Old rule (2026-09-27 to 2026-10-08):** halt and flatten if the rolling-24h loss is >= 4% (`KILL_24H_LOSS = 0.04`),
  manual reset only.
* **New rule (from 2026-10-08):** halt and flatten if the rolling-24h loss **exceeds 10%** (`KILL_24H_LOSS = 0.10`).
  After a **72h cooling-off** (`KILL_24H_COOLOFF_HOURS = 72`, counted from the time the halt was acted on, never
  earlier than the breaching candle's close), it re-enables automatically. `halted_at_utc` and `reenable_at_utc`
  are kept in `state.json` and in `AUTO_HALTED`. The trend filter then acts at its next normal daily decision, with
  no forced re-entry. At re-enable the peak-equity watermark is re-based to current equity. Without that, the 10%
  drawdown rule, which a >10% 24h loss always breaches too, would re-halt the account at once.
  Unchanged: the 10%-from-peak drawdown halt and the stale-data halt stay manual-reset. A `HALTED` file created by
  hand is a manual halt and is never auto-cleared.
* **Values fixed before the replay was re-run, and not tuned on it.** Re-run on the same window as the original replay
  (hourly candles 2025-09-25 23:00 UTC to 2026-09-26 22:00 UTC, $1,000 start, `python -m crypto_engine.replay
  --end-hour 2026-09-26T22:00`):

  | rules | final equity | 24h-switch trips | notes |
  |---|---|---|---|
  | original (4% 24h, manual) | $1,083.83 | 1 (2026-08-23 12:00 AWST, -4.90%) | in cash to the end (reproduced exactly) |
  | **new (>10% 24h, 72h auto re-enable)** | **$1,059.02** | **0** | the unchanged 10% peak-drawdown halt tripped 2026-08-31 10:00 AWST (-10.12% from $1,180.21), manual reset, in cash to the end |
  | no kill switch at all (research shadow) | $1,277.55 | n/a | max hourly DD -11.3% |

  With the new rule the 24h switch never fired in the replay year. The loss came from the pre-existing drawdown
  halt, which was not part of this change and was not altered.
* **Live resume (operator-approved one-off):** the 72h cooling-off would run from 2026-10-07 20:00 AWST to
  2026-10-10 20:00 AWST. The operator approved an immediate resume instead, so `crypto/state/HALTED` was deleted
  on `crypto-state` on 2026-10-08.
