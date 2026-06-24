# План: разбивка `service.py` на доменные mixin-классы

> Цель: разбить god-класс `IncidentBotService` (2918 строк, 55 методов) на пакет
> `service/`, где методы вынесены в доменные **mixin-классы**, собираемые в один
> `IncidentBotService`. Каждый файл — читаемый для LLM-агента (≤ ~500 строк, один
> домен).
>
> Паттерн: **Mixin-классы по доменам** (выбран ради читаемости при минимальном
> риске — см. §2). Объём: **план → по этапам, один домен за PR**.

---

## 1. Целевая структура

```
src/mm_jira_bot/service/
  __init__.py        # re-export: from .coordinator import IncidentBotService
  coordinator.py     # IncidentBotService(AlertMixin, IncidentMixin, ...) — конструктор, роутеры, auth
  _shared.py         # SharedMixin + free-функции форматтеров (см. §3)
  _alerts.py         # AlertMixin
  _incidents.py      # IncidentMixin
  _postmortem.py     # PostmortemMixin
  _thread_summary.py # ThreadSummaryMixin
  _jira_sync.py      # JiraSyncMixin
  _debug.py          # DebugMixin  (тонкий — большая часть уже в debug_admin.py)
```

Импорт `from mm_jira_bot.service import IncidentBotService` **остаётся рабочим**
(re-export в `__init__.py`), поэтому `web.py` и `debug_admin.py` по импортам не
трогаем. Файлы миксинов — `_`-префикс, т.к. это внутренняя кухня пакета.

---

## 2. Механика связывания: mixin (определяет весь рефакторинг)

Shared state класса: `settings`, `repository`, `mattermost`, `jira`, `llm`,
`_authorized_user_ids`, `_authorization_enforced`.

**Все домены — это `*Mixin` без своего `__init__`.** State устанавливается один раз
в `IncidentBotService.__init__` (в `coordinator.py`), миксины обращаются к нему
через `self` как и раньше. Финальный класс:

```python
class IncidentBotService(
    SharedMixin,
    AlertMixin,
    IncidentMixin,
    JiraSyncMixin,
    PostmortemMixin,
    ThreadSummaryMixin,
    DebugMixin,
):
    def __init__(self, *, settings, repository, mattermost_client, jira_client, llm_client=None):
        ...  # как сейчас, без изменений
    # роутеры handle_websocket_event / handle_reaction / handle_slash_command — здесь
```

**Почему mixin, а не handler-классы с ctx:** при общем state через `self`/ctx
связанность у обоих вариантов одинаковая, и разбивка файлов идентична. Разница —
только цена и риск. Mixin сохраняет внутренние вызовы один-в-один
(`self._ensure_jira_issue()` остаётся как есть), не требует слоя делегатов, не
вводит координаторный ctx и **не создаёт риска циклических импортов**. Цель
рефактора — читаемость файлов для LLM; mixin достигает её при минимальном диффе.
(Размен: домены НЕ становятся независимо инстанцируемыми/инжектируемыми — это
сознательно отброшено как ненужное сейчас.)

### Типизация миксинов — КОНВЕНЦИЯ (подтверждена на пилоте thread_summary)

Миксин ссылается на атрибуты state и sibling-методы, которых в нём самом нет.
Пилот вживую показал pyright-вывод (80 ошибок на первой наивной попытке) и зафиксировал
правило: **MOVE-ONLY распространяется и на типы — не вводим проверок строже, чем было
в исходном `service.py` (baseline pyright = 0).** Конкретно:

1. **State-атрибуты: объявлять ТОЛЬКО те, что трогает этот миксин, и тем же типом,
   что декларирует `coordinator.__init__`.** В `__init__` типизированы только
   `settings: Settings` и `repository: AlertTicketRepository`; параметры
   `mattermost_client` / `jira_client` / `llm_client` идут БЕЗ аннотаций. Значит:
   - `settings: Settings`, `repository: AlertTicketRepository` — где используются;
   - `mattermost`, `jira`, `llm` → **`Any`** (`from typing import Any`).
   ⚠️ Конкретные классы (`MattermostClient`, `PostmortemLlmClient`) НЕЛЬЗЯ — они
   ужесточают тип собранного класса, и фейки в тестах (`service.mattermost.posts`,
   `service.llm = FakeLlmClient()`) перестают проходить pyright. Это нарушение
   move-only (пришлось бы править тесты — страховочный трос).

2. **Cross-mixin вызовы: инлайновые стабы под `if TYPE_CHECKING:` в самом миксине**
   (НЕ центральный `_ServiceProtocol` — он стал бы растущим узлом связности, который
   каждый PR правит; инлайн делает файл самоописательным для LLM-агента). Сигнатуры
   повторяют реальные. `@staticmethod`-методы стабятся тоже как `@staticmethod`,
   иначе pyright выдаёт `reportIncompatibleMethodOverride` на собранном классе.

```python
from __future__ import annotations
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    from mm_jira_bot.config import Settings

class ThreadSummaryMixin:
    settings: Settings
    mattermost: Any
    llm: Any

    if TYPE_CHECKING:  # стабы sibling-методов из других классов собранного сервиса
        @staticmethod
        def _box_thread_reply(
            message: str, props: dict | None, color: str
        ) -> tuple[str, dict | None]: ...
        def _resolve_prompt_template(self, key: str) -> str | None: ...
```

3. **Логгер: `log = get_logger("mm_jira_bot.service")` в КАЖДОМ файле пакета**
   (не `__name__` — иначе имя логгера сместится; ни один из gate'ов это не ловит,
   ставить вручную).

4. **Разделяемые runtime-имена (констант/dataclass), которые миксин импортирует и
   которые определены в coordinator → выносить в `_shared.py`** (лист графа импортов,
   ничего не импортирует обратно). Иначе цикл: coordinator импортирует миксин для
   наследования → миксин импортирует имя из ещё-не-инициализированного coordinator →
   `ImportError: partially initialized module`. На пилоте так переехали `ActionResult`,
   `SUMMARY_PENDING_TEXT`, `SUMMARY_FAILED_TEXT`, `_PROMPT_KEY_SUMMARY/POSTMORTEM`.
   Reorder-хак (импорт миксина после определений) НЕ годится — isort (`ruff`, правило
   `I`) поднимет импорт обратно наверх.

---

## 3. `_shared.py` — общие примитивы (выносим вместе с первым потребителем)

Используются ≥2 доменами (по карте вызовов):

| метод | потребители | куда |
|---|---|---|
| `box_thread_reply` (staticmethod, чистый форматтер) | thread_summary, incident/alert | **free-функция** в `_shared.py` |
| `post_alert_thread_reply` | alert, jira_sync, incident, auth-notice | SharedMixin (трогает mattermost) |
| `post_incident_thread_reply` | incident, postmortem-пути | SharedMixin |
| `resolve_user_display` | alert, postmortem, incident, debug | SharedMixin |
| `interactive_controls_enabled` | alert, incident, jira_sync | SharedMixin |
| `action_callback_url` | alert, incident, jira_sync | SharedMixin |
| `resolve_prompt_template` / `prompt_env_default` | postmortem, thread_summary, debug_admin (внешний) | SharedMixin |

Дискриминатор «функция или метод SharedMixin»: **трогает ли она clients/state**.
Чистые форматтеры (`box_thread_reply`, `summary_base_props` если без state) →
module-level free-функции. Дёргающие `self.mattermost/repository/...` → SharedMixin.

⚠️ `_prompt_env_default` читается **снаружи** из `debug_admin.py:33` — имя с `_`,
но де-факто публичный API. Метод остаётся на собранном классе (через SharedMixin) →
внешний доступ `service._prompt_env_default(...)` сохраняется без изменений.

⚠️ Перенос shared-примитивов — это извлечение с cross-domain последствиями, **не**
часть скелета. Переносить вместе с первым доменом-потребителем (пилот), а не в PR #0.

---

## 4. Раскладка методов по миксинам

### coordinator.py — IncidentBotService (infra / auth / routing)
`__init__`, `resolve_authorized_users`, `_degrade_authorization`, `_is_authorized`,
`handle_websocket_event`, `_is_bot_post`, `handle_reaction` (роутер),
`handle_slash_command` (роутер). Роутеры зовут доменные методы напрямую через
`self` (они доступны через mixin-наследование).

### _alerts.py — AlertMixin
`handle_alert_post`, `handle_alert_action`, `_alert_action_attachments`,
`open_feedback_dialog`, `handle_feedback_dialog_submission`, `apply_validity_label`,
`_alert_attachments`.
Cross-domain (через `self`): `_ensure_jira_issue`, `_handle_expected_repeat`,
`confirm_incident`, `handle_incident_action`.

### _incidents.py — IncidentMixin
`handle_manual_incident_post`, `_incident_duty_help`, `_post_incident_thread_mention`,
`_incident_controls_attachment`, `handle_incident_action`, `_incident_create_task`,
`handle_incident_checkmark`, `_set_incident_validity`, `_mark_incident_post_completed`,
`apply_incident_end_time`, `confirm_incident`, `_publish_incident_message_if_needed`.
Cross-domain: `generate_incident_postmortem`, `_update_jira_for_confirmation`,
`generate_thread_summary`.

### _postmortem.py — PostmortemMixin
`generate_incident_postmortem`, `_set_time_to_fix`, `_apply_postmortem_validity`,
`_ensure_postmortem_jira_issue`, `_postmortem_thread_context`.
Cross-domain: вся summary-механика (`_post_summary_placeholder`, `_set_summary_status`,
`_generate_and_finalize_summary`, `_create_thread_summary_reply`,
`_finalize_thread_summary_reply`).

### _thread_summary.py — ThreadSummaryMixin  ← ПИЛОТ
`generate_thread_summary`, `_create_thread_summary_reply`, `_edit_summary_reply`,
`_set_summary_status`, `_finalize_thread_summary_reply`, `_summary_base_props`,
`_post_summary_placeholder`, `_make_summary_stream_callback`,
`_generate_and_finalize_summary`, `_publish_thread_summary`.
(`_box_thread_reply` → free-функция в `_shared.py`.)

### _jira_sync.py — JiraSyncMixin
`_ensure_jira_issue`, `_handle_expected_repeat`, `_create_jira_issue`,
`_stub_jira_issue`, `_display_jira_issue`, `_update_jira_for_confirmation`,
`process_pending_work`, `backfill_recent_alerts`.
Cross-domain: `_alert_action_attachments`, `_post_alert_thread_reply`,
`confirm_incident`.

### _debug.py — DebugMixin
`debug_create_from_link`, `debug_recreate_jira_issue`.

---

## 5. Внешний публичный API — НЕ ЛОМАТЬ

Подтверждено grep'ом (web.py, debug_admin.py). С mixin **делегаты не нужны** — все
эти методы оказываются на собранном `IncidentBotService` через наследование, имена
и сигнатуры сохраняются автоматически.

**Методы:** `handle_websocket_event`, `process_pending_work`,
`resolve_authorized_users`, `backfill_recent_alerts`, `handle_slash_command`,
`handle_alert_action`, `handle_feedback_dialog_submission`, `debug_create_from_link`,
`debug_recreate_jira_issue`, `_prompt_env_default`.
(`handle_alert_post` / `handle_reaction` извне НЕ зовутся — только через
`handle_websocket_event`; отдельного внимания не требуют.)

**Атрибуты:** `repository`, `settings`, `mattermost`, `jira`, `llm` — ставятся в
`__init__`, остаются на инстансе.

**Конструктор:** `IncidentBotService(settings=, repository=, mattermost_client=, jira_client=, llm_client=)`.

`ops.py` и `__main__.py` сервис напрямую не трогают.

---

## 6. Принципы исполнения (обязательны)

- **MOVE-ONLY.** Каждый PR — чистый перенос методов между файлами, **без правок
  логики и без разбиения методов**. Роутеры (`handle_reaction`, `handle_alert_action`)
  и крупные методы переносятся целиком. Хочется разбить метод — отдельный PR ПОСЛЕ
  переноса. Именно move-only делает рефактор верифицируемым зелёными тестами.
- **TYPE_CHECKING против циклов.** Импорты типов координатора/доменов в миксинах —
  только под `if TYPE_CHECKING:`. С mixin runtime-импортов между доменами быть не
  должно вовсе (всё резолвится наследованием) — это снимает главный риск слома.
- **Покрытие до старта.** В репо есть `.coverage`. Прогнать покрытие именно по
  `service.py` до первого PR — зелёные тесты защищают только покрытый код; знать
  слепые зоны cross-domain проводки нужно ДО переноса.
- **После каждого PR:** `pytest` зелёный + `ruff` + `pyright`. Обновлять
  `AGENTS.md` / `CHANGELOG.md` по требованию CLAUDE.md.

---

## 7. Порядок PR (по возрастанию связанности)

| # | PR | Содержание | Риск |
|---|----|-----------|------|
| 0 | ✅ **Скелет** | Создать пакет `service/`, перенести класс БЕЗ изменений в `coordinator.py`, `__init__.py` re-export. Никаких миксинов и shared-переносов. Тесты зелёные. | мин. |
| 1 | ✅ **ПИЛОТ** | thread_summary → ThreadSummaryMixin + `box_thread_reply` в `_shared.py`. Проверить mixin-подход и вывод pyright. | низкий |
| 2 | ✅ postmortem → PostmortemMixin (`_postmortem.py`, 5 методов; shared-переносов не потребовалось) | зависит от thread_summary (вынесен) | средний |
| 3 | jira_sync → JiraSyncMixin (+ SharedMixin примитивы по мере нужды) | средний |
| 4a | incidents → IncidentMixin (с `confirm_incident`) | выше |
| 4b | alerts → AlertMixin | выше |
| 5 | debug → DebugMixin; финальная зачистка `coordinator.py` | низкий |
| 6 | Разбить `test_service.py` (4795 строк) по доменам | низкий |

**Про PR #6 / тесты:** разбивать `test_service.py` — В КОНЦЕ. До этого момента он
остаётся единым и зелёным как **страховочный трос**, доказывающий сохранение
поведения на каждом src-PR. Дробить тест-файл синхронно = трогать сам трос во
время рефактора. Re-export гарантирует, что тест не требует правок по импортам всё
это время. Финальная разбивка теста — косметика; гарантию даёт зелёный прогон.

---

## 8. Открытые вопросы — РАЗРЕШЕНЫ

1. SharedMixin vs free-функции → дискриминатор «трогает ли clients/state». ✓ (§3)
2. Делегаты на координаторе → **не нужны при mixin** (наследование). ✓ (§5)
3. incidents+alerts → раздельно, incidents первым (PR 4a/4b). ✓ (§7)
4. Разбивка тестов → PR #6 в конце, трос зелёный всю дорогу. ✓ (§7)
5. Паттерн mixin vs handler → **mixin** (читаемость при мин. риске). ✓ (§2)
