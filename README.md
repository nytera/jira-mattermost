# Mattermost Jira Incident Bot

Сервис слушает канал алертов в Mattermost, создает Jira issue для каждого нового алерта и позволяет явно подтвердить валидный инцидент реакцией `:incident:` или командой `/incident <mattermost_message_link>`.

## Workflow

1. Бот подключается к Mattermost WebSocket API и слушает события `posted` и `reaction_added`.
2. Новое сообщение в `MATTERMOST_ALERT_CHANNEL_ID` сохраняется в таблицу `alert_tickets`.
3. Для сообщения создается Jira issue с текстом алерта, автором, временем, permalink, `post_id`, каналом и `Valid Incident = false`.
4. Связь `mattermost_post_id -> jira_issue_key` хранится локально и защищена уникальным индексом.
5. Пользователь подтверждает инцидент реакцией `:incident:` на оригинальное сообщение или slash-командой `/incident <link>`.
6. Бот публикует сообщение в `MATTERMOST_INCIDENT_CHANNEL_ID`, обновляет Jira `Valid Incident = true`, добавляет комментарий со ссылкой на incident-сообщение и, если задано, делает transition issue.

```mermaid
flowchart LR
  A["Alert channel post"] --> B["alert_tickets row"]
  B --> C["Jira issue Valid Incident=false"]
  A --> D["reaction :incident:"]
  A --> E["/incident permalink"]
  D --> F["Confirm by original post_id"]
  E --> F
  F --> G["Post to incidents channel"]
  F --> H["Update Jira Valid Incident=true"]
  H --> I["Jira comment + optional transition"]
```

## Mattermost Bot Account

Создайте bot account или отдельного пользователя-интеграцию, выпустите personal access token и добавьте бота в оба канала:

- канал алертов: право читать сообщения и реакции;
- канал инцидентов: право писать сообщения;
- WebSocket доступ к `/api/v4/websocket`;
- REST доступ к `/api/v4/posts`, `/api/v4/channels/{channel_id}`, `/api/v4/channels/{channel_id}/posts`.

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

## Jira Setup

Создайте Jira API token в Atlassian Account settings и укажите:

- `JIRA_BASE_URL`, например `https://company.atlassian.net`;
- `JIRA_EMAIL`;
- `JIRA_API_TOKEN`;
- `JIRA_PROJECT_KEY`;
- `JIRA_ISSUE_TYPE`, имя или numeric id issue type;
- `JIRA_VALID_INCIDENT_FIELD_ID`, например `customfield_12345`;
- `JIRA_CONFIRMED_STATUS_ID`, id transition в статус `Confirmed Incident`, опционально.

Custom field id можно найти в Jira admin UI в настройках поля или через Jira REST API field list. Для Jira Cloud используется REST API v3 и Atlassian Document Format для `description` и комментариев.

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
- `JIRA_EMAIL`
- `JIRA_API_TOKEN`
- `JIRA_PROJECT_KEY`
- `JIRA_ISSUE_TYPE`
- `JIRA_VALID_INCIDENT_FIELD_ID`
- `JIRA_CONFIRMED_STATUS_ID`
- `DATABASE_URL`
- `INCIDENT_TIMEZONE=Europe/Moscow`, timezone для даты в названии Jira issue

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
- Если Jira уже вернула `Valid Incident = true`, локальный `valid_incident` синхронизируется.

## Recovery and Retry

Для временных ошибок Mattermost и Jira используются retries с exponential backoff. Если создание Jira issue не удалось, строка остается с `creation_status=failed_jira`, и фоновый worker повторит попытку.

Если подтверждение пришло до создания Jira issue, бот сохраняет `pending_confirmation_*`, а после успешного создания issue продолжит публикацию в канал инцидентов и обновление Jira.

После перезапуска сервис:

- поднимает pending worker;
- обрабатывает незавершенные Jira creation и confirmation;
- опционально делает backfill последних сообщений из канала алертов (`BACKFILL_RECENT_POSTS_LIMIT`).

## Logs

Логи пишутся в stdout как JSON. Важные события:

- `mattermost.alert.received`;
- `jira.issue.created`;
- `jira.issue.create_failed`;
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

Тесты покрывают создание Jira issue, защиту от дублей, confirmation через reaction и slash command, повторное подтверждение, невалидную slash-ссылку, отсутствие локальной связи, Jira payload и формат incident-сообщения.

## API References

- Mattermost API documentation: https://developers.mattermost.com/api-documentation/
- Mattermost slash commands: https://docs.mattermost.com/integrations-guide/slash-commands.html
- Jira Cloud REST API v3: https://developer.atlassian.com/cloud/jira/platform/rest/v3/
