from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

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
    # Read-only (shadow) mode only: the real prod incident post id, adopted from a
    # prod-bot incident post. Kept separate from the shadow's own ``incident_post_id``
    # stub so the shadow mirrors its own incident message (formatting test) while a
    # ✅ on the real prod incident post still resolves to this ticket. See
    # ``docs/read-only.md``. Not unique — the prod-id and ``readonly-`` namespaces
    # are disjoint, so a two-row match is impossible in practice.
    prod_incident_post_id: Mapped[str | None] = mapped_column(String(64), index=True)
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
    columns_to_add = {
        "alert_signature": "VARCHAR(255)",
        "resolved_at": "TIMESTAMP WITH TIME ZONE"
        if engine.dialect.name == "postgresql"
        else "TIMESTAMP",
        "root_post_id": "VARCHAR(64)",
        "expected_repeat_linked": "BOOLEAN DEFAULT FALSE",
        "prod_incident_post_id": "VARCHAR(64)",
    }
    for column_name, column_type in columns_to_add.items():
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
                "CREATE INDEX IF NOT EXISTS ix_alert_tickets_prod_incident_post_id "
                "ON alert_tickets (prod_incident_post_id)"
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


class AlertTicketRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get_by_post_id(self, post_id: str) -> AlertTicket | None:
        with self._session_factory() as session:
            return session.scalar(
                select(AlertTicket).where(AlertTicket.mattermost_post_id == post_id)
            )

    def get_by_incident_post_id(self, post_id: str) -> AlertTicket | None:
        """Resolve a ticket by its incident post id.

        Matches the shadow's own ``incident_post_id`` first, then the adopted
        ``prod_incident_post_id`` (read-only mode). Two ordered lookups, not an
        ``OR``, so the result is deterministic even if both columns ever held the
        same id — never relying on ``scalar()`` picking an arbitrary row.
        """
        with self._session_factory() as session:
            ticket = session.scalar(
                select(AlertTicket).where(AlertTicket.incident_post_id == post_id)
            )
            if ticket is not None:
                return ticket
            return session.scalar(
                select(AlertTicket).where(AlertTicket.prod_incident_post_id == post_id)
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

    def set_prod_incident_post_id(self, post_id: str, prod_incident_post_id: str) -> None:
        """Record the adopted real prod incident post id (read-only mode)."""

        def apply(ticket: AlertTicket) -> None:
            ticket.prod_incident_post_id = prod_incident_post_id

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
