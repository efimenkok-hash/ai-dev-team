"""
core/confirmation_gate.py

Step 14a: policy module that decides whether a planned action runs
automatically or requires explicit user confirmation in chat. Implements
the spec: "удаление/переименование, новая зависимость, изменение CI,
push в main, бюджет > $1, изменение ядра/критической инфраструктуры —
обязательное подтверждение, и обязательно с обоснованием почему".

The gate is pure logic: no I/O, no LLM, no telegram. It takes a structured
description of a planned action and returns a decision + Russian-language
rationale. The Telegram bridge consumes that decision: if confirmation
is required, it sends the user a message with the rationale and waits
for [Подтвердить] / [Отменить].

CONTRACTS:
1. ActionDescriptor is frozen; fields validated in __post_init__.
2. ConfirmationDecision is frozen; reason field is non-empty Russian.
3. ConfirmationGate is constructed with explicit defaults; protected_paths
   and cost_threshold can be overridden per project (via adapter).
4. evaluate(action) -> ConfirmationDecision is pure and deterministic.
5. evaluate_batch(actions) -> BatchDecision aggregates per-action results
   and exposes require_any_confirmation flag for short-circuit.
6. Path matching is prefix-based after normalisation: 'core/' matches
   'core/orchestrator.py'; case-sensitive; '..' rejected as path traversal.
7. Order of checks: high-cost > always-ask kind > protected-path on
   MODIFY/WRITE_NEW > auto. First hit wins; the reason is the matching
   trigger.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum


class ActionKind(str, Enum):
    WRITE_NEW = "WRITE_NEW"
    MODIFY = "MODIFY"
    DELETE = "DELETE"
    RENAME = "RENAME"
    ADD_DEPENDENCY = "ADD_DEPENDENCY"
    REMOVE_DEPENDENCY = "REMOVE_DEPENDENCY"
    CI_CHANGE = "CI_CHANGE"
    PUSH_TO_MAIN = "PUSH_TO_MAIN"


# Default critical infrastructure paths — these always require confirmation
# on MODIFY or WRITE_NEW. Adapter can override via constructor arg.
DEFAULT_PROTECTED_PATHS: tuple[str, ...] = (
    "core/orchestrator.py",
    "core/fsm.py",
    "core/memory.py",
    "core/quality_gates.py",
    "core/runtime_validator.py",
    "core/contracts.py",
    "core/observability.py",
    "core/agents.py",
    "core/router.py",
    "core/adapter.py",
    "core/git_integration.py",
    "core/patcher.py",
    "core/self_improvement.py",
    "core/agent_personas.py",
    "core/whisper_client.py",
    "core/vision_client.py",
    "core/confirmation_gate.py",
    ".github/workflows/",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "main.py",
    "scripts/",
)

DEFAULT_COST_THRESHOLD_USD = 1.0

# Action kinds that ALWAYS require confirmation, regardless of path or cost.
_ALWAYS_ASK_KINDS = frozenset({
    ActionKind.DELETE,
    ActionKind.RENAME,
    ActionKind.ADD_DEPENDENCY,
    ActionKind.REMOVE_DEPENDENCY,
    ActionKind.CI_CHANGE,
    ActionKind.PUSH_TO_MAIN,
})

# Action kinds where path-protection rules apply (modifying existing or
# writing into a protected directory).
_PATH_GUARDED_KINDS = frozenset({
    ActionKind.MODIFY,
    ActionKind.WRITE_NEW,
})


@dataclass(frozen=True)
class ActionDescriptor:
    kind: ActionKind
    target_path: str = ""
    detail: str = ""
    estimated_cost_usd: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ActionKind):
            raise ValueError(f"invalid_kind:{self.kind!r}")
        if not isinstance(self.target_path, str):
            raise ValueError("non_string_target_path")
        if any(part == ".." for part in self.target_path.split("/")):
            raise ValueError(f"path_traversal:{self.target_path}")
        if not isinstance(self.detail, str):
            raise ValueError("non_string_detail")
        if (
            isinstance(self.estimated_cost_usd, bool)
            or not isinstance(self.estimated_cost_usd, (int, float))
            or self.estimated_cost_usd < 0
        ):
            raise ValueError(f"invalid_cost:{self.estimated_cost_usd!r}")


@dataclass(frozen=True)
class ConfirmationDecision:
    require_confirmation: bool
    reason: str
    action: ActionDescriptor


@dataclass(frozen=True)
class BatchDecision:
    decisions: tuple[ConfirmationDecision, ...]
    require_any_confirmation: bool

    def asks(self) -> tuple[ConfirmationDecision, ...]:
        """Subset of decisions that require confirmation."""
        return tuple(d for d in self.decisions if d.require_confirmation)


class ConfirmationGate:
    def __init__(
        self,
        *,
        protected_paths: tuple[str, ...] = DEFAULT_PROTECTED_PATHS,
        cost_threshold_usd: float = DEFAULT_COST_THRESHOLD_USD,
    ) -> None:
        if not isinstance(protected_paths, tuple):
            raise ValueError("protected_paths_must_be_tuple")
        for p in protected_paths:
            if not isinstance(p, str) or not p.strip():
                raise ValueError("empty_protected_path")
        if (
            isinstance(cost_threshold_usd, bool)
            or not isinstance(cost_threshold_usd, (int, float))
            or cost_threshold_usd < 0
        ):
            raise ValueError(
                f"invalid_cost_threshold:{cost_threshold_usd!r}"
            )
        self._protected_paths = tuple(_normalise_path(p) for p in protected_paths)
        self._cost_threshold = float(cost_threshold_usd)

    @property
    def protected_paths(self) -> tuple[str, ...]:
        return self._protected_paths

    @property
    def cost_threshold_usd(self) -> float:
        return self._cost_threshold

    def evaluate(self, action: ActionDescriptor) -> ConfirmationDecision:
        if not isinstance(action, ActionDescriptor):
            raise ValueError(
                f"invalid_action_type:{type(action).__name__}"
            )

        # 1. High cost: ask regardless of kind. Cost is the most fundamental
        # gate — runaway-budget protection.
        if action.estimated_cost_usd > self._cost_threshold:
            return ConfirmationDecision(
                require_confirmation=True,
                reason=(
                    f"оценочная стоимость ${action.estimated_cost_usd:.2f} "
                    f"превышает порог ${self._cost_threshold:.2f}"
                ),
                action=action,
            )

        # 2. Always-ask kinds (delete, rename, dep changes, CI changes, push to main).
        if action.kind in _ALWAYS_ASK_KINDS:
            return ConfirmationDecision(
                require_confirmation=True,
                reason=_reason_for_always_ask_kind(action),
                action=action,
            )

        # 3. Path-guarded kinds: MODIFY/WRITE_NEW that hits a protected path.
        if action.kind in _PATH_GUARDED_KINDS:
            normalised = _normalise_path(action.target_path)
            for protected in self._protected_paths:
                if _path_matches(normalised, protected):
                    return ConfirmationDecision(
                        require_confirmation=True,
                        reason=_reason_for_protected_path(action, protected),
                        action=action,
                    )

        # 4. Auto.
        return ConfirmationDecision(
            require_confirmation=False,
            reason="auto: безопасное действие в рамках политики",
            action=action,
        )

    def evaluate_batch(
        self,
        actions: Iterable[ActionDescriptor],
    ) -> BatchDecision:
        decisions = tuple(self.evaluate(a) for a in actions)
        require_any = any(d.require_confirmation for d in decisions)
        return BatchDecision(
            decisions=decisions,
            require_any_confirmation=require_any,
        )


def _normalise_path(path: str) -> str:
    """Strip leading './' iterations and surrounding whitespace.

    Preserves trailing '/' (it carries semantic meaning — directory marker).
    """
    p = path.strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def _path_matches(path: str, protected: str) -> bool:
    """True if path is at or under protected.

    Examples:
      'core/orchestrator.py' matches protected 'core/orchestrator.py' (exact)
      '.github/workflows/ci.yml' matches '.github/workflows/' (dir prefix)
      'core/orchestrator.py' matches 'core/' (implicit dir prefix)
      'corex/file.py' does NOT match 'core/' (no false-positive on substring)
      '' does NOT match anything.
    """
    if not path or not protected:
        return False
    if path == protected:
        return True
    if protected.endswith("/") and path.startswith(protected):
        return True
    return bool(
        not protected.endswith("/") and path.startswith(protected + "/")
    )


def _reason_for_always_ask_kind(action: ActionDescriptor) -> str:
    """Russian-language rationale for kinds that always require confirmation."""
    path = action.target_path or "неуказанный путь"
    detail = action.detail or ""
    detail_suffix = f" — {detail}" if detail else ""

    kind = action.kind
    if kind is ActionKind.DELETE:
        return (
            f"удаление файла '{path}'{detail_suffix}: "
            f"восстановление через бот невозможно"
        )
    if kind is ActionKind.RENAME:
        return (
            f"переименование/перемещение '{path}'{detail_suffix}: "
            f"ломает прямые ссылки в импортах и в документации"
        )
    if kind is ActionKind.ADD_DEPENDENCY:
        target = path if action.target_path else "requirements.txt"
        return (
            f"добавление новой зависимости в {target}{detail_suffix}: "
            f"расширяет внешнюю поверхность проекта"
        )
    if kind is ActionKind.REMOVE_DEPENDENCY:
        target = path if action.target_path else "requirements.txt"
        return (
            f"удаление зависимости из {target}{detail_suffix}: "
            f"может сломать существующий код"
        )
    if kind is ActionKind.CI_CHANGE:
        return (
            f"изменение CI '{path}'{detail_suffix}: "
            f"затрагивает все будущие билды"
        )
    if kind is ActionKind.PUSH_TO_MAIN:
        return (
            f"push напрямую в main{detail_suffix}: "
            f"пропускает feature-branch + PR review"
        )
    return f"действие требует подтверждения: {kind.value}{detail_suffix}"


def _reason_for_protected_path(action: ActionDescriptor, protected: str) -> str:
    detail = action.detail or ""
    detail_suffix = f" — {detail}" if detail else ""
    verb = "модификация" if action.kind is ActionKind.MODIFY else "создание файла"
    return (
        f"{verb} в защищённой зоне '{protected}': "
        f"'{action.target_path}'{detail_suffix}. "
        f"Это критическая инфраструктура / ядро."
    )
