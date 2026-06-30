from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from mm_jira_bot import domain
from mm_jira_bot.domain import (
    MattermostPost,
    backend_now,
    datetime_from_mattermost_ms,
)
from mm_jira_bot.repository import (
    AlertTicket,
    AlertTicketRepository,
    create_database_engine,
    create_session_factory,
    init_db,
    normalize_database_url,
    ticket_to_post,
)

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture()
def engine(tmp_path):
    eng = create_database_engine(f"sqlite:///{tmp_path / 'repo.db'}")
    init_db(eng)
    return eng


@pytest.fixture()
def session_factory(engine):
    return create_session_factory(engine)


@pytest.fixture()
def repo(session_factory):
    return AlertTicketRepository(session_factory)


@pytest.fixture()
def moscow_tz():
    """Set runtime tz to Europe/Moscow for the test body, restore afterwards.

    The runtime tz is process-global (domain._runtime_timezone); sibling tests
    in this file run in-process, so leaking it would poison them.
    """
    previous = domain.runtime_timezone().key
    domain.configure_runtime_timezone("Europe/Moscow")
    try:
        yield
    finally:
        domain.configure_runtime_timezone(previous)


def make_post(
    post_id: str = "post-1",
    channel_id: str = "chan-1",
    message: str = "CPU usage is above 95%",
    create_at: int = 1_700_000_000_000,
) -> MattermostPost:
    return MattermostPost(
        id=post_id,
        channel_id=channel_id,
        user_id="author-1",
        message=message,
        create_at=create_at,
        channel_name="alerts",
    )


def make_ticket(**overrides) -> AlertTicket:
    fields = {
        "mattermost_post_id": "post-1",
        "mattermost_channel_id": "chan-1",
        "mattermost_message_url": "https://mm/url",
        "mattermost_message_text": "msg",
        "mattermost_author_id": "author-1",
    }
    fields.update(overrides)
    return AlertTicket(**fields)


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


def test_create_or_classify_alert_is_idempotent(repo, session_factory):
    post = make_post(post_id="dup-post")
    ticket1, created1, root1 = repo.create_or_classify_alert(
        post, message_url="https://mm/1", channel_name="alerts"
    )
    assert created1 is True
    assert root1 is None  # first firing is its own root

    ticket2, created2, _root2 = repo.create_or_classify_alert(
        post, message_url="https://mm/1", channel_name="alerts"
    )
    assert created2 is False
    assert ticket2.id == ticket1.id

    with session_factory() as session:
        count = session.scalar(
            select(AlertTicket.id).where(AlertTicket.mattermost_post_id == "dup-post")
        )
        all_rows = list(session.scalars(select(AlertTicket)))
    assert count is not None
    assert len(all_rows) == 1


def test_create_or_get_incident_thread_is_idempotent(repo, session_factory):
    post = make_post(post_id="inc-post", channel_id="incidents")
    ticket1, created1 = repo.create_or_get_incident_thread(
        post, message_url="https://mm/inc", channel_name="incidents"
    )
    assert created1 is True
    assert ticket1.incident_post_id == "inc-post"

    ticket2, created2 = repo.create_or_get_incident_thread(
        post, message_url="https://mm/inc", channel_name="incidents"
    )
    assert created2 is False
    assert ticket2.id == ticket1.id

    with session_factory() as session:
        rows = list(session.scalars(select(AlertTicket)))
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# uq_active_root partial unique index
# --------------------------------------------------------------------------- #


def test_uq_active_root_rejects_second_open_root(session_factory):
    # create_or_classify_alert swallows IntegrityError and reclassifies, so we
    # exercise the constraint with direct inserts instead.
    with session_factory() as session:
        session.add(
            make_ticket(
                mattermost_post_id="root-a",
                alert_signature="title:Sig",
                mattermost_channel_id="chan-x",
                root_post_id=None,
                resolved_at=None,
            )
        )
        session.commit()

    with session_factory() as session:
        session.add(
            make_ticket(
                mattermost_post_id="root-b",
                alert_signature="title:Sig",
                mattermost_channel_id="chan-x",
                root_post_id=None,
                resolved_at=None,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_uq_active_root_allows_second_root_after_first_resolved(repo, session_factory):
    with session_factory() as session:
        session.add(
            make_ticket(
                mattermost_post_id="root-a",
                alert_signature="title:Sig",
                mattermost_channel_id="chan-y",
                root_post_id=None,
                resolved_at=None,
            )
        )
        session.commit()

    # Close the first episode by stamping resolved_at on its active root.
    closed = repo.mark_episode_resolved("title:Sig", "chan-y", backend_now())
    assert closed is not None
    assert closed.mattermost_post_id == "root-a"

    # A new open root for the same (signature, channel) is now permitted.
    with session_factory() as session:
        session.add(
            make_ticket(
                mattermost_post_id="root-c",
                alert_signature="title:Sig",
                mattermost_channel_id="chan-y",
                root_post_id=None,
                resolved_at=None,
            )
        )
        session.commit()  # must not raise

    with session_factory() as session:
        rows = list(
            session.scalars(select(AlertTicket).where(AlertTicket.alert_signature == "title:Sig"))
        )
    assert len(rows) == 2


# --------------------------------------------------------------------------- #
# init_db migration of a legacy pre-episode schema
# --------------------------------------------------------------------------- #


def test_init_db_migrates_legacy_schema(tmp_path):
    eng = create_database_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    # A legacy table missing the episode columns + postmortem_comment_added.
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE alert_tickets ("
                "id INTEGER PRIMARY KEY, "
                "mattermost_post_id VARCHAR(64), "
                "mattermost_channel_id VARCHAR(64), "
                "mattermost_message_url TEXT, "
                "mattermost_message_text TEXT, "
                "mattermost_author_id VARCHAR(64), "
                "creation_status VARCHAR(32), "
                "confirmation_status VARCHAR(32), "
                "valid_incident BOOLEAN"
                ")"
            )
        )

    init_db(eng)

    inspector = inspect(eng)
    columns = {col["name"] for col in inspector.get_columns("alert_tickets")}
    for expected in (
        "alert_signature",
        "resolved_at",
        "root_post_id",
        "postmortem_comment_added",
        "expected_repeat_linked",
    ):
        assert expected in columns

    index_names = {idx["name"] for idx in inspector.get_indexes("alert_tickets")}
    assert "ix_alert_tickets_alert_signature" in index_names
    assert "uq_active_root" in index_names


def test_init_db_is_idempotent(tmp_path):
    eng = create_database_engine(f"sqlite:///{tmp_path / 'idem.db'}")
    init_db(eng)
    init_db(eng)  # running twice must not raise
    inspector = inspect(eng)
    assert inspector.has_table("alert_tickets")


# --------------------------------------------------------------------------- #
# Timezone round-trip & ticket_to_post
# --------------------------------------------------------------------------- #


def test_timezone_roundtrip_shifts_by_runtime_offset(moscow_tz, session_factory):
    """CHARACTERIZATION (not the task's stated expectation).

    With runtime tz = Europe/Moscow (UTC+3), persisting an aware datetime and
    reading it back does NOT preserve the original epoch ms. SQLite drops tzinfo
    on read, yielding the Moscow *wall-clock* as a naive value; ticket_to_post
    then treats that naive datetime as UTC, so the recovered epoch is shifted
    forward by the runtime offset (+3h = +10_800_000 ms). See caveat in summary.
    """
    epoch_ms = 1_700_000_000_000
    with session_factory() as session:
        session.add(
            make_ticket(
                mattermost_post_id="tz-post",
                mattermost_message_created_at=datetime_from_mattermost_ms(epoch_ms),
            )
        )
        session.commit()

    with session_factory() as session:
        ticket = session.scalar(
            select(AlertTicket).where(AlertTicket.mattermost_post_id == "tz-post")
        )
        assert ticket is not None
        # tzinfo stripped on read.
        assert ticket.mattermost_message_created_at.tzinfo is None
        recovered = ticket_to_post(ticket).create_at

    assert recovered == epoch_ms + 10_800_000


def test_ticket_to_post_null_created_at_yields_zero():
    ticket = make_ticket(mattermost_message_created_at=None)
    assert ticket_to_post(ticket).create_at == 0


def test_ticket_to_post_naive_datetime_treated_as_utc():
    # A naive datetime is interpreted as UTC, not shifted by runtime tz.
    naive = datetime(2023, 11, 14, 22, 13, 20)  # epoch 1_700_000_000_000 in UTC
    ticket = make_ticket(mattermost_message_created_at=naive)
    expected = int(naive.replace(tzinfo=UTC).timestamp() * 1000)
    assert ticket_to_post(ticket).create_at == expected
    assert ticket_to_post(ticket).create_at == 1_700_000_000_000


# --------------------------------------------------------------------------- #
# list_pending_jira / list_pending_confirmations
# --------------------------------------------------------------------------- #


def test_list_pending_jira_filters_key_null_and_status(repo, session_factory):
    with session_factory() as session:
        # Eligible: key NULL + pending_jira.
        session.add(
            make_ticket(
                mattermost_post_id="pj-pending",
                jira_issue_key=None,
                creation_status="pending_jira",
            )
        )
        # Eligible: key NULL + failed_jira (also included per the status set).
        session.add(
            make_ticket(
                mattermost_post_id="pj-failed",
                jira_issue_key=None,
                creation_status="failed_jira",
            )
        )
        # Excluded: has a jira key.
        session.add(
            make_ticket(
                mattermost_post_id="pj-created",
                jira_issue_key="OPS-1",
                creation_status="jira_created",
            )
        )
        # Excluded: key NULL but status not in the pending set.
        session.add(
            make_ticket(
                mattermost_post_id="pj-other",
                jira_issue_key=None,
                creation_status="pending_postmortem",
            )
        )
        session.commit()

    ids = {t.mattermost_post_id for t in repo.list_pending_jira()}
    assert ids == {"pj-pending", "pj-failed"}


def test_list_pending_confirmations_excludes_valid_incident(repo, session_factory):
    with session_factory() as session:
        session.add(
            make_ticket(
                mattermost_post_id="pc-pending",
                valid_incident=False,
                confirmation_status="pending_confirmation",
            )
        )
        # Excluded: valid_incident True even with a pending-ish status.
        session.add(
            make_ticket(
                mattermost_post_id="pc-valid",
                valid_incident=True,
                confirmation_status="confirming",
            )
        )
        # Excluded: status not in the pending set.
        session.add(
            make_ticket(
                mattermost_post_id="pc-none",
                valid_incident=False,
                confirmation_status="none",
            )
        )
        session.commit()

    ids = {t.mattermost_post_id for t in repo.list_pending_confirmations()}
    assert ids == {"pc-pending"}


# --------------------------------------------------------------------------- #
# list_alerts: limit clamp & ordering
# --------------------------------------------------------------------------- #


def test_list_alerts_clamps_limit_lower_and_upper(repo, session_factory):
    base = datetime(2023, 1, 1, tzinfo=UTC)
    with session_factory() as session:
        for i in range(205):
            session.add(
                make_ticket(
                    mattermost_post_id=f"la-{i}",
                    created_at=base.replace(microsecond=i),
                )
            )
        session.commit()

    assert len(repo.list_alerts(limit=0)) == 1  # lower clamp -> 1
    assert len(repo.list_alerts(limit=999)) == 200  # upper clamp -> 200


def test_list_alerts_orders_created_at_desc(repo, session_factory):
    base = datetime(2023, 5, 1, tzinfo=UTC)
    with session_factory() as session:
        session.add(make_ticket(mattermost_post_id="a-old", created_at=base.replace(hour=1)))
        session.add(make_ticket(mattermost_post_id="a-mid", created_at=base.replace(hour=5)))
        session.add(make_ticket(mattermost_post_id="a-new", created_at=base.replace(hour=9)))
        session.commit()

    ordered = [t.mattermost_post_id for t in repo.list_alerts(limit=10)]
    assert ordered == ["a-new", "a-mid", "a-old"]


# --------------------------------------------------------------------------- #
# _mutate / _require_ticket KeyError on unknown post
# --------------------------------------------------------------------------- #


def test_mutate_raises_keyerror_on_unknown_post(repo):
    with pytest.raises(KeyError):
        repo.set_last_error("nope", "boom")


# --------------------------------------------------------------------------- #
# mark_episode_resolved no-op when no open episode
# --------------------------------------------------------------------------- #


def test_mark_episode_resolved_returns_none_when_no_open_episode(repo):
    assert repo.mark_episode_resolved("title:Nothing", "chan-z", backend_now()) is None


# --------------------------------------------------------------------------- #
# stats_summary
# --------------------------------------------------------------------------- #


def test_stats_summary_counts(repo, session_factory):
    with session_factory() as session:
        session.add(
            make_ticket(
                mattermost_post_id="ds-1",
                creation_status="pending_jira",
                jira_issue_key=None,
                valid_incident=False,
                validity_label=None,
            )
        )
        session.add(
            make_ticket(
                mattermost_post_id="ds-2",
                creation_status="jira_created",
                jira_issue_key="OPS-2",
                valid_incident=True,
            )
        )
        session.add(
            make_ticket(
                mattermost_post_id="ds-3",
                creation_status="failed_jira",
                jira_issue_key=None,
                valid_incident=False,
                validity_label=None,
            )
        )
        session.commit()

    summary = repo.stats_summary()
    assert summary["total"] == 3
    assert summary["pending_jira"] == 2  # two rows with NULL jira_issue_key
    assert summary["failed"] == 1
    assert summary["confirmed"] == 1
    assert summary["empty_validity"] == 2
    assert summary["creation_statuses"]["pending_jira"] == 1


# --------------------------------------------------------------------------- #
# normalize_database_url
# --------------------------------------------------------------------------- #


def test_normalize_database_url_postgres_scheme():
    assert (
        normalize_database_url("postgres://user:pass@host:5432/db")
        == "postgresql+psycopg://user:pass@host:5432/db"
    )


def test_normalize_database_url_postgresql_scheme():
    assert (
        normalize_database_url("postgresql://user:pass@host:5432/db")
        == "postgresql+psycopg://user:pass@host:5432/db"
    )


def test_normalize_database_url_sqlite_unchanged():
    assert normalize_database_url("sqlite:///tmp/bot.db") == "sqlite:///tmp/bot.db"
