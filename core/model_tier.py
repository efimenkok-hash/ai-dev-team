"""
core/model_tier.py

Step 14b-1: configurable model stacks for the agent pipeline. A "tier"
(economy / standard / premium) defines, *per agent role*, an ordered chain
of OpenRouter model identifiers. The dispatcher walks the chain top-down
and falls back to the next model on failure.

Design intent (from the spec):
- The user picks the tier per task (or per chat) — cheaper for small jobs,
  premium for hard ones. The bot explains the trade-off before charging.
- Models are *data*, not code. Adding a new model = adding a string to
  a tuple. The visual UI for this lives in the bot (commands `/tier`,
  `/models`) — this module just stores and validates the shape.
- Each role has its own chain because Architect benefits from a strong
  generalist, Programmer from a coding-tuned model, Reviewer/QA from
  another strong generalist, etc.

CONTRACTS:
1. TierConfig is frozen; all fields validated in __post_init__.
2. models_per_role MUST cover ALL 8 known roles (planning_agent, pm_agent,
   architect_agent, writer_agent, reviewer_agent, tester_agent, qa_agent,
   fixer_agent). Missing role -> ValueError.
3. Each chain MUST be a non-empty tuple of non-empty strings (model ids).
4. estimated_cost_usd must be > 0 and < 100 (sanity).
5. DEFAULT_TIERS provides ECONOMY / STANDARD / PREMIUM with verified
   OpenRouter model identifiers (mid-2026 baseline; can be replaced via
   the registry without touching code).
6. TierRegistry rejects duplicate names; .active() returns the currently-
   selected tier (default: STANDARD).
7. to_dict / from_dict round-trip stably for persistence and bot config.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

# Keep in sync with core.orchestrator.REQUIRED_AGENTS — duplicated to avoid
# an import cycle (orchestrator doesn't depend on this module).
REQUIRED_ROLES: frozenset[str] = frozenset({
    "planning_agent",
    "pm_agent",
    "architect_agent",
    "writer_agent",
    "reviewer_agent",
    "tester_agent",
    "qa_agent",
    "fixer_agent",
})

# OpenRouter limits per provider; we just need a simple positive cap to
# catch obvious mistakes when someone configures a tier.
_MAX_REASONABLE_COST_USD = 100.0


class ModelTierName(str, Enum):
    ECONOMY = "ECONOMY"
    STANDARD = "STANDARD"
    PREMIUM = "PREMIUM"


@dataclass(frozen=True)
class TierConfig:
    name: str
    description: str
    estimated_cost_usd: float
    models_per_role: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("empty_tier_name")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("empty_tier_description")
        if (
            isinstance(self.estimated_cost_usd, bool)
            or not isinstance(self.estimated_cost_usd, (int, float))
        ):
            raise ValueError(f"invalid_cost_type:{type(self.estimated_cost_usd).__name__}")
        if self.estimated_cost_usd <= 0:
            raise ValueError(f"non_positive_cost:{self.estimated_cost_usd}")
        if self.estimated_cost_usd > _MAX_REASONABLE_COST_USD:
            raise ValueError(f"cost_too_high:{self.estimated_cost_usd}")

        if not isinstance(self.models_per_role, Mapping):
            raise ValueError("models_per_role_must_be_mapping")

        # All required roles must be present and non-trivial.
        missing = REQUIRED_ROLES - set(self.models_per_role.keys())
        if missing:
            raise ValueError(f"missing_roles:{','.join(sorted(missing))}")
        unexpected = set(self.models_per_role.keys()) - REQUIRED_ROLES
        if unexpected:
            raise ValueError(f"unknown_roles:{','.join(sorted(unexpected))}")

        normalised: dict[str, tuple[str, ...]] = {}
        for role, chain in self.models_per_role.items():
            if not isinstance(chain, tuple):
                raise ValueError(f"chain_must_be_tuple:{role}")
            if not chain:
                raise ValueError(f"empty_chain:{role}")
            cleaned: list[str] = []
            seen: set[str] = set()
            for model_id in chain:
                if not isinstance(model_id, str):
                    raise ValueError(f"non_string_model_id:{role}")
                stripped = model_id.strip()
                if not stripped:
                    raise ValueError(f"empty_model_id:{role}")
                if stripped in seen:
                    raise ValueError(f"duplicate_in_chain:{role}:{stripped}")
                seen.add(stripped)
                cleaned.append(stripped)
            normalised[role] = tuple(cleaned)

        # Persist normalised form (frozen requires object.__setattr__).
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "description", self.description.strip())
        object.__setattr__(self, "estimated_cost_usd", float(self.estimated_cost_usd))
        object.__setattr__(self, "models_per_role", dict(normalised))

    def chain_for(self, agent_role: str) -> tuple[str, ...]:
        """Returns the fallback chain for the given role, or raises KeyError."""
        if agent_role not in self.models_per_role:
            raise KeyError(f"unknown_role:{agent_role}")
        return self.models_per_role[agent_role]

    def primary_model(self, agent_role: str) -> str:
        """Convenience: first model in the chain for a role."""
        return self.chain_for(agent_role)[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "name": self.name,
            "description": self.description,
            "estimated_cost_usd": self.estimated_cost_usd,
            "models_per_role": {
                role: list(chain)
                for role, chain in self.models_per_role.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TierConfig":
        if not isinstance(data, Mapping):
            raise ValueError("invalid_dump_type")
        if data.get("schema_version") != 1:
            raise ValueError(
                f"unsupported_schema_version:{data.get('schema_version')}"
            )
        for key in ("name", "description", "estimated_cost_usd", "models_per_role"):
            if key not in data:
                raise ValueError(f"missing_key:{key}")
        models = {
            role: tuple(chain)
            for role, chain in data["models_per_role"].items()
        }
        return cls(
            name=data["name"],
            description=data["description"],
            estimated_cost_usd=data["estimated_cost_usd"],
            models_per_role=models,
        )


# ---------------------------------------------------------------------------
# Default tiers — verified OpenRouter model identifiers (mid-2026).
# These can be edited at runtime via TierRegistry without code changes.
# ---------------------------------------------------------------------------

DEFAULT_TIERS: tuple[TierConfig, ...] = (
    TierConfig(
        name=ModelTierName.ECONOMY.value,
        description=(
            "💰 Дёшево — ~$0.20 за задачу. Подходит для проб и простых "
            "правок. Качество архитектуры/ревью среднее."
        ),
        estimated_cost_usd=0.20,
        models_per_role={
            "planning_agent": ("openai/gpt-4o-mini", "qwen/qwen3-coder"),
            "pm_agent": ("openai/gpt-4o-mini",),
            "architect_agent": ("openai/gpt-4o-mini", "qwen/qwen3-coder"),
            "writer_agent": ("qwen/qwen3-coder", "openai/gpt-4o-mini"),
            "reviewer_agent": ("openai/gpt-4o-mini",),
            "tester_agent": ("openai/gpt-4o-mini", "qwen/qwen3-coder"),
            "qa_agent": ("openai/gpt-4o-mini",),
            "fixer_agent": ("qwen/qwen3-coder", "openai/gpt-4o-mini"),
        },
    ),
    TierConfig(
        name=ModelTierName.STANDARD.value,
        description=(
            "🛠 Нормально — ~$1.00 за задачу. Сильный архитектор и ревьюер, "
            "кодовая модель для программиста, дешёвые помощники для рутины."
        ),
        estimated_cost_usd=1.00,
        models_per_role={
            "planning_agent": ("anthropic/claude-haiku-4.5", "openai/gpt-4o-mini"),
            "pm_agent": ("anthropic/claude-haiku-4.5",),
            "architect_agent": (
                "anthropic/claude-sonnet-4.6",
                "openai/gpt-5",
                "anthropic/claude-haiku-4.5",
            ),
            "writer_agent": (
                "qwen/qwen3-coder",
                "anthropic/claude-sonnet-4.6",
            ),
            "reviewer_agent": (
                "anthropic/claude-sonnet-4.6",
                "openai/gpt-5",
            ),
            "tester_agent": (
                "anthropic/claude-haiku-4.5",
                "qwen/qwen3-coder",
            ),
            "qa_agent": (
                "anthropic/claude-sonnet-4.6",
                "anthropic/claude-haiku-4.5",
            ),
            "fixer_agent": ("qwen/qwen3-coder", "anthropic/claude-haiku-4.5"),
        },
    ),
    TierConfig(
        name=ModelTierName.PREMIUM.value,
        description=(
            "💎 Дорого — ~$4.00 за задачу. Архитектор и ревьюер на топовых "
            "моделях с двумя fallback'ами. Для критичных задач."
        ),
        estimated_cost_usd=4.00,
        models_per_role={
            "planning_agent": (
                "anthropic/claude-haiku-4.5",
                "anthropic/claude-sonnet-4.6",
            ),
            "pm_agent": (
                "anthropic/claude-sonnet-4.6",
                "anthropic/claude-haiku-4.5",
            ),
            "architect_agent": (
                "anthropic/claude-opus-4.7",
                "anthropic/claude-opus-4.6",
                "openai/gpt-5.5",
            ),
            "writer_agent": (
                "qwen/qwen3-coder",
                "anthropic/claude-opus-4.6",
                "anthropic/claude-sonnet-4.6",
            ),
            "reviewer_agent": (
                "anthropic/claude-opus-4.6",
                "anthropic/claude-sonnet-4.6",
                "openai/gpt-5.5",
            ),
            "tester_agent": (
                "anthropic/claude-sonnet-4.6",
                "anthropic/claude-haiku-4.5",
            ),
            "qa_agent": (
                "anthropic/claude-opus-4.6",
                "anthropic/claude-sonnet-4.6",
            ),
            "fixer_agent": (
                "anthropic/claude-sonnet-4.6",
                "qwen/qwen3-coder",
            ),
        },
    ),
)


class TierRegistry:
    """Manages a set of tiers + tracks which is currently active.

    The bot's `/tier` and `/models` commands manipulate this registry at
    runtime. Default construction loads DEFAULT_TIERS with STANDARD active.
    """

    def __init__(
        self,
        tiers: Iterable[TierConfig] = DEFAULT_TIERS,
        *,
        active_name: str = ModelTierName.STANDARD.value,
    ) -> None:
        self._by_name: dict[str, TierConfig] = {}
        for tier in tiers:
            if not isinstance(tier, TierConfig):
                raise ValueError(
                    f"invalid_tier_type:{type(tier).__name__}"
                )
            if tier.name in self._by_name:
                raise ValueError(f"duplicate_tier:{tier.name}")
            self._by_name[tier.name] = tier
        if not self._by_name:
            raise ValueError("empty_tier_registry")
        if active_name not in self._by_name:
            raise ValueError(f"unknown_active_tier:{active_name}")
        self._active_name = active_name

    def register(self, tier: TierConfig) -> None:
        if not isinstance(tier, TierConfig):
            raise ValueError(
                f"invalid_tier_type:{type(tier).__name__}"
            )
        if tier.name in self._by_name:
            raise ValueError(f"duplicate_tier:{tier.name}")
        self._by_name[tier.name] = tier

    def replace(self, tier: TierConfig) -> None:
        """Overwrite an existing tier (used by `/tier set` to update models)."""
        if not isinstance(tier, TierConfig):
            raise ValueError(
                f"invalid_tier_type:{type(tier).__name__}"
            )
        if tier.name not in self._by_name:
            raise KeyError(f"unknown_tier:{tier.name}")
        self._by_name[tier.name] = tier

    def get(self, name: str) -> TierConfig:
        if name not in self._by_name:
            raise KeyError(f"unknown_tier:{name}")
        return self._by_name[name]

    def list_names(self) -> list[str]:
        return sorted(self._by_name.keys())

    def all(self) -> tuple[TierConfig, ...]:
        return tuple(self._by_name[n] for n in sorted(self._by_name))

    def set_active(self, name: str) -> None:
        if name not in self._by_name:
            raise KeyError(f"unknown_tier:{name}")
        self._active_name = name

    def active(self) -> TierConfig:
        return self._by_name[self._active_name]

    def active_name(self) -> str:
        return self._active_name

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def __len__(self) -> int:
        return len(self._by_name)


def default_registry() -> TierRegistry:
    """Convenience factory: registry with DEFAULT_TIERS, STANDARD active."""
    return TierRegistry()


def load_tiers_from_iterable(
    data: Iterable[Mapping[str, Any]],
    *,
    active_name: str = ModelTierName.STANDARD.value,
) -> TierRegistry:
    """Builds a TierRegistry from a list of dump-dict's (round-trip with to_dict)."""
    tiers = tuple(TierConfig.from_dict(d) for d in data)
    return TierRegistry(tiers, active_name=active_name)
