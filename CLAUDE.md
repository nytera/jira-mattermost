# CLAUDE.md

Роутер документации: какой документ открыть под конкретную задачу. Документация
разбита на маленькие файлы — открывай нужный, не читай всё подряд.

## Основные правила

- **Перед коммитом — гейт.** Прогоняй `/gate` (ruff + ruff format `--check` + pyright
  + pytest + `scripts/gen_service_map.py --check`) из `.venv`. Подробности и baseline
  pyright — в [AGENTS.md](AGENTS.md).
- **Карта сервиса генерируется, не пишется руками.** [`docs/reference/service-map.md`](docs/reference/service-map.md)
  собирается из AST. После изменения кода (новые/изменённые публичные сигнатуры,
  маршруты, миксины, файлы) перегенерируй:
  `.venv/bin/python scripts/gen_service_map.py && git add docs/reference/service-map.md`.
  Иначе шаг `--check` в гейте упадёт.
- **Doc-sync.** При изменении поведения/архитектуры до коммита приведи в соответствие
  нужный документ (см. таблицу ниже) и добавь запись в `[Unreleased]` в
  [CHANGELOG.md](CHANGELOG.md). Эталон «что генерируется vs что пишется руками» —
  [docs/architecture.md](docs/architecture.md).
- **Язык.** `docs/` и `AGENTS.md` — английский; `README.md` и `CHANGELOG.md` — русский.

## Куда смотреть под задачу

| Задача / область | Документ |
|---|---|
| Что где лежит: дерево файлов, сигнатуры, маршруты, MRO | [docs/reference/service-map.md](docs/reference/service-map.md) (генерируется) |
| Общая архитектура, сборка миксинов, два потока, `_shared` leaf | [docs/architecture.md](docs/architecture.md) |
| Обработка алертов, кнопки/меню алерта, validity-реакции, feedback (`_alerts.py`) | [docs/domains/alerts.md](docs/domains/alerts.md) |
| Жизненный цикл инцидента, ручные инциденты, чекмарк/END (`_incidents.py`) | [docs/domains/incidents.md](docs/domains/incidents.md) |
| Создание Jira-задачи по алерту, эпизоды/повторы, pending work (`_jira_sync.py`) | [docs/domains/jira-sync.md](docs/domains/jira-sync.md) |
| Генерация постмортема, LLM-поля Jira, стриминг (`_postmortem.py`) | [docs/domains/postmortem.md](docs/domains/postmortem.md) |
| Саммари треда (`_thread_summary.py`) | [docs/domains/thread-summary.md](docs/domains/thread-summary.md) |
| Дебаг-панель, create-from-link, recreate (`_debug.py`, `debug_admin.py`) | [docs/domains/debug.md](docs/domains/debug.md) |
| Резолв полей/опций Jira, формат date-time, тестовый режим | [docs/jira.md](docs/jira.md) |
| Схема БД, миграции, идемпотентность, таймзона | [docs/persistence.md](docs/persistence.md) |
| Переменные окружения (матрица required/optional) | [docs/config.md](docs/config.md) |
| Preflight, ops-канал, метрики, recovery/retry, логи | [docs/operations.md](docs/operations.md) |
| Тесты, харнес, как запускать | [docs/testing.md](docs/testing.md) |
| Стиль кода, конвенции тестов, commit/PR, гейт | [AGENTS.md](AGENTS.md) |
| Пользовательская настройка и поведение | [README.md](README.md) |
| Хронология изменений | [CHANGELOG.md](CHANGELOG.md) |
