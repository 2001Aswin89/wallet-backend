# Tables missing from the pasted brief (transcribed from the screenshots)

These three tables were images in the original assignment page. They sit alongside `assignment-brief.md`.

## Milestone 3 — How the server responds
| Situation | Server does | Response |
|---|---|---|
| New key | Inserts the key and runs the transfer in one transaction, saves the response, commits | 201 Created |
| Key seen, same body, completed | Returns the saved response exactly, including the original transfer_id | Original status |
| Key seen, same body, still running | Tells the client to retry shortly | 409 Conflict |
| Key seen, different body | Rejects the request; no money moves | 422 Unprocessable |

## Milestone 5, Part A — Fraud rules
| Rule | Fires when | Action |
|---|---|---|
| Velocity | More than 5 outgoing transfers from one wallet in 10 minutes | Hold |
| Amount spike | Amount is over 5x the wallet's 30-day average transfer | Hold |
| New payee, large amount | First transfer to this recipient, above ₹10,000 | Flag, allow |

## Section 9 — API contract
Every POST requires an Idempotency-Key header. Amounts are integers in paise.

| Method | Path | Purpose | Key responses |
|---|---|---|---|
| POST | /users | Create a user | 201 |
| POST | /users/{id}/wallets | Open a wallet | 201 |
| GET | /wallets/{id}/balance | Current balance | 200 |
| GET | /wallets/{id}/statement | Entries, newest first, paginated | 200 |
| POST | /transfers/topup | Cash-in account → wallet | 201, 409, 422 |
| POST | /transfers | Wallet → wallet | 201, 202 (held), 409, 422 |
| POST | /transfers/withdraw | Wallet → cash-out account | 201, 422 |
| POST | /transfers/{id}/reverse | Reversal transfer that points to the original | 201, 409 |
| GET | /transfers/{id} | Transfer, its entries, its status history | 200 |
| POST | /admin/reconciliation/run | Manual reconciliation run | 202 |
| GET | /admin/reconciliation/runs/{id} | Run report and issues | 200 |
| GET | /admin/risk/queue | Held and flagged transfers | 200 |
| POST | /admin/risk/{transfer_id}/decision | Release or reverse a held transfer | 200 |
| POST | /users/{id}/statement/ask | Plain-English statement Q&A | 200 |

## Not in the pasted brief
The **Stretch Milestone** (sharding and consistent hashing) is listed in the learning table but its section was not included. Check the original course page for its requirements.
