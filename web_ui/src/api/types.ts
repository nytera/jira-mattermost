// Shapes mirror the JSON the FastAPI admin API returns (see admin_api.py).

export interface AlertTicket {
  id: number;
  mattermost_post_id: string;
  mattermost_channel_id: string;
  mattermost_channel_name: string | null;
  mattermost_message_url: string;
  mattermost_author_id: string;
  mattermost_message_created_at: string | null;
  mattermost_alert_title: string | null;
  mattermost_message_preview: string;
  mattermost_message_text?: string;
  jira_issue_key: string | null;
  jira_issue_url: string | null;
  valid_incident: boolean;
  alert_signature: string | null;
  root_post_id: string | null;
  is_root: boolean;
  expected_repeat_linked: boolean;
  incident_post_id: string | null;
  incident_message_url: string | null;
  confirmed_by_user_id: string | null;
  confirmed_at: string | null;
  resolved_at: string | null;
  creation_status: string;
  confirmation_status: string;
  pending_confirmation_by_user_id: string | null;
  pending_confirmation_at: string | null;
  jira_confirmation_comment_added: boolean;
  validity_label: string | null;
  validity_status: string | null;
  validity_is_empty: boolean;
  last_error: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface AlertsResponse {
  alerts: AlertTicket[];
  limit: number;
  status: string | null;
  validity: string | null;
}

export interface TimeseriesPoint {
  date: string;
  total: number;
  confirmed: number;
}

export interface ChannelCount {
  channel_id: string;
  channel_name: string | null;
  count: number;
}

export interface AdminStats {
  total: number;
  open: number;
  closed: number;
  pending_jira: number;
  failed: number;
  confirmed: number;
  empty_validity: number;
  by_creation_status: Record<string, number>;
  by_confirmation_status: Record<string, number>;
  by_validity_label: Record<string, number>;
  mtta_seconds: number | null;
  mttr_seconds: number | null;
  timeseries_days: number;
  timeseries_daily: TimeseriesPoint[];
  top_channels: ChannelCount[];
}

export interface FeedbackItem {
  id: number;
  user_id: string;
  user_display_name: string;
  message: string;
  created_at: string | null;
}

export interface LogRecord {
  [key: string]: unknown;
  timestamp?: string;
  level?: string;
  logger?: string;
  message?: string;
  event?: string;
}

export interface LogsResponse {
  logs: LogRecord[];
  available: boolean;
}

export interface PromptSetting {
  key: string;
  label: string;
  value: string;
  source: "db" | "env" | "default";
  default: string;
}

export interface SettingsResponse {
  prompts: PromptSetting[];
}

export interface ConfirmationResult {
  status: string;
  message: string;
  jira_issue_url: string | null;
  incident_message_url: string | null;
}

export interface CreateFromLinkResult {
  ok: boolean;
  status: string;
  message: string;
  mattermost_post_id: string | null;
  jira_issue_key: string | null;
  jira_issue_url: string | null;
}

export interface RecreateResult {
  ok: boolean;
  status: string;
  message: string;
  mattermost_post_id: string;
  jira_issue_key: string | null;
  jira_issue_url: string | null;
  previous_jira_issue_key: string | null;
  previous_jira_issue_url: string | null;
}
