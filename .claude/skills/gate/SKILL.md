---
name: gate
description: "Run the mm_jira_bot gate and report PASS/FAIL, separating new failures from the known pre-existing pyright baseline. Two tiers: the DEFAULT quick gate (ruff + ruff format --check + pyright + pytest) for everyday iteration, and the FULL gate (`/gate full`, adds service-map --check + doc-sync) run before a PR / merge to main. Use when the user types /gate or asks to run the gate / checks / tests. This is the cheap DIRECT run — do it inline in the main loop; for a full move-only review with adversarial body/stub checks, use the service-verifier agent instead."
---

Прогони гейт сервиса `mm_jira_bot` прямо в основном цикле (без подагента — это
дешёвый прямой запуск) и выдай короткий вердикт.

## Два тира

CI нет, поэтому тиры запускаются командой:

- **`/gate` (по умолчанию, быстрый)** — на каждой итерации. ruff + format + pyright + pytest.
- **`/gate full`** — перед PR / вливанием в main. Быстрый тир + service-map `--check` + doc-sync.

Если аргумент `full` (или пользователь явно просит «полный гейт / перед PR») — гоняй
**полный** список. Иначе — **быстрый**.

## Быстрый тир (из локального venv)

Запусти по очереди, не прерывайся на первой же ошибке — нужен полный срез:

```
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/pyright
.venv/bin/pytest -q
```

Если `.venv` нет — создай и поставь зависимости, потом повтори:
`python -m venv .venv && .venv/bin/pip install -e ".[test]"`.

## Полный тир — дополнительно

Только при `/gate full`. К быстрому тиру добавь:

```
.venv/bin/python scripts/gen_service_map.py --check
```

Этот шаг проверяет, что `docs/reference/service-map.md` не устарел относительно кода
(дерево/сигнатуры/маршруты/MRO). При расхождении он печатает дифф и завершается с кодом 1.
Фикс: `.venv/bin/python scripts/gen_service_map.py && git add docs/reference/service-map.md`.

И проверь **doc-sync** (см. `CLAUDE.md` / `AGENTS.md`): при изменении поведения/архитектуры
нужный документ в `docs/` приведён в соответствие и в `CHANGELOG.md` есть запись в
`[Unreleased]`. Это ручная проверка — отметь в вердикте, что doc-sync ОК или что нужно дописать.

## Baseline pyright (НЕ паникуй на известной ошибке)

На чистом дереве pyright стабильно даёт **1 pre-existing ошибку**:
`tests/test_postmortem.py` — `Cannot access attribute "status" for class "ActionResult"`
(`reportAttributeAccessIssue`, ~стр. 311). Она была до текущих изменений.

- Ровно 1 ошибка и именно эта → pyright **PASS** (0 новых).
- Больше одной → определи новые (сравни с прогоном на чистом HEAD: `git stash` или
  `git show HEAD:<файл>`) и пиши «N errors: M pre-existing + K new». Любая новая → **FAIL**.

## Вердикт (коротко)

- Быстрый: **ruff** PASS/FAIL · **format** PASS/FAIL · **pyright** «1 pre-existing + 0 new» ·
  **pytest** «N passed».
- Полный — добавь: **service-map** PASS/FAIL · **doc-sync** OK / надо дописать.
- **ИТОГ:** ✅ всё зелёное относительно baseline / ❌ есть новые проблемы — перечисли их
  (файл:строка + суть). Если гонял только быстрый тир — напомни, что перед PR нужен `/gate full`.

Не выдавай пропущенную проверку за PASS. Если что-то не запустилось — скажи прямо.
