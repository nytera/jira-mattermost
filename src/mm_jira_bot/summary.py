from __future__ import annotations

from mm_jira_bot.postmortem import trim_transcript


def build_thread_summary_prompt(
    *,
    thread_url: str,
    participants: list[str],
    transcript: str,
    max_chars: int,
) -> str:
    trimmed_transcript = trim_transcript(transcript, max_chars=max_chars)
    participant_text = ", ".join(participants) if participants else "не указано"
    return f"""Сделай краткое саммари треда Mattermost/Band для дежурных инженеров.

Правила:
- Пиши на русском, кратко и по делу.
- Используй только факты из треда ниже, не выдумывай.
- Времена указывай по московскому времени в формате HH:MM.
- Формат ответа строго такой:
  Суть: одно-два предложения о том, что происходит.
  Хронология:
  - HH:MM - что произошло
  Статус: текущее состояние (в работе / решено / ждём / не указано).
  Дальше: что осталось сделать, если это обсуждалось; иначе "не указано".
- Если каких-то данных нет, пиши "не указано".
- Не добавляй code fences и служебные пояснения.

Тред: {thread_url}
Участники: {participant_text}

Тред:
{trimmed_transcript}
"""


def format_thread_summary_reply(summary: str) -> str:
    return "\n".join(["📝 **Саммари треда**", "", summary.strip()])
