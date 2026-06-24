"""Разделяемые между доменными mixin-классами примитивы пакета `service/`.

Этот модуль — **лист графа импортов** пакета: он ничего не импортирует обратно из
`coordinator`/доменных миксинов, поэтому константы и dataclass'ы, нужные сразу
нескольким доменам в runtime, живут здесь. Так разрывается цикл «coordinator
импортирует миксин → миксин импортирует имя из coordinator (ещё не определённое)».
"""

from __future__ import annotations

from dataclasses import dataclass

# Тексты плейсхолдера/ошибки тредового саммари (используются ThreadSummaryMixin и
# координатором при публикации саммари из разных потоков).
SUMMARY_PENDING_TEXT = "⏳ Генерация саммари…"
SUMMARY_FAILED_TEXT = "Не удалось сгенерировать саммари, попробуйте позже."

# DB-override keys for the runtime-editable LLM prompt templates (debug panel).
# `_PROMPT_KEY_*` читаются снаружи из `debug_admin.py` через ре-экспорт в
# `service/__init__.py` — менять имена нельзя.
_PROMPT_KEY_SUMMARY = "llm_summary_prompt"
_PROMPT_KEY_POSTMORTEM = "llm_postmortem_prompt"


@dataclass(frozen=True)
class ActionResult:
    """Ephemeral feedback shown to the user who clicked an alert button.

    ``update_attachments``, when set, replaces the originating post's attachments
    via the Mattermost interactive-action ``update`` response (used to swap the
    "Создать задачу" prompt for the full controls card after task creation).
    """

    message: str
    update_attachments: list[dict] | None = None
