# Mattermost Jira Incident Bot

Сервис слушает канал алертов в Mattermost, создает Jira issue для каждого нового алерта и позволяет явно подтвердить валидный инцидент реакцией `:incident:` или командой `/incident <mattermost_message_link>`.

## Workflow

1. Бот подключается к Mattermost WebSocket API и слушает события `posted` и `reaction_added`.
2. Новое сообщение в `MATTERMOST_ALERT_CHANNEL_ID` сохраняется в таблицу `alert_tickets`; название алерта берется из первой содержательной строки сообщения.
3. Для сообщения создается Jira issue с текстом алерта, автором, временем, permalink, `post_id`, каналом, `Источник = Crit alert` и `Был ли крит алерт? = Да`. Поле `Valid Incident`/`Валидность` при создании не отправляется: Jira должна поставить свое дефолтное значение. После создания бот отвечает в тред исходного алерта ссылкой на созданную Jira issue.
4. Связь `mattermost_post_id -> jira_issue_key` хранится локально и защищена уникальным индексом.
5. Пользователь подтверждает инцидент реакцией `:incident:` на оригинальное сообщение или slash-командой `/incident <link>`.
6. Бот публикует сообщение в `MATTERMOST_INCIDENT_CHANNEL_ID`, обновляет Jira `Valid Incident = Валидный`, добавляет комментарий со ссылкой на incident-сообщение и, если задано, делает transition issue. После подтверждения бот также отвечает в тред исходного алерта о том, что инцидент заведён (ссылка на Jira, валидность, ссылка на сообщение в канале инцидентов). Имя подтвердившего показывается как `Имя Фамилия (@username)`, а не как сырой `user_id`.
7. Когда по валидному инциденту нажимают галочку (`:white_check_mark:`, `:heavy_check_mark:` или `:ballot_box_with_check:`) на корневом сообщении incident-треда, бот заполняет Jira `Окончание` временем этой реакции. Если настроен LLM, бот также отправляет весь тред в OpenAI-compatible API, оставляет в Jira description PM-шаблон со ссылкой на инцидент, автором и участниками, добавляет LLM-отчет комментарием и публикует краткое summary обратно в тред. Если галочку поставили на корневом сообщении ручного incident-треда без исходного алерта, бот создает новую Jira issue с PM-шаблоном в description, но не заполняет alert-only поля `Источник` и `Был ли крит алерт?`. Галочки на replies игнорируются.

```mermaid
flowchart LR
  A["Alert channel post"] --> B["alert_tickets row"]
  B --> C["Jira issue with Jira default Valid Incident"]
  A --> D["reaction :incident:"]
  A --> E["/incident permalink"]
  D --> F["Confirm by original post_id"]
  E --> F
  F --> G["Post to incidents channel"]
  F --> H["Update Jira Valid Incident=Валидный"]
  H --> I["Jira comment + optional transition"]
```

## Mattermost Bot Account

Создайте bot account или отдельного пользователя-интеграцию, выпустите personal access token и добавьте бота в оба канала:

- канал алертов: право читать сообщения и реакции, а также писать ответы в тред (бот отвечает в тред алерта о созданной задаче и смене статуса);
- канал инцидентов: право писать сообщения;
- WebSocket доступ к `/api/v4/websocket`;
- REST доступ к `/api/v4/posts`, `/api/v4/channels/{channel_id}`, `/api/v4/channels/{channel_id}/posts`, `/api/v4/users/{user_id}` (чтобы показать имя/username подтвердившего вместо сырого `user_id`).
- REST доступ к `/api/v4/posts/{post_id}/thread` для генерации постмортема по incident-треду.
- REST доступ к `/api/v4/actions/dialogs/open` для формы обратной связи.

`MATTERMOST_BOT_USER_ID` нужен, чтобы бот не обрабатывал собственные сообщения.

## Slash Command `/incident`

В Mattermost откройте **Product Menu -> Integrations -> Slash Commands** и создайте команду:

- Trigger Word: `incident`
- Request URL: `https://your-bot.example.com/mattermost/slash/incident`
- Request Method: `POST`
- Response Username: например `incident-bot`

Если Mattermost показывает token для slash command, положите его в `MATTERMOST_SLASH_TOKEN`. Команда ожидает permalink на оригинальный алерт:

```text
/incident https://mattermost.example.com/team/pl/abcdefghijklmnopqrstuvwx01
```

Также поддерживается Mattermost redirect permalink вида `/_redirect/pl/<post_id>`.

## Validity Reactions

Помимо подтверждения валидного инцидента (`:incident:`), есть две «лёгкие» реакции, которые проставляют поле `Валидность` в Jira, при заданном `JIRA_END_FIELD` заполняют `Окончание` временем реакции и пишут короткий ответ в тред алерта. Они **не** публикуют сообщение в канал инцидентов, не добавляют комментарий и не меняют статус задачи:

- `:man_gesturing_no:` → `Валидность = Ложный`;
- `:arrows_counterclockwise:` → `Валидность = Ожидаемый`.

Имена реакций настраиваются через `MATTERMOST_FALSE_INCIDENT_REACTION_NAME` и `MATTERMOST_EXPECTED_INCIDENT_REACTION_NAME`. Побеждает последняя реакция: каждая новая реакция перезаписывает поле `Валидность` в Jira своим значением. Если на момент реакции Jira issue ещё не создана, обновление пропускается (best-effort).

## Action Buttons

Если задан `SERVICE_PUBLIC_URL`, бот добавляет под алерт (в свой ответ-в-треде
о созданной Jira-задаче) интерактивную карточку из двух блоков: основной блок с
жирной строкой `Создана задача`, меню валидности и
действиями `🚨 Инцидент` / `📝 Summary`; ниже отдельный серый блок
`Обратная связь по алерту`. Эмодзи-реакции выше продолжают работать как фоллбэк.
Основной блок использует синий акцент `#3B82F6`, блок обратной связи — серый
`#4B5563`. Карточка повторяет те же сценарии:

- **Выбрать валидность ▼** → меню `Ложный` / `Ожидаемый` / `Валидный`;
- **🚨 Инцидент** → полное подтверждение инцидента (`:incident:`-флоу: пост в канал инцидентов, комментарий, transition);
- **📝 Summary** → бот отправляет тред в LLM и публикует краткое саммари ответом в тред (требует настроенный `LLM_API_TOKEN`; без него кнопка отвечает эфемерным сообщением и ничего не постит).
- **Обратная связь по алерту** → открывает Mattermost dialog с textarea, сохраняет сообщение в `alert_feedback` и пишет в тред `Получили обратную связь от <username>`.

Mattermost POST'ит действия на `https://your-bot.example.com/mattermost/actions/alert`, а submit формы обратной связи — на `https://your-bot.example.com/mattermost/dialogs/feedback`. Чтобы бот мог формировать абсолютные callback URL, `SERVICE_PUBLIC_URL` должен указывать на публичный адрес сервиса (без хвостового `/`). У интерактивных действий Mattermost нет встроенной подписи запроса, поэтому эндпоинты рассчитаны на доступ только из внутренней сети / за reverse-proxy. Нажавший видит результат эфемерным сообщением. Бот отвечает на нажатие только в своём посте, поэтому отдельных прав в Mattermost кнопки не требуют.

## Jira Setup

Для on-prem/Data Center Jira создайте personal access token и укажите:

- `JIRA_BASE_URL`, например `https://jira.example.com`;
- `JIRA_API_TOKEN`, personal access token;
- `JIRA_PROJECT_KEY`;
- `JIRA_ISSUE_TYPE`, имя или numeric id issue type;
- `JIRA_VALID_INCIDENT_FIELD`, например `Валидность`;
- `JIRA_SOURCE_FIELD`, например `Источник`;
- `JIRA_IS_CRIT_ALERT_FIELD`, например `Был ли крит алерт?`;
- `JIRA_START_FIELD`, например `Начало`, date-time picker поле, в которое пишется время прихода алерта, опционально;
- `JIRA_END_FIELD`, например `Окончание`, date-time picker поле, в которое пишется время реакции `Ложный`/`Ожидаемый` или галочки на сообщении валидного инцидента, опционально;
- `JIRA_CONFIRMED_STATUS_ID`, id transition в статус `Confirmed Incident`, опционально;
- `JIRA_CREATE_ENABLED=false`, тестовый режим без создания задач в Jira, опционально;
- `JIRA_STUB_ISSUE_KEY=ADSDEV-12024`, ключ задачи, который бот покажет в Mattermost в тестовом режиме; если не задан, бот сгенерирует ключ вида `PROJECT-12345`.

Бот умеет принимать как имя поля, в том числе на русском, так и старый `customfield_*` id. Если передано имя, он сам один раз находит соответствующий Jira field id через REST API и дальше использует его.

Для Jira 9.x on-prem/Data Center используется REST API v2 и `Authorization: Bearer ...`. Для option-полей (`select`, `radiobuttons`) бот берет допустимые значения из issue-type create metadata:

- `GET /rest/api/2/issue/createmeta/{projectKey}/issuetypes`;
- `GET /rest/api/2/issue/createmeta/{projectKey}/issuetypes/{issueTypeId}`.

`JIRA_SOURCE_FIELD` должен иметь option `Crit alert`, а `JIRA_IS_CRIT_ALERT_FIELD` должен иметь option `Да` для выбранных `JIRA_PROJECT_KEY` и `JIRA_ISSUE_TYPE`. `JIRA_VALID_INCIDENT_FIELD` при создании issue не отправляется, потому что дефолт выставляет сама Jira; при подтверждении бот обновляет это поле в option `Валидный`.

Если `JIRA_CREATE_ENABLED=false`, бот не вызывает Jira create issue: он сразу сохраняет stub-ключ как связанную задачу и публикует обычный ответ в Mattermost. Для фиксированного `JIRA_STUB_ISSUE_KEY` в БД хранится уникальный технический ключ с suffix от Mattermost post id, чтобы несколько тестовых алертов не конфликтовали по уникальному индексу, а в Mattermost показывается чистый ключ вроде `ADSDEV-12024`. Остальные Jira-действия после этого, например обновление `Валидность`, комментарии и transition при подтверждении, остаются включены и будут обращаться к Jira по сохранённому stub-ключу.

`JIRA_START_FIELD` (если задано) — date-time picker поле, которое заполняется временем прихода алерта при создании issue. Значение отправляется в формате ISO 8601 с offset вида `+0300` и обязательной дробной частью секунд (например, `2026-06-16T14:30:00.000+0300`); `dd.MM.yyyy HH:mm` — это только формат отображения в Jira UI. Время приводится к `INCIDENT_TIMEZONE`.

`JIRA_END_FIELD` (если задано) — date-time picker поле, которое заполняется временем нажатия lightweight реакции `Ложный`/`Ожидаемый`. Для валидного инцидента (`:incident:` или `/incident`) это поле при подтверждении не обновляется; оно заполняется позже, когда на корневом сообщении incident-треда нажимают галочку (`:white_check_mark:`, `:heavy_check_mark:` или `:ballot_box_with_check:`). Галочки на replies игнорируются. Формат для Jira REST API такой же, как у `JIRA_START_FIELD`.

## LLM Postmortems

Если задан `LLM_API_TOKEN`, галочка на корневом сообщении в `MATTERMOST_INCIDENT_CHANNEL_ID` запускает генерацию постмортема по всему треду инцидента. Бот:

- берет root-сообщение и ответы треда, включая оригинальное сообщение;
- резолвит имена авторов через Mattermost;
- передает тред в OpenAI-compatible endpoint `LLM_BASE_URL` (`https://corellm.wb.ru/deepseek/v1` по умолчанию);
- обновляет Jira description PM-шаблоном и детерминированными полями: основное сообщение инцидента, участники, автор постмортема;
- добавляет Jira comment с полным LLM-отчетом;
- публикует короткое summary ответом в incident-тред.

Для ручного incident-треда без исходного алерта новая Jira issue не получает alert-only поля `Источник = Crit alert` и `Был ли крит алерт? = Да`.

Настройки:

- `LLM_BASE_URL`
- `LLM_API_TOKEN` (также поддерживаются `CORELLM_API_TOKEN` и `OPENAI_API_KEY`)
- `LLM_MODEL`
- `LLM_MAX_TOKENS`
- `LLM_THREAD_MAX_CHARS`

## Startup Preflight

На старте бот логирует конфигурацию без секретов и запускает non-fatal проверки зависимостей:

- `database` — проверяет доступ к БД и пишет счетчики тикетов;
- `mattermost` — проверяет `/users/me`, `MATTERMOST_ALERT_CHANNEL_ID` и `MATTERMOST_INCIDENT_CHANNEL_ID`;
- `jira` — заранее резолвит field ids, issue type, createmeta и options `Валидный`, `Ложный`, `Ожидаемый`, `Crit alert`, `Да`;
- `llm` — если настроен `LLM_API_TOKEN`, делает маленький smoke request в `chat/completions`.

В `LOG_FORMAT=json` пишутся все startup-события: `startup.configuration`, `startup.preflight.check_started`, `startup.preflight.check_ok`, `startup.preflight.check_failed`, `startup.preflight.completed`. В `LOG_FORMAT=text` шумные `check_started`/`check_ok` скрываются, остается короткий итог preflight и ошибки. Ошибка preflight не останавливает приложение, но сразу показывает проблему с доступом, токеном, моделью или Jira metadata.

## Configuration

Скопируйте `.env.example` в `.env` и заполните значения:

```bash
cp .env.example .env
```

Минимальные переменные:

- `MATTERMOST_URL`
- `MATTERMOST_TOKEN`
- `MATTERMOST_ALERT_CHANNEL_ID`
- `MATTERMOST_INCIDENT_CHANNEL_ID`
- `MATTERMOST_INCIDENT_REACTION_NAME=incident`
- `MATTERMOST_BOT_USER_ID`
- `JIRA_BASE_URL`
- `JIRA_API_TOKEN`
- `JIRA_PROJECT_KEY`
- `JIRA_ISSUE_TYPE`
- `JIRA_VALID_INCIDENT_FIELD`
- `JIRA_SOURCE_FIELD`
- `JIRA_IS_CRIT_ALERT_FIELD`
- `JIRA_CONFIRMED_STATUS_ID`
- `DATABASE_URL`
- `INCIDENT_TIMEZONE=Europe/Moscow`, timezone для backend-времени в Jira payload, incident-сообщениях и логах

Для SQLite локально:

```env
DATABASE_URL=sqlite:///./mattermost_jira_bot.db
```

Для Postgres:

```env
DATABASE_URL=postgresql://incident_bot:incident_bot@postgres:5432/incident_bot
```

## Run Locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
python -m mm_jira_bot
```

Сервис слушает HTTP на `0.0.0.0:8080`. Health check:

```bash
curl http://localhost:8080/healthz
```

## Debug Admin

По умолчанию debug-админка выключена. Чтобы включить ее локально или в
закрытом контуре, задайте:

```env
DEBUG_ADMIN_ENABLED=true
```

После этого будут доступны:

- `http://localhost:8080/debug/admin` при локальном запуске;
- `GET /debug/admin` — простая HTML-страница со списком алертов и действиями;
- `GET /debug/admin/api/summary` — счетчики по статусам;
- `GET /debug/admin/api/alerts?limit=50&status=failed_jira` — список тикетов;
- `GET /debug/admin/api/alerts/{post_id}` — полная карточка тикета, включая сохраненное название алерта и обратную связь;
- `POST /debug/admin/api/alerts/{post_id}/jira/recreate` — создать Jira issue для тикета без `jira_issue_key`;
- `POST /debug/admin/api/alerts/{post_id}/jira/recreate?force=true` — создать новую Jira issue и заменить локальную связь;
- `POST /debug/admin/api/alerts/create-from-link` — создать Jira issue из вставленной Mattermost/Band ссылки или `post_id`;
- `GET /debug/admin/api/logs` — последние записи из in-memory log buffer.

Важно: у debug-админки нет отдельной авторизации, кроме флага
`DEBUG_ADMIN_ENABLED`, и она использует тот же HTTP-порт, что и бот
(`8080` в текущем `uvicorn.run`). Не выставляйте ее наружу без firewall/reverse proxy.
Force recreate не удаляет и не закрывает старую Jira issue; он только создает
новую задачу и обновляет локальную связь. Если алерт уже был подтвержден, бот
повторно применит Jira confirmation к новой задаче, но не создаст второй
incident-post в Mattermost.

## Docker

```bash
docker compose up --build
```

Если используете Postgres из `docker-compose.yml`, задайте:

```env
DATABASE_URL=postgresql://incident_bot:incident_bot@postgres:5432/incident_bot
```

## Database Schema

Модель хранится в SQLAlchemy, а SQL-миграции лежат в `migrations/`: базовая таблица тикетов, таблица обратной связи и добавление названия алерта. При старте сервис вызывает `create_all` и выполняет небольшие совместимые `ALTER TABLE`, поэтому для локального запуска отдельный мигратор не нужен.

Основная таблица: `alert_tickets`.

Ключевые поля:

- `mattermost_post_id` с уникальным индексом;
- `mattermost_alert_title`, короткое название алерта из первой строки сообщения;
- `jira_issue_key`;
- `valid_incident`;
- `incident_post_id`;
- `jira_confirmation_comment_added`;
- `creation_status` и `confirmation_status` для retry.

Обратная связь хранится в таблице `alert_feedback`: `mattermost_post_id`, `user_id`, отображаемое имя пользователя, текст сообщения и время создания.

## Idempotency

- Jira issue создается только после успешной вставки строки с уникальным `mattermost_post_id`.
- Повторное событие `posted` видит существующий `jira_issue_key` и пропускает создание.
- Повторная реакция или slash-команда возвращает уже существующий Jira issue и не публикует второй incident post.
- Jira comment добавляется один раз, флаг хранится в `jira_confirmation_comment_added`.
- Если Jira уже вернула `Valid Incident = Валидный`, локальный `valid_incident` синхронизируется.

## Recovery and Retry

Для временных ошибок Mattermost и Jira используются retries с exponential backoff. Если создание Jira issue не удалось, строка остается с `creation_status=failed_jira`, и фоновый worker повторит попытку.

Если подтверждение пришло до создания Jira issue, бот сохраняет `pending_confirmation_*`, а после успешного создания issue продолжит публикацию в канал инцидентов и обновление Jira.

После перезапуска сервис:

- поднимает pending worker;
- обрабатывает незавершенные Jira creation и confirmation из таблицы `alert_tickets`;
- по умолчанию не делает backfill старых сообщений из канала алертов и создает задачи только по новым WebSocket событиям после запуска;
- если нужно намеренно обработать последние сообщения из канала, включите `ENABLE_BACKFILL_ON_STARTUP=true` и задайте `BACKFILL_RECENT_POSTS_LIMIT`.

Если в БД уже есть старые строки без `jira_issue_key`, pending worker будет пытаться создать Jira issue для них каждые `PENDING_WORK_INTERVAL_SECONDS`. Чтобы полностью остановить ретраи старых алертов, очистите такие строки вручную после проверки:

```sql
SELECT id, mattermost_post_id, creation_status, confirmation_status, created_at, last_error
FROM alert_tickets
WHERE jira_issue_key IS NULL
ORDER BY created_at;

DELETE FROM alert_tickets
WHERE jira_issue_key IS NULL
  AND creation_status IN ('pending_jira', 'failed_jira');
```

## Logs

Логи пишутся в stdout. Формат выбирается переменной `LOG_FORMAT`:

- `LOG_FORMAT=json` (по умолчанию) — по одному JSON-объекту на событие, удобно для сбора в Loki/ELK и т.п.; сохраняет полную детализацию.
- `LOG_FORMAT=text` — компактные читаемые строки вида `время УРОВЕНЬ событие key=value …`, удобно при локальном запуске. На `INFO` stdout показывает только важные бизнес-события, а технические `check_ok`, skip/no-op, Jira metadata/cache и низкоуровневые Mattermost notice-события скрываются. `WARNING` и `ERROR` проходят всегда.

Уровень логирования задаётся `LOG_LEVEL` (по умолчанию `INFO`).

Важные события:

- `mattermost.alert.received`;
- `jira.issue.created`;
- `jira.issue.create_stubbed`;
- `jira.issue.create_failed`;
- `mattermost.alert_thread.reply_failed`;
- `mattermost.user.lookup_failed`;
- `jira.client.configured`;
- `jira.field.resolved`;
- `jira.issue_type.resolved`;
- `jira.create_metadata.loaded`;
- `jira.option.resolved`;
- `jira.issue.payload_prepared`;
- `jira.http.error`;
- `mattermost.reaction.received`;
- `mattermost.slash_command.received`;
- `mattermost.action.received`;
- `mattermost.action.post_lookup_failed`;
- `mattermost.action.unknown`;
- `feedback.received`;
- `incident.confirmed`;
- `mattermost.incident_message.published`;
- `jira.valid_incident.updated`;
- `jira.comment.added`;
- `jira.issue.transitioned`;
- `mattermost.alert_thread.summary_published`;
- `summary.skipped_llm_not_configured`;
- `summary.failed`;
- `summary.completed`;
- `postmortem.completed`;
- skip-события идемпотентности.

В Docker:

```bash
docker compose logs -f bot
```

## Tests

```bash
pytest
```

Тесты покрывают создание Jira issue, защиту от дублей, confirmation через reaction и slash command, повторное подтверждение, невалидную slash-ссылку, отсутствие локальной связи, Jira payload, Jira option metadata и формат incident-сообщения, а также интерактивную карточку (наличие/отсутствие controls, validity menu, incident, summary и feedback actions), thread summary через LLM и no-op при отсутствии LLM.

## API References

- Mattermost API documentation: https://developers.mattermost.com/api-documentation/
- Mattermost slash commands: https://docs.mattermost.com/integrations-guide/slash-commands.html
- Mattermost interactive messages: https://developers.mattermost.com/integrate/plugins/interactive-messages/
- Mattermost interactive dialogs: https://developers.mattermost.com/integrate/plugins/interactive-dialogs/
- Jira Data Center REST API: https://developer.atlassian.com/server/jira/platform/rest-apis/
