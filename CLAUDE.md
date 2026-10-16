# Digital Wallet Backend (course assignment)

UPI-style wallet backend. PostgreSQL 15+, Python 3.12 + FastAPI, psycopg 3 with raw SQL (no ORM), Docker Compose (`db`, `api`, `worker`).

The submission deadline has already passed. Optimise for a working, tested, submittable repo as fast as possible. Another assignment is due this Saturday.

## Read first
1. `docs/assignment-brief.md` is the spec. `docs/brief-tables.md` holds three tables that were screenshots in the original.
2. `docs/reference-plan.md` is the approved design: noun list, ER diagram, locking flow, idempotency flow, reconciliation SQL, fraud rules, Q&A design, build order. Do not re-litigate it; follow it.
3. `migrations/001_init.sql` is the schema. It builds cleanly on PostgreSQL 16 and its constraints and triggers were smoke-tested.

## Non-negotiables (from the brief)
- Money is `BIGINT` paise. Never float.
- Keep the money path visible: raw SQL, explicit transaction boundaries, explicit `SELECT ... FOR UPDATE` in ascending `account_id` order.
- `docker compose up` starts everything. Migrations build the schema from an empty database.
- Every POST needs an `Idempotency-Key` header (400 if missing).
- One error shape everywhere: `{"code": "...", "message": "...", "request_id": "..."}`.
- Ledger entries are never updated or deleted.

## Decisions already made
- Stored `accounts.balance` plus the ledger, reconciled nightly. Credit increases balance, debit decreases it.
- Positive `amount` plus `direction` on `ledger_entries`.
- Transfers run at Read Committed with row locks. Reconciliation runs at Repeatable Read.
- Held transfers: `status='pending_review'`, no ledger entries, no balance change. Release writes the entries. The admin decision "reverse" means `rejected` (nothing to undo).
- Idempotency: the first statement in the transfer transaction is `INSERT ... ON CONFLICT (user_id, key) DO NOTHING`. A failed request rolls back its key too, so a retry is re-evaluated. Callers with no user (`POST /users`, admin POSTs) use the partial unique index on `key WHERE user_id IS NULL`.
- Reconciliation: `POST /admin/reconciliation/run` returns 202 and inserts a `running` row. `FOR UPDATE NOWAIT` on `job_locks`; if the lock is held, mark the run `skipped`. Report root causes, not raw rows, via `group_key`.
- Auth is not specified by the brief: `X-User-Id` header for user endpoints, `X-Admin-Token` (env var) for `/admin/*`.
- Fraud rules (thresholds live in the `risk_rules` table): velocity, amount spike, new payee. Rules skip top-ups. No 30-day history means the spike rule does not fire.
- LLM sits behind one small interface with a mock for tests. The LLM never writes SQL; it only returns JSON that the code validates, then runs its own parameterised query scoped to the caller's accounts.

## Open items that need the user
- The Stretch Milestone spec (sharding and consistent hashing) was not in the pasted brief. Ask for it, or build the small prototype described in `docs/reference-plan.md` section 14.
- LLM provider: free tier (Gemini, Groq or local Ollama). `LLM_PROVIDER=mock` for tests.
- The 5-minute demo video is the user's to record. Prepare the script (reference plan section 16).
- The user prefers plan-first work. The plan is approved, so start building. If you must ask questions, send them in one batch.

## Build order (finish and test each step before the next)
1. Compose + Dockerfile + FastAPI skeleton + migration on start + health check + error shape + request_id middleware.
2. Users, wallets, top-up, balance, statement. Then the deliberately unsafe transfer (for the "break it first" record), then the safe locked transfer.
3. **Stampede test**: ₹500 wallet, 100 concurrent ₹10 transfers with distinct keys. Exactly 50 succeed, final balance 0, debits equal credits. Loop 20 times.
4. Idempotency plus tests: 5 sequential, 10 parallel, different body gives 422, missing header gives 400.
5. Reversal, `GET /transfers/{id}`, audit history.
6. Reconciliation job, worker scheduler, the three-corruption test (`scripts/corrupt.py`).
7. Fraud rules, risk queue and decision endpoints, held-transfer flow.
8. LLM interface, mock, Q&A endpoint, 10 sample questions, prompt-injection test, nightly flag-explanation step.
9. `scripts/seed.py` (1,000 users, 2,000 wallets, 100,000 transfers via COPY or batch inserts, internally consistent).
10. Measurements: `EXPLAIN ANALYZE` on the checks at 1M entries, OFFSET 100000 vs cursor pagination, fraud-rule overhead.
11. `docs/isolation.md` lab (four cases), `docs/schema.md`, README answers, CI workflow, stretch prototype.

## Working agreements
- Run real tests against a real Postgres (the compose `db` service). Do not mock the database.
- Every number in the README (timings, plans, overhead) must come from an actual run. Paste real output; never write expected output as if it were measured.
- `docs/isolation.md` needs real two-session `psql` output. Scripting it with two connections and capturing the output is fine.
- Do not weaken the ledger triggers to make a test pass. The corruption test disables them for one session only, with `SET session_replication_role = replica` as superuser.
- Commit after each build step with a clear message.
