ALTER TABLE alert_tickets
    ADD COLUMN IF NOT EXISTS alert_signature VARCHAR(255);
ALTER TABLE alert_tickets
    ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE alert_tickets
    ADD COLUMN IF NOT EXISTS root_post_id VARCHAR(64);
ALTER TABLE alert_tickets
    ADD COLUMN IF NOT EXISTS expected_repeat_linked BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS ix_alert_tickets_alert_signature
    ON alert_tickets (alert_signature);
CREATE INDEX IF NOT EXISTS ix_alert_tickets_root_post_id
    ON alert_tickets (root_post_id);
CREATE INDEX IF NOT EXISTS ix_alert_tickets_signature_channel
    ON alert_tickets (alert_signature, mattermost_channel_id);

-- At most one active root per (signature, channel) episode: guards the
-- concurrent first-firing race so the loser retries as a repeat.
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_root
    ON alert_tickets (alert_signature, mattermost_channel_id)
    WHERE resolved_at IS NULL AND root_post_id IS NULL;
