from app import db


def test_health_ok_and_schema_built(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["system_accounts"] == 3
    assert r.headers["x-request-id"]


def test_request_id_echoed(client):
    r = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"


def test_unknown_path_uses_error_shape(client):
    r = client.get("/nope")
    assert r.status_code == 404
    body = r.json()
    assert set(body) == {"code", "message", "request_id"}
    assert body["code"] == "NOT_FOUND"
    assert body["request_id"] == r.headers["x-request-id"]


def test_tx_rolls_back_on_exception(client):
    try:
        with db.tx() as conn:
            conn.execute("INSERT INTO users (name, email, phone) VALUES ('t', 'rb@x.io', '+910000000001')")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with db.tx() as conn:
        n = conn.execute("SELECT count(*) AS n FROM users WHERE email = 'rb@x.io'").fetchone()["n"]
    assert n == 0


def test_migrations_idempotent(client):
    from app.migrate import run_migrations

    assert run_migrations() == []
