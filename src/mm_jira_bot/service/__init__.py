"""Пакет сервиса инцидент-бота.

`IncidentBotService` и связанные с ним публичные имена исторически жили в одном
модуле `mm_jira_bot.service`. Модуль разбит на доменные mixin-файлы внутри пакета
`service/`; здесь — обратно-совместимый ре-экспорт, чтобы внешние импорты
(`from mm_jira_bot.service import IncidentBotService`) продолжали работать.
"""

from mm_jira_bot.service.coordinator import (
    _PROMPT_KEY_POSTMORTEM,
    _PROMPT_KEY_SUMMARY,
    IncidentBotService,
    parse_post_id_from_text,
)

__all__ = [
    "IncidentBotService",
    "parse_post_id_from_text",
    "_PROMPT_KEY_POSTMORTEM",
    "_PROMPT_KEY_SUMMARY",
]
