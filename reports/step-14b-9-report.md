# Отчёт: Step 14b-9 + Audit Fix

**Дата:** 2026-05-05  
**Статус:** ✅ Закрыто  
**Коммиты:** `4d6d605`, `8824b78`

---

## Что было сделано

### Step 14b-9 — writer artifact → worktree → validator → commit

Реализована полная цепочка от вывода агента-писателя до git-коммита в worktree.

#### Новый формат writer artifact

Агент-писатель теперь возвращает строгий JSON вместо FILE:-блоков:

```json
{"files": [{"path": "relative/path.py", "content": "UTF-8 source"}]}
```

Системный промпт `_WRITER_SYSTEM` обновлён с v1.3 до v2.0 в `core/dispatcher_agents.py`.

#### Новые модули

**`core/writer_to_worktree.py`** — атомарная запись файлов в worktree:
- Валидирует весь список файлов перед записью (нет частичных записей)
- Защита от path traversal: абсолютные пути, `..`-сегменты, `relative_to()` проверка
- 23 теста в `tests/test_writer_to_worktree.py`

**`core/sandbox_runtime_hook.py`** — фабрика `RuntimeValidationHook` для Orchestrator:
- `make_sandbox_hook(handle, adapter_factory, validator)` → callable
- Вызывает `write_artifact_to_worktree` → создаёт `ProjectAdapter` → запускает `RuntimeValidator.validate()`
- 13 тестов в `tests/test_sandbox_runtime_hook.py`

#### Интеграция в `core/real_task_handler.py`

В `_build_run_fn` после `sandbox.acquire()`:
- Создаётся `RuntimeValidator(INPLACE, lint=True, tests=True)`
- Создаётся `make_sandbox_hook(handle, adapter_factory, validator)`
- Hook передаётся в `Orchestrator(runtime_validator=runtime_hook)`
- При `State.SUCCESS` — вызывается `sandbox.commit_in_worktree()`

---

### Audit Fix — коммит не должен тихо проваливаться

**Проблема:** оригинальный код использовал `contextlib.suppress(Exception)` вокруг `commit_in_worktree()`. При любой ошибке (например, `nothing_to_commit`) пайплайн возвращал `SUCCESS` — что было ложью.

**Исправление:** заменено на `try/except/else`:

```python
if final_state == State.SUCCESS:
    try:
        summary["commit_sha"] = sandbox.commit_in_worktree(...)
    except Exception as commit_exc:
        summary["final_state"] = "FAIL"
        summary["failure_reason"] = f"commit_failed:..."
        emitter.emit_task_failed(reason=f"commit_failed · ...")
    else:
        emitter.emit_task_completed(summary=f"branch=... · commit=...")
```

**Результат:** если коммит не прошёл — пользователь получает `❌ Не получилось` с причиной `commit_failed`, а не фиктивное `✅ Готово`.

---

## Тесты

| Файл | Тестов | Результат |
|------|--------|-----------|
| `test_writer_to_worktree.py` | 23 | ✅ |
| `test_sandbox_runtime_hook.py` | 13 | ✅ |
| `test_real_task_handler.py` | +4 новых | ✅ |
| Все тесты (без faiss/code_retriever) | 1480 | ✅ |

Новые тесты для audit fix:
- `test_commit_in_worktree_called_on_success` — коммит вызывается ровно один раз при SUCCESS
- `test_commit_in_worktree_not_called_on_fail` — коммит не вызывается при FAIL
- `test_commit_sha_appears_in_success_message` — SHA появляется в финальном сообщении
- `test_commit_error_surfaces_as_task_failed_not_success` — ошибка коммита → `Не получилось`, а не `Готово`

---

## Коммиты на GitHub

| SHA | Описание |
|-----|----------|
| `4d6d605` | `14b-9: writer→worktree→validator→commit pipeline` |
| `8824b78` | `fix(real_task_handler): commit failure after SUCCESS surfaces as task_failed, not silent ✅` |

Ветка: `main` → `origin/main` ✅
