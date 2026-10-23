# Fraud-rule overhead

Database: 500,000 transfers across 2,003 accounts (about 499 ledger entries per wallet). 1,000 transfers per mode per round, 2 rounds, alternating off/on. Times in milliseconds, measured with `time.perf_counter()` in the application process (one pooled connection, localhost).

## Each rule's query on its own

| rule | mean | p50 | p95 | samples |
|---|---|---|---|---|
| VELOCITY (count in window, `idx_transfers_from_time`) | 0.261 | 0.233 | 0.341 | 1000 |
| AMOUNT_SPIKE (avg over 30 days, `idx_transfers_from_time`) | 0.299 | 0.266 | 0.453 | 1000 |
| NEW_PAYEE_LARGE (EXISTS, `idx_transfers_pair`; large amount forced) | 0.121 | 0.111 | 0.202 | 1000 |

## End-to-end peer transfer (BEGIN to COMMIT)

| rules | mean | p50 | p95 | samples |
|---|---|---|---|---|
| disabled | 3.348 | 2.895 | 5.296 | 2000 |
| enabled (none fire) | 3.609 | 3.378 | 4.845 | 2000 |

**Added by the rules: 0.260 ms on average per transfer (7.8% of the 3.348 ms baseline), 0.483 ms at the median.**

NEW_PAYEE_LARGE only runs its query when the amount exceeds the threshold, so on ordinary transfers it costs one comparison; the figure above is the worst case. VELOCITY and AMOUNT_SPIKE run on every outgoing transfer.
