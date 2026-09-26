-- Replay system of record. Everything the fleet did, every capsule, replay,
-- hypothesis, approval and policy version lives here.

CREATE TABLE runs (                 -- one per sim boot
    id              TEXT PRIMARY KEY,
    seed            BIGINT NOT NULL,
    map_hash        TEXT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_tick       INT NOT NULL DEFAULT 0,
    policy_version  INT
);

CREATE TABLE events (               -- append-only log of everything
    id        BIGSERIAL PRIMARY KEY,
    ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id    TEXT,
    tick      INT,
    robot_id  TEXT,
    type      TEXT NOT NULL,
    payload   JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX events_type_id ON events (type, id);
CREATE INDEX events_run_tick ON events (run_id, tick);

CREATE TABLE ticks (                -- flight recorder: inputs, state hash, frame per tick
    run_id      TEXT NOT NULL,
    tick        INT NOT NULL,
    state_hash  TEXT NOT NULL,
    inputs      JSONB NOT NULL DEFAULT '[]',
    frame       JSONB NOT NULL,
    PRIMARY KEY (run_id, tick)
);

CREATE TABLE snapshots (            -- full sim state every 5 s
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL,
    tick        INT NOT NULL,
    state       JSONB NOT NULL,
    state_hash  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, tick)
);

CREATE TABLE policy_versions (
    id                 SERIAL PRIMARY KEY,
    version            INT NOT NULL UNIQUE,
    parent_id          INT REFERENCES policy_versions (id),
    rules              JSONB NOT NULL,
    fix_dsl            TEXT,
    hash               TEXT NOT NULL,
    approved_by        TEXT,
    source_capsule_id  INT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE SEQUENCE job_seq;

CREATE TABLE jobs (
    id             TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    lines          JSONB NOT NULL,
    dock           TEXT NOT NULL,
    status         TEXT NOT NULL,   -- pending | assigned | active | done | wrong_item | exception
    robot_id       TEXT,
    run_id         TEXT,
    created_tick   INT,
    deadline_tick  INT,
    assigned_tick  INT,
    done_tick      INT,
    result         JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX jobs_status ON jobs (status, created_at);

CREATE TABLE assignments (          -- every dispatch decision the rules made
    id          BIGSERIAL PRIMARY KEY,
    job_id      TEXT NOT NULL REFERENCES jobs (id),
    robot_id    TEXT NOT NULL,
    run_id      TEXT,
    steps       JSONB NOT NULL,
    input_id    TEXT,
    reason      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE failures (             -- failure inbox (projection of failure events)
    id          SERIAL PRIMARY KEY,
    event_id    BIGINT REFERENCES events (id),
    run_id      TEXT NOT NULL,
    tick        INT NOT NULL,
    type        TEXT NOT NULL,
    robot_id    TEXT,
    detail      JSONB NOT NULL DEFAULT '{}',
    scenario    TEXT,
    status      TEXT NOT NULL,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, tick, type, robot_id)
);

CREATE TABLE capsules (
    id             SERIAL PRIMARY KEY,
    failure_id     INT UNIQUE REFERENCES failures (id),
    run_id         TEXT NOT NULL,
    snapshot_id    BIGINT REFERENCES snapshots (id),
    start_tick     INT NOT NULL,
    end_tick       INT NOT NULL,
    fail_tick      INT NOT NULL,
    hash           TEXT NOT NULL,
    blob           JSONB NOT NULL,
    size_bytes     INT,
    in_regression  BOOLEAN NOT NULL DEFAULT false,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE hypotheses (
    id          SERIAL PRIMARY KEY,
    capsule_id  INT NOT NULL REFERENCES capsules (id),
    round       INT NOT NULL DEFAULT 1,
    rank        INT NOT NULL,
    cause       TEXT NOT NULL,
    fix_dsl     TEXT NOT NULL,
    rationale   TEXT,
    source      TEXT NOT NULL,      -- playbook | vultr:<model>
    status      TEXT NOT NULL,      -- trial | failed | regression | ready | rejected | approved | superseded
    gate        JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (capsule_id, round, rank)
);

CREATE TABLE replays (              -- the replay job queue and its results
    id                SERIAL PRIMARY KEY,
    capsule_id        INT NOT NULL REFERENCES capsules (id),
    hypothesis_id     INT REFERENCES hypotheses (id),
    kind              TEXT NOT NULL,   -- reproduce | trial | regression | proof | suite
    policy_version    INT,
    policy_rules      JSONB,           -- null: the capsule's recorded policy
    control_rules     JSONB,
    control_failures  JSONB,
    status            TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | error
    outcome           TEXT,
    failures          JSONB,
    new_failures      JSONB,
    trajectory_hash   TEXT,
    matches_live      BOOLEAN,
    first_divergence  INT,
    frames            JSONB,
    worker            TEXT,
    error             TEXT,
    duration_ms       INT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ
);
CREATE INDEX replays_queue ON replays (status, id);
CREATE INDEX replays_capsule ON replays (capsule_id, kind);

CREATE TABLE users (
    username  TEXT PRIMARY KEY,
    pw_hash   TEXT NOT NULL,
    role      TEXT NOT NULL DEFAULT 'operator'
);
