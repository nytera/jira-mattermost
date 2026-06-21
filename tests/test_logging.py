from __future__ import annotations

import json
import logging

from mm_jira_bot.logging import (
    JsonFormatter,
    TextInfoFilter,
    TextFormatter,
    _build_formatter,
    get_logger,
)


def _record(
    event: str,
    level: int = logging.INFO,
    **fields: object,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="mm_jira_bot.service",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=event,
        args=(),
        exc_info=None,
    )
    record.extra_fields = {"event": event, **fields}
    return record


def test_json_formatter_emits_structured_line() -> None:
    line = JsonFormatter().format(_record("jira.issue.created", jira_issue_key="BAND-1"))
    payload = json.loads(line)
    assert payload["message"] == "jira.issue.created"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "mm_jira_bot.service"
    assert payload["jira_issue_key"] == "BAND-1"


def test_text_formatter_uses_human_label_and_short_field_names() -> None:
    line = TextFormatter().format(
        _record(
            "jira.issue.created",
            jira_issue_key="BAND-1",
            mattermost_post_id="post-1",
            metadata={"hidden": True},
        )
    )
    assert "INFO" in line
    assert "service jira issue created" in line
    assert "event=" not in line
    assert "jira=BAND-1" in line
    assert "post=post-1" in line
    assert "jira_issue_key=" not in line
    assert "metadata=" not in line


def test_text_formatter_quotes_values_with_spaces() -> None:
    line = TextFormatter().format(_record("jira.issue.create_failed", error="boom failed"))
    assert 'error="boom failed"' in line


def test_build_formatter_selects_by_name() -> None:
    assert isinstance(_build_formatter("text"), TextFormatter)
    assert isinstance(_build_formatter("TEXT"), TextFormatter)
    assert isinstance(_build_formatter("json"), JsonFormatter)
    assert isinstance(_build_formatter("anything-else"), JsonFormatter)


def test_text_info_filter_hides_noisy_info_event() -> None:
    assert not TextInfoFilter().filter(_record("startup.preflight.check_ok"))


def test_text_info_filter_passes_business_info_event() -> None:
    assert TextInfoFilter().filter(_record("mattermost.alert.received"))


def test_text_info_filter_passes_warnings_independently_of_allowlist() -> None:
    record = _record("startup.preflight.check_ok", level=logging.WARNING)
    assert TextInfoFilter().filter(record)


def test_event_logger_passes_fields_through() -> None:
    log = get_logger("mm_jira_bot.test")
    records: list[logging.LogRecord] = []

    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    stdlib_logger = logging.getLogger("mm_jira_bot.test")
    stdlib_logger.addHandler(handler)
    stdlib_logger.setLevel(logging.INFO)
    try:
        log.info("some.event", key="value")
    finally:
        stdlib_logger.removeHandler(handler)

    assert records
    assert records[0].extra_fields == {"event": "some.event", "key": "value"}
