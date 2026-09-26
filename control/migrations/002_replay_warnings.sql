-- Throughput failures seen only after the capsule window: shown to the approver, not blocking.
ALTER TABLE replays ADD COLUMN warnings JSONB;
