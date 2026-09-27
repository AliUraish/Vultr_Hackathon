-- The incident investigator: an agent that runs simulated experiments on a capsule
-- (reproduce, isolate the cause, what-if, stress-test and tune fixes) before it proposes one.

CREATE TABLE investigations (
    id           SERIAL PRIMARY KEY,
    failure_id   INT NOT NULL REFERENCES failures (id),
    capsule_id   INT NOT NULL REFERENCES capsules (id),
    status       TEXT NOT NULL DEFAULT 'running',   -- running | done | error
    source       TEXT NOT NULL,                      -- openai:<model> | scripted
    budget       JSONB NOT NULL DEFAULT '{}',        -- {steps, sims}
    sims         INT NOT NULL DEFAULT 0,             -- simulations run so far
    report       JSONB,                              -- findings: cause, evidence, recommended fix
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ
);
CREATE INDEX investigations_failure ON investigations (failure_id, id);

CREATE TABLE experiments (          -- one instrument call: every number in a report points here
    id                SERIAL PRIMARY KEY,
    capsule_id        INT NOT NULL REFERENCES capsules (id),
    investigation_id  INT REFERENCES investigations (id),
    kind              TEXT NOT NULL,     -- reproduce | isolate | what_if | stress | tune | baseline
    params            JSONB NOT NULL DEFAULT '{}',
    status            TEXT NOT NULL DEFAULT 'running',  -- running | done | error
    result            JSONB,
    summary           TEXT,
    sims              INT NOT NULL DEFAULT 0,
    duration_ms       INT,
    error             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ
);
CREATE INDEX experiments_capsule ON experiments (capsule_id, kind, id);

CREATE TABLE investigation_steps (  -- the agent's trace: which tool, why, what it found
    id                SERIAL PRIMARY KEY,
    investigation_id  INT NOT NULL REFERENCES investigations (id),
    n                 INT NOT NULL,
    tool              TEXT NOT NULL,
    args              JSONB NOT NULL DEFAULT '{}',
    why               TEXT,
    status            TEXT NOT NULL DEFAULT 'running',  -- running | done | error
    summary           TEXT,
    experiment_id     INT REFERENCES experiments (id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    UNIQUE (investigation_id, n)
);

-- Worker jobs for experiments: `spec` says what to simulate, `result` holds what came back.
ALTER TABLE replays ADD COLUMN experiment_id INT REFERENCES experiments (id);
ALTER TABLE replays ADD COLUMN spec JSONB;
ALTER TABLE replays ADD COLUMN result JSONB;
CREATE INDEX replays_experiment ON replays (experiment_id);

ALTER TABLE hypotheses ADD COLUMN investigation_id INT REFERENCES investigations (id);
ALTER TABLE hypotheses ADD COLUMN evidence JSONB;  -- stress-test numbers behind the fix
