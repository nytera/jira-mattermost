CREATE TABLE IF NOT EXISTS alert_feedback (
    id INTEGER PRIMARY KEY,
    mattermost_post_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    user_display_name VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_alert_feedback_mattermost_post_id
    ON alert_feedback (mattermost_post_id);
