# 5-minute demo script

The brief wants one video showing: a transfer end to end, a retry, a held transfer, a reconciliation run and a Q&A
answer. Recording it is yours to do. This is the order and the commands, tuned to about five minutes.

**Before recording** (not on camera):

```bash
docker compose up --build        # db + api + worker; wait for "Application startup complete"
```

Open a second terminal. Use `curl` (or Swagger at http://localhost:8000/docs; set the headers there). Admin token is
`change-me` unless you changed `.env.example`.

Handy shell variables:

```bash
API=http://localhost:8000
key() { python -c "import uuid;print(uuid.uuid4())"; }
```

## 0:00 Setup: two users, wallets, money in (45 s)

Say: "Money is whole paise; every POST needs an Idempotency-Key."

```bash
curl -s $API/users -H 'content-type: application/json' -H "Idempotency-Key: $(key)" \
  -d '{"name":"Asha Kapoor","email":"asha@example.com","phone":"+919800000001","kyc_status":"verified"}'
curl -s $API/users -H 'content-type: application/json' -H "Idempotency-Key: $(key)" \
  -d '{"name":"Rahul Sharma","email":"rahul@example.com","phone":"+919800000002","kyc_status":"verified"}'
curl -s $API/users/1/wallets -H 'content-type: application/json' -H 'X-User-Id: 1' -H "Idempotency-Key: $(key)" -d '{"label":"main"}'
curl -s $API/users/2/wallets -H 'content-type: application/json' -H 'X-User-Id: 2' -H "Idempotency-Key: $(key)" -d '{"label":"main"}'
curl -s $API/transfers/topup -H 'content-type: application/json' -H 'X-User-Id: 1' -H "Idempotency-Key: $(key)" \
  -d '{"wallet_id":4,"amount":5000000}'
```

(Wallet ids start at 4 because ids 1-3 are the system accounts. Adjust if your database is not fresh.)

## 0:45 One transfer end to end (60 s)

```bash
K=$(key)
curl -s $API/transfers -H 'content-type: application/json' -H 'X-User-Id: 1' -H "Idempotency-Key: $K" \
  -d '{"from_wallet_id":4,"to_wallet_id":5,"amount":50000,"note":"dinner"}'
curl -s $API/transfers/<transfer_id> -H 'X-User-Id: 1'          # entries: one debit, one credit; status history
curl -s $API/wallets/4/balance -H 'X-User-Id: 1'
```

Point at: the two ledger entries of equal amount, the status-history row written by the database trigger.

## 1:45 One retry (45 s)

Re-run the same curl with the same `$K`. Say: "Same key, same body: the saved response comes back byte for byte and
no second transfer exists." Then change the amount and keep `$K`: **422 IDEMPOTENCY_KEY_REUSED**. Show the balance has not moved.

## 2:30 One held transfer (60 s)

Fire six small transfers quickly (a loop of the command above with fresh keys). The sixth returns **202** with
`"status":"pending_review"` and `"flags":["VELOCITY"]`. Show the balance did not change, then:

```bash
curl -s $API/admin/risk/queue -H 'X-Admin-Token: change-me'
curl -s $API/admin/risk/<transfer_id>/decision -H 'content-type: application/json' -H 'X-Admin-Token: change-me' \
  -H "Idempotency-Key: $(key)" -d '{"decision":"release"}'
```

Say: "A held transfer moves no money. Release re-checks the balance, writes the entries, and the audit log records `admin:1`."

## 3:30 One reconciliation run (45 s)

```bash
curl -s -X POST $API/admin/reconciliation/run -H 'X-Admin-Token: change-me' -H "Idempotency-Key: $(key)"   # 202
curl -s $API/admin/reconciliation/runs/<run_id> -H 'X-Admin-Token: change-me'                               # passed, 0 issues
```

Optional, for effect (needs psql and the docker db): corrupt a balance with
`docker compose exec db psql -U wallet -c "UPDATE accounts SET balance = balance + 777 WHERE id = 4"`
and run it again: **failed**, one root cause, `stored_balance`, expected vs actual. Mention it reports and never fixes.

## 4:15 One Q&A answer (45 s)

The default mock LLM understands its fixed sample questions and one relative pattern, "How much did I send to
&lt;name&gt; this month?" (also "last month"). Set `LLM_PROVIDER` and a key in `.env.example` for free-form questions.

```bash
curl -s $API/users/1/statement/ask -H 'content-type: application/json' -H 'X-User-Id: 1' -H "Idempotency-Key: $(key)" \
  -d '{"question":"How much did I send to Rahul this month?"}'
```

You should see the total from this demo plus the transfer ids it came from. Say: "The model only turns the question into JSON. My code validates it and runs its own parameterised query limited to
my wallets; the answer lists the transfer ids it came from. The model never writes SQL."

## 5:00 Close

"Stampede, idempotency, reconciliation and injection tests all pass; the README has the design answers and measurements."
