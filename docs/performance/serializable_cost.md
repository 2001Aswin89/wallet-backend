# Serializable versus Read Committed on one hot wallet

100 concurrent Rs 10 transfers out of a Rs 500 wallet, 5 runs per level, one connection per thread. Correct outcome every time: 50 succeed, 50 INSUFFICIENT_FUNDS, final balance 0.

| isolation level | median wall time (s) | transactions started (incl. retries) | 40001 retries | worst single request: attempts | all runs correct |
|---|---|---|---|---|---|
| READ COMMITTED + row locks (the app) | 2.06 | 100 | 0 | 1 | True |
| SERIALIZABLE + row locks + retry on 40001 | 3.08 | 2417 | 2317 | 45 | True |
