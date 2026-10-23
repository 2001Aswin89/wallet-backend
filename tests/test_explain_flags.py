"""Milestone 5, Part B: the nightly step that explains flagged transfers. The rules decide; the LLM
only writes an explanation and a label, and bad replies are discarded."""
import json
import uuid

from app import db
from app.llm.base import LLMError
from app.worker.explain_flags import explain_flags
from tests.helpers import funded_wallet, rules_state
from tests.qa_story import INJECTION


class Scripted:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def held_transfer(client):
    """Two transfers with VELOCITY limit 1: the second is held. Returns (transfer_id, status)."""
    a, wa = funded_wallet(client, 100_000)
    _, wb = funded_wallet(client, 0)
    h = lambda: {"X-User-Id": str(a), "Idempotency-Key": str(uuid.uuid4())}
    with rules_state(VELOCITY={"threshold_value": 1}):
        assert client.post("/transfers", json={"from_wallet_id": wa, "to_wallet_id": wb, "amount": 100,
                                               "note": "first"}, headers=h()).status_code == 201
        r = client.post("/transfers", json={"from_wallet_id": wa, "to_wallet_id": wb, "amount": 200,
                                            "note": INJECTION}, headers=h())
    assert r.status_code == 202
    return r.json()["transfer_id"]


def flag_row(tid):
    with db.tx() as conn:
        return conn.execute("SELECT llm_label, llm_explanation FROM risk_flags WHERE transfer_id = %s", (tid,)).fetchone()


GOOD = json.dumps({"explanation": "Sentence one. Sentence two.", "label": "likely_fraud"})


def test_explanation_and_label_are_saved_and_the_hold_is_untouched(client):
    tid = held_transfer(client)
    llm = Scripted(GOOD)
    assert explain_flags(llm, transfer_ids=[tid])["explained"] == 1
    row = flag_row(tid)
    assert row["llm_label"] == "likely_fraud" and row["llm_explanation"] == "Sentence one. Sentence two."
    with db.tx() as conn:                     # the model cannot release or reject anything
        assert conn.execute("SELECT status FROM transfers WHERE id=%s", (tid,)).fetchone()["status"] == "pending_review"
    assert explain_flags(llm, transfer_ids=[tid])["explained"] == 0       # already explained: not asked again


def test_the_model_sees_the_rule_and_last_ten_transfers_as_data(client):
    tid = held_transfer(client)
    llm = Scripted(GOOD)
    explain_flags(llm, transfer_ids=[tid])
    system, user = llm.calls[0]
    assert "untrusted" in system and INJECTION not in system
    payload = json.loads(user)                                             # the note is only a JSON string value
    assert payload["rule"]["code"] == "VELOCITY" and payload["rule"]["action_taken"] == "held"
    assert payload["flagged_payment"]["note"] == INJECTION
    assert 1 <= len(payload["wallet_last_10_transfers"]) <= 10


def test_bad_replies_are_discarded_and_retried_next_run(client):
    tid = held_transfer(client)
    for bad in ('{"explanation": "ok. fine.", "label": "approve_immediately"}', "not json",
                '{"label": "review"}', '{"explanation": "", "label": "review"}', "[]"):
        assert explain_flags(Scripted(bad), transfer_ids=[tid])["explained"] == 0
        assert flag_row(tid)["llm_label"] is None
    assert explain_flags(Scripted(LLMError("down")), transfer_ids=[tid])["explained"] == 0
    assert flag_row(tid)["llm_label"] is None
    assert explain_flags(Scripted(GOOD), transfer_ids=[tid])["explained"] == 1      # next night it works


def test_mock_provider_explains_without_a_network(client):
    from app.llm.mock import MockLLM
    tid = held_transfer(client)
    assert explain_flags(MockLLM(), transfer_ids=[tid])["explained"] == 1
    assert flag_row(tid)["llm_label"] == "review"
