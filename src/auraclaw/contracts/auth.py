from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSessionAuthRequest:
    """Inbound AgentSession proof; sensitive values are only used before event append.

    `handoff_code` and `access_token` must never be written to Session Events,
    Runtime checkpoints or Tool results. They exist here so the Session service can
    claim or resolve a Java AgentSession, then persist only the safe binding below.
    A bare `agent_session_id` is not proof and must not create a new binding.
    """

    agent_session_id: str | None = None
    conversation_id: str | None = None
    handoff_code: str | None = None
    access_token: str | None = None

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.agent_session_id,
                self.conversation_id,
                self.handoff_code,
                self.access_token,
            )
        )


@dataclass(frozen=True)
class AgentSessionBinding:
    """Safe AgentSession handle that may cross Python service boundaries."""

    agent_session_id: str
    conversation_id: str
    resolved_by: str = "agent_session_id"

    def as_event_payload(self) -> dict[str, str]:
        return {
            "agent_session_id": self.agent_session_id,
            "conversation_id": self.conversation_id,
            "resolved_by": self.resolved_by,
        }

    @classmethod
    def from_event_payload(cls, value: object) -> AgentSessionBinding | None:
        if not isinstance(value, dict):
            return None
        agent_session_id = value.get("agent_session_id")
        conversation_id = value.get("conversation_id")
        if not agent_session_id or not conversation_id:
            return None
        return cls(
            agent_session_id=str(agent_session_id),
            conversation_id=str(conversation_id),
            resolved_by=str(value.get("resolved_by", "event")),
        )
