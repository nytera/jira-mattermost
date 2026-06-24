---
name: gate
description: "Run the mm_jira_bot pre-commit gate (ruff + ruff format --check + pyright + pytest in .venv) and report PASS/FAIL, separating new failures from the known pre-existing pyright baseline. Use when the user types /gate or asks to run the gate / checks / tests before committing. This is the cheap DIRECT run — do it inline in the main loop; for a full move-only review with adversarial body/stub checks, use the service-verifier agent instead."
---

Прогони гейт сервиса `mm_jira_bot` прямо в основном цикле (без подагента — это
дешёвый прямой запуск) и выдай короткий вердикт.

## Команды (из локального venv)

Запусти по очереди, не прерывайся на первой же ошибке — нужен полный срез:

```
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/pyright
.venv/bin/pytest -q
.venv/bin/python scripts/gen_service_map.py --check
```

Последний шаг проверяет, что `docs/reference/service-map.md` не устарел относительно кода
(дерево/сигнатуры/маршруты/MRO). При расхождении он печатает дифф и завершается с кодом 1.
Фикс: `.venv/bin/python scripts/gen_service_map.py && git add docs/reference/service-map.md`.

Если `.venv` нет — создай и поставь зависимости, потом повтори:
`python -m venv .venv && .venv/bin/pip install -e ".[test]"`.

## Baseline pyright (НЕ паникуй на известной ошибке)

На чистом дереве pyright стабильно даёт **1 pre-existing ошибку**:
`tests/test_postmortem.py` — `Cannot access attribute "status" for class "ActionResult"`
(`reportAttributeAccessIssue`, ~стр. 311). Она была до текущих изменений.

- Ровно 1 ошибка и именно эта → pyright **PASS** (0 новых).
- Больше одной → определи новые (сравни с прогоном на чистом HEAD: `git stash` или
  `git show HEAD:<файл>`) и пиши «N errors: M pre-existing + K new». Любая новая → **FAIL**.

## Вердикт (коротко)

- **ruff** PASS/FAIL · **format** PASS/FAIL · **pyright** «1 pre-existing + 0 new» ·
  **pytest** «N passed» · **service-map** PASS/FAIL.
- **ИТОГ:** ✅ всё зелёное относительно baseline / ❌ есть новые проблемы — перечисли их
  (файл:строка + суть).

Не выдавай пропущенную проверку за PASS. Если что-то не запустилось — скажи прямо.
