from __future__ import annotations

from .models import (
    ChatRecord,
    InspectedChat,
    InspectedRequest,
    InspectedTunnel,
    RecoveryHint,
    RequestState,
    RequestStatus,
    SessionInspection,
)
from .store import RequestStore, SessionStore


def recovery_hint_for(error: str | None) -> RecoveryHint | None:
    if not error:
        return None
    if "ContextTooLargeError" in error or "context budget" in error:
        return RecoveryHint(
            code="context-too-large",
            message="Reduce required P0 context, narrow include/focus, split the request, or retry with a compact planning profile.",
        )
    if "no worker registered" in error or "tunnel" in error.lower():
        return RecoveryHint(
            code="tunnel-unavailable",
            message="Probe tunnels and verify the browser extension worker or selected tunnel is connected.",
        )
    if "conversation_id" in error or "conversation" in error.lower():
        return RecoveryHint(
            code="conversation-binding",
            message="Inspect the request status and final response; final conversation binding may not have completed.",
        )
    return RecoveryHint(
        code="inspect-request-status",
        message="Inspect the request status and saved prompt/response files for the authoritative failure evidence.",
    )


class SessionInspectionService:
    """Read-only application service for session dashboard state."""

    def __init__(self, sessions: SessionStore, requests: RequestStore) -> None:
        self.sessions = sessions
        self.requests = requests

    def inspect_session(
        self,
        session_id: str,
        *,
        include_archived: bool = True,
        tunnels: list[InspectedTunnel] | None = None,
    ) -> SessionInspection:
        session = self.sessions.get_session(session_id)
        chats = self.sessions.list_chats(session_id, include_archived=include_archived)
        inspected = [self._inspect_chat(chat, session.active_chat_id) for chat in chats]
        return SessionInspection(
            session_id=session.session_id,
            title=session.title,
            repo_root=session.repo_root,
            active_chat_id=session.active_chat_id,
            created_at=session.created_at,
            updated_at=session.updated_at,
            closed_at=session.closed_at,
            state="closed" if session.closed_at else "open",
            chat_count=len(inspected),
            request_count=sum(chat.request_count for chat in inspected),
            chats=inspected,
            tunnels=tunnels or self._unknown_tunnels(inspected),
        )

    def _inspect_chat(self, chat: ChatRecord, active_chat_id: str | None) -> InspectedChat:
        latest = self._latest_request(chat)
        return InspectedChat(
            chat_id=chat.chat_id,
            title=chat.title,
            kind=chat.kind,
            is_active=chat.chat_id == active_chat_id,
            is_archived=chat.archived_at is not None,
            conversation_id=chat.conversation_id,
            conversation_binding_state=self._conversation_binding_state(chat, latest),
            tunnel_id=chat.tunnel_id or (latest.tunnel_id if latest else None),
            request_count=len(chat.request_ids),
            latest_request=latest,
        )

    def _latest_request(self, chat: ChatRecord) -> InspectedRequest | None:
        statuses: list[RequestStatus] = []
        for request_id in chat.request_ids:
            try:
                statuses.append(self.requests.load_status(request_id))
            except FileNotFoundError:
                continue
        if not statuses:
            return None
        latest = sorted(statuses, key=lambda item: (item.created_at, item.updated_at, item.request_id))[-1]
        return InspectedRequest(
            request_id=latest.request_id,
            mode=latest.mode,
            route_kind=latest.route_kind,
            route_name=latest.route_name,
            skill=latest.skill,
            chat_policy=latest.chat_policy,
            state=latest.state,
            created_at=latest.created_at,
            updated_at=latest.updated_at,
            provider=latest.provider,
            tunnel_id=latest.tunnel_id,
            conversation_id=latest.conversation_id,
            has_partial_text=bool(latest.partial_text),
            partial_text=latest.partial_text,
            partial_text_updated_at=latest.partial_text_updated_at,
            error=latest.error,
            recovery_hint=recovery_hint_for(latest.error) if latest.state == RequestState.FAILED else None,
        )

    @staticmethod
    def _conversation_binding_state(chat: ChatRecord, latest: InspectedRequest | None) -> str:
        if chat.conversation_id:
            return "bound"
        if latest and latest.state in {RequestState.RUNNING_FINAL, RequestState.WAITING_FINAL}:
            return "pending"
        if latest and latest.chat_policy and latest.chat_policy.value == "temporary":
            return "unavailable"
        return "unbound"

    @staticmethod
    def _unknown_tunnels(chats: list[InspectedChat]) -> list[InspectedTunnel]:
        ids = sorted({chat.tunnel_id for chat in chats if chat.tunnel_id})
        return [InspectedTunnel(tunnel_id=tunnel_id) for tunnel_id in ids]
