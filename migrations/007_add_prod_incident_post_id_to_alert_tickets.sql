-- Read-only (shadow) mode: the adopted real prod incident post id, stored
-- separately from the shadow's own readonly- incident_post_id stub so a ✅ on the
-- real prod incident post resolves to the ticket. Not unique — the prod-id and
-- readonly- namespaces are disjoint. See docs/read-only.md.
ALTER TABLE alert_tickets
    ADD COLUMN IF NOT EXISTS prod_incident_post_id VARCHAR(64);

CREATE INDEX IF NOT EXISTS ix_alert_tickets_prod_incident_post_id
    ON alert_tickets (prod_incident_post_id);
