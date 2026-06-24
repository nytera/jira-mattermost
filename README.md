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

## Повторные («ожидаемые») алерты

Бот группирует сработки одного алерта в **эпизод** — от первой сработки до резолва (`✅`) — и автоматически помечает повторы, чтобы дежурный видел: это не новая проблема, а повтор уже известной.

- **Сигнатура** алерта определяется по его заголовку (что бот достаёт из текста/ссылки Grafana), а не по UID правила — так firing и резолв одного алерта совпадают, даже если в резолв-сообщении нет ссылки.
- Первая сработка эпизода — **корневой** алерт, обрабатывается как обычно.
- Каждая следующая сработка того же алерта в том же канале, пока эпизод не закрыт, — **повтор**. По нему бот: ставит реакцию `:arrows_counterclockwise:` («Ожидаемый») на сообщение; создаёт свою Jira-задачу как обычно; выставляет ей `Валидность = Ожидаемый`; дописывает в её описание блок со ссылками на корневой алерт и корневую задачу; создаёт в Jira реальную связь **«is child of»** (повтор — ребёнок корневой задачи); постит в тред бокс «Прилинковано к <корневая задача>».
- **Резолв** (`✅`) закрывает эпизод и сам по себе не создаёт ни тикет, ни задачу. Следующая сработка снова становится корневой — цикл повторяется.

Тип связи Jira настраивается через `JIRA_REPEAT_LINK_INWARD` (по умолчанию `is child of`): бот резолвит его в имя типа связи через `GET /rest/api/2/issueLinkType`. Если два разных правила имеют одинаковый заголовок, они попадут в один эпизод (максимум одна лишняя пометка «Ожидаемый», правится вручную).

## Validity Reactions

Помимо подтверждения валидного инцидента (`:incident:`), есть две «лёгкие» реакции, которые проставляют поле `Валидность` в Jira, при заданном `JIRA_END_FIELD` заполняют `Окончание` временем реакции и пишут короткий ответ в тред алерта. Они **не** публикуют сообщение в канал инцидентов, не добавляют комментарий и не меняют статус задачи:

- `:man_gesturing_no:` → `Валидность = Ложный`;
- `:arrows_counterclockwise:` → `Валидность = Ожидаемый`.

Имена реакций настраиваются через `MATTERMOST_FALSE_INCIDENT_REACTION_NAME` и `MATTERMOST_EXPECTED_INCIDENT_REACTION_NAME`. Побеждает последняя реакция: каждая новая реакция перезаписывает поле `Валидность` в Jira своим значением. Если на момент реакции Jira issue ещё не создана, обновление пропускается (best-effort).

**Важно — разный смысл по каналам.** Описанный выше «лёгкий» путь работает только в **алертном** канале. В **инцидентном** канале те же `:man_gesturing_no:`/`:arrows_counterclockwise:` на корне треда не только проставляют `Валидность`, но и **завершают инцидент с постмортемом** — как галочка `✅` (которая означает «Валидный»). То есть при любой выбранной валидности ПМ вставляется в задачу как при валидном. Постмортем генерируется один раз: повторная реакция на уже закрытом инциденте лишь меняет `Валидность` в Jira и постит в тред инцидента то же шаблонное уведомление «Валидность обновлена», но ПМ-комментарий не дублирует.

### Ограничение круга пользователей (опционально)

По умолчанию бот реагирует на реакции и нажатия кнопок от любого пользователя. Чтобы он учитывал действия только определённых людей, перечислите их в `MATTERMOST_AUTHORIZED_USERNAMES` (через запятую `,` или точку с запятой `;`, можно с `@`) — бот резолвит список в user-id на старте и периодически. Пусто = разрешено всем.

- В списке можно смешивать **логины** и **группы Mattermost**: каждый элемент сначала пробуется как логин, а что не нашлось как логин — резолвится как группа и разворачивается в её участников. Например `ivanov, sre-team`.
- Состав групп перечитывается раз в `MATTERMOST_AUTHORIZED_REFRESH_SECONDS` секунд (по умолчанию `300`): добавили человека в группу — он появится в allowlist в течение ~5 минут.
- Под ограничение попадают: реакции (`Инцидент`, `Ложный`/`Ожидаемый`, саммари, чекмарк-постмортем), кнопки/меню валидности и инцидента, slash-команда `/incident`.
- **Обратная связь доступна всем** — кнопка и форма фидбэка не ограничиваются.
- Если элемент не нашёлся ни как логин, ни как группа (опечатка), он логируется (`authorized_users.unresolved`), остальные продолжают работать. Группы могут требовать лицензии/прав, которых нет у токена — сбой резолва групп логируется (`authorized_users.groups_resolve_failed`) и не ломает allowlist по логинам. Если первичный резолв упал целиком (Mattermost недоступен), гейт остаётся открытым (fail-open); при периодическом обновлении сбой/пустой ответ **сохраняет** последний рабочий набор, а не сбрасывает его.

## Action Buttons

Если задан `SERVICE_PUBLIC_URL`, бот добавляет под алерт (в свой ответ-в-треде
о созданной Jira-задаче) интерактивные контролы одним ответом из двух блоков:
синий блок с жирной строкой `Создана задача`, меню валидности и кнопками
`🚨 Инцидент` / `📝 Summary` под ним, и ниже отдельный серый блок с
`💬 Обратная связь по алерту`.
Эмодзи-реакции выше продолжают работать как фоллбэк. Основной блок использует
синий акцент `#3B82F6`, блок обратной связи — серый `#4B5563`.

Если задан `MATTERMOST_DUTY_MENTION` (например `:look: @sre-ads-duty`), бот
добавляет его текстом **над боксом** «Создана задача» — пинг дежурного срабатывает
при каждом firing-алерте (resolve-алерты задачу не создают, поэтому не пингуются).
Работает в обоих режимах: с кнопками и в режиме «только эмодзи». Упоминание лежит в
теле сообщения, иначе `@group` не нотифицирует; пинг сработает, если группа состоит
в алертном канале.

Карточка повторяет те же сценарии:

- **Выбрать валидность ▼** → меню `Ложный` / `Ожидаемый` / `Валидный`;
- **🚨 Инцидент** → полное подтверждение инцидента (`:incident:`-флоу: пост в канал инцидентов, комментарий, transition);
- **📝 Summary** → бот отправляет тред в LLM и публикует фактологическое саммари-инцидентный отчёт ответом в тред (сначала плейсхолдер «Генерация саммари…», затем замена готовым отчётом; требует настроенный `LLM_API_TOKEN`; без него кнопка отвечает эфемерным сообщением и ничего не постит).
- **💬 Обратная связь по алерту** → открывает Mattermost dialog с textarea, сохраняет сообщение в `alert_feedback` и пишет в тред `Получили обратную связь от <username>`.

Mattermost POST'ит действия на `https://your-bot.example.com/mattermost/actions/alert`, а submit формы обратной связи — на `https://your-bot.example.com/mattermost/dialogs/feedback`. Чтобы бот мог формировать абсолютные callback URL, `SERVICE_PUBLIC_URL` должен указывать на публичный адрес сервиса (без хвостового `/`). У интерактивных действий Mattermost нет встроенной подписи запроса, поэтому эндпоинты рассчитаны на доступ только из внутренней сети / за reverse-proxy. Нажавший видит результат эфемерным сообщением. Бот отвечает на нажатие только в своём посте, поэтому отдельных прав в Mattermost кнопки не требуют.

Интерактивные карточки **по умолчанию выключены** (`INTERACTIVE_BUTTONS_ENABLED=false`): бот работает в режиме «только эмодзи» и не добавляет ни одну карточку (карточка алерта, карточка ручного инцидента, обратная связь) даже при заданном `SERVICE_PUBLIC_URL`. Чтобы включить кнопки, выставьте `INTERACTIVE_BUTTONS_ENABLED=true` и задайте `SERVICE_PUBLIC_URL`.

Под первым сообщением о создании бот публикует **памятку дежурному SRE** — боксированный список доступных эмодзи-реакций (без подсказок по кнопкам). Отключается `DUTY_HELP_ENABLED=false` (по умолчанию `true`). Памятка постится во всех трёх тредах: firing-алерта, ручного инцидента и **инцидента, заведённого из алерта**. Памятки алерта и инцидента различаются: в алертной перечислены `завести инцидент / ложный / ожидаемый / саммари`, в инцидентной — `✅ валидный / ложный / ожидаемый` (каждая = завершить инцидент + постмортем) и `саммари`.

Эмодзи-саммари (по умолчанию `:memo:`, настраивается `MATTERMOST_SUMMARY_REACTION_NAME`) работает в любом треде (алерт/инцидент/ручной) как аналог кнопки **📝 Саммари**: отправляет тред в LLM и публикует фактологический отчёт ответом в тред, Jira не трогает.

При закрытии инцидента, если задан `JIRA_TIME_TO_FIX_FIELD`, бот пишет в это числовое поле длительность инцидента в **минутах** (от времени создания до конца); поле резолвится по имени, как остальные. Это вторичное поле best-effort: ошибка записи логируется и не ломает закрытие.

## Сводка: что на что влияет

Колонка «Права» — попадает ли действие под `MATTERMOST_AUTHORIZED_USERNAMES`
(если список не задан — разрешено всем). Всё, что «игнорируется», — это тихий
no-op с записью в лог, без ошибки пользователю.

**Реакции (эмодзи):**

| Канал | Эмодзи | Права | Результат |
|---|---|---|---|
| Алертный | `incident` | да | Полное подтверждение инцидента (пост в инцидентный канал, `Валидность=Валидный`, коммент, опц. transition, ответ в треде) |
| Алертный | `man_gesturing_no` / `arrows_counterclockwise` | да | Лёгкий путь: Jira `Валидность=Ложный`/`Ожидаемый`, опц. `END`-поле, ответ в треде. Побеждает последняя |
| Алертный | галочка | — | Игнор («только в инцидентных тредах») |
| Инцидентный | галочка (`✅`) на **корне** треда | да | Валидный: `END`-поле + (если есть LLM) постмортем; для ручного треда — создаёт Jira-задачу |
| Инцидентный | `man_gesturing_no` / `arrows_counterclockwise` на **корне** треда | да | Ложный/Ожидаемый + завершение инцидента с постмортемом (как галочка, но со своей валидностью). На уже закрытом — только меняет `Валидность`, ПМ не дублируется |
| Инцидентный | галочка / Ложный / Ожидаемый на **ответе** | — | Игнор |
| Инцидентный | `incident` | — | Игнор («не в алерт-канале») |
| Любой | саммари (`:memo:`) | да | Фактологическое саммари треда в тред (LLM), Jira не трогает |
| Любой | нерелевантный эмодзи (👍 и т.п.) | — | Игнор |

**Повторные алерты (автоматически, без реакции пользователя):** firing того же алерта, что уже открыт (по заголовку, в том же канале) → бот сам ставит `:arrows_counterclockwise:` на повтор, создаёт задачу, `Валидность=Ожидаемый`, ссылки на корень в описании, Jira-связь «is child of» и бокс «Прилинковано к» в треде. Резолв (`✅`) закрывает эпизод и ничего не создаёт. Подробнее — раздел «Повторные («ожидаемые») алерты».

**Памятка дежурному** (`DUTY_HELP_ENABLED` ≠ `false`, по умолчанию вкл.): под первым сообщением о создании в треде firing-алерта, ручного инцидента **и инцидента из алерта** бот постит боксированный список эмодзи-реакций. Алертная и инцидентная памятки различаются (в инцидентной валидность = завершить + постмортем).

**Time to fix** (`JIRA_TIME_TO_FIX_FIELD` задан): при закрытии инцидента в числовое поле пишется длительность в минутах (создание → конец); best-effort, при ошибке/отсутствии старта/неположительной длительности — пропуск с логом.

**Кнопки/меню в треде** (появляются при `SERVICE_PUBLIC_URL` и `INTERACTIVE_BUTTONS_ENABLED=true`; по умолчанию кнопки выключены — режим «только эмодзи»):

_Карточка алерта (алертный канал):_

При создании задачи по firing-алерту, если задан `MATTERMOST_DUTY_MENTION`, бот
пингует дежурного текстом над боксом «Создана задача» (resolve-алерты задачу не
создают — не пингуются). Работает и в режиме «только эмодзи».

| Контрол | Права | Результат |
|---|---|---|
| **Выбрать валидность ▼** (`Ложный`/`Ожидаемый`/`Валидный`) | да | Лёгкий путь: ставит Jira `Валидность`, ответ в треде |
| **🚨 Инцидент** | да | Полное подтверждение инцидента (как реакция `incident`); после клика кнопка меняется на **✅ Подтверждён**, меню валидности убирается (переезжает в инцидентную карточку), а в тред падает уведомление со ссылкой на сообщение инцидента |
| **📝 Summary** | да | Тред → LLM → фактологическое саммари-отчёт ответом в тред (плейсхолдер «Генерация саммари…» → замена; без `LLM_API_TOKEN` — эфемерный no-op) |
| **💬 Обратная связь** | **нет (всем)** | Открывает форму, пишет в `alert_feedback` и постит уведомление в тред |

_Карточка инцидента (инцидентный канал): для ручных — на корневой пост не от бота;
для инцидентов из алертов — под сообщением инцидента (без «➕ Создать задачу»):_

| Контрол | Права | Результат |
|---|---|---|
| **➕ Создать задачу** (только ручные) | да | Создаёт Jira issue (без alert-полей), карточка сменяется на контролы ниже |
| **Выбрать валидность ▼** | да | Ставит Jira `Валидность` (не перетирается «Завершением») |
| **🏁 Завершить** | да | Полный постмортем в Jira + end-time (как галочка; нужен `LLM_API_TOKEN`) и фактологическое саммари-отчёт в тред (плейсхолдер → замена); после клика кнопка меняется на **✅ Завершено** |
| **📝 Саммари** | да | Фактологическое саммари-отчёт в тред (плейсхолдер → замена), Jira не трогает |

Неразрешённому пользователю кнопка (кроме обратной связи) отвечает эфемерным
`Недостаточно прав для этого действия.`; запрещённая реакция просто игнорируется.
Повторные реакции/нажатия идемпотентны (исключение — повторная галочка на ручном
инциденте перегенерирует постмортем).

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
- `JIRA_REPEAT_LINK_INWARD`, тип связи Jira для линковки повторного алерта к корневому как «is child of» (по умолчанию `is child of`); резолвится в имя типа связи через `GET /rest/api/2/issueLinkType`, опционально;
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
- публикует в incident-тред фактологическое саммари-инцидентный отчёт (отдельный LLM-промпт, не из текста постмортема, запись в Jira не делает): сначала плейсхолдер «Генерация саммари…», затем заменяет его готовым отчётом со ссылкой на Jira-постмортем в конце.

Для ручного incident-треда без исходного алерта новая Jira issue не получает alert-only поля `Источник = Crit alert` и `Был ли крит алерт? = Да`.

Настройки:

- `LLM_BASE_URL`
- `LLM_API_TOKEN` (также поддерживаются `CORELLM_API_TOKEN` и `OPENAI_API_KEY`)
- `LLM_MODEL`
- `LLM_MAX_TOKENS` — потолок длины ответа LLM (токены). При подробных отчётах поднимай, иначе отчёт обрежется.
- `LLM_THREAD_MAX_CHARS` — лимит входного текста треда (символы); длиннее — обрезается голова+хвост.
- `LLM_POSTMORTEM_PROMPT` / `LLM_POSTMORTEM_PROMPT_FILE` — переопределяют промпт постмортема (это он формирует Jira-комментарий). Плейсхолдеры: `{incident_thread_url}`, `{participants}`, `{postmortem_author}`, `{transcript}`. Не задано — используется встроенный шаблон.
- `LLM_SUMMARY_PROMPT` / `LLM_SUMMARY_PROMPT_FILE` — переопределяют промпт саммари в тред. Плейсхолдеры: `{thread_url}`, `{participants}`, `{transcript}`.

  Для обоих промптов используй вариант `*_FILE` (путь к файлу): загрузчик `.env` построчный и из inline-значения сохранит лишь первую строку. Если заданы оба, `*_FILE` важнее. Системные промпты (роль/гайдлайны качества) задаются в коде и не выносятся в env.

## Ручные инциденты (кнопки в инцидентном канале)

Помимо галочки, для инцидентов, заведённых **вручную** (сообщение прямо в
`MATTERMOST_INCIDENT_CHANNEL_ID`, без исходного алерта), есть кнопочный флоу.
Требует `SERVICE_PUBLIC_URL` и `INTERACTIVE_BUTTONS_ENABLED` ≠ `false`.

- На **каждое новое корневое сообщение от человека** (не от бота/вебхука — бот
  отличает их по `props.from_bot` / `props.from_webhook` и по
  `MATTERMOST_BOT_USER_ID`) бот постит в тред карточку с кнопкой **➕ Создать
  задачу**. Jira-задача при этом ещё не создаётся.
- Если задан `MATTERMOST_DUTY_MENTION` (например `:look: @sre-ads-duty`), бот
  добавляет его текстом над карточкой — так пинг `@group` реально срабатывает
  (упоминание во вложении не нотифицирует). В режиме «только эмодзи»
  (`SERVICE_PUBLIC_URL` не задан или `INTERACTIVE_BUTTONS_ENABLED=false`) карточки
  нет, но `MATTERMOST_DUTY_MENTION` всё равно постится отдельным сообщением в тред
  (один раз на инцидент), чтобы дежурного позвали; действия — через чекмарк-флоу.
- По клику **➕ Создать задачу** создаётся Jira issue (без alert-only полей), а
  карточка сменяется на контролы: **меню валидности** (`Ложный`/`Ожидаемый`/
  `Валидный` → пишет в поле, как в алерте), **🏁 Завершить** и
  **📝 Саммари**.
- **🏁 Завершить** = тот же полный постмортем в Jira, что и галочка (PM-шаблон
  с участниками/автором/ссылкой, отчёт комментарием в Jira, название через LLM,
  end-time), плюс фактологическое саммари-отчёт в incident-тред (отдельный
  LLM-промпт, плейсхолдер → замена). Выбранную валидность **не перетирает**: если
  стоит `Ложный`, он таким и останется.
- **📝 Саммари** — фактологическое саммари-отчёт треда в тред (плейсхолдер →
  замена), Jira не трогает (это не текст постмортема из Jira; промпт — общий с
  саммари при завершении).
- Старая **галочка** на корне треда продолжает работать параллельно.

**Инциденты из алертов** (подтверждённые через `:incident:` / кнопку 🚨) получают
в инцидентном канале **ту же карточку контролов** — меню валидности, 🏁 Завершить
и 📝 Саммари — но **без** «Создать задачу», т.к. Jira-задача уже создана. Сверху
карточка показывает блок **«Создана задача: <ссылка>»** (как в алертах). Карточка
постится ответом под сообщением инцидента сразу при подтверждении.

Под allowlist (`MATTERMOST_AUTHORIZED_USERNAMES`) попадают все эти кнопки.

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

## Ops-канал и метрики Prometheus

Наблюдаемость за здоровьем самого бота. Обе поверхности независимы и по
умолчанию: канал — выключен, метрики — включены.

### Канал ops-алертов (`MATTERMOST_OPS_CHANNEL_ID`)

Отдельный канал для ошибок самого бота — это **не** канал алертов Grafana
(`MATTERMOST_ALERT_CHANNEL_ID`). Каждое `ERROR`-событие (обрыв websocket,
падение фонового `pending_work`, ошибки Jira/LLM, провал preflight) постится
туда цветной плашкой. Включается опционально:

```env
MATTERMOST_OPS_CHANNEL_ID=ops-channel-id
MATTERMOST_OPS_COOLDOWN_SECONDS=300
```

- Бот должен состоять в канале с правом записи.
- Антишторм: повтор того же события глушится в окне `…_COOLDOWN_SECONDS`
  (по умолчанию 300 с).
- Доставка best-effort: если пост не удался, ошибка уходит в лог `warning`, а
  бот продолжает работу. При незаданном канале — ошибки только в логах.

### Метрики Prometheus (`/metrics`)

По умолчанию эндпоинт `GET http://localhost:8080/metrics` включён (Prometheus
рассчитан на скрейпинг). Отключить:

```env
METRICS_ENABLED=false
```

Основные серии:

- `bot_http_requests_total{client,method,status}` и
  `bot_http_request_duration_seconds{client,method}` — внешние HTTP-вызовы
  (`client` = `jira` / `mattermost` / `llm`). Примечание: стриминговые
  LLM-вызовы (`LLM_STREAM=true`, по умолчанию) идут мимо общей точки
  инструментации и в HTTP-метрики не попадают;
- `bot_errors_total{event}` — количество `ERROR`-событий по имени события;
- `bot_ops_alerts_dropped_total` — сброшенные из-за переполнения очереди
  ops-алерты;
- `bot_tickets_total`, `bot_tickets_pending_jira`, `bot_tickets_failed`,
  `bot_tickets_confirmed`, `bot_tickets_by_creation_status{status}`,
  `bot_tickets_by_confirmation_status{status}` — состояние очереди тикетов
  (снимается на скрейпе через `repository.debug_summary()`).

Важно: у `/metrics`, как и у debug-админки, нет отдельной авторизации и он
использует тот же HTTP-порт (`8080`). Не выставляйте наружу без firewall/reverse
proxy.

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
- `http.request.failed` (необработанное исключение в HTTP-эндпоинте → ответ 500);
- `http.request.bad_json` / `http.request.bad_body` (битое тело запроса → 400);
- skip-события идемпотентности.

Ошибки и трейсбеки: неожиданные исключения (в фоновых циклах, обработчике
websocket-событий, startup-backfill и HTTP-эндпоинтах) логируются на уровне
`ERROR` со стеком (`exc_info`) и полем `error_type` — в `LOG_FORMAT=json` стек
лежит в ключе `exception`, в `text` печатается отдельной строкой под событием.
Ожидаемые ошибки интеграций (`ApiError`) логируются компактно, без стека. Сбой
сбора ticket-gauge'ов на скрейпе `/metrics` логируется как `metrics.collect_failed`
(WARNING, со стеком) — `/metrics` при этом не падает.

Логи самого `uvicorn` унифицированы: `__main__.py` запускает сервер с
`log_config=None`, поэтому `uvicorn.access`/`uvicorn.error`/lifecycle-сообщения
идут через тот же `LOG_FORMAT` (в `json` — JSON-строкой с `logger="uvicorn.access"`)
и попадают в in-memory ring buffer debug-admin (`GET …/api/logs`). В `LOG_FORMAT=text`
чужие INFO-записи (например `Uvicorn running on …` и access-лог) видны, а наши
шумные INFO-события (`check_ok`, skip/no-op и т.п.) по-прежнему скрыты.

В Docker:

```bash
docker compose logs -f bot
```

## Tests

```bash
pytest                                              # весь набор
pytest --cov=mm_jira_bot --cov-report=term-missing  # с отчётом покрытия (~78%)
```

Тесты покрывают создание Jira issue, защиту от дублей, confirmation через reaction и slash command, повторное подтверждение, невалидную slash-ссылку, отсутствие локальной связи, Jira payload, Jira option metadata и формат incident-сообщения, а также интерактивную карточку (наличие/отсутствие controls, validity menu, incident, summary и feedback actions), thread summary через LLM и no-op при отсутствии LLM.

## Lint & format

```bash
ruff check src tests    # линтер (правила E,F,I,UP,B,SIM)
ruff format src tests   # автоформат (добавь --check для проверки без записи)
```

`ruff` и `pytest-cov` ставятся вместе с тестовыми зависимостями
(`pip install -e ".[test]"`). Конфигурация — в `pyproject.toml`.

## API References

- Mattermost API documentation: https://developers.mattermost.com/api-documentation/
- Mattermost slash commands: https://docs.mattermost.com/integrations-guide/slash-commands.html
- Mattermost interactive messages: https://developers.mattermost.com/integrate/plugins/interactive-messages/
- Mattermost interactive dialogs: https://developers.mattermost.com/integrate/plugins/interactive-dialogs/
- Jira Data Center REST API: https://developer.atlassian.com/server/jira/platform/rest-apis/
