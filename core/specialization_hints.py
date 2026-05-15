from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.agent_role_catalog import SPECIALIST_ROLE_ORDER

_SPECIALIST_ORDER_INDEX = {
    role: index for index, role in enumerate(SPECIALIST_ROLE_ORDER)
}


def _normalize_specialist_role(role: str) -> str:
    if not isinstance(role, str) or not role.strip():
        raise ValueError("empty_specialist_role")
    normalized = role.strip().lower()
    if normalized not in _SPECIALIST_ORDER_INDEX:
        raise ValueError(f"unknown_specialist_role:{normalized}")
    return normalized


def _normalize_reason(reason: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("empty_specialization_reason")
    return reason.strip()


@dataclass(frozen=True)
class SpecializationHint:
    specialist_role: str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "specialist_role",
            _normalize_specialist_role(self.specialist_role),
        )
        object.__setattr__(
            self,
            "reason",
            _normalize_reason(self.reason),
        )


@dataclass(frozen=True)
class SpecializationHints:
    items: tuple[SpecializationHint, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple):
            raise ValueError("specialization_hints_items_must_be_tuple")
        normalized_items: list[SpecializationHint] = []
        seen_roles: set[str] = set()
        for item in self.items:
            if not isinstance(item, SpecializationHint):
                raise ValueError(
                    "invalid_specialization_hint_type:"
                    f"{type(item).__name__}"
                )
            if item.specialist_role in seen_roles:
                raise ValueError(
                    "duplicate_specialization_hint:"
                    f"{item.specialist_role}"
                )
            seen_roles.add(item.specialist_role)
            normalized_items.append(item)
        normalized_items.sort(
            key=lambda item: _SPECIALIST_ORDER_INDEX[item.specialist_role]
        )
        object.__setattr__(self, "items", tuple(normalized_items))

    @property
    def is_empty(self) -> bool:
        return not self.items

    @classmethod
    def empty(cls) -> SpecializationHints:
        return cls(())

    @classmethod
    def from_pm_payload(cls, payload: Any) -> SpecializationHints:
        if not isinstance(payload, dict):
            raise ValueError(
                "invalid_pm_payload_type:"
                f"{type(payload).__name__}"
            )
        if "specialization_hints" not in payload:
            raise ValueError("missing_specialization_hints")
        raw_items = payload["specialization_hints"]
        if not isinstance(raw_items, list):
            raise ValueError("specialization_hints_payload_must_be_list")
        return cls(
            tuple(
                SpecializationHint(
                    specialist_role=item.get("specialist_role"),
                    reason=item.get("reason"),
                )
                for item in raw_items
            )
        )

    def to_pm_payload(self) -> list[dict[str, str]]:
        return [
            {
                "specialist_role": item.specialist_role,
                "reason": item.reason,
            }
            for item in self.items
        ]
