-- Relay application schema.
-- Loaded automatically when the Postgres container starts for the first time.

CREATE TABLE customers (
    id                     TEXT PRIMARY KEY,
    name                   TEXT NOT NULL,
    allowed_llm_providers  TEXT[] NOT NULL,
    crm_access_token       TEXT,
    crm_refresh_token      TEXT,
    crm_token_expires_at   TIMESTAMPTZ,
    crm_connected_at       TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per webhook received. The worker polls this table for new work.
CREATE TABLE calls (
    id             BIGSERIAL PRIMARY KEY,
    call_id        TEXT NOT NULL UNIQUE,
    customer_id    TEXT NOT NULL REFERENCES customers(id),
    deal_id        TEXT NOT NULL,
    transcript_id  TEXT,
    transcript     TEXT,
    status         TEXT NOT NULL DEFAULT 'received'
                   CHECK (status IN ('received', 'in_progress', 'done', 'failed')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    received_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    picked_at      TIMESTAMPTZ,
    completed_at   TIMESTAMPTZ,
    error          TEXT
);

CREATE INDEX calls_status_idx ON calls (status, next_attempt_at);

-- One row per processed call: what the LLM returned and what it cost.
CREATE TABLE summaries (
    id                 BIGSERIAL PRIMARY KEY,
    call_ref           BIGINT NOT NULL UNIQUE REFERENCES calls(id),
    provider           TEXT NOT NULL,
    model              TEXT,
    summary            TEXT,
    next_step          TEXT,
    next_steps         JSONB NOT NULL DEFAULT '[]'::jsonb,
    prompt_tokens      INTEGER,
    completion_tokens  INTEGER,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- Index supports the 90-day raw transcript retention sweep.
CREATE INDEX calls_retention_idx ON calls (received_at) WHERE transcript IS NOT NULL;
