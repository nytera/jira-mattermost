from __future__ import annotations

# Attachment accent colors shared across the bot's Mattermost posts. Kept in a
# tiny import-free leaf module so every domain mixin can pull a color without a
# heavier dependency.

# Neutral slate accent for plain bot thread notices (status/validity/summary/
# postmortem), so every bot comment renders as a boxed attachment instead of a
# bare message.
NOTICE_ATTACHMENT_COLOR = "#64748B"
# Light slate-400 accent for the on-call duty cheat-sheet — a neutral reference
# card, lighter than the slate-500 plain notices but in the same slate family.
DUTY_HELP_ATTACHMENT_COLOR = "#94A3B8"
# Red/green for the incident-channel post: red while open, green when resolved.
INCIDENT_OPEN_COLOR = "#EF4444"
INCIDENT_DONE_COLOR = "#22C55E"
# Deep red accent for bot self-health alerts posted to the ops channel.
OPS_ALERT_COLOR = "#DC2626"
# Blue accent for the "issue created" feed posted to the ops channel.
OPS_ISSUE_CREATED_COLOR = "#2563EB"
