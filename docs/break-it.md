# Milestone 2, Step 2: breaking it first

`scripts/break_it.py` implements the naive transfer the brief describes: read the balance,
check it in code, then write, with no lock, at the default Read Committed level. It was run
against PostgreSQL 17.5 with 100 threads, each firing one Rs 10 transfer out of a Rs 500 wallet.

Command: `python -m scripts.break_it`. Output below is copied from three real runs:

```
--- run 1
requests that reported success      : 100   (correct answer: 50)
transfer rows actually recorded      : 100
source wallet stored balance (paise) : 41000   (correct answer: 0)
source wallet balance from ledger    : -50000   (the ledger always tells the truth)
destination stored balance (paise)   : 9000   (ledger says 100000)
LOST UPDATE DETECTED: stored balance != ledger
--- run 2
requests that reported success      : 100   (correct answer: 50)
transfer rows actually recorded      : 100
source wallet stored balance (paise) : 42000   (correct answer: 0)
source wallet balance from ledger    : -50000   (the ledger always tells the truth)
destination stored balance (paise)   : 8000   (ledger says 100000)
LOST UPDATE DETECTED: stored balance != ledger
--- run 3
requests that reported success      : 100   (correct answer: 50)
transfer rows actually recorded      : 100
source wallet stored balance (paise) : 43000   (correct answer: 0)
source wallet balance from ledger    : -50000   (the ledger always tells the truth)
destination stored balance (paise)   : 10000   (ledger says 100000)
LOST UPDATE DETECTED: stored balance != ledger
```

## What went wrong

* **All 100 transfers passed the balance check.** Every thread read the balance (Rs 500) before
  any other thread wrote, so each believed there was enough money. 100 x Rs 10 left the wallet,
  twice what it held. The ledger shows -Rs 500: a real overdraft.
* **The stored balance is wrong too.** Each thread wrote `balance = <stale read> - 1000`, so the
  writes overwrite each other (a lost update). The stored balance ended around Rs 410 while the
  ledger says -Rs 500. The destination shows about Rs 90 although Rs 1,000 was received.
* **The database CHECK did not catch it.** `balance >= 0` only sees the (wrong) value being
  written, which is positive. A constraint cannot detect that the number is stale.
* **The deferred balanced-ledger trigger did not catch it either.** Each transfer is balanced on
  its own; the harm is in the shared state across transfers. The nightly reconciliation (check 3
  stored vs computed, check 4 overdraft) is exactly what would flag this.

## The fix

`app/services/transfer.py` locks both account rows with `SELECT ... FOR UPDATE` in ascending id
order, re-reads the balance under the lock, then writes. `tests/test_stampede.py` runs the same
100 x Rs 10 scenario against the real API 20 times in a row: exactly 50 succeed, the wallet ends
at 0, and debits equal credits every time. A second test fires 50 A->B and 50 B->A transfers at
once to show the fixed lock order prevents deadlocks.
