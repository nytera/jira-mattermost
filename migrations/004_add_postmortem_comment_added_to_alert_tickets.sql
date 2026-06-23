ALTER TABLE alert_tickets
    ADD COLUMN IF NOT EXISTS postmortem_comment_added BOOLEAN NOT NULL DEFAULT FALSE;
