-- ============================================================
-- Digital Wallet — 001_init.sql
-- PostgreSQL 15+. Builds the whole schema from an empty database.
-- Money is stored as whole paise in BIGINT. Never FLOAT/DOUBLE.
-- Sign convention: credit INCREASES an account's balance, debit DECREASES it.
--   balance = SUM(credit amounts) - SUM(debit amounts)
-- ============================================================

-- ------------------------------------------------------------
-- users
-- ------------------------------------------------------------
CREATE TABLE users (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        VARCHAR(150) NOT NULL,
    email       VARCHAR(254) NOT NULL,
    phone       VARCHAR(20)  NOT NULL,
    kyc_status  VARCHAR(20)  NOT NULL DEFAULT 'pending'
                CHECK (kyc_status IN ('pending', 'verified', 'rejected')),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);
-- email/phone are data we do not control (users change them, they can be recycled),
-- so they are UNIQUE but never the primary key.
CREATE UNIQUE INDEX uq_users_email ON users (lower(email));
CREATE UNIQUE INDEX uq_users_phone ON users (phone);

-- ------------------------------------------------------------
-- accounts: user wallets AND system accounts live in ONE table,
-- so ledger_entries has a single FK target and every query/check
-- (balances, reconciliation) treats them uniformly.
-- ------------------------------------------------------------
CREATE TABLE accounts (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_type  VARCHAR(20) NOT NULL CHECK (account_type IN ('user_wallet', 'system')),
    user_id       BIGINT REFERENCES users (id),
    label         VARCHAR(50) NOT NULL,            -- 'main', 'savings', or the system code
    system_code   VARCHAR(30) UNIQUE,              -- CASH_IN | CASH_OUT | FEE_REVENUE
    currency      CHAR(3)     NOT NULL DEFAULT 'INR' CHECK (currency = 'INR'),
    balance       BIGINT      NOT NULL DEFAULT 0,  -- stored balance, reconciled nightly
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT accounts_owner_shape CHECK (
        (account_type = 'user_wallet' AND user_id IS NOT NULL AND system_code IS NULL)
     OR (account_type = 'system'      AND user_id IS NULL     AND system_code IS NOT NULL)
    ),
    -- user wallets can never go below zero; system accounts may.
    CONSTRAINT accounts_no_overdraft CHECK (account_type = 'system' OR balance >= 0),
    CONSTRAINT uq_accounts_user_label UNIQUE (user_id, label)
);
CREATE INDEX idx_accounts_user ON accounts (user_id);

INSERT INTO accounts (account_type, label, system_code) VALUES
    ('system', 'CASH_IN',     'CASH_IN'),
    ('system', 'CASH_OUT',    'CASH_OUT'),
    ('system', 'FEE_REVENUE', 'FEE_REVENUE');

-- ------------------------------------------------------------
-- transfers: the business event
-- ------------------------------------------------------------
CREATE TABLE transfers (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    type                  VARCHAR(20) NOT NULL
                          CHECK (type IN ('top_up', 'peer_transfer', 'withdrawal', 'fee', 'reversal')),
    status                VARCHAR(20) NOT NULL
                          CHECK (status IN ('pending_review', 'completed', 'rejected')),
    amount                BIGINT      NOT NULL CHECK (amount > 0),
    from_account_id       BIGINT      NOT NULL REFERENCES accounts (id),  -- debited
    to_account_id         BIGINT      NOT NULL REFERENCES accounts (id),  -- credited
    initiated_by          BIGINT      REFERENCES users (id),              -- NULL = system/admin
    reverses_transfer_id  BIGINT      UNIQUE REFERENCES transfers (id),   -- at most one reversal per transfer
    note                  VARCHAR(255),                                   -- user input: treat as DATA
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at          TIMESTAMPTZ,

    CONSTRAINT transfers_distinct_accounts CHECK (from_account_id <> to_account_id),
    CONSTRAINT transfers_reversal_link CHECK ((type = 'reversal') = (reverses_transfer_id IS NOT NULL))
);
-- fraud rules + statement questions look at an account's recent transfers
CREATE INDEX idx_transfers_from_time ON transfers (from_account_id, created_at DESC);
CREATE INDEX idx_transfers_to_time   ON transfers (to_account_id, created_at DESC);
CREATE INDEX idx_transfers_pair      ON transfers (from_account_id, to_account_id);
CREATE INDEX idx_transfers_held      ON transfers (created_at) WHERE status = 'pending_review';

-- ------------------------------------------------------------
-- ledger_entries: one debit or credit line on exactly one account.
-- Positive amount + separate direction (not signed amounts):
--   * CHECK (amount > 0) rules out zero/garbage lines
--   * "debit side" and "credit side" are directly queryable for the
--     balance checks without CASE-on-sign logic.
-- ------------------------------------------------------------
CREATE TABLE ledger_entries (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transfer_id BIGINT      NOT NULL REFERENCES transfers (id),
    account_id  BIGINT      NOT NULL REFERENCES accounts (id),
    direction   VARCHAR(6)  NOT NULL CHECK (direction IN ('debit', 'credit')),
    amount      BIGINT      NOT NULL CHECK (amount > 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- statement query: WHERE account_id = ? ORDER BY created_at DESC, id DESC
CREATE INDEX idx_ledger_account_time ON ledger_entries (account_id, created_at DESC, id DESC);
-- per-transfer balance check + joins back to transfers
CREATE INDEX idx_ledger_transfer ON ledger_entries (transfer_id);

-- Append-only: block UPDATE, DELETE and TRUNCATE.
CREATE FUNCTION ledger_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'ledger_entries is append-only: % is not allowed', TG_OP
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$;
CREATE TRIGGER trg_ledger_no_update_delete
    BEFORE UPDATE OR DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_append_only();
CREATE TRIGGER trg_ledger_no_truncate
    BEFORE TRUNCATE ON ledger_entries
    FOR EACH STATEMENT EXECUTE FUNCTION ledger_append_only();

-- Debits must equal credits per transfer. DEFERRED so it is checked at COMMIT,
-- after all lines of the transfer have been inserted.
CREATE FUNCTION ledger_check_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    d BIGINT;
    c BIGINT;
BEGIN
    SELECT COALESCE(SUM(amount) FILTER (WHERE direction = 'debit'),  0),
           COALESCE(SUM(amount) FILTER (WHERE direction = 'credit'), 0)
      INTO d, c
      FROM ledger_entries
     WHERE transfer_id = NEW.transfer_id;
    IF d <> c THEN
        RAISE EXCEPTION 'transfer % is unbalanced: debits=% credits=%', NEW.transfer_id, d, c
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER trg_ledger_balanced
    AFTER INSERT ON ledger_entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger_check_balanced();

-- ------------------------------------------------------------
-- idempotency_keys
-- ------------------------------------------------------------
CREATE TABLE idempotency_keys (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id        BIGINT      REFERENCES users (id),   -- NULL only for callers with no user (POST /users, admin POSTs)
    key            UUID        NOT NULL,
    request_hash   CHAR(64)    NOT NULL,           -- SHA-256 hex of method+path+canonical body
    endpoint       VARCHAR(100) NOT NULL,
    status         VARCHAR(20) NOT NULL DEFAULT 'in_progress'
                   CHECK (status IN ('in_progress', 'completed')),
    response_code  INTEGER,
    response_body  JSONB,
    transfer_id    BIGINT REFERENCES transfers (id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_idempotency_user_key UNIQUE (user_id, key)   -- does the hard work
);
-- UNIQUE (user_id, key) does not apply when user_id is NULL, so those callers get their own index.
CREATE UNIQUE INDEX uq_idempotency_anon_key ON idempotency_keys (key) WHERE user_id IS NULL;
CREATE INDEX idx_idempotency_created ON idempotency_keys (created_at);  -- 24h cleanup job

-- ------------------------------------------------------------
-- transfer_status_history: audit log (who + when) for every status change
-- ------------------------------------------------------------
CREATE TABLE transfer_status_history (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transfer_id  BIGINT      NOT NULL REFERENCES transfers (id),
    from_status  VARCHAR(20),                       -- NULL for the creation row
    to_status    VARCHAR(20) NOT NULL,
    changed_by   VARCHAR(50) NOT NULL,              -- 'user:12', 'admin:3', 'system'
    reason       VARCHAR(255),
    changed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_status_history_transfer ON transfer_status_history (transfer_id, changed_at);

-- The app does: SELECT set_config('app.actor', 'user:12', true)  at the start of each transaction.
CREATE FUNCTION transfers_log_status() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    actor TEXT := COALESCE(NULLIF(current_setting('app.actor', true), ''), 'system');
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO transfer_status_history (transfer_id, from_status, to_status, changed_by)
        VALUES (NEW.id, NULL, NEW.status, actor);
    ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
        INSERT INTO transfer_status_history (transfer_id, from_status, to_status, changed_by)
        VALUES (NEW.id, OLD.status, NEW.status, actor);
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER trg_transfers_log_status
    AFTER INSERT OR UPDATE OF status ON transfers
    FOR EACH ROW EXECUTE FUNCTION transfers_log_status();

-- ------------------------------------------------------------
-- fraud: rules (thresholds live in the DB, not in code) and flags
-- ------------------------------------------------------------
CREATE TABLE risk_rules (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code             VARCHAR(40) NOT NULL UNIQUE,
    description      VARCHAR(255) NOT NULL,
    threshold_value  BIGINT      NOT NULL,          -- paise, a count, or a percent depending on rule
    window_seconds   INTEGER,                       -- for time-window rules
    action           VARCHAR(10) NOT NULL CHECK (action IN ('hold', 'flag')),
    enabled          BOOLEAN     NOT NULL DEFAULT TRUE,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Rules from the brief (Milestone 5, Part A).
-- VELOCITY:        hold when a wallet already has >= threshold_value outgoing transfers in window_seconds (the 6th is held)
-- AMOUNT_SPIKE:    hold when amount > threshold_value x the wallet's average transfer over window_seconds
-- NEW_PAYEE_LARGE: flag (allow) when first transfer to this recipient and amount > threshold_value paise
INSERT INTO risk_rules (code, description, threshold_value, window_seconds, action) VALUES
    ('VELOCITY',        'More than 5 outgoing transfers from one wallet in 10 minutes',   5,       600,     'hold'),
    ('AMOUNT_SPIKE',    'Amount over 5x the wallet''s 30-day average transfer',           5,       2592000, 'hold'),
    ('NEW_PAYEE_LARGE', 'First transfer to this recipient, above Rs 10,000 (paise)',      1000000, NULL,    'flag');

CREATE TABLE risk_flags (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transfer_id      BIGINT      NOT NULL REFERENCES transfers (id),
    rule_id          BIGINT      NOT NULL REFERENCES risk_rules (id),
    action_taken     VARCHAR(10) NOT NULL CHECK (action_taken IN ('held', 'flagged')),
    details          JSONB       NOT NULL DEFAULT '{}',   -- measured value vs threshold
    llm_explanation  TEXT,                                -- filled by nightly job (Milestone 5B)
    llm_label        VARCHAR(20) CHECK (llm_label IN ('likely_ok', 'review', 'likely_fraud')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    decision      VARCHAR(10) CHECK (decision IN ('release', 'reverse')),
    reviewed_by      VARCHAR(50),
    reviewed_at      TIMESTAMPTZ
);
CREATE INDEX idx_risk_flags_transfer ON risk_flags (transfer_id);
CREATE INDEX idx_risk_flags_unexplained ON risk_flags (created_at) WHERE llm_label IS NULL;

-- ------------------------------------------------------------
-- reconciliation: one row per run, one row per problem
-- ------------------------------------------------------------
CREATE TABLE reconciliation_runs (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,                          -- NULL while running (POST /admin/reconciliation/run returns 202)
    status        VARCHAR(10) NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running', 'passed', 'failed', 'error', 'skipped')),
    checks_run    INTEGER     NOT NULL DEFAULT 0,
    issue_count   INTEGER     NOT NULL DEFAULT 0,      -- distinct root-cause groups
    triggered_by  VARCHAR(50) NOT NULL DEFAULT 'schedule',
    note          VARCHAR(255)                          -- e.g. 'skipped: another run holds the lock'
);

CREATE TABLE reconciliation_issues (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id        BIGINT      NOT NULL REFERENCES reconciliation_runs (id),
    check_name    VARCHAR(40) NOT NULL,
    subject_type  VARCHAR(20) NOT NULL,                -- 'transfer' | 'account' | 'ledger' | 'entry'
    subject_id    BIGINT,
    expected      NUMERIC,
    actual        NUMERIC,
    group_key     VARCHAR(60) NOT NULL,                -- root-cause grouping, see doc section 7
    detail        JSONB       NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_recon_issues_run ON reconciliation_issues (run_id);

CREATE TABLE job_locks (
    job_name   VARCHAR(50) PRIMARY KEY,
    locked_at  TIMESTAMPTZ
);
INSERT INTO job_locks (job_name) VALUES ('reconciliation'), ('explain_flags'), ('idempotency_cleanup');
