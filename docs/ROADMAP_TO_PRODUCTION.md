# AI Dev Team v4 ULTRA — Roadmap to Production

**Состояние на дату создания:** commit `4c58caf`, 1618 tests passed, ruff clean.
**Бюджет:** ~$19 на оставшуюся работу + текущий checked VPS baseline
см. `docs/HOSTING_PROVIDER_DECISION.md` (`Hetzner Cloud CPX22`, `hel1`,
`€8.49/mo` excl. VAT with `1 x Primary IPv4` on `2026-05-17`).

Этот документ — самодостаточный план: можно дать любой нейросети (Sonnet 4.6 рекомендуется как основная модель, Opus только под аудит) и она поймёт, что делать. Каждая фаза имеет: цель, шаги, acceptance criteria, ориентировочную стоимость.

---

## Раздел 0 — Как читать этот документ для AI-ассистента

Перед началом работы:
1. Прочитай корневой `README.md`.
2. Прочитай `docs/fsm_spec.md` — спецификация FSM оркестратора.
3. Прочитай этот файл целиком до начала работы по конкретной фазе.
4. Открой соответствующий `core/<module>.py` под фазу.

Правила для AI:
- Никогда не оставлять «потом починим». Каждый шаг — `ruff check .` чисто, `pytest` полностью зелёный.
- Frozen-датаклассы с `__post_init__` валидацией всех полей.
- Полная валидация инжектов конструкторов через `isinstance` + `ValueError`.
- Никаких `shell=True` в subprocess. Никаких хардкоженных API-ключей.
- В конце каждого шага — одна строка коммита.
- В системном промпте каждого ответа — указывай используемую модель.

---

## Раздел 1 — Текущее состояние (snapshot)

### Модули в `core/` (21 файл)

| Файл | Что делает |
|------|------------|
| `agent_personas.py` | 8 frozen-персон с эмодзи, callsigns, voice traits |
| `agents.py` | Legacy 8 функций-агентов через `core.router.ask_openrouter` (одна модель) |
| `background_runner.py` | Single-worker thread pool с `CancellationToken`, `TaskHandle`, `RunnerBusyError` |
| `bot_commands.py` | Парсер слэш-команд, `CommandRegistry`, 11 команд |
| `bot_runner.py` | env-driven билдер; собирает весь bridge из env. Содержит `cleanup_orphan_worktrees_from_env`, `_try_build_sandbox`, `_real_pipeline_eligible` |
| `confirmation_gate.py` | `ConfirmationGate` с порогом по cost для авто-vs-ask решений |
| `dispatcher_agents.py` | TIER-AWARE фабрика registry, заменяет `agents.py` для real pipeline |
| `llm_dispatcher.py` | OpenRouter HTTP-клиент с fallback-цепочками, `LLMRequest`/`LLMResponse`/`LLMAttempt` |
| `memory.py` | `PipelineMemory`: артефакты + transitions + agent calls |
| `model_tier.py` | `TierConfig`, `TierRegistry`, тарифы ECONOMY/STANDARD/PREMIUM |
| `observability.py` | LogRecord, MetricSample, AgentPerformance, CostSnapshot |
| `orchestrator.py` | FSM-оркестратор, `REQUIRED_AGENTS`, `RuntimeValidationHook` |
| `progress_emitter.py` | Типизированные events + `wrap_agent_with_progress` декоратор |
| `real_task_handler.py` | Bridge↔pipeline integration — центральный glue, `make_real_task_handler` |
| `runtime_validator.py` | Запускает ruff/pytest, возвращает `ValidationReport` |
| `sandbox_runtime_hook.py` | Пишет writer artifact на диск + зовёт validator |
| `sandbox_workspace.py` | Git worktree manager: `acquire`, `release`, `commit_in_worktree`, `push_named_branch`, `gh_pr_create`, `cleanup_orphans` |
| `task_history.py` | Thread-safe ring buffer `TaskHistory` с `TaskSummary` |
| `telegram_bridge.py` | Pure-logic transport-agnostic Telegram glue |
| `tier_session.py` | Per-chat tier state с опциональной JSON-persistence |
| `writer_to_worktree.py` | JSON→files materialiser с защитой от path traversal |

### Bot commands (11)
`/projects /switch /budget /agents /tier /log /stop /retry /push /pr /help`

### Что персистится сейчас
- `BOT_STATE_DIR/tier_sessions.json` — выбранные тарифы (Step 19, опционально через env)
- `pipeline_log.jsonl` — Observability JsonLinesSink (если включена)
- Всё остальное (TaskHistory, BudgetState) — in-memory, теряется при рестарте

### Известные пробелы
- **Cost budget** не enforced в production-пути (Observability не инжектится в `make_real_task_handler` из `build_real_task_handler_from_env`)
- 1 бот, 1 чат, агенты говорят как персоны через подписи (не отдельные DM)
- Нет web-dashboard
- Нет 24/7 hosting'а — бот стартует вручную

---

## Раздел 2 — Финальное видение (где мы хотим оказаться)

### Три поверхности взаимодействия

1. **Telegram Team Chat** — главный чат, где юзер ставит задачу и получает финальную сводку
2. **8 Telegram DM с per-agent ботами** — у каждого агента свой бот (`@aidt_planner_bot`, `@aidt_architect_bot`, ...). Юзер может писать персонально любому
3. **Web Dashboard "Office"** — `https://<domain>/office` — read-only view: текущая задача, статусы агентов, cost burn, история, логи

### Модель межагентного общения

Три уровня:

**Pipeline messaging** (есть): orchestrator передаёт артефакты по цепочке через PipelineMemory.

**Async dialog** (новое): Агенты постят уточняющие вопросы в общий Telegram-канал, где они все админы. Например: «Архитектор: @Программист — async или sync? @Менеджер — какой приоритет?»

**Direct broadcast** (новое): Юзер DM любому агенту — тот отвечает single-shot LLM-вызовом без полного пайплайна. Каждый бот хранит последние 10 реплик контекста.

### "Найм агентов" (production)

Три уровня сложности:

**L1 — Static expansion** (рекомендуется): Добавляем 3 новые роли в `REQUIRED_AGENTS`: `security_agent`, `devops_agent`, `data_agent`. Они становятся постоянными членами команды. PM-агент решает в плане, нужны ли они.

**L2 — Dynamic specialization** (рекомендуется): PM-агент при планировании может пометить задачу `specialization_hints: ["postgres", "rust"]`. Промпты writer/reviewer'а augment'ятся доменными инструкциями.

**L3 — Sub-agents** (research-territory, отложить): Программист может разбить задачу на N параллельных подзадач и заспавнить N programmer-инстансов параллельно. Требует расширения `BackgroundTaskRunner` до N воркеров.

### Инфраструктура 24/7

- **Hetzner Cloud CPX22** (`hel1`) — current `C1.1` purchase-ready VPS choice;
  exact checked cost and purchase status live in
  `docs/HOSTING_PROVIDER_DECISION.md`
- **systemd unit** для авто-рестарта при крашах
- **nginx** reverse proxy с HTTPS через Let's Encrypt
- **Daily backup** state.db в приватный GitHub-репо
- **Healthcheck-бот** — алерт в Telegram при недоступности >5min
- **Optional**: домен (~$10/yr) для красоты

---

## Раздел 3 — Phase-by-phase plan с бюджетом

### Phase A — Validate current pipeline ($2 + ~$1 LLM API, 1-2 sessions)

**Цель**: Доказать, что текущий стек работает end-to-end на реальном OpenRouter.

#### A1. Field test (твоё ручное действие, $0 на разработку)
1. В `.env` должно быть: `REPO_PATH=/Users/efimenko_k/sandbox-project`, `BOT_STATE_DIR=/Users/efimenko_k/.ai-dev-team`
2. Перезапусти бота: `python scripts/run_telegram_bot.py`
3. В логе должна быть строка `Real LLM pipeline: True`
4. В Telegram: `/tier set ECONOMY`
5. Отправь: `Добавь функцию square(x: int) -> int в src/example.py, возвращает x*x. Плюс тест в tests/test_example.py: assert square(3) == 9`
6. Жди 2-5 минут, наблюдай прогресс
7. **Acceptance**: Pipeline доходит до SUCCESS или FAIL с понятной причиной. При SUCCESS — ветка `feature/task-...` существует в `~/sandbox-project` с реальным кодом.
8. Сохрани логи бота и финальный текст в чате

#### A2. Iterate prompts ($1-2, 1 Sonnet session)
Если в A1 был FAIL — найти, какой агент развалил JSON.
- File: `core/dispatcher_agents.py`, секции `_PLANNING_SYSTEM`, `_PM_SYSTEM`, `_ARCHITECT_SYSTEM`, `_WRITER_SYSTEM`, `_REVIEWER_SYSTEM`, `_TESTER_SYSTEM`, `_QA_SYSTEM`, `_FIXER_SYSTEM`
- Усилить инструкции по JSON-формату, добавить пример валидного output'а
- **Acceptance**: 5 разных простых задач (square, factorial, palindrome check, sort list, count words) все доходят до SUCCESS на ECONOMY tier

#### A3. Cost budget enforcement ($0.50, 1 Sonnet session)
- Текущий gap: `cost_budget_usd=$5` декоративный — `Orchestrator._check_cost_budget` требует `Observability + cost_estimator`, оба не передаются из `build_real_task_handler_from_env`
- Изменения:
  - `RealTaskHandlerConfig`: добавить опциональный `observability: Observability | None`
  - `build_real_task_handler_from_env`: построить `Observability(sinks=[JsonLinesSink(state_dir/pipeline_log.jsonl)])` и `cost_estimator` из `core.observability`
  - Передать оба в `Orchestrator(observability=..., cost_estimator=...)`
- Тесты: мок Observability сообщает `cost_snapshot.total_usd > 5.0` → орчестратор терминирует с `cost_budget_exceeded`
- **Acceptance**: новый opt-in тест триггерит budget exceeded после фейкового $5 расхода

---

### Phase B — Storage migration в SQLite ($1.50, 2 sessions)

**Цель**: Один файл `state.db`, всё состояние переживает рестарт.

#### B1. SQLite schema ($0.50, 1 Sonnet session)
- Создать `core/state_db.py` с классом `StateDB(path: Path)`:

```python
class StateDB:
    def __init__(self, path: Path): ...  # creates schema if missing
    
    # Tier sessions
    def get_tier(self, chat_id: int) -> str | None: ...
    def set_tier(self, chat_id: int, tier_name: str) -> None: ...
    def reset_tier(self, chat_id: int) -> None: ...
    
    # Task history
    def record_task(self, summary: TaskSummary) -> None: ...
    def get_task(self, task_id: str) -> TaskSummary | None: ...
    def recent_tasks(self, n: int = 10) -> list[TaskSummary]: ...
    
    # Budget
    def get_budget(self, chat_id: int) -> float | None: ...
    def set_budget(self, chat_id: int, usd: float) -> None: ...
    
    # Schema version (для миграций)
    def schema_version(self) -> int: ...
```

- Используй `sqlite3` из stdlib, **WAL mode** для concurrent чтения из веб-дашборда
- Тесты: round-trip, concurrent access из нескольких потоков, schema migration v1→v2
- **Acceptance**: 30+ тестов, ruff чисто

#### B2. Migrate existing modules to use StateDB ($1, 1 Sonnet session)
- `TierSessionStore` → внутри использует StateDB вместо JSON
- `TaskHistory` → читает/пишет в StateDB
- `_BudgetState` → читает/пишет в StateDB
- `bot_runner.build_bridge_from_env`: читает env `STATE_DB_PATH` (default `~/.ai-dev-team/state.db`)
- Migration: если есть старый `tier_sessions.json` — импортировать на первом старте, удалить файл
- **Acceptance**: все 1648+ тестов проходят (1618 существующих + 30 новых из B1)

---

### Phase C — 24/7 deployment ($1 на скрипты + manual VPS, ~30min)

**Цель**: Бот крутится постоянно, не падает.

#### C1. Hetzner setup (manual, ~30 min)
1. Регистрация на hetzner.cloud
2. Создать сервер CPX22 (Ubuntu 24.04, `hel1` / Helsinki; exact checked
   purchase-ready spec in `docs/HOSTING_PROVIDER_DECISION.md`)
3. SSH-ключ в проект → подключение
4. `apt update && apt install python3.12 python3.12-venv git nginx ufw`
5. Установить gh CLI: https://cli.github.com (скрипт от GitHub)
6. ufw allow 22, 80, 443
7. `git clone <твой репо>`
8. `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
9. Создать `.env` с НОВЫМИ ключами (после ротации старых)
10. `gh auth login` (через токен)

#### C2. systemd unit + healthcheck ($1, 1 Sonnet session)
- Создать `deploy/aidt-bot.service`:
```ini
[Unit]
Description=AI Dev Team Bot
After=network.target

[Service]
Type=simple
User=aidt
WorkingDirectory=/home/aidt/ai-dev-team
ExecStart=/home/aidt/ai-dev-team/.venv/bin/python scripts/run_telegram_bot.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

- Создать `scripts/healthcheck.py` — пингует Telegram getMe API, шлёт алерт владельцу если бот не отвечает >5min
- Cron: `0 3 * * * /home/aidt/ai-dev-team/scripts/backup_state.sh` (упаковка state.db и push в приватный backup-репо)
- **Acceptance**: `systemctl start aidt-bot`, `kill -9 $(pgrep -f run_telegram_bot)`, через 10 сек бот снова работает

#### C3. Domain + HTTPS (manual, ~30 min)
- Купить домен (~$10/yr) или использовать бесплатный duckdns.org
- DNS: A-запись на IP сервера
- `certbot --nginx -d aidt.example.com`
- Зарезервировано для Phase D

---

### Phase D — Web dashboard "Office" ($3, 3 sessions)

**Цель**: Read-only веб-страница, где видно агентов в реальном времени.

#### D1. FastAPI backend ($1, 1 session)
- Создать `web/main.py`:
```python
from fastapi import FastAPI, WebSocket
from core.state_db import StateDB

app = FastAPI()
db = StateDB(Path(os.environ["STATE_DB_PATH"]))

@app.get("/api/status")
def status(): ...
    # current task + agent statuses

@app.get("/api/history")
def history(): ...
    # last 50 tasks

@app.get("/api/agents")
def agents(): ...
    # per-agent metrics from observability

@app.websocket("/ws/events")
async def events(ws: WebSocket): ...
    # subscribes to ProgressEmitter, broadcasts JSON events
```

- Добавить `requirements.txt`: `fastapi`, `uvicorn[standard]`
- 20+ тестов через TestClient
- **Acceptance**: `curl http://localhost:8001/api/status` → JSON с актуальными данными

#### D2. Frontend ($1.50, 1 session)
- Создать `web/static/index.html` — single-page HTMX + Tailwind app
- 4 таба:
  - **Dashboard**: текущая задача, 8 agent cards с эмодзи + статус (idle/working/failed), live-stream events
  - **History**: таблица всех задач, клик → детали
  - **Agents**: per-agent stats (latency p50/p95, success rate, cost)
  - **Settings**: read-only текущий tier, budget, persona-конфиг
- Dark theme
- WebSocket для live-обновлений
- **No build step** — pure HTML+JS+CDN libs
- **Acceptance**: открываешь в браузере, видишь live-обновление пока бот работает

#### D3. Wire to nginx ($0.50, 1 session)
- nginx config:
```nginx
server {
    server_name aidt.example.com;
    location /office { root /home/aidt/ai-dev-team/web; }
    location /api { proxy_pass http://127.0.0.1:8001; }
    location /ws { proxy_pass http://127.0.0.1:8001; proxy_http_version 1.1; ... }
}
```
- systemd unit для FastAPI (`aidt-web.service`)
- Добавить URL `/office` в `make_help_handler` output
- **Acceptance**: `https://aidt.example.com/office` показывает живой dashboard

---

### Phase E — Multi-bot architecture ($4-5, 4 sessions)

**Цель**: 8 отдельных Telegram-ботов, каждый = свой агент.

#### E1. 8 BotFather tokens (manual, ~20 min)
В @BotFather создать 8 ботов:
- @aidt_planner_bot, @aidt_pm_bot, @aidt_architect_bot, @aidt_writer_bot
- @aidt_reviewer_bot, @aidt_tester_bot, @aidt_qa_bot, @aidt_fixer_bot

Каждому установить аватар и описание (можно одну и ту же эмодзи персоны).

В `.env`:
```
TELEGRAM_AGENT_TOKENS=planning_agent:1234:AAA,pm_agent:5678:BBB,architect_agent:9012:CCC,...
```

#### E2. MultiBotBridge ($2, 2 sessions)
- Новый файл `core/multi_bot_bridge.py`:
```python
class MultiBotBridge:
    """Manages N PTB Applications, one per agent role.
    
    Each ProgressEvent (agent_started, agent_finished) triggers a send
    from the corresponding bot to the user's per-agent DM.
    """
    def __init__(self, agent_tokens: dict[str, str], owner_chat_ids: frozenset[int]): ...
    def route_event(self, event: ProgressEvent, user_chat_id: int) -> None: ...
```
- Менять `scripts/run_telegram_bot.py`: читать `TELEGRAM_AGENT_TOKENS`, инициализировать N приложений
- При `agent_started("architect_agent")` → architect-бот шлёт в DM юзера: «🏗 Я начал думать над архитектурой...»
- При `agent_finished` → тот же бот: «🏗 Готово (1234 ms). Передаю Программисту»
- Юзер может DM архитектору с вопросом: «Какой паттерн используешь?» → architect-агент отвечает single-shot LLM-вызовом через `dispatcher.dispatch` со своей tier-цепочкой
- Тесты с мок-PTB
- **Acceptance**: 8 различных DM-чатов в Telegram, каждый отображает свою долю прогресса

#### E3. Team channel ($1, 1 session)
- Manual: создать Telegram supergroup, пригласить туда все 9 ботов как админов
- Boot-time: каждый бот при старте проверяет, что состоит в канале. Если нет — алерт владельцу
- В системный промпт каждого агента добавить tool/инструкцию:
  ```
  Если тебе нужна помощь от другого агента — заверши свой ответ JSON-полем:
  "team_message": {"to": "architect_agent", "question": "..."}
  ```
- Orchestrator при чтении ответа: если есть `team_message` → бот этого агента постит в team-канал «@Архитектор — должна быть REST или GraphQL?»
- Юзер видит обсуждение в реальном времени
- **Acceptance**: при сложной задаче в team-чате видна цепочка из 2-3 уточняющих сообщений между ботами

#### E4. DM-driven workflows ($1, 1 session)
- Юзер может DM любому агенту с вопросом БЕЗ запуска полного пайплайна
- Бот архитектора: «Какая разница между async и sync?» — single-shot LLM call, ответ
- Per-bot session memory: последние 10 exchanges хранятся в StateDB (`agent_dialog_history` table)
- **Acceptance**: 5 вопросов разным ботам — каждый отвечает в своём стиле, помнит предыдущий контекст

---

### Phase F — Visual polish ($1.50, 1-2 sessions)

#### F1. Inline keyboards ($0.50)
- `/tier` показывает кнопки `[💰 Economy] [🛠 Standard] [💎 Premium]`
- `/push` спрашивает подтверждение `[✅ Push] [❌ Cancel]`
- `/pr` аналогично
- Реализация: Telegram `InlineKeyboardMarkup`, callback handlers в `scripts/run_telegram_bot.py`
- **Acceptance**: 4 разных команды через кнопки, без typed input

#### F2. Markdown code formatting ($0.50)
- Writer agent output обернуть в ```` ```python ... ``` ```` блоки в Telegram
- `parse_mode="MarkdownV2"` с правильным escape'ом
- **Acceptance**: код в чате отображается моноширинно с синтаксис-цветами Telegram

#### F3. Progress bar message ($0.50)
- Заменить per-agent stream на одно editing-сообщение:
  ```
  🟢🟢🟢🟢🟡⚪⚪⚪ 4/8 · writer_agent in progress
  ```
- Telegram `edit_message_text` API
- При каждом `agent_finished` редактируем то же сообщение
- **Acceptance**: чат не флудится 16 сообщениями, видна одна красивая прогресс-полоска

---

### Phase G — Hiring agents L1+L2 ($2.50, 2-3 sessions)

#### G1. Static expansion: 3 новые роли ($1, 1 session)
- В `REQUIRED_AGENTS` (orchestrator.py): добавить `security_agent`, `devops_agent`, `data_agent`
- В `agent_personas.py`: 3 новые `AgentPersona` с эмодзи (🔐 🛠 📊) и описаниями
- В `agents.py` и `dispatcher_agents.py`: 3 новых функции с промптами
- В `model_tier.py:DEFAULT_TIERS`: добавить chains для 3 новых ролей в каждом тарифе
- В `orchestrator.py:State`: новые опциональные states `SECURITY_REVIEW`, `DEVOPS_CHECK`, `DATA_REVIEW` после QA
- PM-агент промпт расширить: может включать новые роли в `dependencies` поле плана
- **Acceptance**: simple task на ECONOMY проходит без вызова новых агентов; complex task с упоминанием безопасности — вызывает security_agent

#### G2. Dynamic specialization ($1, 1 session)
- PM-agent JSON output gains optional field:
```json
"specialization_hints": ["postgres", "rust", "k8s"]
```
- Когда set, writer_agent prompt augment'ится:
```
The user mentioned: PostgreSQL. Apply these domain-specific guidelines:
- Use parameterised queries, not string formatting
- ...
```
- Список доменов с инструкциями в `core/specialization_kb.py` (Knowledge Base)
- Тесты с мок LLM responses
- **Acceptance**: PM возвращает hints → writer показывает в логах augmented prompt → final code соответствует hints

#### G3. /team command ($0.50, 1 session)
- Юзер: `/team add security`, `/team remove devops`, `/team list`
- Хранится в StateDB (per-chat agent overrides)
- При запуске задачи orchestrator использует filtered REQUIRED_AGENTS
- **Acceptance**: юзер выключает security_agent → следующая задача его не вызывает

---

### Phase H — Production hardening ($2, 2 sessions)

#### H1. Error tracking ($0.50)
- Self-hosted GlitchTip (бесплатно, тот же VPS) или Sentry free tier
- Все exception в bot/worker → автоматически в трекер
- Алерт владельцу в Telegram при новой группе ошибок

#### H2. Rate limit handling ($0.50)
- Wrap-decorator над PTB `bot.send_message`: при HTTP 429 → читать `retry_after` header → exponential backoff
- Не критично для одного юзера, но сразу масштабируется

#### H3. Backup verification ($0.50)
- Cron: раз в неделю — restore drill: восстановить state.db из backup'а в /tmp, прогнать sanity-тест
- Алерт при failed restore

#### H4. Alert escalation ($0.50)
- Если бот падает 3 раза за 1 час → SMS через Twilio (~$0.01/сообщение) владельцу
- Альтернатива: Telegram-бот «watchdog» с другого VPS пишет тебе в личку

---

## Раздел 4 — Бюджет с разбивкой

| Phase | Стоимость dev | Cumulative | Что получишь |
|-------|---------------|------------|---------------|
| A — Validate (A1+A2+A3) | $2 | $2 | Знание реально ли работает + cost guard |
| B — SQLite (B1+B2) | $1.50 | $3.50 | Состояние не теряется при рестарте |
| C — Hetzner deploy (C2 only, C1+C3 manual) | $1 | $4.50 | 24/7 работа, autorestart |
| D — Office dashboard (D1+D2+D3) | $3 | $7.50 | Web-UI с live-обновлением |
| E — Multi-bot (E1 manual, E2+E3+E4) | $4 | $11.50 | 8 DM-ботов + team channel + direct queries |
| F — Visual polish (F1+F2+F3) | $1.50 | $13 | Inline keyboards, code blocks, progress bar |
| G — Hiring (G1+G2+G3) | $2.50 | $15.50 | 11 агентов, спецы, /team управление |
| H — Hardening (H1+H2+H3+H4) | $2 | $17.50 | Алерты, бэкапы, restart escalation |
| Buffer на правки | $1.83 | $19.33 | Запас |

**Влезает в $19.33 ровно.** Если что-то пойдёт дольше — выкидывай в порядке: H4 → H1 → D (web) → E4 → G2.

**Регулярные затраты после deploy**:
- Hetzner Cloud CPX22 + `1 x Primary IPv4` — `€8.49/mo` excl. VAT (checked on
  `2026-05-17`; see `docs/HOSTING_PROVIDER_DECISION.md`)
- OpenRouter API — зависит от использования; ECONOMY tier ~$0.20-1 за задачу
- Domain (опционально) — ~$10/yr

---

## Раздел 5 — Порядок выполнения

Чёткий рекомендуемый порядок:

1. **A1** (твоё ручное действие) — прогнать боевой тест, увидеть что работает.
2. **A2** — фикс промптов если нашлись проблемы.
3. **A3** — cost enforcement.
4. **B1+B2** — SQLite (без неё web-dashboard и multi-bot становятся хрупкими).
5. **C1** (твой manual) — поднять Hetzner.
6. **C2** — systemd, бот переезжает на сервер. **С этого момента весь дальнейший дев происходит против live-бота на VPS.**
7. **D1+D2+D3** — web dashboard.
8. **E1** (твой manual) — создать 8 ботов.
9. **E2+E3+E4** — multi-bot архитектура.
10. **F1+F2+F3** — UI polish.
11. **G1+G2+G3** — hiring новых агентов.
12. **H1-H4** — hardening.

---

## Раздел 6 — Hand-off prompt for any AI

При передаче проекта другой нейросети — копируй этот блок:

> Проект: AI Dev Team v4 ULTRA, multi-agent pipeline для автономной разработки.
> 
> Я работаю по плану в `docs/ROADMAP_TO_PRODUCTION.md`. Сейчас на Phase X, шаг Y.
> 
> Прежде чем что-либо делать:
> 1. Прочитай `docs/ROADMAP_TO_PRODUCTION.md` полностью.
> 2. Прочитай `docs/fsm_spec.md`.
> 3. Прочитай README.md.
> 4. Прочитай файлы в core/ относящиеся к текущей фазе.
> 5. Запусти `pytest` и `ruff check .` чтобы убедиться что всё зелёное.
> 
> Правила:
> - Каждое сообщение начинай со строки `**Модель: <название>**`. По умолчанию используй Sonnet 4.6 для рутины, Opus только под архитектурно-сложные решения, Haiku — под тривиальные правки.
> - Frozen-датаклассы с `__post_init__` валидацией.
> - Полная валидация инжектов через ValueError.
> - Никаких shell=True. Никаких хардкоженных API-ключей.
> - В конце каждого шага: ruff чисто, pytest зелёный, одна строка коммита.
> - Бюджет ограничен — экономь токены.
> 
> Текущий шаг: <конкретный шаг из roadmap>

---

## Раздел 7 — Чек-лист перед production

После завершения всех фаз проверь:

- [ ] `ruff check .` чисто
- [ ] `pytest` все зелёные
- [ ] Бот работает на Hetzner, выживает `kill -9`
- [ ] HTTPS на /office
- [ ] 8 DM-ботов отвечают
- [ ] Team channel с 9 админами активен
- [ ] State.db backup'ится ежедневно
- [ ] Healthcheck шлёт алерт при downtime >5 min
- [ ] Полный e2e тест на реальной задаче проходит SUCCESS
- [ ] Cost budget enforcement работает (тест триггерит exceeded)
- [ ] /push реально пушит в GitHub
- [ ] /pr создаёт draft PR
- [ ] При рестарте бота tier-выборы и история сохраняются
- [ ] Inline keyboards работают на /tier, /push, /pr
- [ ] Progress bar редактируется, не флудит
- [ ] /team add/remove работает
- [ ] PM может вызывать security/devops/data агентов на сложных задачах

---

**End of roadmap.** Если что-то непонятно — открой соответствующий core/-файл и читай docstring + контракты.
