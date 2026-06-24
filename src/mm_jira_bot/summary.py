from __future__ import annotations

from mm_jira_bot.postmortem import trim_transcript

# User-prompt template for the in-thread incident summary. Overridable via
# ``LLM_SUMMARY_PROMPT`` / ``LLM_SUMMARY_PROMPT_FILE`` (see config.py). Supported
# placeholders, substituted by ``build_thread_summary_prompt``: ``{thread_url}``,
# ``{participants}``, ``{transcript}`` (the trimmed thread; substituted last so
# thread text can safely contain brace-looking tokens).
DEFAULT_SUMMARY_PROMPT = """Составь саммари этого треда как инцидентный отчёт по фактам.

Начни с блока мета-информации:
- Название инцидента (кратко)
- Сервис
- Дата/время начала инцидента
- Дата/время восстановления
- Длительность
- Текущий статус
- Как обнаружена проблема (сотрудник / алерт / клиент)

Дальше структура:
1) Проблема
- Что произошло и как проявлялось.

2) Impact
- Кого/что затронуло, критичность, масштаб (если данных нет — TBD).

3) Причина
- Подтверждённая root cause.
- Если причина не подтверждена: перечисли [Гипотеза] с уровнем уверенности (high/medium/low).

4) Решение
- Что сделали, что сработало/не сработало, что остаётся сделать.

5) Участники
- Список участников инцидента: только Имя Фамилия, без тегов и без ролей.

6) Хронология
- Отрази ключевые события по времени, используй часовой пояс МСК (обнаружение, диагностика, важные изменения в понимании проблемы, действия по исправлению, результат), добавь значимые детали, которые повлияли на ход инцидента. Там, где важно, указывай участника (инициатор/исполнитель действия), а не только само действие.

7) Риски рецидива
- Что может повториться в ближайшее время и почему.

8) Проблемы и открытые вопросы
- Проблемы на обсуждение: список слабых мест, заметных по треду (формат: проблема + почему это риск).
- Недостающие данные / открытые вопросы: какие факты не указаны или требуют подтверждения, чтобы закрыть инцидентный анализ.

Правила:
- Не выдумывай факты.
- Явно отделяй факты от предположений.
- Если видишь важные аспекты инцидента, добавь дополнительные блоки или детали, которых нет в шаблоне.

Тред: {thread_url}
Участники: {participants}

Тред:
{transcript}
"""


def build_thread_summary_prompt(
    *,
    thread_url: str,
    participants: list[str],
    transcript: str,
    max_chars: int,
    template: str | None = None,
) -> str:
    trimmed_transcript = trim_transcript(transcript, max_chars=max_chars)
    participant_text = ", ".join(participants) if participants else "не указано"
    body = template or DEFAULT_SUMMARY_PROMPT
    # Metadata first, transcript last (thread text never re-scanned for tokens).
    return (
        body.replace("{thread_url}", thread_url)
        .replace("{participants}", participant_text)
        .replace("{transcript}", trimmed_transcript)
    )


def format_thread_summary_reply(summary: str) -> str:
    return "\n".join(["📝 **Саммари треда**", "", summary.strip()])


def format_thread_summary_streaming(partial: str) -> str:
    """In-progress render of the summary while the LLM streams it into the thread.

    The header carries a "генерируется…" marker so the partial text never reads as
    final; ``format_thread_summary_reply`` overwrites it with the clean header once
    the full text arrives.
    """
    return "\n".join(["📝 **Саммари треда** _(генерируется…)_", "", partial.strip()])
