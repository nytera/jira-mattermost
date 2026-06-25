# Аудит mm_jira_bot — баги корректности и нецелесообразность

**Дата:** 2026-06-25 · **Объём:** весь `src/` (28 модулей, ~9500 строк), `migrations/`, тесты.
**Метод:** multi-agent аудит — 14 finder-срезов по доменам, каждая находка проверена 2–3
независимыми адверсари-верификаторами (перечитка кода, чтение зеркального теста и доменного
дока, repo-wide grep), затем критик полноты + точечный кросс-файловый раунд. 79 агентов.
Все ссылки `file:line` сверены с реальным кодом вручную.

**Итог:** 26 подтверждённых находок (после дедупликации — 25 уникальных), 1 отклонена.
**Критичных/высоких нет.** 10 medium, 16 low. Корректность — 17, нецелесообразность — 9.

> Как читать: Часть I — баги корректности (medium → low), Часть II — нецелесообразность.
> Severity показан с учётом калибровки верификаторов; где мнения расходились или находка
> «жёлтая», добавлена **Оговорка**. Тесно связанные находки сведены в один пункт.

## Сводка по severity

| Severity | Корректность | Нецелесообразность | Всего |
|---|---|---|---|
| medium | 10 | 0 | 10 |
| low | 7 | 9 | 16 |
| **Всего** | **17** | **9** | **26** |

---

# Часть I — Баги корректности

## C1 (medium) — `validity_label` в БД рассинхронизируется с Jira после подтверждения
**Где:** `service/_jira_sync.py:317-340` (источник), `service/_alerts.py:538-549` (потребитель). Объединяет `probe0-1` + `probe0-2`. conf=high.

**Суть.** «Валидность» в Jira и булев «valid incident» — это **одно** поле
(`settings.jira_valid_incident_field`), доступное через разные значения опции. При
подтверждении `_update_jira_for_confirmation` пишет в Jira `Валидный`, но **не обновляет**
колонку `ticket.validity_label` (ни `mark_confirmed`, ни `sync_valid_incident_from_jira`
её не трогают). Если раньше алерт был помечен `Ложный`/`Ожидаемый`, после confirm в Jira
стоит `Валидный`, а в БД остаётся `Ложный`. Затем guard `apply_validity_label`
(`_alerts.py:538`, `if ticket.validity_label == validity_label`) читает устаревшую колонку
и **молча гасит** повторную попытку оператора вернуть `Ложный` — Jira так и остаётся
`Валидный`, причём пользователю показывается ложно-успешное «Готово: Валидность = Ложный».

**Последствие.** Persisted-представление валидности расходится с Jira для сценария
«false → confirm → false снова». Реальное действие оператора no-op'ится без видимой ошибки.

**Рекомендация.** На пути подтверждения синхронизировать колонку с тем, что пишется в Jira
(вызвать `set_validity_label(...VALID_INCIDENT_CONFIRMED_VALUE)` внутри `mark_confirmed` /
`sync_valid_incident_from_jira`). Тогда оба guard'а станут корректны.

**Оговорка.** Верификаторы снизили severity high→medium: основные потребители
(`admin_api._validity_status`, запросы «empty validity») сперва смотрят на `valid_incident`
и показывают `Валидный` корректно — устаревшая колонка влияет только на dedup-guard'ы.
Прекондиция — многошаговая последовательность оператора, не общий путь.

## C2 (medium) — Кнопка/меню validity пишет Time-to-Fix без END (асимметрия каналов)
**Где:** `service/_alerts.py:410-412, 573`. Источник `probe1-1`. conf=high.

**Суть.** Реакция-эмодзи и кнопка/меню ведут в один `apply_validity_label`, но с разным
входом. Реакция передаёт `validity_set_at` (непустой) → `set_validity` пишет END **и**
`_set_time_to_fix` пишет TTF. Кнопка/меню (`handle_alert_action`) зовёт без
`validity_set_at` → `None` → END **не пишется** (gate `ended_at is not None`), а TTF на
строке 573 **всё равно пишется** (`validity_set_at or backend_now()`). Одно и то же
логическое действие даёт разное состояние Jira в зависимости от канала.

**Последствие.** `Ложный`/`Ожидаемый` через меню оставляет JIRA_END_FIELD пустым, но TTF
заполняет → внутренне противоречивые данные (длительность без END). Метрики/отчёты по END
для false/expected недосчитываются, когда операторы жмут кнопки вместо эмодзи.

**Рекомендация.** Согласовать каналы: либо `handle_alert_action` передаёт
`validity_set_at=backend_now()`, либо gate `_set_time_to_fix` на непустой `validity_set_at`
(тогда кнопка не пишет ни END, ни TTF). Добавить тест на button-path (сейчас
`test_action_menu_sets_validity` не проверяет ни END, ни TTF — расхождение не покрыто).

**Оговорка.** «Запись END+TTF на неподтверждённом алерте» — это **не** баг (документировано
и протестировано). Реальный дефект только в отсутствии END при наличии TTF на button-пути.
Доковое противоречие (`docs/jira.md:80-84`) — спорное, основа находки от доков не зависит.

## C3 (medium) — No-LLM режим: finalize не идемпотентен (дубли записей Jira и уведомлений)
**Где:** `service/_incidents.py:455-511` (+ helper `:531-569`). Объединяет `incidents-1` + `incidents-2`. conf=high.

**Суть.** Guard повторных реакций в `handle_incident_checkmark` завязан на
`ticket.postmortem_comment_added` (`:456`), а этот флаг ставит **только** путь постмортема.
Когда `self.llm is None` (документированный режим), постмортем пропускается, флаг никогда не
ставится, ранний возврат `:456-468` недостижим. Каждая следующая реакция на тот же инцидент
заново гонит весь путь: `apply_incident_end_time` перезаписывает END+TTF (`:482-487`), а для
validity-эмодзи `_set_incident_validity` (`:498`) перезаписывает поле Jira и **повторно
постит** уведомление «Валидность обновлена» — без guard'а равенства метки (в отличие от
`apply_validity_label` и already-finalized ветки).

**Последствие.** В no-LLM деплоях повторная validity-реакция (два оператора, или
remove-then-re-add) спамит тред дублями «Валидность обновлена» и шлёт лишние записи в Jira.

**Рекомендация.** Сделать no-LLM finalize идемпотентным: ставить finalize-маркер при записи
end-time и short-circuit'ить по нему (как `postmortem_comment_added`). Дополнительно — внести
guard `if ticket.validity_label == validity_label: return` в начало `_set_incident_validity`
(`incidents-2`), чтобы helper был идемпотентен независимо от вызывающего.

**Оговорка.** Только validity-эмодзи даёт видимый дубль уведомления; обычный чекмарк
(`validity_label=None`) лишь повторяет идемпотентную запись END/TTF. Один guard равенства
метки **не** устраняет повторную запись END — нужен именно finalize-маркер.

## C4 (medium) — Sync-back коммитит `valid_incident=True` до блока комментария → потеря без восстановления
**Где:** `service/_jira_sync.py:326-364`. Источник `jira-sync-1`. conf=high.

**Суть.** В sync-back ветке (`jira_valid is True`) `sync_valid_incident_from_jira` (`:328`)
сразу коммитит `valid_incident=True`. Только **после** этого идёт блок
`set_description → add_confirmation_comment` (`:342-364`). Если description/comment бросит
`ApiError`, строка остаётся `valid_incident=True`, `jira_confirmation_comment_added=False`.
Вызывающий ловит ApiError и зовёт `mark_confirmation_failed`, но та ничего не меняет, т.к.
`valid_incident` уже True. Движок ретраев `list_pending_confirmations` фильтрует
`valid_incident.is_(False)` (`repository.py:676`) → строка навсегда исключена; повторный
confirm рано возвращает `ALREADY_CONFIRMED`.

**Последствие.** Jira-issue остаётся без шаблона-постмортема и без confirmation-комментария,
без автоматического восстановления. Прямо противоречит инварианту в `docs/domains/jira-sync.md`
и комментарию в коде (`:342-346`).

**Рекомендация.** Отложить локальный коммит `valid_incident` на sync-back пути до успеха
блока комментария (как на non-sync пути). Вариант B (расширить
`list_pending_confirmations`) сам по себе не чинит — нужен вариант A.

**Оговорка.** Достаточно **одного** сбоя комментария при входе в sync-back ветку (не двух).
Итоговый статус строки — `confirming` (не `confirmed`), но он так же исключён фильтром.

## C5 (medium) — Транспортные ошибки обходят retry и `except ApiError`
**Где:** `http.py:58-72` (корень) + `jira.py:509,565,616,680` (точки вызова). Объединяет `config-http-retry-1` + `jira-client-1`. conf=high.

**Суть.** `retry_async` ловит **только** `ApiError`. `_request` оборачивает сырые
`httpx.HTTPError` через `wrap_transport_error` (`http.py:119-121`) — поэтому таймауты
ретраятся. Но helper `_retry` (`:58-72`) этого **не** делает, и все прямые `_retry`-операции
в `jira.py` (`_get_field_id`, `_get_issue_type_id`, `_get_create_fields_for_issue_type`,
`_get_link_type_name`) бьют `await self._client.get(...)` без обёртки. Сырой
`ConnectTimeout`/`ReadTimeout` (a) не ретраится и (b) пролетает мимо `except ApiError`
вызывающих. `llm.py:269-270` обёртку делает — асимметрия и есть баг.

**Последствие.** Транзиентный сетевой сбой при резолве метаданных Jira (особенно
`_get_link_type_name` — не прогревается preflight'ом, бьёт «вхолодную» на repeat-alert link,
и поля после рестарта на первом create) не ретраится и всплывает сырым httpx-исключением.

**Рекомендация.** Гарантировать в `_retry`/`retry_async` тот же контракт, что в `_request`:
ловить `httpx.HTTPError` и конвертировать в retryable `ApiError`. Централизация в `_retry`
убирает футган на каждой точке вызова.

**Оговорка.** Верификаторы убрали два преувеличения `jira-client-1`: (1) **нет** «teardown
websocket» — события идут в отдельных `asyncio.create_task` с `except Exception`
(`web.py:205`), сырой httpx ловится и логируется; (2) retry-next-delivery recovery
**сохраняется** (`expected_repeat_linked` ставится только после успеха). Теряется лишь
inline-ретрай и точечный лог. Severity high→medium.

## C6 (medium) — `websocket_loop` переподключается без backoff при чистом закрытии
**Где:** `web.py:216-235`. Источник `mattermost-web-1`. conf=high.

**Суть.** `await asyncio.sleep(5)` стоит только в ветке `except Exception`. Библиотека
`websockets` на штатном закрытии (`ConnectionClosedOK`, коды 1000/1001 — например rolling
restart Mattermost) **возвращается без исключения**: `async for` в `mattermost.py:391`
завершается, генератор `websocket_events()` отдаёт управление, внешний `while True`
немедленно повторяется. Sleep не выполняется → переподключение с нулевой задержкой; если
сервер недоступен в момент рестарта, это превращается в плотный busy-loop переподключений.

**Последствие.** При штатном закрытии (деплой/рестарт) бот может закрутить tight reconnect
loop без backoff, долбя сервер попытками соединения, пока сокет не восстановится.

**Рекомендация.** Добавить bounded backoff и на пути штатного завершения (после `async for`
или в `finally`), сохранив проброс `CancelledError`.

**Оговорка.** Уже, чем «сотни/сек при любом закрытии»: (1) только close-фрейм 1000/1001 идёт
без backoff (abrupt drop = 1006 → исключение → sleep(5)); (2) одиночное чистое закрытие = один
быстрый безвредный reconnect; устойчивый шторм требует, чтобы каждый reconnect в окне тоже
принимался-и-чисто-закрывался. Наивный `finally: sleep(5)` задержит graceful shutdown на 5с.

## C7 (medium) — `/metrics`: синхронный DB-запрос блокирует event loop на каждом scrape
**Где:** `metrics.py:61-74`. Источник `ops-metrics-logging-1`. conf=high.

**Суть.** `TicketStatsCollector.collect()` синхронно зовёт `repository.stats_summary()`
(несколько COUNT/GROUP BY в sync-сессии). `collect()` вызывается из `generate_latest()`
внутри `async def metrics()` (`web.py:384-386`) → синхронный DB-roundtrip исполняется в
потоке event loop. Кодовая база уже знает, что вызов блокирующий: startup-preflight
оффлоадит тот же `stats_summary` через `asyncio.to_thread` (`web.py:91`), а путь метрик — нет.

**Последствие.** Каждый Prometheus-scrape (15–60с, возможно конкурентно) блокирует event
loop на время DB-roundtrip → хвостовая задержка обработки WS-событий, slash/action-хендлеров
и фоновых циклов, риск ping-timeout вебсокета при нагрузке/контеншене БД.

**Рекомендация.** Сделать роут `/metrics` обычным `def` (FastAPI уведёт в threadpool), либо
рендерить gauge'и из значения, сэмплированного off-loop (кэш `stats_summary` с обновлением
через `asyncio.to_thread` по интервалу).

## C8 (medium) — Prompt времени окончания помечает наивный старт как UTC
**Где:** `postmortem.py:271-283` (+ `domain.py:20-23`, `service/_postmortem.py:504-508,557-558`). Источник `postmortem-1`. conf=medium.

**Суть.** `build_incident_end_time_prompt` рендерит старт через `backend_datetime(start)`,
а `backend_datetime` трактует **наивный** datetime как **UTC** и конвертит в runtime-tz.
Старт берётся из `ticket.mattermost_message_created_at` — на SQLite (дефолт) tz теряется,
значение приходит наивным с runtime-tz wall-clock. Значит prompt сдвигает старт на offset
(например +3ч в МСК). А `_parse_incident_end_time` (`:557-558`) локализует **тот же** наивный
старт в runtime-tz (не UTC) для проверки нижней границы. Prompt сообщает LLM время, на часы
расходящееся с границей, против которой код реально валидирует.

**Последствие.** На дефолтном SQLite с non-UTC tz prompt называет сдвинутую нижнюю границу
старта → LLM может выдать неверное «время восстановления», ухудшая точность END/Time-to-Fix.

**Рекомендация.** Локализовать наивный старт в runtime-tz так же, как валидатор
(`start.replace(tzinfo=runtime_timezone())`), вместо `backend_datetime`. Держать согласованным
с `_parse_incident_end_time` / `_set_time_to_fix`.

## C9 (low) — Панель показывает пустой prompt-override как активный источник `db`
**Где:** `admin_api.py:27-43`. Источник `debug-1`. conf=medium.

**Суть.** `_prompt_settings_payload` выбирает источник по `if db_value is not None` (`:34`),
поэтому сохранённая пустая строка (`""`) показывается в UI как `source="db"`. А реальный
резолвер `_resolve_prompt_template` (`service/_shared.py:112`) использует
`get_setting(key) or _prompt_env_default(key)` — truthy-проверку, и пустую строку трактует
как отсутствие, падая на env/default. Пути расходятся в трактовке пустого override.

**Последствие.** Оператор, очистивший textarea и нажавший Save, видит бейдж «панель/db», думая,
что override активен, а бот молча использует env/default. Состояние UI ≠ фактическое поведение.

**Рекомендация.** Согласовать: либо отклонять/игнорировать пустые значения в
`admin_save_setting` (пустое = сброс/удаление), либо в `_prompt_settings_payload`
использовать `if db_value:` под truthiness резолвера.

## C10 (low) — `recreate`: check-then-act гонка может породить дубли Jira-issue
**Где:** `service/_admin.py:204-258`. Источник `debug-2`. conf=low.

**Суть.** `admin_recreate_jira_issue` читает `ticket.jira_issue_key` (`:207/215`), и при
отсутствии ключа (или `force=true`) зовёт `_create_jira_issue` (`:231`) до персиста через
`replace_jira_issue` (`:252`). Между чтением и созданием нет ни лока, ни in-flight маркера.
Два конкурентных POST для одного post_id проходят guard, оба создают issue, второй
`replace_jira_issue` затирает первый → осиротевший issue.

**Последствие.** Двойной клик «Retry» (или два оператора/вкладки) создаёт два реальных
Jira-issue для одного алерта; осиротевший надо искать и закрывать вручную.

**Рекомендация.** Сериализовать recreate по post_id (asyncio-lock по ключу или DB
compare-and-set `creation_status` → sentinel `creating` перед сетевым вызовом). Тот же guard
помог бы и `_ensure_jira_issue`.

**Оговорка.** Автообновление UI само recreate **не** зовёт (только перечитывает список) —
реальные триггеры — двойной клик по недебаунснутой кнопке, две вкладки/оператора, программные
ретраи. conf=low.

## C11 (low) — `fetch_recent_channel_posts` индексирует `posts[order]` без guard'а
**Где:** `mattermost.py:363-377`. Источник `mattermost-web-2`. conf=medium.

**Суть.** Парс-замыкание делает `posts[post_id]` голым индексом, тогда как соседний
`get_thread_posts` (`:156-161`) защищён `if item in posts and isinstance(...)`. Если id из
`order` отсутствует в `posts` (усечённый/частичный ответ API), `posts[post_id]` бросает
KeyError и рушит весь вызов.

**Последствие.** Один битый ответ обрывает **весь** startup-backfill (все алерты окна не
обработаны) вместо пропуска одной записи.

**Рекомендация.** Повторить guard `get_thread_posts`: фильтровать
`post_id in posts and isinstance(posts[post_id], dict)` перед конструированием.

**Оговорка.** Малый радиус: backfill выключен по умолчанию (`ENABLE_BACKFILL_ON_STARTUP=false`)
и идёт раз при старте.

## C12 (low) — `mention_from_display` возвращает первый `@`-токен, а не `(@username)`
**Где:** `formatting.py:217-223`. Источник `llm-format-domain-2`. conf=low.

**Суть.** `_MENTION = re.compile(r"@[^\s()]+")` с `search` возвращает первое совпадение. Вход —
строка `'Full Name (@username)'`. Если в полном имени есть `@` до скобок, вернётся он, а не
реальный `@username`.

**Последствие.** В уведомлении о неавторизованной реакции (`coordinator.py ~252-258`) пинг
уйдёт не тому хэндлу. Ограничено: имена/фамилии в Mattermost редко содержат `@`.

**Рекомендация.** Сперва матчить скобочную форму `\(@([^)\s]+)\)` с fallback на текущий
паттерн (или брать последнее совпадение).

## C13 (low) — Startup `ALTER TABLE` добавляет boolean-колонки без `NOT NULL`
**Где:** `repository.py:146-167`. Источник `migrations-1`. conf=medium.

**Суть.** `_ensure_alert_ticket_columns()` добавляет `postmortem_comment_added` (`:151`) и
episode-колонки, включая `expected_repeat_linked` (`:160`), как `BOOLEAN DEFAULT FALSE` **без**
`NOT NULL`. И модель (`mapped_column(Boolean, default=False)`), и миграции 004/005 объявляют их
`NOT NULL`. На БД, обновлённой через startup-путь (а не fresh `create_all`), колонки nullable.

**Последствие.** Латентный риск: сегодня все вставки идут через ORM (`default=False`),
NULL'ов нет. Но будущий raw-insert или ручной `UPDATE ... = NULL` запишет NULL → трактуется
как falsy → молчаливый повтор постмортема/repeat-link.

**Рекомендация.** Эмитить `BOOLEAN NOT NULL DEFAULT FALSE`, чтобы схемы `create_all` и
in-place upgrade совпадали с моделью и миграциями.

## C14 (low) — `list_alerts` молча игнорирует любой `validity` кроме `'empty'`
**Где:** `repository.py:228-232`. Объединяет `repository-2` = `migrations-2` (дубль). conf=medium.

**Суть.** Фильтр добавляется только при `validity == "empty"`; любое другое непустое значение
проваливается без условия и возвращает нефильтрованный набор. Значение приходит из HTTP
query-строки админ-API (`admin_api.py:183-187`) и эхо-возвращается в ответе (`:193`) —
ответ заявляет фильтр, который не применён.

**Последствие.** Запрос с `validity` ≠ `"empty"` (опечатка или ручной запрос) возвращает все
алерты, тогда как метаданные ответа сообщают запрошенный фильтр. Фронтенд шлёт только
`"empty"`, так что живого слома нет — но контракт эндпоинта вводит в заблуждение.

**Рекомендация.** Валидировать/нормализовать неизвестные значения (ошибка или пустой
результат), либо сузить сигнатуру до поддерживаемого литерала.

---

# Часть II — Нецелесообразность (дизайн/износ)

## D1 (low) — Закрытие инцидента дважды тянет тред и резолвит имена участников
**Где:** `service/_postmortem.py:441-483, 510-513`. Источник `postmortem-2`. conf=high.

**Суть.** На пути чекмарка `handle_incident_checkmark` сперва зовёт `_resolve_incident_end_time`
→ `_postmortem_thread_context` (полный `get_thread_posts` + `_resolve_user_display` на каждого
юзера). Сразу после `generate_incident_postmortem` зовёт `_postmortem_thread_context`
**снова**, повторяя идентичный fetch треда и per-user lookup'ы. Кэша у `_resolve_user_display`
/ `get_user_display_name` нет → каждый участник тянется из Mattermost дважды + два fetch'а треда.

**Последствие.** Каждое закрытие шлёт лишний fetch треда и N лишних per-user HTTP-roundtrip'ов.

**Рекомендация.** Резолвить контекст треда один раз и переиспользовать: построить
`(thread_messages, participants, author)` единожды и прокинуть в обе функции (между ними
`apply_incident_end_time` в тред ничего не постит — переиспользование безопасно).

**Оговорка.** Severity снижена medium→low: лишние MM-вызовы малы относительно двух
последовательных LLM-вызовов на этом пути, и каждый деградирует graceful (ApiError → пустой
результат/возврат id). «Удвоение латентности» — преувеличение.

## D2 (low) — Дублирование validity-select и заголовка в двух builder'ах
**Где:** `actions.py:155-183, 253-291`. Источник `alerts-1`. conf=high.

**Суть.** `build_alert_controls_attachment` и `build_incident_controls_attachment` строят
идентичный select `'Выбрать валидность ▼'` и заголовок `'**Создана задача: <link>**'`
независимо. Различие — только integration-helper (`_integration` vs `_incident_integration`).

**Последствие.** Два источника правды для validity-меню и заголовка; правку одного надо
вручную дублировать в другом.

**Рекомендация.** Вынести select-dict и заголовок в общие helper'ы, параметризованные
builder'ом integration-конверта.

**Оговорка.** Опции автопропагируются (общий кортеж `VALIDITY_OPTIONS`, общий
`ACTION_VALIDITY`) — руками синхронить надо только литерал имени, скелет dict'а и f-строку
заголовка.

## D3 (low) — `_post_incident_thread_reply` почти полностью дублирует `_post_alert_thread_reply`
**Где:** `service/coordinator.py:337-368` (и `service/_shared.py:131-171`). Источник `service-infra-1`. conf=low.

**Суть.** Обе функции структурно идентичны (один `create_post` с `root_id`, тот же
catch-ApiError-лог-возврат). Различия: ключ-маркер props (`mattermost_incident_post_id` vs
`mattermost_alert_post_id`), опциональный `mention` у alert-варианта и namespace лог-события.

**Рекомендация.** Вынести один приватный `_post_thread_reply(..., thread_id_key, event_key,
mention=None)`; тонкие обёртки делегируют. Поместить общий helper в `_shared.py` (намеренный
leaf графа импортов), сохранив архитектуру.

## D4 (low) — Ветка `set_valid_incident(False)` («Не заполнено») недостижима + доковое расхождение
**Где:** `jira.py:332-336`. Источник `jira-client-2`. conf=high.

**Суть.** Все продакшн-вызовы передают `True` (`_jira_sync.py:335`, `_postmortem.py:381`).
Write-ветка `value=False → VALID_INCIDENT_EMPTY_VALUE` не вызывается. Сопутствующе:
`docs/jira.md` заявляет, что preflight проверяет опцию «Не заполнено», но `preflight_check`
(`:171-182`) её не резолвит.

**Рекомендация.** Либо убрать параметр `value: bool` и False-ветку, либо (если оставить)
добавить `VALID_INCIDENT_EMPTY_VALUE` в опции `preflight_check`. Поправить `docs/jira.md`.

**Оговорка.** Константа `VALID_INCIDENT_EMPTY_VALUE` **не** мертва — её читает
`get_valid_incident` (`:317`, маппит read-back в False). Мёртв только write-бранч.

## D5 (low) — `create_or_get_alert` — тест-only shim в продакшн-модуле
**Где:** `repository.py:322-332`. Источник `repository-1`. conf=high.

**Суть.** `create_or_get_alert` зовёт `create_or_classify_alert` и отбрасывает третий элемент
кортежа (root). Repo-wide grep: ссылается только из тестов; ни один `src/`-модуль не зовёт
(прод-путь `_alerts.py:217` использует `create_or_classify_alert` напрямую).

**Рекомендация.** Удалить shim и перевести тесты на `create_or_classify_alert` (6 голых
вызовов без изменений, 1 site `ticket, _ =` → `ticket, _, _ =`); либо явно задокументировать
как kept-for-tests.

**Оговорка.** Это **не** «dead-code» (исполняется 7 тестами) и **не** ослабляет покрытие
(полный 3-tuple контракт тестируется напрямую в `test_repository.py:95`). Точная
характеристика — тест-only публичный хелпер в прод-модуле.

## D6 (low) — Разошедшиеся дубли extractor'ов текста attachment'а
**Где:** `domain.py:36-79` (и `postmortem.py:50-79`). Источник `llm-format-domain-1`. conf=high.

**Суть.** `_attachment_field_text` (byte-identical) и `_attachment_text` (разошлись) есть в обоих
модулях. Версия `domain.py` итерирует `('pretext','title','text')`+fields; версия
`postmortem.py` добавляет `'footer'`, `image_url`, `title_link`. Правка одного не пропагирует
в другой.

**Рекомендация.** Безопасный DRY-выигрыш — расшарить byte-identical `_attachment_field_text`.
К `_attachment_text` подходить осторожно: расхождение, вероятно, намеренное (ingest хочет
только title-несущий текст, транскрипт LLM — всё).

**Оговорка.** Заявленный сценарий «дубль Jira-issue из-за маркера ✅ в footer» —
**нереализуем** (`is_resolved_alert` смотрит первую непустую строку = title, а footer всегда
позже). Остаётся чистая maintainability-находка, не баг корректности.

## D7 (low) — Reference-миграции: `CURRENT_TIMESTAMP` и нет `onupdate`
**Где:** `migrations/001_create_alert_tickets.sql:26-27`. Источник `migrations-3`. conf=high.

**Суть.** Миграция объявляет `created_at/updated_at DEFAULT CURRENT_TIMESTAMP` без onupdate,
тогда как модель использует `default=backend_now` / `onupdate=backend_now`.

**Рекомендация.** Аннотировать в `.sql`, что дефолты иллюстративны и живая схема приходит из
`init_db()`. Изменений кода не требуется.

**Оговорка.** `.sql` нигде в `src` не исполняются (живую схему даёт `create_all` +
`_ensure_alert_ticket_columns`). `default/onupdate=backend_now` — Python-side callable'ы (не
server_default, DDL не эмитят), а колонки TIMESTAMPTZ хранят один инстант независимо от tz —
исходная формулировка про «UTC вместо tz» и «updated_at не обновится» неверна. Дивергенция
тривиальна.

## D8 (low) — `test_migrations` сверяет только имена колонок для 2 из 3 таблиц
**Где:** `tests/test_migrations.py:194-221`. Источник `probe4-1`. conf=high.

**Суть.** `test_migrations_match_model_schema` — guard против дрейфа `create_all` (dev/SQLite)
vs `migrations/*.sql` (prod/Postgres). Для всех трёх таблиц сверяются только **имена** колонок;
типы/NOT NULL/defaults не сверяются нигде, а uniqueness/индексы — только для `alert_tickets`.
`alert_feedback` и `app_settings` без проверки uniqueness/индексов.

**Рекомендация.** Минимально — добавить явные проверки PK `app_settings.key` и индекса
`ix_alert_feedback_mattermost_post_id` в обеих схемах.

**Оговорка.** Живого дрейфа сегодня нет (схемы совпадают, тест проходит). Наивный цикл
uniqueness по всем таблицам **сломает** сьют (PK `app_settings` виден в SQLite через PRAGMA, но
не в `get_indexes` модели) — нужен спец-кейс PK через `get_pk_constraint`.

---

# Приложение

## Отклонённая находка
- **`repository-3`** (`repository.py:261-266`) — «`stats_summary.pending_jira` расходится с
  `list_pending_jira`». Отклонена адверсари-верификацией: расхождение семантик намеренное
  (summary считает все без jira_key; `list_pending_jira` дополнительно фильтрует по
  `creation_status`), живого дефекта нет.

## Покрытие
Срезы: alerts, incidents, jira-sync, postmortem, thread-summary, debug, service-infra,
jira-client, mattermost-web, repository, config-http-retry, llm-format-domain,
ops-metrics-logging, migrations. Кросс-файловые probe'ы: validity-desync (×2), END/TTF
асимметрия, TYPE_CHECKING-стабы, MRO-коллизии, `create_all` vs миграции, пустой Jira field key.

## Заметки по методологии
- Каждая находка проверена ≥2 независимыми верификаторами; при расхождении — третий решающий.
- **Оговорки** отражают калибровки верификаторов (часто снижают severity или сужают сценарий) —
  читать вместе с основным текстом.
- Все `file:line` сверены с реальным кодом на момент аудита (HEAD `660e39b`).
- Полные машинные тексты находок и verdict'ы — в выводе workflow-прогона `wf_98f808f7-7bd`.
