-- The site this deployment runs: an LLM-generated enterprise profile (facility, catalog,
-- customers, carriers, asset register, cost model), orders that drive the robots, a
-- tamper-evident audit ledger over the event log, and signed policy versions.

CREATE TABLE site (                 -- exactly one row: the facility this control plane runs
    id          INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    code        TEXT NOT NULL,
    name        TEXT NOT NULL,
    profile     JSONB NOT NULL,     -- facility, carriers, robots (asset register), associates, cost model
    source      TEXT NOT NULL,      -- openai:<model> | generator
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE catalog (              -- what sits in each rack slot
    slot         TEXT PRIMARY KEY,  -- A1..F8 (the sim's location key)
    sku          TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    category     TEXT NOT NULL,
    cls          TEXT NOT NULL,     -- handling class: boxed | loose_small | fragile | heavy
    uom          TEXT NOT NULL,
    unit_value   NUMERIC(10, 2) NOT NULL,
    unit_weight  NUMERIC(8, 2) NOT NULL
);

CREATE TABLE customers (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    segment    TEXT NOT NULL,
    tier       TEXT NOT NULL,       -- platinum | gold | standard
    sla_hours  INT NOT NULL
);

CREATE SEQUENCE order_seq START 104200;

CREATE TABLE orders (
    id           TEXT PRIMARY KEY,
    customer_id  TEXT NOT NULL REFERENCES customers (id),
    dock         TEXT NOT NULL,
    carrier      TEXT NOT NULL,
    lines        JSONB NOT NULL,    -- [{slot, sku, name, qty, unit_value}]
    value        NUMERIC(12, 2) NOT NULL,
    priority     TEXT NOT NULL,     -- standard | expedite
    status       TEXT NOT NULL,     -- released | picking | shipped | short_shipped | exception
    job_id       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    ship_by      TIMESTAMPTZ NOT NULL,
    shipped_at   TIMESTAMPTZ
);
CREATE INDEX orders_status ON orders (status, created_at);
ALTER TABLE jobs ADD COLUMN order_id TEXT;

-- Audit ledger: a sealer assigns every event a sequence number, groups them into blocks,
-- and chains the blocks by Merkle root; the block heads are also witnessed by VM B.
ALTER TABLE events ADD COLUMN seq BIGINT;
ALTER TABLE events ADD COLUMN leaf TEXT;
CREATE INDEX events_unsealed ON events (id) WHERE seq IS NULL;
CREATE UNIQUE INDEX events_seq ON events (seq);

CREATE TABLE audit_blocks (
    n            BIGINT PRIMARY KEY,
    first_seq    BIGINT NOT NULL,
    last_seq     BIGINT NOT NULL,
    events       INT NOT NULL,
    merkle_root  TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL,
    witnessed    BOOLEAN NOT NULL DEFAULT false,
    sealed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Policies are signed (Ed25519) by the control plane; sim nodes hold only the public key.
ALTER TABLE policy_versions ADD COLUMN signature TEXT;
ALTER TABLE policy_versions ADD COLUMN key_id TEXT;
