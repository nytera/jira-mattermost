from __future__ import annotations

from dataclasses import dataclass

# Interactive action ids carried in the button ``context``. They are stable
# identifiers (not labels), so renaming a button never breaks an in-flight post.
ACTION_VALID = "valid"
ACTION_FALSE = "false"
ACTION_EXPECTED = "expected"
ACTION_INCIDENT = "incident"
ACTION_SUMMARY = "summary"

ACTION_CALLBACK_PATH = "/mattermost/actions/alert"


@dataclass(frozen=True)
class AlertActionButton:
    id: str
    # ``name`` is what Mattermost renders; it accepts ``:shortcode:`` emoji.
    name: str
    style: str | None = None


# Order matters: this is the left-to-right order of the buttons under the alert.
ALERT_ACTION_BUTTONS: tuple[AlertActionButton, ...] = (
    AlertActionButton(ACTION_VALID, ":white_check_mark: Валидный", style="good"),
    AlertActionButton(ACTION_FALSE, ":x: Ложный", style="danger"),
    AlertActionButton(ACTION_EXPECTED, ":arrows_counterclockwise: Ожидаемый"),
    AlertActionButton(ACTION_INCIDENT, ":rotating_light: Дать инцидент", style="primary"),
    AlertActionButton(ACTION_SUMMARY, ":memo: Саммари треда"),
)


def alert_action_callback_url(service_public_url: str) -> str:
    return f"{service_public_url.rstrip('/')}{ACTION_CALLBACK_PATH}"


def build_alert_action_attachment(
    *,
    alert_post_id: str,
    callback_url: str,
) -> dict:
    """Build the message attachment that carries the alert action buttons.

    Each button posts to ``callback_url`` with a ``context`` that identifies the
    action and the alert post it applies to.
    """
    actions = []
    for button in ALERT_ACTION_BUTTONS:
        context: dict[str, str] = {
            "action": button.id,
            "alert_post_id": alert_post_id,
        }
        action: dict = {
            "id": button.id,
            "name": button.name,
            "type": "button",
            "integration": {"url": callback_url, "context": context},
        }
        if button.style:
            action["style"] = button.style
        actions.append(action)
    return {
        "fallback": "Действия по алерту",
        "actions": actions,
    }
