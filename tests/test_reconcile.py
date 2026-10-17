"""Milestone 4 acceptance: seed 10,000 transfers, corrupt three things directly in the database,
and the job must report exactly those three root causes and nothing else.

Runs against a throwaway database so other tests' data (and the break-it demo) cannot interfere.
Tests in this file are order-dependent: the corruption test must come last.
"""
import uuid

import httpx
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from app import config
from app.migrate import run_migrations
from app.worker.reconcile import run_reconciliation
from scripts.corrupt import corrupt
from scripts.seed import seed


@pytest.fixture(scope="module")
def recon_dsn():
    name = f"recon_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    info = conninfo_to_dict(config.DATABASE_URL)
    info["dbname"] = name
    dsn = make_conninfo(**info)
    run_migrations(dsn)
    seed(dsn, users=300, wallets_per_user=2, transfers=10_000)
    yield dsn
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def report(dsn, run_id):
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        run = conn.execute("SELECT * FROM reconciliation_runs WHERE id = %s", (run_id,)).fetchone()
        issues = conn.execute("SELECT * FROM reconciliation_issues WHERE run_id = %s", (run_id,)).fetchall()
    return run, issues


def test_clean_seed_passes_with_no_issues(recon_dsn):
    result = run_reconciliation("test", dsn=recon_dsn)
    run, issues = report(recon_dsn, result["run_id"])
    assert run["status"] == "passed" and run["issue_count"] == 0 and issues == []
    assert run["finished_at"] is not None and run["checks_run"] == 7


def test_second_run_is_skipped_while_the_lock_is_held(recon_dsn):
    with psycopg.connect(recon_dsn) as holder:
        holder.execute("SELECT 1 FROM job_locks WHERE job_name = 'reconciliation' FOR UPDATE")
        result = run_reconciliation("test", dsn=recon_dsn)
        run, _ = report(recon_dsn, result["run_id"])
        assert run["status"] == "skipped" and "lock" in run["note"]
    again = run_reconciliation("test", dsn=recon_dsn)
    assert again["status"] == "passed"


def test_three_corruptions_are_reported_as_exactly_three_root_causes(recon_dsn):
    hit = corrupt(recon_dsn)
    result = run_reconciliation("test", dsn=recon_dsn)
    run, issues = report(recon_dsn, result["run_id"])
    groups = {i["group_key"] for i in issues}
    assert groups == {f"transfer:{hit['changed_amount_transfer']}",
                      f"transfer:{hit['deleted_credit_transfer']}",
                      f"account:{hit['bad_balance_account']}"}, (hit, [(i["check_name"], i["group_key"]) for i in issues])
    assert run["status"] == "failed" and run["issue_count"] == 3
    # raw rows are all kept, one per failing check
    checks = {i["check_name"] for i in issues}
    assert {"transfer_balance", "stored_balance", "global_balance"} <= checks
    # the job reports but never fixes: a second run finds the same three
    again = run_reconciliation("test", dsn=recon_dsn)
    assert again["status"] == "failed" and again["issue_count"] == 3


def test_admin_endpoint_returns_202_and_the_run_completes(live_url):
    key = str(uuid.uuid4())
    h = {"X-Admin-Token": config.ADMIN_TOKEN, "Idempotency-Key": key}
    assert httpx.post(f"{live_url}/admin/reconciliation/run", headers={"Idempotency-Key": key}).status_code == 401
    r = httpx.post(f"{live_url}/admin/reconciliation/run", headers=h)
    assert r.status_code == 202 and r.json()["status"] == "running"
    run_id = r.json()["run_id"]
    replay = httpx.post(f"{live_url}/admin/reconciliation/run", headers=h)
    assert replay.status_code == 202 and replay.json()["run_id"] == run_id     # same key, same run
    import time
    for _ in range(100):
        body = httpx.get(f"{live_url}/admin/reconciliation/runs/{run_id}",
                         headers={"X-Admin-Token": config.ADMIN_TOKEN}).json()
        if body["status"] != "running":
            break
        time.sleep(0.2)
    assert body["status"] in ("passed", "failed", "skipped"), body
    assert httpx.get(f"{live_url}/admin/reconciliation/runs/999999999",
                     headers={"X-Admin-Token": config.ADMIN_TOKEN}).status_code == 404
