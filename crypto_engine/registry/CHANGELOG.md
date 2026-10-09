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

## 2026-10-09: BTC trend account (`crypto-paper`, `c_btc_regime`), 10%-from-peak drawdown halt changed, and every resume re-bases the peak. Declared trial #46

**Honest disclosure:** this is another post-hoc change. It was made **after a second live halt**, the day after
trial #45, and is not pre-registered. Count it as one more tested variant: **46** crypto trials in total (31 research
variants + 13 aggressive variants + #45 + #46). Like #45 it changes a risk rule, not a signal, so it is recorded here
and not in `crypto_trials.jsonl` (the Deflated-Sharpe N of the strategy research stays 31).

* **What triggered it:** the 2026-10-08 operator resume deleted `HALTED`, but the code path **did not re-base the peak
  watermark**. The account restarted at $957.35 cash with the peak still at $1,012.15, so it began already -5.4% "in
  drawdown". It re-bought the 9-coin basket at the 2026-10-08 09:00 AWST open. On the candle closing 2026-10-09 00:00
  AWST (16:00 UTC) equity was $897.91: **-6.2% since the restart**, but -11.29% against the stale peak. So the
  10%-from-peak drawdown halt (manual reset) fired. The hourly run that acted on it ran late, at 2026-10-09 05:18 AWST
  (21:18 UTC, commit 895efeb on `crypto-state`). It wrote `HALTED` (kind manual) and queued a full liquidation, which
  **sold all 9 coins** at the 06:00 AWST open (22:00 UTC; 9 SELL fills, $1.71 of costs), leaving **$911.44 cash**, no
  positions. The 08:00 AWST daily decision (bar 2026-10-08) was blocked ("HALTED - no new entries"). The halt was
  therefore **partly caused by the un-re-based watermark at the operator resume**; with a re-based peak ($957.35) the
  drawdown at the worst close would have been -7.4% (worst close $886.74 at 02:00 AWST), below the 10% limit.
* **Old rule (2026-09-27 to 2026-10-09):** equity <= 90% of the peak watermark -> halt + flatten, manual reset only;
  an operator resume did not touch the peak.
* **New rule (from 2026-10-09):** the drawdown halt gets the same treatment as the 24h-loss halt: cancel pending
  orders, flatten at the next hourly open, **72h cooling-off** (`KILL_MAX_DD_AUTO = True`,
  `KILL_MAX_DD_COOLOFF_HOURS = 72`; `AUTO_HALTED`, `halted_at_utc` / `reenable_at_utc` in `state.json`), then
  **automatic re-enable with the peak watermark re-based to current equity**; the trend filter acts at its next daily
  decision (nothing forced). **Any resume re-bases the peak** (`REBASE_PEAK_ON_RESUME = True`): the automatic
  re-enable, the operator deleting `AUTO_HALTED` early, and the operator deleting `HALTED`; the journal entry records
  the previous and new peak. Unchanged: the stale-data halt stays manual (`HALTED`); a `HALTED` file created by hand is
  a manual halt and is never auto-cleared; the 24h-loss rule of #45 (>10%, 72h); the aggressive account.
* **Values fixed before the replay was re-run, and not tuned on it** (10% and 72h are the existing values). Same
  window as before (hourly candles 2025-09-25 23:00 UTC to 2026-09-26 22:00 UTC, $1,000 start, `python -m
  crypto_engine.replay --end-hour 2026-09-26T22:00`, re-run 2026-10-09):

  | rules | final equity | halts | notes |
  |---|---|---|---|
  | original (4% 24h, manual) | $1,083.83 | 1 (24h, 2026-08-23) | reproduced exactly; in cash to the end |
  | 2026-10-08 (#45: >10% 24h auto, drawdown manual) | $1,059.02 | 1 (drawdown, 2026-08-31 10:00 AWST) | reproduced exactly; in cash to the end |
  | **2026-10-09 (#46: both auto, 72h, peak re-based)** | **$1,083.17** | **2 (both drawdown)** | **2 automatic re-enables; invested at the end; 75 fills, $15.23 costs** |
  | no kill switch (research shadow) | $1,277.55 | n/a | max hourly DD -11.3% |

  Halts under #46: 2026-08-31 10:00 AWST (-10.12% from $1,180.21, equity $1,060.80) -> re-enabled 2026-09-03 11:00
  AWST at $1,059.02 (new peak); 2026-09-16 10:00 AWST (-10.07% from $1,100.57, equity $989.69) -> re-enabled
  2026-09-19 11:00 AWST at $994.88. The 24h-loss switch never fired. Max hourly drawdown of the equity curve (against
  its all-time high, not the re-based watermark) was -17.9%, worse than the -10.3% of the #45 rules (which sat in cash
  from 31 Aug) and the -11.3% with no switch. The result is still $194 below having no switch at all.
* **Live resume (operator-approved, 2026-10-09):** `crypto/state/HALTED` deleted on `crypto-state` after this code was
  pushed. The new code path re-based the peak to current equity at the first processed candle and journaled it
  (`KILL_SWITCH_RESET` with `previous_peak_equity` / `new_peak_equity`).
