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
# Blue accent for the main controls block ("Создана задача" + validity + buttons).
ACTION_ATTACHMENT_COLOR = "#3B82F6"
# Muted gray accent for the feedback block below.
FEEDBACK_ATTACHMENT_COLOR = "#4B5563"
# A blank line (the zero-width space keeps Mattermost from trimming it) that
# adds a little vertical spacing between the notice and the controls below.
CONTROLS_SPACER = "\n​"


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
) -> dict:
    """Main block: "Создана задача" notice, the validity menu, and the
    incident/summary buttons under it. Rendered with the blue accent."""
    issue_text = f"[{title}]({title_link})" if title_link else title
    validity_select = {
        "id": ACTION_VALIDITY,
        "name": "Выбрать валидность ▼",
        "type": "select",
        "integration": _integration(
            ACTION_VALIDITY, alert_post_id=alert_post_id, callback_url=callback_url
        ),
        "options": [
            {"text": option.text, "value": option.value}
            for option in VALIDITY_OPTIONS
        ],
    }
    buttons = [
        _button_action(button, alert_post_id=alert_post_id, callback_url=callback_url)
        for button in PRIMARY_ACTION_BUTTONS
    ]
    return {
        "fallback": title,
        "color": ACTION_ATTACHMENT_COLOR,
        # Trailing blank line gives a little spacing before the row of controls.
        "text": f"**Создана задача: {issue_text}**{CONTROLS_SPACER}",
        "actions": [validity_select, *buttons],
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
