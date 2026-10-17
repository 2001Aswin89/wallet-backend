from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends

from app import db
from app.auth import require_admin
from app.errors import AppError
from app.services.idempotency import idempotency_key, run_idempotent
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
