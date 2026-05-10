from __future__ import annotations

from dataclasses import dataclass

from core.agent_personas import AgentPersona, PersonaRegistry
from core.coordinator_onboarding import describe_context_source
from core.coordinator_role import COORDINATOR_ROLE
from core.project_registry import ProjectSnapshot

BASELINE_TEAM_ROLE_ORDER = (
    COORDINATOR_ROLE,
    "planning_agent",
    "pm_agent",
    "architect_agent",
    "writer_agent",
    "reviewer_agent",
    "tester_agent",
    "qa_agent",
    "fixer_agent",
)

_VALID_TEAM_PROPOSAL_CONTEXT_SOURCES = frozenset(
    {"bound_chat", "owner_dm_single_project"}
)

_ROLE_MANDATES = {
    COORDINATOR_ROLE: (
        "Control-plane lead and project captain; keeps project scope, "
        "context, and coordination authoritative."
    ),
    "planning_agent": (
        "Structures the task, clarifies constraints, and defines the initial "
        "execution outline."
    ),
    "pm_agent": (
        "Turns the plan into concrete internal work packages and keeps the "
        "execution sequence organized."
    ),
    "architect_agent": (
        "Defines the solution shape, tradeoffs, and implementation boundaries."
    ),
    "writer_agent": (
        "Implements the code changes inside the approved project contour."
    ),
    "reviewer_agent": (
        "Reviews the implementation for correctness, regressions, and risk."
    ),
    "tester_agent": (
        "Adds or validates tests and checks edge cases for the implementation."
    ),
    "qa_agent": (
        "Acts as the final quality gate before success is accepted."
    ),
    "fixer_agent": (
        "Applies targeted fixes when review or QA rejects the current result."
    ),
}


def _normalize_owner_task_text(owner_task_text: str) -> str:
    if not isinstance(owner_task_text, str) or not owner_task_text.strip():
        raise ValueError("empty_owner_task_text")
    return owner_task_text.strip()


def _validate_required_roles(personas: PersonaRegistry) -> None:
    missing = [role for role in BASELINE_TEAM_ROLE_ORDER if role not in personas]
    if missing:
        raise ValueError(f"missing_required_persona_roles:{','.join(missing)}")


@dataclass(frozen=True)
class CoordinatorTeamProposalContext:
    snapshot: ProjectSnapshot
    owner_task_text: str
    context_source: str
    personas: PersonaRegistry

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, ProjectSnapshot):
            raise ValueError(
                f"invalid_project_snapshot_type:{type(self.snapshot).__name__}"
            )
        if self.snapshot.runtime_binding is None:
            raise ValueError("snapshot_missing_runtime_binding")
        object.__setattr__(
            self,
            "owner_task_text",
            _normalize_owner_task_text(self.owner_task_text),
        )
        if (
            not isinstance(self.context_source, str)
            or self.context_source not in _VALID_TEAM_PROPOSAL_CONTEXT_SOURCES
        ):
            raise ValueError(f"invalid_context_source:{self.context_source!r}")
        if not isinstance(self.personas, PersonaRegistry):
            raise ValueError(
                f"invalid_persona_registry_type:{type(self.personas).__name__}"
            )
        _validate_required_roles(self.personas)


class CoordinatorTeamProposalService:
    @staticmethod
    def _default_team_role_order() -> tuple[str, ...]:
        return BASELINE_TEAM_ROLE_ORDER

    @staticmethod
    def _role_mandate(role: str) -> str:
        if not isinstance(role, str) or role not in _ROLE_MANDATES:
            raise ValueError(f"unknown_team_proposal_role:{role!r}")
        return _ROLE_MANDATES[role]

    def _require_context(
        self,
        context: CoordinatorTeamProposalContext,
    ) -> CoordinatorTeamProposalContext:
        if not isinstance(context, CoordinatorTeamProposalContext):
            raise ValueError(
                "invalid_coordinator_team_proposal_context_type:"
                f"{type(context).__name__}"
            )
        if context.snapshot.runtime_binding is None:
            raise ValueError("snapshot_missing_runtime_binding")
        _validate_required_roles(context.personas)
        return context

    @staticmethod
    def _persona_lines(persona: AgentPersona, mandate: str) -> list[str]:
        return [
            f"- role_id: {persona.agent_role}",
            f"  human_name: {persona.human_name}",
            f"  title: {persona.title}",
            f"  seniority: {persona.seniority}",
            f"  mandate: {mandate}",
        ]

    def build_team_proposal_artifact(
        self,
        context: CoordinatorTeamProposalContext,
    ) -> str:
        context = self._require_context(context)
        snapshot = context.snapshot
        coordinator_persona = context.personas.for_role(COORDINATOR_ROLE)

        lines = [
            "Coordinator team proposal",
            "",
            "Project anchor:",
            f"- project_id: {snapshot.project.project_id}",
            f"- slug: {snapshot.project.slug}",
            f"- name: {snapshot.project.name}",
            "",
            "Context mode:",
            f"- mode: {describe_context_source(context.context_source)}",
            "",
            "Project captain:",
            f"- role_id: {coordinator_persona.agent_role}",
            f"  human_name: {coordinator_persona.human_name}",
            f"  title: {coordinator_persona.title}",
            f"  seniority: {coordinator_persona.seniority}",
            (
                "  mandate: project captain and control-plane lead; "
                "coordinates the team instead of executing the whole pipeline alone."
            ),
            "",
            "Proposed internal team:",
        ]

        for role in self._default_team_role_order():
            persona = context.personas.for_role(role)
            lines.extend(
                self._persona_lines(
                    persona,
                    self._role_mandate(role),
                )
            )

        lines.extend(
            [
                "",
                "Owner task anchor:",
                "<<<OWNER_TASK",
                context.owner_task_text,
                "OWNER_TASK",
                "",
                "Scope and non-goals:",
                "This proposal is anchored to the current project contour only.",
                "Do not invent another project or runtime contour.",
                "Hiring and external roles are not auto-activated at this step.",
            ]
        )
        return "\n".join(lines)
