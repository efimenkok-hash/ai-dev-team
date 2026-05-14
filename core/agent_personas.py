"""
core/agent_personas.py

Step 14a of the ULTRA spec: bot persona layer. Each functional agent or
system control-plane role (coordinator_agent, planning_agent, pm_agent,
architect_agent, ...) is wrapped in an AgentPersona that gives it a
human-readable identity for Telegram replies.

This is the "voice" layer — pure data + small string helpers. It does not
talk to LLMs, does not touch network, does not cross any FSM boundaries.
It exists because users want to interact with a *team*, not with a wall
of structured JSON.

CONTRACTS:
1. AgentPersona is frozen; mutation raises FrozenInstanceError.
2. agent_role MUST be one of the known system roles for this product.
   Unknown roles -> ValueError at construction time (fail fast).
3. human_name must be non-empty Russian/Latin text (chars: word, Cyrillic,
   space, hyphen, apostrophe), 1-40 chars after strip.
4. title (e.g. "Архитектор") must be non-empty.
5. seniority is one of VALID_SENIORITIES; default "junior".
6. voice_traits is a non-empty tuple of short adjectives describing tone,
   used by prompt-building code in telegram_bridge to seed agent voice.
7. format_signature(body) -> "<Title> <Name>: <body>".
   Idempotent: if body already starts with the signature, returns body
   unchanged (protects against double-signing in chains).
8. DEFAULT_PERSONAS provides a complete set for all known default personas,
   including future specialist roles that are not baseline workers yet.
9. PersonaRegistry maps agent_role -> AgentPersona, with .for_role()
   raising KeyError for unknown roles, .all() returning sorted tuple.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from core.agent_role_catalog import KNOWN_AGENT_ROLES

VALID_SENIORITIES = ("junior", "middle", "senior", "lead")

# Allowed characters: word (letters/digits/underscore in any script via
# re.UNICODE default), Cyrillic block (redundant safety), space, hyphen,
# apostrophe. Length 1-40 after strip. Rejects @, !, /, etc.
_NAME_RE = re.compile(r"^[\wЀ-ӿ\s\-']{1,40}$", re.UNICODE)


@dataclass(frozen=True)
class AgentPersona:
    """Persona for a functional agent role.

    `callsign` is the short codename used in chat signatures (e.g. "Ядро").
    `title` is the descriptive role label in Russian, shown in /agents
    perfomance views and used in system prompts ("Архитектор").
    The `human_name` field name is preserved for backwards compat with
    earlier code paths; semantically it's the callsign.
    """

    agent_role: str
    human_name: str  # callsign, e.g. "Архитектор"
    title: str       # descriptive role label, e.g. "Архитектор"
    seniority: str = "junior"
    voice_traits: tuple[str, ...] = ()
    emoji: str = ""  # thematic icon (optional, used by chat formatting)

    def __post_init__(self) -> None:
        if self.agent_role not in KNOWN_AGENT_ROLES:
            raise ValueError(f"unknown_agent_role:{self.agent_role}")

        if not isinstance(self.human_name, str):
            raise ValueError("non_string_human_name")
        stripped_name = self.human_name.strip()
        if not stripped_name:
            raise ValueError("empty_human_name")
        if not _NAME_RE.match(stripped_name):
            raise ValueError(f"invalid_human_name:{self.human_name!r}")

        if not isinstance(self.title, str):
            raise ValueError("non_string_title")
        stripped_title = self.title.strip()
        if not stripped_title:
            raise ValueError("empty_title")

        if self.seniority not in VALID_SENIORITIES:
            raise ValueError(f"invalid_seniority:{self.seniority}")

        if not isinstance(self.voice_traits, tuple):
            raise ValueError("voice_traits_must_be_tuple")
        if not self.voice_traits:
            raise ValueError("empty_voice_traits")
        for trait in self.voice_traits:
            if not isinstance(trait, str) or not trait.strip():
                raise ValueError("empty_voice_trait")

        if not isinstance(self.emoji, str):
            raise ValueError("non_string_emoji")

        # Persist normalised forms (frozen requires __setattr__ via object).
        object.__setattr__(self, "human_name", stripped_name)
        object.__setattr__(self, "title", stripped_title)
        object.__setattr__(self, "emoji", self.emoji.strip())

    @property
    def callsign(self) -> str:
        """Short codename used in chat signatures (e.g. 'Ядро')."""
        return self.human_name

    @property
    def display_name(self) -> str:
        """Alias of callsign — what users see in chat (e.g. 'Ядро')."""
        return self.human_name

    @property
    def qualified_name(self) -> str:
        """Callsign with role label, e.g. 'Ядро (Архитектор)'.

        Used in /agents performance tables and system prompts where the
        callsign alone could be ambiguous to a fresh reader. If callsign
        and title are identical (the default — role IS the name), returns
        just the callsign without redundant parentheses.
        """
        if self.human_name == self.title:
            return self.human_name
        return f"{self.human_name} ({self.title})"

    def format_signature(self, body: str) -> str:
        """Prefixes body with this persona's chat signature.

        Idempotent: if body already starts with `<callsign>: `, returns
        body unchanged. Strips leading whitespace from body before the
        check so '\\n  Ядро: x' doesn't double-sign.
        """
        if not isinstance(body, str):
            raise ValueError("non_string_body")
        body_stripped = body.lstrip()
        if not body_stripped:
            raise ValueError("empty_body")
        sig = f"{self.human_name}: "
        if body_stripped.startswith(sig):
            return body_stripped
        return sig + body_stripped


# Default personas: callsign IS the role title. "Должность есть имя" — what
# the user sees in chat ("Архитектор: предлагаю стек") is unambiguous and
# self-explanatory; no separate codename layer.
DEFAULT_PERSONAS: tuple[AgentPersona, ...] = (
    AgentPersona(
        agent_role="coordinator_agent",
        human_name="Координатор",
        title="Координатор",
        seniority="lead",
        voice_traits=("собранный", "держит контекст", "координирует действия"),
        emoji="🎯",
    ),
    AgentPersona(
        agent_role="planning_agent",
        human_name="Планировщик",
        title="Планировщик",
        seniority="middle",
        voice_traits=("структурный", "методичный", "разбивает на этапы"),
        emoji="🗺",
    ),
    AgentPersona(
        agent_role="pm_agent",
        human_name="Менеджер",
        title="Менеджер",
        seniority="senior",
        voice_traits=("организованный", "коротко", "ставит сроки"),
        emoji="📊",
    ),
    AgentPersona(
        agent_role="architect_agent",
        human_name="Архитектор",
        title="Архитектор",
        seniority="senior",
        voice_traits=("системный", "видит большую картину", "ссылается на принципы"),
        emoji="🧠",
    ),
    AgentPersona(
        agent_role="writer_agent",
        human_name="Программист",
        title="Программист",
        seniority="middle",
        voice_traits=("прагматичный", "пишет чисто", "комментирует только сложное"),
        emoji="⚒",
    ),
    AgentPersona(
        agent_role="reviewer_agent",
        human_name="Ревьюер",
        title="Ревьюер",
        seniority="senior",
        voice_traits=("дотошный", "ищет крайние случаи", "уважительный"),
        emoji="🔍",
    ),
    AgentPersona(
        agent_role="tester_agent",
        human_name="Тестировщик",
        title="Тестировщик",
        seniority="middle",
        voice_traits=("параноидальный", "покрывает edge cases", "пишет читаемо"),
        emoji="🧪",
    ),
    AgentPersona(
        agent_role="qa_agent",
        human_name="QA-инженер",
        title="QA-инженер",
        seniority="senior",
        voice_traits=("строгий", "финальное слово", "беспристрастный"),
        emoji="✅",
    ),
    AgentPersona(
        agent_role="fixer_agent",
        human_name="Фиксер",
        title="Фиксер",
        seniority="middle",
        voice_traits=("быстрый", "хирургический", "минимум изменений"),
        emoji="🩹",
    ),
    AgentPersona(
        agent_role="security_agent",
        human_name="Безопасник",
        title="Безопасник",
        seniority="senior",
        voice_traits=("подозрительный", "видит угрозы", "харднит контур"),
        emoji="🛡",
    ),
    AgentPersona(
        agent_role="devops_agent",
        human_name="Девопс",
        title="Девопс",
        seniority="senior",
        voice_traits=("операционный", "любит надёжность", "думает о деплое"),
        emoji="⚙️",
    ),
    AgentPersona(
        agent_role="data_agent",
        human_name="Дата-инженер",
        title="Дата-инженер",
        seniority="senior",
        voice_traits=("аккуратный", "следит за схемами", "проверяет корректность данных"),
        emoji="🧮",
    ),
)


class PersonaRegistry:
    """Registry mapping functional agent_role -> AgentPersona instance.

    Default construction loads DEFAULT_PERSONAS; pass a custom iterable
    for tests or alternate teams.
    """

    def __init__(
        self,
        personas: Iterable[AgentPersona] = DEFAULT_PERSONAS,
    ) -> None:
        self._by_role: dict[str, AgentPersona] = {}
        for persona in personas:
            if not isinstance(persona, AgentPersona):
                raise ValueError(
                    f"invalid_persona_type:{type(persona).__name__}"
                )
            if persona.agent_role in self._by_role:
                raise ValueError(f"duplicate_persona:{persona.agent_role}")
            self._by_role[persona.agent_role] = persona

    def for_role(self, agent_role: str) -> AgentPersona:
        if agent_role not in self._by_role:
            raise KeyError(f"no_persona_for:{agent_role}")
        return self._by_role[agent_role]

    def all(self) -> tuple[AgentPersona, ...]:
        return tuple(self._by_role[r] for r in sorted(self._by_role))

    def list_roles(self) -> list[str]:
        return sorted(self._by_role.keys())

    def __contains__(self, role: object) -> bool:
        return role in self._by_role

    def __len__(self) -> int:
        return len(self._by_role)


def default_registry() -> PersonaRegistry:
    """Convenience: returns a registry preloaded with DEFAULT_PERSONAS."""
    return PersonaRegistry()
