import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db
from app.errors import install_error_handlers
from app.middleware import RequestIdMiddleware
from app.migrate import run_migrations
from app.routes import admin, transfers, users, wallets

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations()
    db.open_pool()
    yield
    db.close_pool()


app = FastAPI(title="Digital Wallet", lifespan=lifespan)
app.add_middleware(RequestIdMiddleware)
install_error_handlers(app)
app.include_router(users.router)
app.include_router(wallets.router)
app.include_router(transfers.router)
app.include_router(admin.router)


@app.get("/health")
def health():
    with db.tx() as conn:
        row = conn.execute(
            "SELECT count(*) AS n, max(filename) AS latest FROM schema_migrations"
        ).fetchone()
        system = conn.execute("SELECT count(*) AS n FROM accounts WHERE account_type = 'system'").fetchone()
    return {"status": "ok", "migrations": row["n"], "latest": row["latest"], "system_accounts": system["n"]}
