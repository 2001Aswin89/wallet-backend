# Reconciliation checks at scale

Database: 1,000,000 ledger entries, 500,000 transfers, 2,003 accounts. PostgreSQL 17.5 on x86_64-windows, compiled by msvc-19.44.35209, 64-bit.
Each number is `EXPLAIN (ANALYZE, BUFFERS)` execution time on a warm cache (second run of two). Variants drop an index inside a rolled-back transaction.

| check | as-is | without idx_ledger_transfer | without idx_ledger_account_time |
|---|---|---|---|
| 1. global_balance (sum of debits = sum of credits) | 144 ms | 150 ms | 179 ms |
| 2. transfer_balance (every transfer balances) | 709 ms | 1,428 ms | 638 ms |
| 3+4. stored_balance and overdraft (one pass over accounts + ledger) | 616 ms | 597 ms | 610 ms |
| 5a. orphan_transfer (completed transfer with no entries) | 257 ms | 237 ms | 256 ms |
| 5b. orphan_entry (entry with no transfer) | 195 ms | 235 ms | 209 ms |
| 6. transfer_amount (entries add up to the transfer amount) | 1,062 ms | 2,127 ms | 1,080 ms |

## 1. global_balance (sum of debits = sum of credits)

### as-is: 144.2 ms

```text
Finalize Aggregate  (cost=18737.58..18737.59 rows=1 width=16) (actual time=137.468..144.148 rows=1 loops=1)
  Buffers: shared hit=2576 read=6828
  ->  Gather  (cost=18737.34..18737.55 rows=2 width=64) (actual time=137.228..144.136 rows=3 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=2576 read=6828
        ->  Partial Aggregate  (cost=17737.34..17737.35 rows=1 width=64) (actual time=107.832..107.832 rows=1 loops=3)
              Buffers: shared hit=2576 read=6828
              ->  Parallel Seq Scan on ledger_entries  (cost=0.00..13570.67 rows=416667 width=14) (actual time=0.629..28.975 rows=333333 loops=3)
                    Buffers: shared hit=2576 read=6828
Planning Time: 0.080 ms
Execution Time: 144.177 ms
```

### without idx_ledger_transfer: 149.7 ms

```text
Finalize Aggregate  (cost=18737.58..18737.59 rows=1 width=16) (actual time=142.215..149.640 rows=1 loops=1)
  Buffers: shared hit=2768 read=6636
  ->  Gather  (cost=18737.34..18737.55 rows=2 width=64) (actual time=142.036..149.629 rows=3 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=2768 read=6636
        ->  Partial Aggregate  (cost=17737.34..17737.35 rows=1 width=64) (actual time=114.882..114.883 rows=1 loops=3)
              Buffers: shared hit=2768 read=6636
              ->  Parallel Seq Scan on ledger_entries  (cost=0.00..13570.67 rows=416667 width=14) (actual time=0.628..31.558 rows=333333 loops=3)
                    Buffers: shared hit=2768 read=6636
Planning:
  Buffers: shared hit=4
Planning Time: 0.852 ms
Execution Time: 149.680 ms
```

### without idx_ledger_account_time: 179.2 ms

```text
Finalize Aggregate  (cost=18737.58..18737.59 rows=1 width=16) (actual time=170.687..179.194 rows=1 loops=1)
  Buffers: shared hit=2960 read=6444
  ->  Gather  (cost=18737.34..18737.55 rows=2 width=64) (actual time=170.502..179.182 rows=3 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=2960 read=6444
        ->  Partial Aggregate  (cost=17737.34..17737.35 rows=1 width=64) (actual time=108.850..108.851 rows=1 loops=3)
              Buffers: shared hit=2960 read=6444
              ->  Parallel Seq Scan on ledger_entries  (cost=0.00..13570.67 rows=416667 width=14) (actual time=0.264..30.345 rows=333333 loops=3)
                    Buffers: shared hit=2960 read=6444
Planning:
  Buffers: shared hit=4
Planning Time: 1.040 ms
Execution Time: 179.231 ms
```

## 2. transfer_balance (every transfer balances)

### as-is: 709.4 ms

```text
GroupAggregate  (cost=0.42..58091.86 rows=499755 width=24) (actual time=709.336..709.336 rows=0 loops=1)
  Group Key: transfer_id
  Filter: (COALESCE(sum(amount) FILTER (WHERE ((direction)::text = 'debit'::text)), '0'::numeric) <> COALESCE(sum(amount) FILTER (WHERE ((direction)::text = 'credit'::text)), '0'::numeric))
  Rows Removed by Filter: 500000
  Buffers: shared hit=7006 read=4805 written=2032
  ->  Index Scan using idx_ledger_transfer on ledger_entries  (cost=0.42..34303.43 rows=1000000 width=22) (actual time=0.186..188.391 rows=1000000 loops=1)
        Buffers: shared hit=7006 read=4805 written=2032
Planning Time: 0.185 ms
Execution Time: 709.377 ms
```

### without idx_ledger_transfer: 1,427.7 ms

```text
HashAggregate  (cost=98779.00..121786.18 rows=499755 width=24) (actual time=1411.083..1411.084 rows=0 loops=1)
  Group Key: transfer_id
  Filter: (COALESCE(sum(amount) FILTER (WHERE ((direction)::text = 'debit'::text)), '0'::numeric) <> COALESCE(sum(amount) FILTER (WHERE ((direction)::text = 'credit'::text)), '0'::numeric))
  Planned Partitions: 32  Batches: 161  Memory Usage: 8249kB  Disk Usage: 62816kB
  Rows Removed by Filter: 500000
  Buffers: shared hit=9378 read=26, temp read=7234 written=14856
  ->  Seq Scan on ledger_entries  (cost=0.00..19404.00 rows=1000000 width=22) (actual time=0.023..55.492 rows=1000000 loops=1)
        Buffers: shared hit=9378 read=26
Planning:
  Buffers: shared hit=4
Planning Time: 1.574 ms
Execution Time: 1427.657 ms
```

### without idx_ledger_account_time: 637.6 ms

```text
GroupAggregate  (cost=0.42..58091.86 rows=499755 width=24) (actual time=637.604..637.605 rows=0 loops=1)
  Group Key: transfer_id
  Filter: (COALESCE(sum(amount) FILTER (WHERE ((direction)::text = 'debit'::text)), '0'::numeric) <> COALESCE(sum(amount) FILTER (WHERE ((direction)::text = 'credit'::text)), '0'::numeric))
  Rows Removed by Filter: 500000
  Buffers: shared hit=11811
  ->  Index Scan using idx_ledger_transfer on ledger_entries  (cost=0.42..34303.43 rows=1000000 width=22) (actual time=0.016..130.786 rows=1000000 loops=1)
        Buffers: shared hit=11811
Planning:
  Buffers: shared hit=4
Planning Time: 1.125 ms
Execution Time: 637.643 ms
```

## 3+4. stored_balance and overdraft (one pass over accounts + ledger)

### as-is: 615.6 ms

```text
HashAggregate  (cost=32125.62..32155.66 rows=2003 width=35) (actual time=614.778..615.354 rows=2003 loops=1)
  Group Key: a.id
  Batches: 1  Memory Usage: 1137kB
  Buffers: shared hit=9450
  ->  Hash Right Join  (cost=91.07..22125.62 rows=1000000 width=41) (actual time=0.564..331.658 rows=1000001 loops=1)
        Hash Cond: (le.account_id = a.id)
        Buffers: shared hit=9450
        ->  Seq Scan on ledger_entries le  (cost=0.00..19404.00 rows=1000000 width=22) (actual time=0.005..53.988 rows=1000000 loops=1)
              Buffers: shared hit=9404
        ->  Hash  (cost=66.03..66.03 rows=2003 width=27) (actual time=0.552..0.552 rows=2003 loops=1)
              Buckets: 2048  Batches: 1  Memory Usage: 142kB
              Buffers: shared hit=46
              ->  Seq Scan on accounts a  (cost=0.00..66.03 rows=2003 width=27) (actual time=0.009..0.295 rows=2003 loops=1)
                    Buffers: shared hit=46
Planning:
  Buffers: shared hit=19
Planning Time: 0.226 ms
Execution Time: 615.569 ms
```

### without idx_ledger_transfer: 596.9 ms

```text
HashAggregate  (cost=32125.62..32155.66 rows=2003 width=35) (actual time=596.012..596.618 rows=2003 loops=1)
  Group Key: a.id
  Batches: 1  Memory Usage: 1137kB
  Buffers: shared hit=9450
  ->  Hash Right Join  (cost=91.07..22125.62 rows=1000000 width=41) (actual time=0.570..324.109 rows=1000001 loops=1)
        Hash Cond: (le.account_id = a.id)
        Buffers: shared hit=9450
        ->  Seq Scan on ledger_entries le  (cost=0.00..19404.00 rows=1000000 width=22) (actual time=0.005..52.414 rows=1000000 loops=1)
              Buffers: shared hit=9404
        ->  Hash  (cost=66.03..66.03 rows=2003 width=27) (actual time=0.559..0.559 rows=2003 loops=1)
              Buckets: 2048  Batches: 1  Memory Usage: 142kB
              Buffers: shared hit=46
              ->  Seq Scan on accounts a  (cost=0.00..66.03 rows=2003 width=27) (actual time=0.010..0.294 rows=2003 loops=1)
                    Buffers: shared hit=46
Planning:
  Buffers: shared hit=25
Planning Time: 1.234 ms
Execution Time: 596.851 ms
```

### without idx_ledger_account_time: 610.3 ms

```text
HashAggregate  (cost=32125.62..32155.66 rows=2003 width=35) (actual time=609.457..610.047 rows=2003 loops=1)
  Group Key: a.id
  Batches: 1  Memory Usage: 1137kB
  Buffers: shared hit=9450
  ->  Hash Right Join  (cost=91.07..22125.62 rows=1000000 width=41) (actual time=0.624..331.738 rows=1000001 loops=1)
        Hash Cond: (le.account_id = a.id)
        Buffers: shared hit=9450
        ->  Seq Scan on ledger_entries le  (cost=0.00..19404.00 rows=1000000 width=22) (actual time=0.006..53.262 rows=1000000 loops=1)
              Buffers: shared hit=9404
        ->  Hash  (cost=66.03..66.03 rows=2003 width=27) (actual time=0.608..0.608 rows=2003 loops=1)
              Buckets: 2048  Batches: 1  Memory Usage: 142kB
              Buffers: shared hit=46
              ->  Seq Scan on accounts a  (cost=0.00..66.03 rows=2003 width=27) (actual time=0.017..0.323 rows=2003 loops=1)
                    Buffers: shared hit=46
Planning:
  Buffers: shared hit=17
Planning Time: 1.149 ms
Execution Time: 610.304 ms
```

## 5a. orphan_transfer (completed transfer with no entries)

### as-is: 257.2 ms

```text
Gather  (cost=13640.33..32940.27 rows=1 width=8) (actual time=248.767..257.197 rows=0 loops=1)
  Workers Planned: 2
  Workers Launched: 2
  Buffers: shared hit=9805 read=7187
  ->  Parallel Hash Right Anti Join  (cost=12640.33..31940.17 rows=1 width=8) (actual time=200.501..200.503 rows=0 loops=3)
        Hash Cond: (le.transfer_id = t.id)
        Buffers: shared hit=9805 read=7187
        ->  Parallel Seq Scan on ledger_entries le  (cost=0.00..13570.67 rows=416667 width=8) (actual time=0.007..22.309 rows=333333 loops=3)
              Buffers: shared hit=9404
        ->  Parallel Hash  (cost=10036.17..10036.17 rows=208333 width=8) (actual time=81.378..81.379 rows=166667 loops=3)
              Buckets: 524288  Batches: 1  Memory Usage: 23712kB
              Buffers: shared hit=245 read=7187
              ->  Parallel Seq Scan on transfers t  (cost=0.00..10036.17 rows=208333 width=8) (actual time=0.727..43.019 rows=166667 loops=3)
                    Filter: ((status)::text = 'completed'::text)
                    Buffers: shared hit=245 read=7187
Planning:
  Buffers: shared hit=17
Planning Time: 0.247 ms
Execution Time: 257.228 ms
```

### without idx_ledger_transfer: 237.4 ms

```text
Gather  (cost=13640.33..32940.27 rows=1 width=8) (actual time=228.239..237.413 rows=0 loops=1)
  Workers Planned: 2
  Workers Launched: 2
  Buffers: shared hit=9997 read=6995
  ->  Parallel Hash Right Anti Join  (cost=12640.33..31940.17 rows=1 width=8) (actual time=181.397..181.400 rows=0 loops=3)
        Hash Cond: (le.transfer_id = t.id)
        Buffers: shared hit=9997 read=6995
        ->  Parallel Seq Scan on ledger_entries le  (cost=0.00..13570.67 rows=416667 width=8) (actual time=0.011..20.986 rows=333333 loops=3)
              Buffers: shared hit=9404
        ->  Parallel Hash  (cost=10036.17..10036.17 rows=208333 width=8) (actual time=71.088..71.089 rows=166667 loops=3)
              Buckets: 524288  Batches: 1  Memory Usage: 23712kB
              Buffers: shared hit=437 read=6995
              ->  Parallel Seq Scan on transfers t  (cost=0.00..10036.17 rows=208333 width=8) (actual time=0.488..37.236 rows=166667 loops=3)
                    Filter: ((status)::text = 'completed'::text)
                    Buffers: shared hit=437 read=6995
Planning:
  Buffers: shared hit=15
Planning Time: 1.086 ms
Execution Time: 237.441 ms
```

### without idx_ledger_account_time: 256.3 ms

```text
Gather  (cost=13640.33..32940.27 rows=1 width=8) (actual time=247.932..256.264 rows=0 loops=1)
  Workers Planned: 2
  Workers Launched: 2
  Buffers: shared hit=10189 read=6803
  ->  Parallel Hash Right Anti Join  (cost=12640.33..31940.17 rows=1 width=8) (actual time=200.418..200.422 rows=0 loops=3)
        Hash Cond: (le.transfer_id = t.id)
        Buffers: shared hit=10189 read=6803
        ->  Parallel Seq Scan on ledger_entries le  (cost=0.00..13570.67 rows=416667 width=8) (actual time=0.007..22.516 rows=333333 loops=3)
              Buffers: shared hit=9404
        ->  Parallel Hash  (cost=10036.17..10036.17 rows=208333 width=8) (actual time=79.199..79.200 rows=166667 loops=3)
              Buckets: 524288  Batches: 1  Memory Usage: 23712kB
              Buffers: shared hit=629 read=6803
              ->  Parallel Seq Scan on transfers t  (cost=0.00..10036.17 rows=208333 width=8) (actual time=0.685..41.631 rows=166667 loops=3)
                    Filter: ((status)::text = 'completed'::text)
                    Buffers: shared hit=629 read=6803
Planning:
  Buffers: shared hit=23
Planning Time: 1.187 ms
Execution Time: 256.287 ms
```

## 5b. orphan_entry (entry with no transfer)

### as-is: 194.5 ms

```text
Gather  (cost=13119.50..28269.45 rows=1 width=16) (actual time=186.141..194.498 rows=0 loops=1)
  Workers Planned: 2
  Workers Launched: 2
  Buffers: shared hit=10381 read=6611
  ->  Parallel Hash Anti Join  (cost=12119.50..27269.35 rows=1 width=16) (actual time=158.184..158.186 rows=0 loops=3)
        Hash Cond: (le.transfer_id = t.id)
        Buffers: shared hit=10381 read=6611
        ->  Parallel Seq Scan on ledger_entries le  (cost=0.00..13570.67 rows=416667 width=16) (actual time=0.006..20.572 rows=333333 loops=3)
              Buffers: shared hit=9404
        ->  Parallel Hash  (cost=9515.33..9515.33 rows=208333 width=8) (actual time=59.842..59.842 rows=166667 loops=3)
              Buckets: 524288  Batches: 1  Memory Usage: 23712kB
              Buffers: shared hit=821 read=6611
              ->  Parallel Seq Scan on transfers t  (cost=0.00..9515.33 rows=208333 width=8) (actual time=0.603..25.150 rows=166667 loops=3)
                    Buffers: shared hit=821 read=6611
Planning:
  Buffers: shared hit=17
Planning Time: 0.211 ms
Execution Time: 194.523 ms
```

### without idx_ledger_transfer: 234.8 ms

```text
Gather  (cost=13119.50..28269.45 rows=1 width=16) (actual time=226.321..234.726 rows=0 loops=1)
  Workers Planned: 2
  Workers Launched: 2
  Buffers: shared hit=10573 read=6419
  ->  Parallel Hash Anti Join  (cost=12119.50..27269.35 rows=1 width=16) (actual time=163.698..163.701 rows=0 loops=3)
        Hash Cond: (le.transfer_id = t.id)
        Buffers: shared hit=10573 read=6419
        ->  Parallel Seq Scan on ledger_entries le  (cost=0.00..13570.67 rows=416667 width=16) (actual time=0.008..21.926 rows=333333 loops=3)
              Buffers: shared hit=9404
        ->  Parallel Hash  (cost=9515.33..9515.33 rows=208333 width=8) (actual time=56.644..56.645 rows=166667 loops=3)
              Buckets: 524288  Batches: 1  Memory Usage: 23680kB
              Buffers: shared hit=1013 read=6419
              ->  Parallel Seq Scan on transfers t  (cost=0.00..9515.33 rows=208333 width=8) (actual time=0.640..33.520 rows=250000 loops=2)
                    Buffers: shared hit=1013 read=6419
Planning:
  Buffers: shared hit=15
Planning Time: 1.329 ms
Execution Time: 234.756 ms
```

### without idx_ledger_account_time: 208.8 ms

```text
Gather  (cost=13119.50..28269.45 rows=1 width=16) (actual time=198.625..208.746 rows=0 loops=1)
  Workers Planned: 2
  Workers Launched: 2
  Buffers: shared hit=10733 read=6259
  ->  Parallel Hash Anti Join  (cost=12119.50..27269.35 rows=1 width=16) (actual time=168.674..168.677 rows=0 loops=3)
        Hash Cond: (le.transfer_id = t.id)
        Buffers: shared hit=10733 read=6259
        ->  Parallel Seq Scan on ledger_entries le  (cost=0.00..13570.67 rows=416667 width=16) (actual time=0.007..22.638 rows=333333 loops=3)
              Buffers: shared hit=9404
        ->  Parallel Hash  (cost=9515.33..9515.33 rows=208333 width=8) (actual time=59.299..59.300 rows=166667 loops=3)
              Buckets: 524288  Batches: 1  Memory Usage: 23712kB
              Buffers: shared hit=1173 read=6259
              ->  Parallel Seq Scan on transfers t  (cost=0.00..9515.33 rows=208333 width=8) (actual time=0.451..24.166 rows=166667 loops=3)
                    Buffers: shared hit=1173 read=6259
Planning:
  Buffers: shared hit=23
Planning Time: 1.463 ms
Execution Time: 208.774 ms
```

## 6. transfer_amount (entries add up to the transfer amount)

### as-is: 1,062.0 ms

```text
Merge Join  (cost=0.85..93696.41 rows=502266 width=32) (actual time=1061.969..1061.969 rows=0 loops=1)
  Merge Cond: (ledger_entries.transfer_id = t.id)
  Join Filter: ((((COALESCE(sum(ledger_entries.amount) FILTER (WHERE ((ledger_entries.direction)::text = 'debit'::text)), '0'::numeric))::bigint) <> t.amount) OR (((COALESCE(sum(ledger_entries.amount) FILTER (WHERE ((ledger_entries.direction)::text = 'credit'::text)), '0'::numeric))::bigint) <> t.amount))
  Rows Removed by Join Filter: 500000
  Buffers: shared hit=6264 read=14312 written=19
  ->  GroupAggregate  (cost=0.42..56848.75 rows=502266 width=24) (actual time=0.025..768.168 rows=500000 loops=1)
        Group Key: ledger_entries.transfer_id
        Buffers: shared hit=5053 read=6758 written=16
        ->  Index Scan using idx_ledger_transfer on ledger_entries  (cost=0.42..34303.43 rows=1000000 width=22) (actual time=0.016..178.472 rows=1000000 loops=1)
              Buffers: shared hit=5053 read=6758 written=16
  ->  Index Scan using transfers_pkey on transfers t  (cost=0.42..21681.42 rows=500000 width=16) (actual time=0.045..170.563 rows=500000 loops=1)
        Filter: ((status)::text = 'completed'::text)
        Buffers: shared hit=1211 read=7554 written=3
Planning:
  Buffers: shared hit=1
Planning Time: 0.206 ms
Execution Time: 1062.000 ms
```

### without idx_ledger_transfer: 2,126.8 ms

```text
Hash Join  (cost=121153.00..157586.19 rows=502266 width=32) (actual time=2110.886..2110.890 rows=0 loops=1)
  Hash Cond: (e.transfer_id = t.id)
  Join Filter: ((e.debits <> t.amount) OR (e.credits <> t.amount))
  Rows Removed by Join Filter: 500000
  Buffers: shared hit=13411 read=3425, temp read=10900 written=18522
  ->  Subquery Scan on e  (cost=98779.00..125565.73 rows=502266 width=24) (actual time=580.737..1493.802 rows=500000 loops=1)
        Buffers: shared hit=5983 read=3421, temp read=7234 written=14856
        ->  HashAggregate  (cost=98779.00..120543.07 rows=502266 width=24) (actual time=580.735..1457.687 rows=500000 loops=1)
              Group Key: ledger_entries.transfer_id
              Planned Partitions: 32  Batches: 161  Memory Usage: 8249kB  Disk Usage: 62816kB
              Buffers: shared hit=5983 read=3421, temp read=7234 written=14856
              ->  Seq Scan on ledger_entries  (cost=0.00..19404.00 rows=1000000 width=22) (actual time=0.037..73.041 rows=1000000 loops=1)
                    Buffers: shared hit=5983 read=3421
  ->  Hash  (cost=13682.00..13682.00 rows=500000 width=16) (actual time=204.957..204.958 rows=500000 loops=1)
        Buckets: 262144  Batches: 4  Memory Usage: 7904kB
        Buffers: shared hit=7428 read=4, temp written=1647
        ->  Seq Scan on transfers t  (cost=0.00..13682.00 rows=500000 width=16) (actual time=0.040..109.998 rows=500000 loops=1)
              Filter: ((status)::text = 'completed'::text)
              Buffers: shared hit=7428 read=4
Planning:
  Buffers: shared hit=5
Planning Time: 1.061 ms
Execution Time: 2126.818 ms
```

### without idx_ledger_account_time: 1,080.3 ms

```text
Merge Join  (cost=0.85..93696.41 rows=502266 width=32) (actual time=1080.282..1080.283 rows=0 loops=1)
  Merge Cond: (ledger_entries.transfer_id = t.id)
  Join Filter: ((((COALESCE(sum(ledger_entries.amount) FILTER (WHERE ((ledger_entries.direction)::text = 'debit'::text)), '0'::numeric))::bigint) <> t.amount) OR (((COALESCE(sum(ledger_entries.amount) FILTER (WHERE ((ledger_entries.direction)::text = 'credit'::text)), '0'::numeric))::bigint) <> t.amount))
  Rows Removed by Join Filter: 500000
  Buffers: shared hit=1 read=20575
  ->  GroupAggregate  (cost=0.42..56848.75 rows=502266 width=24) (actual time=0.074..787.249 rows=500000 loops=1)
        Group Key: ledger_entries.transfer_id
        Buffers: shared read=11811
        ->  Index Scan using idx_ledger_transfer on ledger_entries  (cost=0.42..34303.43 rows=1000000 width=22) (actual time=0.066..201.171 rows=1000000 loops=1)
              Buffers: shared read=11811
  ->  Index Scan using transfers_pkey on transfers t  (cost=0.42..21681.42 rows=500000 width=16) (actual time=0.040..171.428 rows=500000 loops=1)
        Filter: ((status)::text = 'completed'::text)
        Buffers: shared hit=1 read=8764
Planning:
  Buffers: shared hit=5
Planning Time: 1.055 ms
Execution Time: 1080.313 ms
```
