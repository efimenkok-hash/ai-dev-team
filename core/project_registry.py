"""
core/project_registry.py

Project-centric service layer for the AI Office domain model.

Scope for roadmap step P1.3:
1. Assemble a project as a single aggregate snapshot.
2. Validate snapshot consistency eagerly and deterministically.
3. Provide a small registry API on top of StateDB without touching runtime
   integration, Telegram routing, onboarding, or adapter execution.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.hire_approval import PendingHireRequest
from core.project_models import (
    Project,
    ProjectChatBinding,
    ProjectMembership,
    ProjectPolicy,
)
from core.project_runtime import ProjectRuntimeBinding
from core.project_team_state import ProjectSpecialistRoster
from core.state_db import StateDB
from core.task_history import TaskSummary


@dataclass(frozen=True)
class ProjectSnapshot:
    project: Project
    policy: ProjectPolicy | None = None
    memberships: tuple[ProjectMembership, ...] = ()
    chat_binding: ProjectChatBinding | None = None
    runtime_binding: ProjectRuntimeBinding | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.project, Project):
            raise ValueError(
                f"invalid_project_type:{type(self.project).__name__}"
            )
        if self.policy is not None and not isinstance(self.policy, ProjectPolicy):
            raise ValueError(
                f"invalid_project_policy_type:{type(self.policy).__name__}"
            )
        if self.chat_binding is not None and not isinstance(
            self.chat_binding,
            ProjectChatBinding,
        ):
            raise ValueError(
                "invalid_project_chat_binding_type:"
                f"{type(self.chat_binding).__name__}"
            )
        if self.runtime_binding is not None and not isinstance(
            self.runtime_binding,
            ProjectRuntimeBinding,
        ):
            raise ValueError(
                "invalid_project_runtime_binding_type:"
                f"{type(self.runtime_binding).__name__}"
            )
        if not isinstance(self.memberships, tuple):
            raise ValueError("memberships_must_be_tuple")

        project_id = self.project.project_id
        if self.policy is not None and self.policy.project_id != project_id:
            raise ValueError(
                "project_policy_project_id_mismatch:"
                f"{self.policy.project_id}!={project_id}"
            )
        if self.chat_binding is not None and self.chat_binding.project_id != project_id:
            raise ValueError(
                "project_chat_binding_project_id_mismatch:"
                f"{self.chat_binding.project_id}!={project_id}"
            )
        if (
            self.runtime_binding is not None
            and self.runtime_binding.project_id != project_id
        ):
            raise ValueError(
                "project_runtime_binding_project_id_mismatch:"
                f"{self.runtime_binding.project_id}!={project_id}"
            )

        normalized_memberships: list[ProjectMembership] = []
        member_ids: set[str] = set()
        for membership in self.memberships:
            if not isinstance(membership, ProjectMembership):
                raise ValueError(
                    "invalid_project_membership_type:"
                    f"{type(membership).__name__}"
                )
            if membership.project_id != project_id:
                raise ValueError(
                    "project_membership_project_id_mismatch:"
                    f"{membership.member_id}:{membership.project_id}!={project_id}"
                )
            if membership.member_id in member_ids:
                raise ValueError(
                    f"duplicate_project_member_id:{membership.member_id}"
                )
            member_ids.add(membership.member_id)
            normalized_memberships.append(membership)

        object.__setattr__(
            self,
            "memberships",
            tuple(
                sorted(
                    normalized_memberships,
                    key=lambda membership: membership.member_id,
                )
            ),
        )


class ProjectRegistry:
    def __init__(self, state_db: StateDB) -> None:
        if not isinstance(state_db, StateDB):
            raise ValueError(
                f"invalid_state_db_type:{type(state_db).__name__}"
            )
        self._state_db = state_db

    @property
    def state_db(self) -> StateDB:
        return self._state_db

    def register_project(self, snapshot: ProjectSnapshot) -> None:
        if not isinstance(snapshot, ProjectSnapshot):
            raise ValueError(
                f"invalid_project_snapshot_type:{type(snapshot).__name__}"
            )

        def _write(conn) -> None:
            if self._state_db._project_exists_conn(
                conn,
                snapshot.project.project_id,
            ):
                raise ValueError(
                    f"project_already_exists:{snapshot.project.project_id}"
                )
            self._state_db._upsert_project_conn(conn, snapshot.project)
            if snapshot.policy is not None:
                self._state_db._set_project_policy_conn(conn, snapshot.policy)
            for membership in snapshot.memberships:
                self._state_db._upsert_project_membership_conn(conn, membership)
            if snapshot.chat_binding is not None:
                self._state_db._bind_project_chat_conn(conn, snapshot.chat_binding)
            if snapshot.runtime_binding is not None:
                self._state_db._upsert_project_runtime_binding_conn(
                    conn,
                    snapshot.runtime_binding,
                )

        self._state_db._run_write_transaction(_write)

    def get_project_snapshot(self, project_id: str) -> ProjectSnapshot | None:
        project = self._state_db.get_project(project_id)
        if project is None:
            return None
        return self._build_snapshot(project)

    def get_project_snapshot_by_slug(self, slug: str) -> ProjectSnapshot | None:
        project = self._state_db.get_project_by_slug(slug)
        if project is None:
            return None
        return self._build_snapshot(project)

    def get_project_snapshot_for_chat(
        self,
        chat_provider: str,
        chat_id: int,
    ) -> ProjectSnapshot | None:
        binding = self._state_db.get_project_for_chat(chat_provider, chat_id)
        if binding is None:
            return None
        project = self._state_db.get_project(binding.project_id)
        if project is None:
            raise ValueError(
                f"orphaned_project_chat_binding:{binding.project_id}"
            )
        return self._build_snapshot(project)

    def list_projects(self) -> list[Project]:
        return self._state_db.list_projects()

    def list_project_snapshots(self) -> list[ProjectSnapshot]:
        return [
            self._build_snapshot(project)
            for project in self.list_projects()
        ]

    def list_project_task_history(
        self,
        project_id: str,
        limit: int = 20,
    ) -> list[TaskSummary]:
        return self._state_db.list_project_tasks(project_id, limit=limit)

    def set_project_policy(self, policy: ProjectPolicy) -> None:
        self._state_db.set_project_policy(policy)

    def upsert_project_membership(
        self,
        membership: ProjectMembership,
    ) -> None:
        self._state_db.upsert_project_membership(membership)

    def bind_project_chat(self, binding: ProjectChatBinding) -> None:
        self._state_db.bind_project_chat(binding)

    def get_project_specialist_roster(
        self,
        project_id: str,
    ) -> ProjectSpecialistRoster:
        return self._state_db.get_project_specialist_roster(project_id)

    def add_project_specialist(
        self,
        project_id: str,
        specialist_role: str,
    ) -> ProjectSpecialistRoster:
        self._state_db.add_project_specialist(project_id, specialist_role)
        return self._state_db.get_project_specialist_roster(project_id)

    def remove_project_specialist(
        self,
        project_id: str,
        specialist_role: str,
    ) -> ProjectSpecialistRoster:
        self._state_db.remove_project_specialist(project_id, specialist_role)
        return self._state_db.get_project_specialist_roster(project_id)

    def create_pending_hire_request(
        self,
        request: PendingHireRequest,
    ) -> PendingHireRequest:
        return self._state_db.create_hire_request(request)

    def get_hire_request(
        self,
        request_id: str,
    ) -> PendingHireRequest | None:
        return self._state_db.get_hire_request(request_id)

    def list_pending_hire_requests(
        self,
        project_id: str,
    ) -> tuple[PendingHireRequest, ...]:
        return self._state_db.list_pending_hire_requests(project_id)

    def approve_hire_request(
        self,
        request_id: str,
        actor_user_id: int,
    ) -> PendingHireRequest:
        return self._state_db.mark_hire_request_approved(
            request_id,
            actor_user_id,
        )

    def reject_hire_request(
        self,
        request_id: str,
        actor_user_id: int,
    ) -> PendingHireRequest:
        return self._state_db.mark_hire_request_rejected(
            request_id,
            actor_user_id,
        )

    def set_project_runtime_binding(
        self,
        binding: ProjectRuntimeBinding,
    ) -> None:
        self._state_db.upsert_project_runtime_binding(binding)

    def get_project_runtime_binding(
        self,
        project_id: str,
    ) -> ProjectRuntimeBinding | None:
        return self._state_db.get_project_runtime_binding(project_id)

    def _build_snapshot(self, project: Project) -> ProjectSnapshot:
        if not isinstance(project, Project):
            raise ValueError(
                f"invalid_project_type:{type(project).__name__}"
            )
        return ProjectSnapshot(
            project=project,
            policy=self._state_db.get_project_policy(project.project_id),
            memberships=tuple(
                self._state_db.list_project_memberships(project.project_id)
            ),
            chat_binding=self._state_db.get_project_chat_binding(project.project_id),
            runtime_binding=self._state_db.get_project_runtime_binding(
                project.project_id
            ),
        )
