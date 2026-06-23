CREATE TABLE IF NOT EXISTS alert_tickets (
    id INTEGER PRIMARY KEY,
    mattermost_post_id VARCHAR(64) NOT NULL UNIQUE,
    mattermost_channel_id VARCHAR(64) NOT NULL,
    mattermost_channel_name VARCHAR(255),
    mattermost_message_url TEXT NOT NULL,
    mattermost_message_text TEXT NOT NULL,
    mattermost_alert_title VARCHAR(255),
    mattermost_author_id VARCHAR(64) NOT NULL,
    mattermost_message_created_at TIMESTAMP WITH TIME ZONE,
    jira_issue_key VARCHAR(64) UNIQUE,
    jira_issue_url TEXT,
    valid_incident BOOLEAN NOT NULL DEFAULT FALSE,
    incident_post_id VARCHAR(64) UNIQUE,
    incident_message_url TEXT,
    confirmed_by_user_id VARCHAR(64),
    confirmed_at TIMESTAMP WITH TIME ZONE,
    creation_status VARCHAR(32) NOT NULL DEFAULT 'pending_jira',
    confirmation_status VARCHAR(32) NOT NULL DEFAULT 'none',
    pending_confirmation_by_user_id VARCHAR(64),
    pending_confirmation_at TIMESTAMP WITH TIME ZONE,
    jira_confirmation_comment_added BOOLEAN NOT NULL DEFAULT FALSE,
    postmortem_comment_added BOOLEAN NOT NULL DEFAULT FALSE,
    validity_label VARCHAR(64),
    last_error TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_alert_tickets_mattermost_channel_id
    ON alert_tickets (mattermost_channel_id);

CREATE INDEX IF NOT EXISTS ix_alert_tickets_mattermost_post_id
    ON alert_tickets (mattermost_post_id);
