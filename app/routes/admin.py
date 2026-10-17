from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel

from app import db
from app.auth import require_admin
from app.errors import AppError
from app.services.idempotency import idempotency_key, run_idempotent
from app.services.transfer import decide_held
from app.worker.reconcile import run_reconciliation

router = APIRouter(prefix="/admin")

Admin = Annotated[str, Depends(require_admin)]
Key = Annotated[str, Depends(idempotency_key)]


@router.post("/reconciliation/run", status_code=202)
def start_reconciliation(key: Key, admin: Admin, background: BackgroundTasks):
    """Returns 202 at once with a 'running' run row; the job itself runs in the background.
    If another run holds the job lock, this run is closed as 'skipped' (the brief says to exit)."""
    started: list[int] = []

    def work(conn):
        run = conn.execute("INSERT INTO reconciliation_runs (status, triggered_by) VALUES ('running', %s) "
                           "RETURNING id", (admin,)).fetchone()
        started.append(run["id"])
        return 202, {"run_id": run["id"], "status": "running"}

    response = run_idempotent(user_id=None, key=key, method="POST", path="/admin/reconciliation/run",
                              body={}, work=work)
    if started:  # a replayed key must not start a second job
        background.add_task(run_reconciliation, admin, started[0])
    return response


class DecisionIn(BaseModel):
    decision: Literal["release", "reverse"]


@router.get("/risk/queue")
def risk_queue(admin: Admin, limit: Annotated[int, Query(ge=1, le=500)] = 100):
    """Held transfers, plus transfers that were flagged but allowed and not yet reviewed."""
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT t.id AS transfer_id, t.status, t.type, t.amount, t.from_account_id, t.to_account_id, "
            "t.created_at, json_agg(json_build_object('rule', r.code, 'action', f.action_taken, "
            "'details', f.details, 'llm_label', f.llm_label, 'llm_explanation', f.llm_explanation, "
            "'decision', f.decision) ORDER BY f.id) AS flags "
            "FROM risk_flags f JOIN risk_rules r ON r.id = f.rule_id JOIN transfers t ON t.id = f.transfer_id "
            "WHERE t.status = 'pending_review' OR (f.action_taken = 'flagged' AND f.reviewed_at IS NULL) "
            "GROUP BY t.id ORDER BY t.created_at DESC, t.id DESC LIMIT %s", (limit,)).fetchall()
    for r in rows:
        r["created_at"] = r["created_at"].isoformat()
    return {"count": len(rows), "items": rows}


@router.post("/risk/{transfer_id}/decision")
def risk_decision(transfer_id: int, body: DecisionIn, key: Key, admin: Admin):
    """release: write the entries now (balance is re-checked). reverse: reject the held transfer."""
    def work(conn):
        return 200, decide_held(conn, admin_actor=admin, transfer_id=transfer_id, decision=body.decision)

    return run_idempotent(user_id=None, key=key, method="POST", path=f"/admin/risk/{transfer_id}/decision",
                          body=body.model_dump(), work=work)


@router.get("/reconciliation/runs/{run_id}")
def get_run(run_id: int, admin: Admin):
    with db.tx() as conn:
        run = conn.execute("SELECT * FROM reconciliation_runs WHERE id = %s", (run_id,)).fetchone()
        if run is None:
            raise AppError(404, "RUN_NOT_FOUND", "reconciliation run does not exist")
        issues = conn.execute(
            "SELECT id, check_name, subject_type, subject_id, expected, actual, group_key, detail "
            "FROM reconciliation_issues WHERE run_id = %s ORDER BY id", (run_id,)).fetchall()
    for k in ("started_at", "finished_at"):
        run[k] = run[k].isoformat() if run[k] else None
    for i in issues:
        for k in ("expected", "actual"):
            i[k] = int(i[k]) if i[k] is not None else None
    return {**run, "issues": issues, "root_causes": sorted({i["group_key"] for i in issues})}
