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

ACTION_CALLBACK_PATH = "/mattermost/actions/alert"
FEEDBACK_DIALOG_CALLBACK_PATH = "/mattermost/dialogs/feedback"
ACTION_ATTACHMENT_COLOR = "#3B82F6"
FEEDBACK_ATTACHMENT_COLOR = "#4B5563"
# The "Создана задача" notice shares the muted gray accent of the feedback block.
TITLE_ATTACHMENT_COLOR = FEEDBACK_ATTACHMENT_COLOR


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


# Order matters: this is the left-to-right order of the follow-up controls.
ALERT_ACTION_BUTTONS: tuple[AlertActionButton, ...] = (
    AlertActionButton(ACTION_INCIDENT, "🚨 Инцидент", style="primary"),
    AlertActionButton(ACTION_SUMMARY, "📝 Summary", style="default"),
)

FEEDBACK_ACTION_BUTTON = AlertActionButton(
    ACTION_FEEDBACK, "Обратная связь по алерту", style="default"
)


def alert_action_callback_url(service_public_url: str) -> str:
    return f"{service_public_url.rstrip('/')}{ACTION_CALLBACK_PATH}"


def feedback_dialog_callback_url(service_public_url: str) -> str:
    return f"{service_public_url.rstrip('/')}{FEEDBACK_DIALOG_CALLBACK_PATH}"


def build_alert_title_attachment(
    *,
    title: str,
    title_link: str | None,
) -> dict:
    """Text-only "Создана задача" notice, posted as the first thread reply."""
    issue_text = f"[{title}]({title_link})" if title_link else title
    return {
        "fallback": title,
        "color": TITLE_ATTACHMENT_COLOR,
        "text": f"**Создана задача: {issue_text}**",
    }


def build_alert_actions_attachment(
    *,
    alert_post_id: str,
    callback_url: str,
) -> dict:
    """Validity menu plus follow-up buttons, posted as the second thread reply."""
    actions = [
        {
            "id": ACTION_VALIDITY,
            "name": "Выбрать валидность ▼",
            "type": "select",
            "integration": {
                "url": callback_url,
                "context": {
                    "action": ACTION_VALIDITY,
                    "alert_post_id": alert_post_id,
                },
            },
            "options": [
                {"text": option.text, "value": option.value}
                for option in VALIDITY_OPTIONS
            ],
        }
    ]
    for button in ALERT_ACTION_BUTTONS:
        action: dict = {
            "id": button.id,
            "name": button.name,
            "type": "button",
            "integration": {
                "url": callback_url,
                "context": {
                    "action": button.id,
                    "alert_post_id": alert_post_id,
                },
            },
        }
        if button.style:
            action["style"] = button.style
        actions.append(action)
    return {
        "fallback": "Действия по алерту",
        "color": ACTION_ATTACHMENT_COLOR,
        "actions": actions,
    }


def build_alert_feedback_attachment(
    *,
    alert_post_id: str,
    callback_url: str,
) -> dict:
    context: dict[str, str] = {
        "action": FEEDBACK_ACTION_BUTTON.id,
        "alert_post_id": alert_post_id,
    }
    action: dict = {
        "id": FEEDBACK_ACTION_BUTTON.id,
        "name": FEEDBACK_ACTION_BUTTON.name,
        "type": "button",
        "integration": {"url": callback_url, "context": context},
    }
    if FEEDBACK_ACTION_BUTTON.style:
        action["style"] = FEEDBACK_ACTION_BUTTON.style
    return {
        "fallback": "Обратная связь",
        "color": FEEDBACK_ATTACHMENT_COLOR,
        "actions": [action],
    }
