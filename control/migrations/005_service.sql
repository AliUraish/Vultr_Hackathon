-- Trailer service cases: a hardware fault on the floor, and every step of its recovery
-- (pull over, diagnosis, garage + spare or technician repair), decided by the copilot, checked by rules.
CREATE TABLE service_cases (
    id            SERIAL PRIMARY KEY,
    run_id        TEXT NOT NULL,
    robot_id      TEXT NOT NULL,
    tick          INT NOT NULL,
    code          TEXT NOT NULL,              -- the robot's alarm code
    status        TEXT NOT NULL,              -- detected | safing | diagnosing | recovering | in_repair | dispatched | repairing | resolved
    fault         TEXT,                       -- tire | sensor
    component     TEXT,
    movable       BOOLEAN,
    start_cell    JSONB,
    safe_cell     JSONB,
    bay           JSONB,
    spare         TEXT,
    technician    JSONB,                      -- {name, role, route, depart_at, arrive_at, repair_until, work_order}
    steps         JSONB NOT NULL DEFAULT '[]',
    job_released  TEXT,
    source        TEXT,
    tokens        INT NOT NULL DEFAULT 0,
    deadline      DOUBLE PRECISION,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at   TIMESTAMPTZ
);
CREATE INDEX service_cases_open ON service_cases (status, id);
CREATE INDEX service_cases_robot ON service_cases (robot_id, id);
