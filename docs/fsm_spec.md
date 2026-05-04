# STEP 4: FSM CONTROL

## STATUS
DRAFT_V1

## STATES

### IDLE
Система ожидает новую задачу.

### PLANNING
Работает planning_agent.

### PM
Работает pm_agent.

### ARCHITECT
Работает architect_agent.

### WRITER
Работает writer_agent.

### REVIEW
Работает reviewer_agent.

### TEST
Работает tester_agent.

### QA
Работает qa_agent.

### FIX
Работает fixer_agent после замечаний review/qa.

### SUCCESS
Задача завершена успешно.

### FAIL
Задача завершена с ошибкой.

### BLOCKED
Задача остановлена из-за пустого входа, невалидного JSON или лимитов.

## GLOBAL RULES

- Одновременно активен только один state.
- SUCCESS, FAIL, BLOCKED — terminal states.
- Любой agent может вернуть BLOCKED.
- После FIX система возвращается в REVIEW.



## TRANSITIONS

IDLE -> PLANNING
PLANNING -> PM
PLANNING -> BLOCKED

PM -> ARCHITECT
PM -> BLOCKED

ARCHITECT -> WRITER
ARCHITECT -> BLOCKED

WRITER -> REVIEW
WRITER -> BLOCKED

REVIEW -> TEST
REVIEW -> FIX
REVIEW -> FAIL

TEST -> QA
TEST -> FIX
TEST -> FAIL

QA -> SUCCESS
QA -> FIX
QA -> FAIL

FIX -> REVIEW
FIX -> FAIL

## TRANSITION RULES

- Переход вперёд только при валидном output.
- BLOCKED если агент вернул пустой ответ / невалидный JSON / runtime error.
- FIX вызывается только если есть исправимые замечания.
- FAIL если лимит исправлений исчерпан или критическая ошибка.


## FAIL LOOPS

### LOOP_A: REVIEW_REPAIR_LOOP
REVIEW -> FIX -> REVIEW

Условие:
- reviewer verdict = REJECTED
- есть for_fixer инструкции

### LOOP_B: TEST_REPAIR_LOOP
TEST -> FIX -> REVIEW -> TEST

Условие:
- тесты не проходят
- ошибка исправима кодом

### LOOP_C: QA_REPAIR_LOOP
QA -> FIX -> REVIEW -> TEST -> QA

Условие:
- QA verdict = FAIL
- есть конкретные blockers для исправления

## LOOP RULES

- Каждый loop должен улучшать артефакт.
- Если FIX возвращает тот же результат 2 раза подряд → FAIL.
- Если причина ошибки не изменилась после loop → FAIL.
- После любого FIX обязательный повтор REVIEW.


## ITERATION LIMITS

### GLOBAL LIMITS

MAX_TOTAL_STEPS = 25
MAX_TOTAL_AGENT_CALLS = 40

### LOOP LIMITS

MAX_REVIEW_FIX_LOOPS = 3
MAX_TEST_FIX_LOOPS = 3
MAX_QA_FIX_LOOPS = 2

### STATE LIMITS

PLANNING_MAX_RETRY = 2
PM_MAX_RETRY = 2
ARCHITECT_MAX_RETRY = 2
WRITER_MAX_RETRY = 2

### LIMIT RULES

- При превышении любого MAX_* → FAIL.
- Retry считается только при невалидном output / parse error / empty response.
- Успешный переход state retry не увеличивает.
- FIX loop считается отдельно от retry.


## FAIL_SAFE

### TRIGGERS

1. Повторяющийся одинаковый output 2 раза подряд
2. Невалидный JSON 2 раза подряд
3. Пустой ответ агента 2 раза подряд
4. Runtime exception агента
5. Превышен любой iteration limit
6. Цикл состояний без прогресса
7. Отсутствует обязательный артефакт pipeline

### ACTIONS

- Немедленный переход в FAIL
- Сохранить last_state
- Сохранить failed_agent
- Сохранить failure_reason
- Сохранить artifacts_snapshot
- Остановить дальнейшие вызовы агентов

### RECOVERY MODE

После FAIL orchestrator может:
1. перезапустить задачу с last_good_state
2. отправить в manual review
3. завершить окончательно

## FAIL_SAFE RULES

- FAIL_SAFE имеет приоритет выше всех transitions.
- При срабатывании новых agent calls быть не должно.
