from __future__ import annotations

from dataclasses import dataclass

# Interactive action ids carried in the action ``context``. They are stable
# identifiers (not labels), so renaming a control never breaks an in-flight post.
ACTION_VALIDITY = "validity"
ACTION_VALID = "valid"
ACTION_FALSE = "false"
ACTION_EXPECTED = "expected"
ACTION_INCIDENT = "incident"
ACTION_SUMMARY = "summary"
ACTION_FEEDBACK = "feedback"
# Manual-incident controls (incident channel). create_task creates the Jira
# issue on demand; end_incident runs the full postmortem/closure.
ACTION_CREATE_TASK = "create_task"
ACTION_END_INCIDENT = "end_incident"
# Marks an action context as coming from the incident-channel card so dispatch
# keys by the incident root post id instead of the alert post id.
ACTION_SOURCE_INCIDENT = "incident"

ACTION_CALLBACK_PATH = "/mattermost/actions/alert"
FEEDBACK_DIALOG_CALLBACK_PATH = "/mattermost/dialogs/feedback"
# Blue accent for the main controls block ("Создана задача" + validity + buttons).
ACTION_ATTACHMENT_COLOR = "#3B82F6"
# Muted gray accent for the feedback block below.
FEEDBACK_ATTACHMENT_COLOR = "#4B5563"
# Amber accent for the "create task?" prompt on a manual incident post.
INCIDENT_CREATE_ATTACHMENT_COLOR = "#F59E0B"


@dataclass(frozen=True)
class AlertActionButton:
    id: str
    # ``name`` is what Mattermost renders; it accepts ``:shortcode:`` emoji.
    name: str
    style: str | None = None


@dataclass(frozen=True)
class AlertActionOption:
    text: str
    value: str


# Order matters: this is the top-to-bottom order in the "Валидность" menu.
VALIDITY_OPTIONS: tuple[AlertActionOption, ...] = (
    AlertActionOption("Ложный", ACTION_FALSE),
    AlertActionOption("Ожидаемый", ACTION_EXPECTED),
    AlertActionOption("Валидный", ACTION_VALID),
)


# Order matters: this is the left-to-right order within each controls block.
# Incident/Summary live in their own block; feedback gets a separate block below.
PRIMARY_ACTION_BUTTONS: tuple[AlertActionButton, ...] = (
    AlertActionButton(ACTION_INCIDENT, "🚨 Инцидент", style="primary"),
    AlertActionButton(ACTION_SUMMARY, "📝 Summary", style="default"),
)
FEEDBACK_ACTION_BUTTONS: tuple[AlertActionButton, ...] = (
    AlertActionButton(ACTION_FEEDBACK, "💬 Обратная связь по алерту", style="default"),
)
# Buttons under the validity menu on the manual-incident controls card.
INCIDENT_ACTION_BUTTONS: tuple[AlertActionButton, ...] = (
    AlertActionButton(ACTION_END_INCIDENT, "🏁 Завершить", style="primary"),
    AlertActionButton(ACTION_SUMMARY, "📝 Саммари", style="default"),
)


def alert_action_callback_url(service_public_url: str) -> str:
    return f"{service_public_url.rstrip('/')}{ACTION_CALLBACK_PATH}"


def feedback_dialog_callback_url(service_public_url: str) -> str:
    return f"{service_public_url.rstrip('/')}{FEEDBACK_DIALOG_CALLBACK_PATH}"


def _integration(action: str, *, alert_post_id: str, callback_url: str) -> dict:
    """The Mattermost callback envelope shared by every interactive control."""
    return {
        "url": callback_url,
        "context": {
            "action": action,
            "alert_post_id": alert_post_id,
        },
    }


def _button_action(button: AlertActionButton, *, alert_post_id: str, callback_url: str) -> dict:
    action: dict = {
        "id": button.id,
        "name": button.name,
        "type": "button",
        "integration": _integration(
            button.id, alert_post_id=alert_post_id, callback_url=callback_url
        ),
    }
    if button.style:
        action["style"] = button.style
    return action


def build_alert_controls_attachment(
    *,
    title: str,
    title_link: str | None,
    alert_post_id: str,
    callback_url: str,
    confirmed: bool = False,
) -> dict:
    """Main block: "Создана задача" notice, the validity menu, and the
    incident/summary buttons under it. Rendered with the blue accent. When
    ``confirmed``, the "🚨 Инцидент" button is shown as "✅ Подтверждён"."""
    issue_text = f"[{title}]({title_link})" if title_link else title
    validity_select = {
        "id": ACTION_VALIDITY,
        "name": "Выбрать валидность ▼",
        "type": "select",
        "integration": _integration(
            ACTION_VALIDITY, alert_post_id=alert_post_id, callback_url=callback_url
        ),
        "options": [{"text": option.text, "value": option.value} for option in VALIDITY_OPTIONS],
    }
    primary_buttons = PRIMARY_ACTION_BUTTONS
    if confirmed:
        primary_buttons = tuple(
            AlertActionButton(ACTION_INCIDENT, "✅ Подтверждён", style="default")
            if button.id == ACTION_INCIDENT
            else button
            for button in PRIMARY_ACTION_BUTTONS
        )
    buttons = [
        _button_action(button, alert_post_id=alert_post_id, callback_url=callback_url)
        for button in primary_buttons
    ]
    # Once confirmed, validity is chosen on the incident card, so drop the menu here.
    actions = buttons if confirmed else [validity_select, *buttons]
    return {
        "fallback": title,
        "color": ACTION_ATTACHMENT_COLOR,
        "text": f"**Создана задача: {issue_text}**",
        "actions": actions,
    }


def build_alert_feedback_attachment(
    *,
    alert_post_id: str,
    callback_url: str,
) -> dict:
    """Separate block below: the feedback button, rendered with the muted gray accent."""
    actions = [
        _button_action(button, alert_post_id=alert_post_id, callback_url=callback_url)
        for button in FEEDBACK_ACTION_BUTTONS
    ]
    return {
        "fallback": "Обратная связь по алерту",
        "color": FEEDBACK_ATTACHMENT_COLOR,
        "actions": actions,
    }


def _incident_integration(action: str, *, incident_post_id: str, callback_url: str) -> dict:
    """Callback envelope for the incident-channel card; keyed by the incident post."""
    return {
        "url": callback_url,
        "context": {
            "action": action,
            "source": ACTION_SOURCE_INCIDENT,
            "incident_post_id": incident_post_id,
        },
    }


def build_incident_create_attachment(*, incident_post_id: str, callback_url: str) -> dict:
    """First state of the manual-incident card: a single "Создать задачу" button.

    No Jira issue exists yet; the issue and the full controls appear after the
    click (handled by replacing this attachment in the action response)."""
    return {
        "fallback": "Завести инцидент",
        "color": INCIDENT_CREATE_ATTACHMENT_COLOR,
        "text": "**Завести инцидент по этому сообщению?**",
        "actions": [
            {
                "id": ACTION_CREATE_TASK,
                "name": "➕ Создать задачу",
                "type": "button",
                "style": "primary",
                "integration": _incident_integration(
                    ACTION_CREATE_TASK,
                    incident_post_id=incident_post_id,
                    callback_url=callback_url,
                ),
            }
        ],
    }


def build_incident_controls_attachment(
    *,
    incident_post_id: str,
    callback_url: str,
    issue_key: str | None = None,
    issue_url: str | None = None,
    completed: bool = False,
) -> dict:
    """Management controls: the validity menu and the "Завершить" / "Саммари"
    buttons. When ``issue_key`` is given, a "Создана задача: <link>" header is
    shown above the controls (alert-originated incidents, like the alert card);
    manual incidents omit it since the task is created from this very card. When
    ``completed``, the "🏁 Завершить" button is shown as "✅ Завершено"."""
    validity_select = {
        "id": ACTION_VALIDITY,
        "name": "Выбрать валидность ▼",
        "type": "select",
        "integration": _incident_integration(
            ACTION_VALIDITY, incident_post_id=incident_post_id, callback_url=callback_url
        ),
        "options": [{"text": option.text, "value": option.value} for option in VALIDITY_OPTIONS],
    }
    action_buttons = INCIDENT_ACTION_BUTTONS
    if completed:
        action_buttons = tuple(
            AlertActionButton(ACTION_END_INCIDENT, "✅ Завершено", style="default")
            if button.id == ACTION_END_INCIDENT
            else button
            for button in INCIDENT_ACTION_BUTTONS
        )
    buttons = [
        {
            "id": button.id,
            "name": button.name,
            "type": "button",
            **({"style": button.style} if button.style else {}),
            "integration": _incident_integration(
                button.id, incident_post_id=incident_post_id, callback_url=callback_url
            ),
        }
        for button in action_buttons
    ]
    attachment = {
        "fallback": "Управление инцидентом",
        "color": ACTION_ATTACHMENT_COLOR,
        "actions": [validity_select, *buttons],
    }
    if issue_key:
        issue_text = f"[{issue_key}]({issue_url})" if issue_url else issue_key
        attachment["text"] = f"**Создана задача: {issue_text}**"
    return attachment
