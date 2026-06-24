# Jira integration

The bot talks to **Jira 9.x Data Center / on-prem** over **REST API v2** with
`Authorization: Bearer <JIRA_API_TOKEN>` (a personal access token). All paths are
built as `/rest/api/2/...` (`JiraClient._api_path`). This doc covers the
non-obvious parts of how `JiraClient` turns human-friendly config into the exact
payloads Jira accepts: field-name → id resolution, option lookup, the datetime
format, and the deliberate omissions. For the full method list see
[reference/service-map.md](reference/service-map.md); for the flows that call
these methods see [domains/jira-sync.md](domains/jira-sync.md) and
[domains/postmortem.md](domains/postmortem.md). Env vars live in
[config.md](config.md).

Source of truth: `src/mm_jira_bot/jira.py` and `src/mm_jira_bot/jira_payload.py`.
Trust the code over this prose if they ever drift.

## Field name → id resolution

Every `JIRA_*_FIELD` setting accepts **either** a readable Jira field name
(including Russian, e.g. `Валидность`, `Источник`, `Был ли крит алерт?`) **or** a
raw `customfield_NNNNN` id. The name path exists so operators configure by the
name they see in Jira instead of hunting for `customfield_*` ids.

`JiraClient._get_field_id`:

- If the value matches `^customfield_\d+$`, it is used as-is (cached, no call).
- Otherwise it fetches `GET /rest/api/2/field` **once**, matches the configured
  string against each field's `name` **case-insensitively** (`casefold`), caches
  the resolved id in `self._field_ids`, and reuses it forever after.
- No match → non-retryable `ApiError` ("field named '…' was not found"). The
  whole `/field` list is global (not project/issue-scoped).

The same name-or-id rule applies to `JIRA_ISSUE_TYPE`: a numeric value is used
directly as an id; otherwise `_get_issue_type_id` resolves the name via paginated
createmeta and caches it.

## Option (select / radiobuttons) resolution

`select` and `radiobuttons` fields require Jira to receive a specific option, not
a free string. The bot never makes you configure option ids. It reads the
issue-type **createmeta** and matches the wanted label against `allowedValues`:

- `GET /rest/api/2/issue/createmeta/{JIRA_PROJECT_KEY}/issuetypes` →
  resolve the issue-type id (paginated).
- `GET /rest/api/2/issue/createmeta/{JIRA_PROJECT_KEY}/issuetypes/{id}` →
  the field metadata (cached once in `self._create_fields`).

`_get_option_payload(field_id, value)` matches `value.casefold()` against each
option's `value` (falling back to `name`) and emits `{"id": <option id>}` when
found (`jira_option`). **A missing option raises a non-retryable `ApiError`**
listing the allowed labels — this is a config error, retrying won't help. If the
field isn't in createmeta or has no `allowedValues`, it degrades to
`{"value": <raw string>}` and logs a warning (lets Jira do its own matching).

Required options for the alert-create flow:

- `JIRA_SOURCE_FIELD` must offer the option **`Crit alert`** (`JIRA_SOURCE_VALUE`).
- `JIRA_IS_CRIT_ALERT_FIELD` must offer the option **`Да`** (`JIRA_IS_CRIT_ALERT_VALUE`).

`JIRA_VALID_INCIDENT_FIELD` (`Валидность`) must offer the options the bot sets
later: `Валидный`, `Ложный`, `Ожидаемый`, `Не заполнено`.

`preflight_check` resolves all of the above at startup so misconfiguration fails
loudly before any alert arrives.

## Why `Valid Incident` is not sent on create

`build_jira_issue_payload` deliberately **omits** `JIRA_VALID_INCIDENT_FIELD` on
issue creation (`valid_incident_on_create=False` in the log). The intent: a
freshly created issue should carry **Jira's own default** for that field. The bot
only writes it later — `Валидный` on confirmation (`set_valid_incident` →
`set_validity`), or `Ложный` / `Ожидаемый` via the lightweight validity path. See
[domains/jira-sync.md](domains/jira-sync.md).

## START / END date-time fields and the exact format

`JIRA_START_FIELD` and `JIRA_END_FIELD` are optional **date-time picker** fields,
not option fields, so createmeta option resolution does not apply to them.

- `JIRA_START_FIELD` is set to the alert **arrival** time on create.
- `JIRA_END_FIELD` is set to the **reaction** time: when the lightweight
  `Ложный` / `Ожидаемый` validity path runs, or when a checkmark reaction
  (`white_check_mark` / `heavy_check_mark` / `ballot_box_with_check`) lands on an
  incident-thread root. It is **not** written by the `:incident:` confirmation
  flow. Times are converted to `INCIDENT_TIMEZONE`.

Both use `format_jira_datetime()`. Jira 9.x REST v2 requires **ISO 8601 with a
`[+-]hhmm` offset (no colon)** and **mandatory fractional seconds**, e.g.

```
2026-06-16T14:30:00.000+0300
```

(`strftime("%Y-%m-%dT%H:%M:%S.000%z")`). The `dd.MM.yyyy HH:mm` you see in the
Jira UI is only a display format — sending it, or an RFC-3339 `+03:00` offset,
gets rejected.

## Time to fix (numeric)

`JIRA_TIME_TO_FIX_FIELD` (optional) is a plain numeric field holding **minutes**
(`set_time_to_fix` sends the raw int). Skipped silently when unset. Details of
who computes the minutes live in [domains/postmortem.md](domains/postmortem.md).

## Test mode (`JIRA_CREATE_ENABLED=false`)

In test mode the client makes **no Jira calls for issue-key operations**:

- `create_issue` / `create_postmortem_issue` return a stub `JiraIssue`
  (`stub_jira_issue`): `JIRA_STUB_ISSUE_KEY` plus a Mattermost-post-id suffix for
  DB uniqueness, or a generated `{JIRA_PROJECT_KEY}-NNNNN` key when unset. The
  clean configured key is what Mattermost replies display.
- `get_valid_incident`, `set_validity`, `set_valid_incident`, `set_end_time`,
  `set_time_to_fix`, `set_description`, `add_comment`, `transition_issue`,
  `link_child_of` are **no-ops**.

These no-ops are not cosmetic: the stub key **does not exist in Jira**, so without
them a follow-up call (e.g. setting `Валидность` during `confirm_incident`) would
404 and abort the flow after the incident-channel post but before the
alert-thread reply. Field/option **metadata** reads are global (not issue-scoped)
and are **not** stubbed — they still hit Jira.
