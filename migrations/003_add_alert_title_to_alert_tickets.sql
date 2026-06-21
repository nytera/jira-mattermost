ALTER TABLE alert_tickets
    ADD COLUMN IF NOT EXISTS mattermost_alert_title VARCHAR(255);
