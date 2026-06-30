# CLAUDE.md

Роутер документации + правила репозитория. Документация разбита на маленькие файлы —
открывай нужный под задачу (таблица внизу), не читай всё подряд. Ссылки на `docs/` держи
**обычными markdown-ссылками, не `@`-импортами**: `@` разворачивается в контекст при
старте и раздувает каждую сессию, markdown грузится по требованию. Если код и проза
разошлись — верь коду.

Используй todo list если задача из нескольких шагов.

**Проект:** бот-мост `Mattermost alert → Jira incident`. Python 3.11+, FastAPI +
async `httpx`, SQLAlchemy 2.0. Один процесс; сервис собран из доменных миксинов
(см. [docs/architecture.md](docs/architecture.md)).

## Команды

- `python -m venv .venv && source .venv/bin/activate` — создать и войти в venv.
- `pip install -e ".[test]"` — editable-установка с pytest, ruff, pyright.
- `python -m mm_jira_bot` — локальный запуск на `0.0.0.0:8080`, читает `.env`.
- `curl http://localhost:8080/healthz` — health-check.
- `docker compose up --build` — запуск с Postgres.

Тестовые команды и раскладка — [docs/testing.md](docs/testing.md); переменные
окружения — [docs/config.md](docs/config.md).

## Гейт — два тира (CI нет, всё локально)

Гоняй из локального `.venv` и не останавливайся на первой ошибке — нужен полный срез.

**`/gate` (быстрый)** — на каждой итерации/коммите. Дёшево, гоняй свободно:

```
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/pyright
.venv/bin/pytest -q
```

**`/gate full`** — перед PR / вливанием в `main`. Быстрый тир плюс:

```
.venv/bin/python scripts/gen_service_map.py --check
```

…и doc-sync (ниже). Тяжёлые проверки (service-map, doc-sync) НЕ обязательны на каждой
мелкой правке — достаточно привести их в порядок перед PR. Скилл `/gate` гоняет это
inline; агент `service-verifier` добавляет адверсари-ревью (полный тир) для
move-only рефакторов.

**Baseline pyright (не паникуй на known-ошибке).** На чистом дереве pyright даёт
**ровно 1 pre-existing ошибку** — `reportAttributeAccessIssue` в `tests/test_postmortem.py`
(доступ к `.status` у `ActionResult`). Ровно она → pyright **PASS** (0 новых); любая
другая → новая, **FAIL** (сравни с чистым HEAD: `git stash` или `git show HEAD:<файл>`).
Конфиг линта/типов и per-file исключения (`E501` и пр.) — в `pyproject.toml`.

## Карта сервиса генерируется, не пишется руками

[`docs/reference/service-map.md`](docs/reference/service-map.md) собирается из AST.
После изменения кода (новые/изменённые публичные сигнатуры, маршруты, миксины, файлы)
перегенерируй **перед PR**:
`.venv/bin/python scripts/gen_service_map.py && git add docs/reference/service-map.md`.
Иначе шаг `--check` в `/gate full` упадёт.

## Релизный цикл

Разработка идёт в **ветке версии** (напр. `0.9.0`), не в `main` напрямую.

- **Фича = отдельный коммит** в ветку версии. Дробно и сфокусированно, не «всё одним
  комком».
- **Хотфикс — исключение:** срочную правку можно вести отдельной hotfix-веткой, минуя
  ветку версии.
- **Запись в CHANGELOG** идёт под секцию текущей версии `## [X.Y.Z]` (а не в
  `[Unreleased]`). Заголовок версии создаётся при старте ветки.
- **Мерж в `main`:** когда изменений накопилось достаточно, Claude **сам предлагает**
  влить ветку версии в `main` — с датой в заголовке `## [X.Y.Z] - YYYY-MM-DD`, git-тегом
  `vX.Y.Z` и (если нужно) бампом версии под следующий цикл.

## Doc-sync (перед PR)

При изменении поведения/архитектуры приведи в соответствие нужный документ и добавь
запись в секцию текущей версии `## [X.Y.Z]` в [CHANGELOG.md](CHANGELOG.md):

- домен/архитектура → нужный файл в [`docs/`](docs/);
- пользовательское поведение/конфиг → [README.md](README.md) / [docs/config.md](docs/config.md);
- любое изменение → запись `## [X.Y.Z]` в [CHANGELOG.md](CHANGELOG.md).

`service-map.md` руками НЕ синкается — его держит шаг `--check` в `/gate full`. Эталон
«что генерируется vs что пишется руками» — [docs/architecture.md](docs/architecture.md).

Пиши доки и `CHANGELOG` сжато и по делу, простым языком — без лишних слов и усложнения
того, что можно сказать просто.

## Стиль кода

Держись соседнего стиля: 4 пробела, type hints, `from __future__ import annotations`,
frozen dataclasses для value-объектов, маленькие модули с явной ответственностью.
`snake_case` для функций/переменных/модулей, `PascalCase` для классов. Чёткие
async-границы для Mattermost, Jira и методов сервиса. Формат/линт — `ruff`, типы —
pyright; гоняй перед коммитом, держи диффы узкими.

Меняя/добавляя миксин в `service/`, следуй конвенции из
[docs/architecture.md](docs/architecture.md).

## Тесты

pytest + pytest-asyncio (`asyncio_mode = "auto"` — без `@pytest.mark.asyncio`). Файлы
`test_*.py`, функции `test_<behavior>`; сьют сервиса разбит по доменам зеркально
`service/`; общие фейки/фикстуры — `tests/support.py` и `tests/conftest.py` (фейки +
временная SQLite вместо живых зависимостей). Добавляй/расширяй тесты на любое
изменение поведения — особенно идемпотентность, retry/recovery, slash-команды, формат
Jira payload/опций. Полный харнес — [docs/testing.md](docs/testing.md).

## Commit / PR

Краткие императивные сабжекты (напр. `Add admin API for alert ticket operations`),
маленькие сфокусированные коммиты. PR описывает изменение поведения, перечисляет
команды верификации и результат, отмечает влияние на конфиг/миграции, линкует связанные
задачи. Скриншоты/примеры запросов — только при изменении видимых Mattermost-сообщений
или HTTP-поведения.

**Перед коммитом/PR с осмысленной правкой кода** спроси, не запустить ли `/code-review`
и `/simplify`; `/security-review` — раз в несколько коммитов или при изменениях в
авторизации/секретах/внешнем вводе. Сам не запускай без подтверждения; на doc-only
правках не дёргай.

## Безопасность и конфиг

Вся конфигурация — из env (`config.py`, читается из `.env`); матрица —
[docs/config.md](docs/config.md), шаблон — `.env.example`. Никогда не коммить реальные
токены: Jira/Mattermost токены, channel id, DB URL — секреты. Заметные дефолты:
`ENABLE_BACKFILL_ON_STARTUP=false` (обрабатывать только новые WS-события, не историю),
`ENABLE_WEBSOCKET=true`. Меняя схему — держи `migrations/`, SQLAlchemy-модель и
стартовую инициализацию согласованными ([docs/persistence.md](docs/persistence.md)).

## Язык

`docs/` — английский; `CLAUDE.md`, `README.md`, `CHANGELOG.md` — русский.

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
| Резолв полей/опций Jira, формат date-time, read-only стаб | [docs/jira.md](docs/jira.md) |
| Read-only / shadow-режим: зеркало в аудит-канал, подавление записей, тест-каналы | [docs/read-only.md](docs/read-only.md) |
| Схема БД, миграции, идемпотентность, таймзона | [docs/persistence.md](docs/persistence.md) |
| Переменные окружения (матрица required/optional) | [docs/config.md](docs/config.md) |
| Preflight, ops-канал, recovery/retry, логи | [docs/operations.md](docs/operations.md) |
| Тесты, харнес, как запускать | [docs/testing.md](docs/testing.md) |
| Пользовательская настройка и поведение | [README.md](README.md) |
| Хронология изменений | [CHANGELOG.md](CHANGELOG.md) |
