from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    func,
    inspect,
    or_,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from mm_jira_bot.domain import (
    MattermostPost,
    backend_datetime,
    backend_now,
    datetime_from_mattermost_ms,
)
from mm_jira_bot.formatting import alert_signature, extract_alert_title


def normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgres://")
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    return database_url


def create_database_engine(database_url: str) -> Engine:
    normalized = normalize_database_url(database_url)
    connect_args = {"check_same_thread": False} if normalized.startswith("sqlite") else {}
    return create_engine(normalized, future=True, connect_args=connect_args)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


class AlertTicket(Base):
    __tablename__ = "alert_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mattermost_post_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    mattermost_channel_id: Mapped[str] = mapped_column(String(64), index=True)
    mattermost_channel_name: Mapped[str | None] = mapped_column(String(255))
    mattermost_message_url: Mapped[str] = mapped_column(Text)
    mattermost_message_text: Mapped[str] = mapped_column(Text)
    mattermost_alert_title: Mapped[str | None] = mapped_column(String(255))
    mattermost_author_id: Mapped[str] = mapped_column(String(64))
    mattermost_message_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    jira_issue_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    jira_issue_url: Mapped[str | None] = mapped_column(Text)
    valid_incident: Mapped[bool] = mapped_column(Boolean, default=False)
    incident_post_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    incident_message_url: Mapped[str | None] = mapped_column(Text)
    confirmed_by_user_id: Mapped[str | None] = mapped_column(String(64))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    creation_status: Mapped[str] = mapped_column(String(32), default="pending_jira")
    confirmation_status: Mapped[str] = mapped_column(String(32), default="none")
    pending_confirmation_by_user_id: Mapped[str | None] = mapped_column(String(64))
    pending_confirmation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    jira_confirmation_comment_added: Mapped[bool] = mapped_column(Boolean, default=False)
    postmortem_comment_added: Mapped[bool] = mapped_column(Boolean, default=False)
    validity_label: Mapped[str | None] = mapped_column(String(64))
    alert_signature: Mapped[str | None] = mapped_column(String(255), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    root_post_id: Mapped[str | None] = mapped_column(String(64), index=True)
    expected_repeat_linked: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=backend_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=backend_now, onupdate=backend_now
    )

    # Episode tracking: an episode is (alert_signature, channel) and stays open
    # while its root (root_post_id IS NULL) is unresolved. The partial unique
    # index enforces at most one active root per episode, so two concurrent
    # first firings can't both become roots — the loser retries as a repeat.
    __table_args__ = (
        Index(
            "ix_alert_tickets_signature_channel",
            "alert_signature",
            "mattermost_channel_id",
        ),
        Index(
            "uq_active_root",
            "alert_signature",
            "mattermost_channel_id",
            unique=True,
            sqlite_where=text("resolved_at IS NULL AND root_post_id IS NULL"),
            postgresql_where=text("resolved_at IS NULL AND root_post_id IS NULL"),
        ),
    )


class AlertFeedback(Base):
    __tablename__ = "alert_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mattermost_post_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    user_display_name: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=backend_now)


class AppSetting(Base):
    """Runtime-editable key/value overrides (e.g. LLM prompt templates edited in
    the debug panel). Overrides win over env config; absence ⇒ fall back to env."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=backend_now, onupdate=backend_now
    )


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    _ensure_alert_ticket_columns(engine)


def _ensure_alert_ticket_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    if not inspector.has_table("alert_tickets"):
        return
    columns = {column["name"] for column in inspector.get_columns("alert_tickets")}
    if "mattermost_alert_title" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE alert_tickets ADD COLUMN mattermost_alert_title VARCHAR(255)")
            )
    if "postmortem_comment_added" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE alert_tickets "
                    "ADD COLUMN postmortem_comment_added BOOLEAN DEFAULT FALSE"
                )
            )
    episode_columns = {
        "alert_signature": "VARCHAR(255)",
        "resolved_at": "TIMESTAMP WITH TIME ZONE"
        if engine.dialect.name == "postgresql"
        else "TIMESTAMP",
        "root_post_id": "VARCHAR(64)",
        "expected_repeat_linked": "BOOLEAN DEFAULT FALSE",
    }
    for column_name, column_type in episode_columns.items():
        if column_name not in columns:
            with engine.begin() as connection:
                connection.execute(
                    text(f"ALTER TABLE alert_tickets ADD COLUMN {column_name} {column_type}")
                )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_alert_tickets_alert_signature "
                "ON alert_tickets (alert_signature)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_alert_tickets_root_post_id "
                "ON alert_tickets (root_post_id)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_alert_tickets_signature_channel "
                "ON alert_tickets (alert_signature, mattermost_channel_id)"
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_active_root "
                "ON alert_tickets (alert_signature, mattermost_channel_id) "
                "WHERE resolved_at IS NULL AND root_post_id IS NULL"
            )
        )


def _count_by(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _mean_duration(pairs: Iterable[tuple[datetime | None, datetime | None]]) -> float | None:
    """Mean of non-negative ``end - start`` deltas in seconds (``None`` if empty).

    Naive timestamps localize to the runtime timezone (matching the Jira
    Time-to-Fix path), never assumed UTC — so SQLite (tz-stripped) and Postgres
    agree."""
    deltas: list[float] = []
    for start, end in pairs:
        if start is None or end is None:
            continue
        seconds = (backend_datetime(end) - backend_datetime(start)).total_seconds()
        if seconds >= 0:
            deltas.append(seconds)
    if not deltas:
        return None
    return round(sum(deltas) / len(deltas), 1)


class AlertTicketRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get_by_post_id(self, post_id: str) -> AlertTicket | None:
        with self._session_factory() as session:
            return session.scalar(
                select(AlertTicket).where(AlertTicket.mattermost_post_id == post_id)
            )

    def get_by_incident_post_id(self, post_id: str) -> AlertTicket | None:
        with self._session_factory() as session:
            return session.scalar(
                select(AlertTicket).where(AlertTicket.incident_post_id == post_id)
            )

    def list_alerts(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
        validity: str | None = None,
    ) -> list[AlertTicket]:
        limit = min(max(limit, 1), 200)
        conditions = []
        if status:
            conditions.append(
                or_(
                    AlertTicket.creation_status == status,
                    AlertTicket.confirmation_status == status,
                )
            )
        if validity == "empty":
            conditions.append(
                AlertTicket.valid_incident.is_(False),
            )
            conditions.append(AlertTicket.validity_label.is_(None))
        statement = (
            select(AlertTicket)
            .where(*conditions)
            .order_by(AlertTicket.created_at.desc())
            .limit(limit)
        )
        with self._session_factory() as session:
            return list(session.scalars(statement))

    def stats_summary(self) -> dict:
        with self._session_factory() as session:
            total = session.scalar(select(func.count(AlertTicket.id))) or 0
            creation_statuses = {
                status: count
                for status, count in session.execute(
                    select(AlertTicket.creation_status, func.count(AlertTicket.id))
                    .group_by(AlertTicket.creation_status)
                    .order_by(AlertTicket.creation_status)
                )
            }
            confirmation_statuses = {
                status: count
                for status, count in session.execute(
                    select(AlertTicket.confirmation_status, func.count(AlertTicket.id))
                    .group_by(AlertTicket.confirmation_status)
                    .order_by(AlertTicket.confirmation_status)
                )
            }
            pending_jira = (
                session.scalar(
                    select(func.count(AlertTicket.id)).where(AlertTicket.jira_issue_key.is_(None))
                )
                or 0
            )
            failed = (
                session.scalar(
                    select(func.count(AlertTicket.id)).where(
                        or_(
                            AlertTicket.creation_status == "failed_jira",
                            AlertTicket.confirmation_status == "failed_confirmation",
                        )
                    )
                )
                or 0
            )
            confirmed = (
                session.scalar(
                    select(func.count(AlertTicket.id)).where(AlertTicket.valid_incident.is_(True))
                )
                or 0
            )
            empty_validity = (
                session.scalar(
                    select(func.count(AlertTicket.id)).where(
                        AlertTicket.valid_incident.is_(False),
                        AlertTicket.validity_label.is_(None),
                    )
                )
                or 0
            )
            return {
                "total": total,
                "creation_statuses": creation_statuses,
                "confirmation_statuses": confirmation_statuses,
                "pending_jira": pending_jira,
                "failed": failed,
                "confirmed": confirmed,
                "empty_validity": empty_validity,
            }

    def admin_stats(self, *, timeseries_days: int = 90) -> dict:
        """Rich aggregate for the admin dashboard (separate from the lean
        :meth:`stats_summary` that feeds Prometheus).

        Loads tickets and computes MTTA/MTTR and the daily timeseries in Python
        so the math is identical on SQLite and Postgres (no dialect-specific date
        SQL). Scans the table — call it off the event loop (``asyncio.to_thread``).
        MTTA/MTTR start at ``mattermost_message_created_at`` (alert fire time,
        matching the Jira Time-to-Fix definition)."""
        with self._session_factory() as session:
            tickets = list(session.scalars(select(AlertTicket)))

        cutoff = backend_now() - timedelta(days=timeseries_days)
        daily: dict[str, dict[str, int]] = {}
        channels: dict[tuple[str, str | None], int] = {}
        for t in tickets:
            stamp = t.mattermost_message_created_at or t.created_at
            if stamp is not None and backend_datetime(stamp) >= cutoff:
                day = backend_datetime(stamp).date().isoformat()
                bucket = daily.setdefault(day, {"total": 0, "confirmed": 0})
                bucket["total"] += 1
                if t.valid_incident:
                    bucket["confirmed"] += 1
            channel_key = (t.mattermost_channel_id, t.mattermost_channel_name)
            channels[channel_key] = channels.get(channel_key, 0) + 1

        top_channels = [
            {"channel_id": cid, "channel_name": cname, "count": count}
            for (cid, cname), count in sorted(channels.items(), key=lambda kv: kv[1], reverse=True)[
                :10
            ]
        ]
        return {
            "total": len(tickets),
            "open": sum(1 for t in tickets if t.valid_incident and t.resolved_at is None),
            "closed": sum(1 for t in tickets if t.resolved_at is not None),
            "pending_jira": sum(1 for t in tickets if t.jira_issue_key is None),
            "failed": sum(
                1
                for t in tickets
                if t.creation_status == "failed_jira"
                or t.confirmation_status == "failed_confirmation"
            ),
            "confirmed": sum(1 for t in tickets if t.valid_incident),
            "empty_validity": sum(
                1 for t in tickets if not t.valid_incident and t.validity_label is None
            ),
            "by_creation_status": _count_by(t.creation_status for t in tickets),
            "by_confirmation_status": _count_by(t.confirmation_status for t in tickets),
            "by_validity_label": _count_by((t.validity_label or "Не заполнено") for t in tickets),
            "mtta_seconds": _mean_duration(
                (t.mattermost_message_created_at, t.confirmed_at) for t in tickets
            ),
            "mttr_seconds": _mean_duration(
                (t.mattermost_message_created_at, t.resolved_at) for t in tickets
            ),
            "timeseries_days": timeseries_days,
            "timeseries_daily": [{"date": day, **counts} for day, counts in sorted(daily.items())],
            "top_channels": top_channels,
        }

    def get_setting(self, key: str) -> str | None:
        with self._session_factory() as session:
            row = session.get(AppSetting, key)
            return row.value if row is not None else None

    def set_setting(self, key: str, value: str) -> None:
        with self._session_factory() as session, session.begin():
            existing = session.get(AppSetting, key)
            if existing is None:
                session.add(AppSetting(key=key, value=value))
            else:
                existing.value = value

    def delete_setting(self, key: str) -> None:
        with self._session_factory() as session, session.begin():
            existing = session.get(AppSetting, key)
            if existing is not None:
                session.delete(existing)

    def create_or_get_alert(
        self,
        post: MattermostPost,
        *,
        message_url: str,
        channel_name: str | None,
    ) -> tuple[AlertTicket, bool]:
        ticket, created, _root = self.create_or_classify_alert(
            post, message_url=message_url, channel_name=channel_name
        )
        return ticket, created

    def create_or_classify_alert(
        self,
        post: MattermostPost,
        *,
        message_url: str,
        channel_name: str | None,
        signature: str | None = None,
    ) -> tuple[AlertTicket, bool, AlertTicket | None]:
        """Insert (or fetch) the ticket and classify it within its episode.

        Returns ``(ticket, created, root)`` where ``root`` is the active root of
        the open episode when this ticket is a repeat, or ``None`` when this
        ticket is itself the root. The ``uq_active_root`` partial unique index
        guards the concurrent first-firing race: the loser of the insert retries
        as a repeat under the winner's root.
        """
        if signature is None:
            signature = alert_signature(post.message)
        with self._session_factory() as session:
            existing = session.scalar(
                select(AlertTicket).where(AlertTicket.mattermost_post_id == post.id)
            )
            if existing:
                return existing, False, self._load_root(session, existing)

            root = self._find_active_root(session, signature, post.channel_id)
            ticket = self._new_alert_ticket(
                post,
                message_url=message_url,
                channel_name=channel_name,
                signature=signature,
                root_post_id=root.mattermost_post_id if root else None,
            )
            session.add(ticket)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.scalar(
                    select(AlertTicket).where(AlertTicket.mattermost_post_id == post.id)
                )
                if existing is not None:
                    return existing, False, self._load_root(session, existing)
                # Lost the active-root race → re-classify as a repeat.
                root = self._find_active_root(session, signature, post.channel_id)
                ticket = self._new_alert_ticket(
                    post,
                    message_url=message_url,
                    channel_name=channel_name,
                    signature=signature,
                    root_post_id=root.mattermost_post_id if root else None,
                )
                session.add(ticket)
                session.commit()
            return ticket, True, self._load_root(session, ticket)

    def _new_alert_ticket(
        self,
        post: MattermostPost,
        *,
        message_url: str,
        channel_name: str | None,
        signature: str,
        root_post_id: str | None,
    ) -> AlertTicket:
        return AlertTicket(
            mattermost_post_id=post.id,
            mattermost_channel_id=post.channel_id,
            mattermost_channel_name=channel_name,
            mattermost_message_url=message_url,
            mattermost_message_text=post.message,
            mattermost_alert_title=extract_alert_title(post.message),
            mattermost_author_id=post.user_id,
            mattermost_message_created_at=datetime_from_mattermost_ms(post.create_at),
            alert_signature=signature,
            root_post_id=root_post_id,
            creation_status="pending_jira",
            confirmation_status="none",
            valid_incident=False,
        )

    def _find_active_root(
        self, session: Session, signature: str, channel_id: str
    ) -> AlertTicket | None:
        return session.scalar(
            select(AlertTicket)
            .where(
                AlertTicket.alert_signature == signature,
                AlertTicket.mattermost_channel_id == channel_id,
                AlertTicket.root_post_id.is_(None),
                AlertTicket.resolved_at.is_(None),
            )
            .order_by(AlertTicket.created_at.asc())
            .limit(1)
        )

    def _load_root(self, session: Session, ticket: AlertTicket) -> AlertTicket | None:
        if not ticket.root_post_id:
            return None
        return session.scalar(
            select(AlertTicket).where(AlertTicket.mattermost_post_id == ticket.root_post_id)
        )

    def mark_episode_resolved(
        self, signature: str, channel_id: str, resolved_at: datetime
    ) -> AlertTicket | None:
        """Close the open episode for ``signature`` in ``channel_id`` by stamping
        ``resolved_at`` on its active root. Returns the root, or ``None`` if no
        episode is open (restart / duplicate resolve)."""
        with self._session_factory() as session:
            root = self._find_active_root(session, signature, channel_id)
            if root is None:
                return None
            root.resolved_at = resolved_at
            session.commit()
            return root

    def create_or_get_incident_thread(
        self,
        post: MattermostPost,
        *,
        message_url: str,
        channel_name: str | None,
    ) -> tuple[AlertTicket, bool]:
        with self._session_factory() as session:
            existing = session.scalar(
                select(AlertTicket).where(AlertTicket.mattermost_post_id == post.id)
            )
            if existing:
                return existing, False

            ticket = AlertTicket(
                mattermost_post_id=post.id,
                mattermost_channel_id=post.channel_id,
                mattermost_channel_name=channel_name,
                mattermost_message_url=message_url,
                mattermost_message_text=post.message,
                mattermost_alert_title=extract_alert_title(post.message),
                mattermost_author_id=post.user_id,
                mattermost_message_created_at=datetime_from_mattermost_ms(post.create_at),
                incident_post_id=post.id,
                incident_message_url=message_url,
                creation_status="pending_postmortem",
                confirmation_status="none",
                valid_incident=False,
            )
            session.add(ticket)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.scalar(
                    select(AlertTicket).where(AlertTicket.mattermost_post_id == post.id)
                )
                if existing is None:
                    raise
                return existing, False
            return ticket, True

    def attach_jira_issue(self, post_id: str, issue_key: str, issue_url: str) -> None:
        self.replace_jira_issue(post_id, issue_key, issue_url)

    def replace_jira_issue(
        self,
        post_id: str,
        issue_key: str,
        issue_url: str,
        *,
        reset_confirmation_comment: bool = False,
    ) -> None:
        def apply(ticket: AlertTicket) -> None:
            ticket.jira_issue_key = issue_key
            ticket.jira_issue_url = issue_url
            ticket.creation_status = "jira_created"
            ticket.last_error = None
            if reset_confirmation_comment:
                ticket.jira_confirmation_comment_added = False

        self._mutate(post_id, apply)

    def mark_jira_create_failed(self, post_id: str, error: str) -> None:
        def apply(ticket: AlertTicket) -> None:
            ticket.creation_status = "failed_jira"
            ticket.last_error = error

        self._mutate(post_id, apply)

    def mark_postmortem_failed(self, post_id: str, error: str) -> None:
        def apply(ticket: AlertTicket) -> None:
            ticket.creation_status = "failed_postmortem"
            ticket.last_error = error

        self._mutate(post_id, apply)

    def set_last_error(self, post_id: str, error: str) -> None:
        def apply(ticket: AlertTicket) -> None:
            ticket.last_error = error

        self._mutate(post_id, apply)

    def mark_pending_confirmation(self, post_id: str, user_id: str, confirmed_at: datetime) -> None:
        def apply(ticket: AlertTicket) -> None:
            if ticket.valid_incident:
                return
            ticket.confirmation_status = "pending_confirmation"
            ticket.pending_confirmation_by_user_id = user_id
            ticket.pending_confirmation_at = confirmed_at
            if ticket.confirmed_by_user_id is None:
                ticket.confirmed_by_user_id = user_id
                ticket.confirmed_at = confirmed_at

        self._mutate(post_id, apply)

    def mark_confirmation_started(self, post_id: str, user_id: str, confirmed_at: datetime) -> None:
        def apply(ticket: AlertTicket) -> None:
            if not ticket.valid_incident:
                ticket.confirmation_status = "confirming"
            if ticket.confirmed_by_user_id is None:
                ticket.confirmed_by_user_id = user_id
            if ticket.confirmed_at is None:
                ticket.confirmed_at = confirmed_at

        self._mutate(post_id, apply)

    def set_incident_message(
        self, post_id: str, incident_post_id: str, incident_message_url: str
    ) -> None:
        def apply(ticket: AlertTicket) -> None:
            ticket.incident_post_id = incident_post_id
            ticket.incident_message_url = incident_message_url

        self._mutate(post_id, apply)

    def mark_jira_confirmation_comment_added(self, post_id: str) -> None:
        def apply(ticket: AlertTicket) -> None:
            ticket.jira_confirmation_comment_added = True

        self._mutate(post_id, apply)

    def mark_postmortem_comment_added(self, post_id: str) -> None:
        def apply(ticket: AlertTicket) -> None:
            ticket.postmortem_comment_added = True

        self._mutate(post_id, apply)

    def mark_expected_repeat_linked(self, post_id: str) -> None:
        def apply(ticket: AlertTicket) -> None:
            ticket.expected_repeat_linked = True

        self._mutate(post_id, apply)

    def mark_confirmed(
        self,
        post_id: str,
        *,
        user_id: str,
        confirmed_at: datetime,
    ) -> None:
        def apply(ticket: AlertTicket) -> None:
            ticket.valid_incident = True
            ticket.confirmation_status = "confirmed"
            ticket.confirmed_by_user_id = ticket.confirmed_by_user_id or user_id
            ticket.confirmed_at = ticket.confirmed_at or confirmed_at
            ticket.pending_confirmation_by_user_id = None
            ticket.pending_confirmation_at = None
            ticket.last_error = None

        self._mutate(post_id, apply)

    def mark_confirmation_failed(self, post_id: str, error: str) -> None:
        def apply(ticket: AlertTicket) -> None:
            if not ticket.valid_incident:
                ticket.confirmation_status = "failed_confirmation"
            ticket.last_error = error

        self._mutate(post_id, apply)

    def set_validity_label(self, post_id: str, label: str) -> None:
        def apply(ticket: AlertTicket) -> None:
            ticket.validity_label = label

        self._mutate(post_id, apply)

    def add_feedback(
        self,
        post_id: str,
        *,
        user_id: str,
        user_display_name: str,
        message: str,
    ) -> AlertFeedback:
        with self._session_factory() as session:
            self._require_ticket(session, post_id)
            feedback = AlertFeedback(
                mattermost_post_id=post_id,
                user_id=user_id,
                user_display_name=user_display_name,
                message=message,
            )
            session.add(feedback)
            session.commit()
            session.refresh(feedback)
            return feedback

    def list_feedback(self, post_id: str) -> list[AlertFeedback]:
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(AlertFeedback)
                    .where(AlertFeedback.mattermost_post_id == post_id)
                    .order_by(AlertFeedback.created_at)
                )
            )

    def sync_valid_incident_from_jira(self, post_id: str) -> None:
        def apply(ticket: AlertTicket) -> None:
            ticket.valid_incident = True
            if ticket.confirmation_status not in {"confirmed", "confirming"}:
                ticket.confirmation_status = "confirmed"

        self._mutate(post_id, apply)

    def list_pending_jira(self, limit: int = 50) -> list[AlertTicket]:
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(AlertTicket)
                    .where(
                        AlertTicket.jira_issue_key.is_(None),
                        AlertTicket.creation_status.in_(["pending_jira", "failed_jira"]),
                    )
                    .order_by(AlertTicket.created_at)
                    .limit(limit)
                )
            )

    def list_pending_confirmations(self, limit: int = 50) -> list[AlertTicket]:
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(AlertTicket)
                    .where(
                        AlertTicket.valid_incident.is_(False),
                        AlertTicket.confirmation_status.in_(
                            ["pending_confirmation", "failed_confirmation", "confirming"]
                        ),
                    )
                    .order_by(AlertTicket.updated_at)
                    .limit(limit)
                )
            )

    def _require_ticket(self, session: Session, post_id: str) -> AlertTicket:
        ticket = session.scalar(
            select(AlertTicket).where(AlertTicket.mattermost_post_id == post_id)
        )
        if ticket is None:
            raise KeyError(f"Alert ticket for post_id={post_id} not found")
        return ticket

    def _mutate(self, post_id: str, apply: Callable[[AlertTicket], None]) -> None:
        with self._session_factory() as session:
            ticket = self._require_ticket(session, post_id)
            apply(ticket)
            session.commit()


def ticket_to_post(ticket: AlertTicket) -> MattermostPost:
    create_at = 0
    if ticket.mattermost_message_created_at:
        dt = ticket.mattermost_message_created_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        create_at = int(dt.timestamp() * 1000)
    return MattermostPost(
        id=ticket.mattermost_post_id,
        channel_id=ticket.mattermost_channel_id,
        user_id=ticket.mattermost_author_id,
        message=ticket.mattermost_message_text,
        create_at=create_at,
        channel_name=ticket.mattermost_channel_name,
    )
