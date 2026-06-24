# План: модуляризация документации (README / AGENTS.md / docs/)

## Context

`README.md` (597 строк) и `AGENTS.md` (368 строк) сильно дублируют друг друга:
обе описывают потоки alert→Jira, ручные инциденты, идемпотентность, Jira
field-resolution. README перегружен developer-контентом (Database Schema, Tests,
Lint, каталог log-событий, механика retry), который пользователю не нужен.
AGENTS.md держит глубокую механику прямо в одном файле — плохо читается агентом,
который работает над конкретным доменом.

**Цель:** три уровня с принципом *single source of truth* (каждый факт — в одном
месте, из остальных — ссылка):
- **README.md** — только для пользователя/оператора: setup, env-var reference,
  поведение, матрица «Сводка: что на что влияет».
- **AGENTS.md** — хаб + индекс архитектуры: слои/граф зависимостей, таблица
  ответственности модулей, конвенции (style/test/PR), и «карта документации» с
  путями в `docs/`. Важные детали — кратко; глубокая механика вынесена.
- **docs/** — глубокие разборы **по доменам** (не по файлам).

**Решения пользователя:**
1. **Sequencing — план сейчас, исполнение ПОСЛЕ.** Рефактор `service.py` идёт на
   фоне (REFACTOR_PLAN.md), каждый его PR правит `AGENTS.md` и `CHANGELOG.md`.
   Чтобы не ловить конфликты, этот план **исполняется после того, как PR'ы
   рефактора (особенно затрагивающие AGENTS.md) завершатся**. Сигнал к старту:
   рефактор-агент закончил, `git status` чист по `AGENTS.md`/`service/`.
2. **Гранулярность — по доменам.** Один doc на домен, границы совпадают со слоями
   и мик­синами из REFACTOR_PLAN §4.

⚠️ **Не пиннить line-numbers и несуществующие файлы.** Исходники активно меняются
рефактором. Описывать домены мик­синов по имени (`AlertMixin`/`_alerts.py` и т.д.,
как в REFACTOR_PLAN §4), но **без** якорей вида `_alerts.py:120`.

### Шаг 0 исполнения (ОБЯЗАТЕЛЬНО — план применяется ПОЗЖЕ)
Этот план написан по снимку README.md/AGENTS.md **до** рефактора. К моменту
исполнения рефактор-агент перепишет ровно те секции AGENTS.md, что мы выносим
(«Two flows», «Manual incidents», «Jira field resolution» — они ссылаются на
методы `service.py`, ставшие методами мик­синов). Поэтому **перед исполнением**:
заново прочитать актуальные README.md и AGENTS.md и пере-вывести карту
«секция → docs/». Диапазоны строк в этом плане — pre-refactor, индикативны.

---

## Целевая структура `docs/`

```
docs/
  README.md            # индекс docs/: одна строка на каждый файл
  architecture.md      # слои + граф зависимостей (7 слоёв из карты модулей)
  flows.md             # два идемпотентных потока + ручные инциденты + идемпотентность
  jira.md              # field/option resolution, createmeta, payloads, datetime-форматы
  llm-postmortem.md    # postmortem + thread-summary генерация, streaming, prompt-resolution
  persistence.md       # repository, ORM-модели, init_db vs migrations/, timezone
  observability.md     # logging (каталог событий), metrics (Prometheus series), ops-канал
  service-package.md   # устройство пакета service/: мик­сины, SharedMixin, координатор
```

Дискриминатор «нужен ли отдельный doc»: есть **нетривиальный самостоятельный
материал** И читатель приходит к нему **по концепту**. Поэтому НЕ заводим doc на
30-строчный `summary.py` — такие модули остаются строкой в таблице AGENTS.md.

---

## Распределение контента (что куда переезжает)

### docs/architecture.md
Источник: AGENTS.md `## Architecture` (30–65) + 7-слойный граф зависимостей из
карты модулей (domain → infra → clients → presentation → persistence →
orchestration → web/entry). Single-process модель, background-loops (websocket,
pending_work, authorized_users_refresh, startup_preflight), HTTP-эндпоинты.

### docs/flows.md
Источник: AGENTS.md `### Two flows, both idempotent` (91–180) +
`### Manual incidents: button card` (267–316). Сюда же — механика из README
`Idempotency` (462–468) и механическая часть `Recovery and Retry` (470–480).
Описывать домены по мик­синам: alerts (`_alerts.py`), incidents (`_incidents.py`),
thread-summary (`_thread_summary.py`), routing (coordinator).

### docs/jira.md
Источник: AGENTS.md `### Jira field/option resolution` (182–208 — field-resolution
часть) + механика из README `Jira Setup` (201–208, createmeta-эндпоинты,
datetime-форматы). Покрывает `jira.py`, `jira_payload.py`.

### docs/llm-postmortem.md
Источник: AGENTS.md `### Jira field/option resolution` (209–265 — postmortem,
LLM-streaming, `_set_time_to_fix`, `_resolve_prompt_template` precedence).
Покрывает `llm.py`, `postmortem.py`, `summary.py` и мик­сины
`_postmortem.py`/`_thread_summary.py`.

⚠️ **Шов flows.md ↔ llm-postmortem.md (иначе дубль).** `_thread_summary.py`
упоминается в обоих доменах — это ровно то дублирование, которое план убивает.
Разграничение: **flows.md владеет триггером/роутингом** (реакция summary →
dispatch) и только **ссылается**; **llm-postmortem.md владеет механикой генерации**
(streaming, placeholder, finalize). Тот же шов для postmortem: триггер — во
flows.md, генерация — здесь.

### docs/persistence.md
Источник: AGENTS.md `### Persistence & timezone` (318–329) + README
`Database Schema` (444–460). Двойной механизм миграций: `init_db` +
`_ensure_alert_ticket_columns` **и** `migrations/*.sql` — явно описать оба.
Покрывает `repository.py`, `domain.py` (timezone).

### docs/observability.md
Источник: README `Logs` каталог событий (507–538) + uvicorn-детали (548–553),
`Метрики Prometheus` series-каталог (414–426), `Ops-канал` механика. Покрывает
`logging.py`, `metrics.py`, `ops.py`.

### docs/service-package.md
**Новый материал** (не дублирует существующее): устройство пакета `service/`
после рефактора — мик­син-паттерн, `SharedMixin` vs free-функции, координатор,
re-export shim. ⚠️ **Источник — ФИНАЛЬНЫЙ код пакета `service/`, а не
REFACTOR_PLAN.md.** Тот оставляет решения открытыми (pyright Protocol — «решить
на пилоте»; §8 «разрешены» = намерения). REFACTOR_PLAN использовать только как
структурный ориентир; описывать то, что реально в мик­син-файлах.

---

## Итог по README.md (остаётся / удаляется)

**ОСТАЁТСЯ (user/operator):** Workflow (поведенческий нарратив + mermaid),
Mattermost Bot Account, Slash Command, Повторные алерты, Validity Reactions,
Ограничение круга пользователей, Action Buttons, **Сводка: что на что влияет**
(126–181, главный keep), Jira Setup (env-vars + опции), LLM Postmortems (env-vars
+ поведение), Ручные инциденты (user-видимый флоу), Startup Preflight (operator),
Configuration, Run Locally, Debug Admin (enable + security), Ops-канал/Metrics
(toggles), Docker, API References.

**УДАЛЯЕТСЯ → строка-указатель в docs/:** Database Schema → `docs/persistence.md`;
Idempotency → `docs/flows.md`; механика Recovery/Retry → `docs/flows.md`; каталог
log-событий в `Logs` → `docs/observability.md`; Prometheus series-каталог →
`docs/observability.md`; Tests + Lint/format → AGENTS.md (там уже есть). В README
оставить env-vars/команды этих секций, унести только механику/каталоги.

В MIXED-секциях — хирургия по строкам (детали в карте README выше): держим
env-vars и operator-команды, уносим механику и reference-каталоги.

---

## Итог по AGENTS.md (хаб + индекс)

**ОСТАЁТСЯ:** preamble, `## Build/Test/Dev Commands`, `### Module responsibilities`
(таблица — обновить под пакет `service/` уже сделано рефактором), сжатая
`## Architecture` (overview + ссылка на `docs/architecture.md`),
`## Coding Style`, `## Testing`, `## Commit & PR`, `## Security & Configuration`.

**ВЫНОСИТСЯ в docs/ (заменить на 2–3 строки + ссылку):**
`### Two flows, both idempotent` → `docs/flows.md`;
`### Jira field/option resolution` → `docs/jira.md` + `docs/llm-postmortem.md`;
`### Manual incidents` → `docs/flows.md`;
`### Persistence & timezone` → `docs/persistence.md`.

**ДОБАВИТЬ:** секцию `## Documentation map` — таблица доменов → путь в `docs/`,
чтобы агент находил нужный разбор рядом с кодом.

---

## CLAUDE.md (обязательно — иначе агенты не найдут docs/)

Добавить в список «Где искать информацию» пункт:
`**[docs/](docs/)** — глубокие разборы по доменам; индекс в docs/README.md.`

---

## Критические файлы

- `README.md` — обрезать до user/operator, заменить вынесенные секции на указатели.
- `AGENTS.md` — обрезать глубокую механику, добавить `## Documentation map`.
- `CLAUDE.md` — зарегистрировать `docs/`.
- `docs/*.md` — 8 новых файлов (см. структуру выше).
- `CHANGELOG.md` — запись в `[Unreleased]` про реструктуризацию доки.
- `REFACTOR_PLAN.md` — после завершения рефактора это устаревший root-артефакт;
  удалить или перенести в `docs/` (по согласованию с рефактор-агентом).

---

## Verification

1. **Полнота переноса:** для каждой вынесенной секции — `grep` ключевых терминов
   (напр. `_ensure_jira_issue`, `createmeta`, `_resolve_prompt_template`,
   `LogRingBuffer`) подтверждает, что факт жив ровно в одном docs/-файле, а из
   README/AGENTS на него идёт ссылка (нет осиротевшего дубля).
2. **Битые ссылки:** проверить все markdown-ссылки между README ↔ AGENTS ↔ docs/
   (`grep -rEo '\]\([^)]+\.md[^)]*\)'` + проверка существования путей).
3. **Нет line-anchors:** `grep -rn '\.py:[0-9]' docs/ AGENTS.md README.md` должен
   быть пуст (исходники меняются — якоря протухнут).
4. **README — только user:** ручной просмотр на отсутствие developer-каталогов
   (event-список, Prometheus series, ORM-поля, retry-механика).
5. **CLAUDE.md / docs/README.md индексы** перечисляют все 8 файлов docs/.
6. Содержательная сверка docs/ с кодом запускается ПОСЛЕ завершения рефактора,
   чтобы имена мик­синов/методов совпадали с финальным состоянием `service/`.
