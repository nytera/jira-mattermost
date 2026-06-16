# Mattermost Jira Incident Bot

Сервис слушает канал алертов в Mattermost, создает Jira issue для каждого нового алерта и позволяет явно подтвердить валидный инцидент реакцией `:incident:` или командой `/incident <mattermost_message_link>`.

## Workflow

1. Бот подключается к Mattermost WebSocket API и слушает события `posted` и `reaction_added`.
2. Новое сообщение в `MATTERMOST_ALERT_CHANNEL_ID` сохраняется в таблицу `alert_tickets`.
3. Для сообщения создается Jira issue с текстом алерта, автором, временем, permalink, `post_id`, каналом, `Источник = Crit alert` и `Был ли крит алерт? = Да`. Поле `Valid Incident`/`Валидность` при создании не отправляется: Jira должна поставить свое дефолтное значение. После создания бот отвечает в тред исходного алерта ссылкой на созданную Jira issue.
4. Связь `mattermost_post_id -> jira_issue_key` хранится локально и защищена уникальным индексом.
5. Пользователь подтверждает инцидент реакцией `:incident:` на оригинальное сообщение или slash-командой `/incident <link>`.
6. Бот публикует сообщение в `MATTERMOST_INCIDENT_CHANNEL_ID`, обновляет Jira `Valid Incident = Валидный`, добавляет комментарий со ссылкой на incident-сообщение и, если задано, делает transition issue. После подтверждения бот также отвечает в тред исходного алерта о том, что инцидент заведён (ссылка на Jira, валидность, ссылка на сообщение в канале инцидентов). Имя подтвердившего показывается как `Имя Фамилия (@username)`, а не как сырой `user_id`.

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

Помимо подтверждения валидного инцидента (`:incident:`), есть две «лёгкие» реакции, которые только проставляют поле `Валидность` в Jira и пишут короткий ответ в тред алерта. Они **не** публикуют сообщение в канал инцидентов, не добавляют комментарий и не меняют статус задачи:

- `:man_gesturing_no:` → `Валидность = Ложный`;
- `:arrows_counterclockwise:` → `Валидность = Ожидаемый`.

Имена реакций настраиваются через `MATTERMOST_FALSE_INCIDENT_REACTION_NAME` и `MATTERMOST_EXPECTED_INCIDENT_REACTION_NAME`. Побеждает последняя реакция: каждая новая реакция перезаписывает поле `Валидность` в Jira своим значением. Если на момент реакции Jira issue ещё не создана, обновление пропускается (best-effort).

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
- `JIRA_CONFIRMED_STATUS_ID`, id transition в статус `Confirmed Incident`, опционально.

Бот умеет принимать как имя поля, в том числе на русском, так и старый `customfield_*` id. Если передано имя, он сам один раз находит соответствующий Jira field id через REST API и дальше использует его.

Для Jira 9.x on-prem/Data Center используется REST API v2 и `Authorization: Bearer ...`. Для option-полей (`select`, `radiobuttons`) бот берет допустимые значения из issue-type create metadata:

- `GET /rest/api/2/issue/createmeta/{projectKey}/issuetypes`;
- `GET /rest/api/2/issue/createmeta/{projectKey}/issuetypes/{issueTypeId}`.

`JIRA_SOURCE_FIELD` должен иметь option `Crit alert`, а `JIRA_IS_CRIT_ALERT_FIELD` должен иметь option `Да` для выбранных `JIRA_PROJECT_KEY` и `JIRA_ISSUE_TYPE`. `JIRA_VALID_INCIDENT_FIELD` при создании issue не отправляется, потому что дефолт выставляет сама Jira; при подтверждении бот обновляет это поле в option `Валидный`.

`JIRA_START_FIELD` (если задано) — date-time picker поле, которое заполняется временем прихода алерта при создании issue. Значение отправляется в формате ISO 8601 с offset вида `+0300` и обязательной дробной частью секунд (например, `2026-06-16T14:30:00.000+0300`); `dd.MM.yyyy HH:mm` — это только формат отображения в Jira UI. Время приводится к `INCIDENT_TIMEZONE`.

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
- `GET /debug/admin/api/alerts/{post_id}` — полная карточка тикета;
- `POST /debug/admin/api/alerts/{post_id}/jira/recreate` — создать Jira issue для тикета без `jira_issue_key`;
- `POST /debug/admin/api/alerts/{post_id}/jira/recreate?force=true` — создать новую Jira issue и заменить локальную связь.

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

Модель хранится в SQLAlchemy, а SQL-миграция лежит в `migrations/001_create_alert_tickets.sql`. При старте сервис вызывает `create_all`, поэтому для локального запуска отдельный мигратор не нужен.

Основная таблица: `alert_tickets`.

Ключевые поля:

- `mattermost_post_id` с уникальным индексом;
- `jira_issue_key`;
- `valid_incident`;
- `incident_post_id`;
- `jira_confirmation_comment_added`;
- `creation_status` и `confirmation_status` для retry.

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

- `LOG_FORMAT=json` (по умолчанию) — по одному JSON-объекту на событие, удобно для сбора в Loki/ELK и т.п.;
- `LOG_FORMAT=text` — компактные читаемые строки вида `время УРОВЕНЬ событие key=value …`, удобно при локальном запуске.

Уровень логирования задаётся `LOG_LEVEL` (по умолчанию `INFO`).

Важные события:

- `mattermost.alert.received`;
- `jira.issue.created`;
- `jira.issue.create_failed`;
- `mattermost.alert_thread.issue_notice_published`;
- `mattermost.alert_thread.status_notice_published`;
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
- `incident.confirmed`;
- `mattermost.incident_message.published`;
- `jira.valid_incident.updated`;
- `jira.comment.added`;
- `jira.issue.transitioned`;
- skip-события идемпотентности.

В Docker:

```bash
docker compose logs -f bot
```

## Tests

```bash
pytest
```

Тесты покрывают создание Jira issue, защиту от дублей, confirmation через reaction и slash command, повторное подтверждение, невалидную slash-ссылку, отсутствие локальной связи, Jira payload, Jira option metadata и формат incident-сообщения.

## API References

- Mattermost API documentation: https://developers.mattermost.com/api-documentation/
- Mattermost slash commands: https://docs.mattermost.com/integrations-guide/slash-commands.html
- Jira Data Center REST API: https://developer.atlassian.com/server/jira/platform/rest-apis/
