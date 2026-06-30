from __future__ import annotations

import pytest
from support import (
    FakeJiraClient,
    FakeMattermostClient,
)

from mm_jira_bot.config import Settings
from mm_jira_bot.repository import (
    AlertTicketRepository,
    create_database_engine,
    create_session_factory,
    init_db,
)
from mm_jira_bot.service import IncidentBotService


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        mattermost_url="https://mattermost.example.com",
        mattermost_token="mm-token",
        mattermost_alert_channel_id="alerts-channel",
        mattermost_incident_channel_id="incidents-channel",
        mattermost_incident_reaction_name="incident",
        mattermost_bot_user_id="bot-user",
        jira_base_url="https://jira.example.com",
        jira_api_token="jira-token",
        jira_project_key="OPS",
        jira_issue_type="Incident",
        jira_valid_incident_field="customfield_12345",
        jira_source_field="customfield_23456",
        jira_is_crit_alert_field="customfield_34567",
        jira_start_field=None,
        jira_end_field=None,
        database_url=f"sqlite:///{tmp_path / 'bot.db'}",
        enable_websocket=False,
        enable_backfill_on_startup=False,
    )


@pytest.fixture()
def service(settings):
    engine = create_database_engine(settings.database_url)
    init_db(engine)
    repository = AlertTicketRepository(create_session_factory(engine))
    mattermost = FakeMattermostClient()
    jira = FakeJiraClient()
    service = IncidentBotService(
        settings=settings,
        repository=repository,
        mattermost_client=mattermost,
        jira_client=jira,
    )
    return service
