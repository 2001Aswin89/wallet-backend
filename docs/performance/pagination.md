# Statement pagination at scale

Account: CASH_IN (id 1) with 176,177 entries; ledger has 1,000,000 entries. Median of 7 runs per cell, warm cache.

| OFFSET | LIMIT/OFFSET (median ms) | cursor at same position (median ms) | same 50 rows |
|---|---|---|---|
| 0 | 0.62 | - | - |
| 1,000 | 0.53 | 0.32 | True |
| 10,000 | 2.79 | 0.50 | True |
| 50,000 | 14.79 | 0.60 | True |
| 100,000 | 25.81 | 0.29 | True |

## EXPLAIN (ANALYZE, BUFFERS), OFFSET 100,000

```text
Limit  (cost=26005.49..26018.49 rows=50 width=38) (actual time=28.519..28.536 rows=50 loops=1)
  Buffers: shared hit=6228
  ->  Index Scan using idx_ledger_account_time on ledger_entries le  (cost=0.42..46488.90 rows=178767 width=38) (actual time=0.015..25.746 rows=100050 loops=1)
        Index Cond: (account_id = '1'::smallint)
        Buffers: shared hit=6228
Planning Time: 0.081 ms
Execution Time: 28.553 ms
```

## EXPLAIN (ANALYZE, BUFFERS), cursor resuming after that same row

```text
Limit  (cost=0.42..27.59 rows=50 width=38) (actual time=0.010..0.027 rows=50 loops=1)
  Buffers: shared hit=8
  ->  Index Scan using idx_ledger_account_time on ledger_entries le  (cost=0.42..41590.54 rows=76546 width=38) (actual time=0.010..0.024 rows=50 loops=1)
        Index Cond: ((account_id = '1'::smallint) AND (ROW(created_at, id) < ROW('2026-09-02 07:27:58.386157+05:30'::timestamp with time zone, 428871)))
        Buffers: shared hit=8
Planning Time: 0.091 ms
Execution Time: 0.039 ms
```
