-- Vultr Serverless Inference usage, per minute and per purpose (dispatch, traffic, driver, audit, roads,
-- supervisor, investigator, copilot...): calls, tokens and what they cost.
CREATE TABLE ai_usage (
    minute      TIMESTAMPTZ NOT NULL,
    purpose     TEXT NOT NULL,
    calls       INT NOT NULL DEFAULT 0,
    errors      INT NOT NULL DEFAULT 0,
    tokens_in   BIGINT NOT NULL DEFAULT 0,
    tokens_out  BIGINT NOT NULL DEFAULT 0,
    usd         NUMERIC(14, 6) NOT NULL DEFAULT 0,
    PRIMARY KEY (minute, purpose)
);
