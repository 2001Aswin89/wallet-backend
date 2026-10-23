# Stretch: sharding and consistent hashing

The stretch milestone's own text was not in the brief I was given, so this is a documented prototype with a
runnable demo, based on the learning objective ("sharding and consistent hashing"), not a migration of the app.
If the original spec asks for something specific, check it against this.

* `sharding/hash_ring.py` - the ring (about 40 lines, `bisect`, MD5 positions, virtual nodes).
* `python -m scripts.hash_ring_demo` - prints and writes [sharding-demo.md](sharding-demo.md) with real numbers.
* `tests/test_hash_ring.py` - stable routing, spread, and "adding a shard moves about 1/N of the keys, all to the new shard".

## Design

**Shard key: `user_id`.** A user's wallets, and every ledger entry on those wallets, live on one shard. A balance and
a statement are then single-shard queries, and a peer transfer between two wallets of the same shard is the same
single local transaction as today, with the same row locks.

**Router: a consistent-hash ring with virtual nodes.** Each shard is placed on the ring 100+ times; a user goes to the
first virtual node clockwise from `hash(user_id)`. Adding a shard takes over only the segments just before its
virtual nodes, so about 1/N of the users move, versus almost all of them with `user_id % N`
(measured: 20.1% against 80.0%, see [sharding-demo.md](sharding-demo.md)). With one virtual node per shard the
split is luck-dependent; the demo shows the spread tightening as the count grows (the 10-vnode row is simply an unlucky
placement), which is why the ring uses 100.

**System accounts.** CASH_IN, CASH_OUT and FEE_REVENUE are global today, which would make them cross-shard rows touched
by every top-up. Give each shard its own copy of each system account; "global" figures are the sum across shards.
This also fixes the hot-row problem described in the README, because top-ups no longer all lock one row.

## The hard problem: a transfer between users on different shards

One database transaction can no longer cover both rows, so the "lock both rows, write both entries, commit" step
needs a different shape. Two options:

1. **Two-phase commit** (`PREPARE TRANSACTION` on both shards, then `COMMIT PREPARED`). Gives atomicity, but a
   coordinator crash between phases leaves prepared transactions holding locks until recovered, and it couples the
   availability of both shards to every cross-shard transfer.
2. **A saga with an in-flight state** (what I would build). Debit the sender on shard A and record the transfer as
   `in_flight` in the same local transaction (money is now neither lost nor duplicated: it sits in a per-shard
   "in transit" system account). Then credit the receiver on shard B, idempotently keyed by the transfer id. If B
   rejects it, run the compensating step on A: a **reversal transfer**, which this ledger already supports because
   entries are never edited. Each step is a local, locked, balanced transaction, and a recovery job finishes or
   reverses anything stuck `in_flight`.

Idempotency keys are per user, so they live on the user's shard and the saga steps use the transfer id as their key.

## What I would do next

* **Cross-shard reconciliation:** each shard's books balance on their own; the in-transit accounts across all shards
  must sum to zero, which is a new check.
* **Resharding:** moving a ring segment means copying those users' accounts, transfers and entries, then flipping the
  route, with the ledger's append-only property making "copy then verify the checksum" safe.
* **Hot shards:** a single very busy user cannot be split by `user_id`; that needs splitting their wallets or queueing.
* **Lookup by wallet id:** the API addresses wallets by id, so the id needs to carry the shard (for example a prefix)
  or a small directory table is needed.
