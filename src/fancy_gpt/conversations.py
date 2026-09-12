from __future__ import annotations

from .models import ChatKind, ChatPolicy, ChatRecord, ChatResolution, RawRequest, SessionRecord
from .session_repository import SessionRepository


class ConversationManager:
    """Resolve user-facing session/chat intent into a concrete final-turn target."""

    def __init__(self, store: SessionRepository) -> None:
        self.store = store

    def create_session(self, repo_root: str, title: str | None = None) -> SessionRecord:
        return self.store.create_session(repo_root=repo_root, title=title)

    def create_chat(
        self, session_id: str, title: str, *, independent: bool = False, make_active: bool = True
    ) -> ChatRecord:
        return self.store.create_chat(
            session_id,
            title=title,
            kind=ChatKind.INDEPENDENT if independent else ChatKind.STANDARD,
            make_active=make_active and not independent,
        )

    def resolve_focused(
        self, session_id: str, *, title: str, site: str | None = None
    ) -> ChatResolution:
        """Resolve a one-pass focused turn into a persistent session chat."""
        session = self.store.get_session(session_id)
        # Reuse the repository and lifecycle validation shared with review runs.
        self.store.ensure_session(repo_root=session.repo_root, session_id=session_id)
        if session.active_chat_id:
            chat = self.store.get_chat(session_id, session.active_chat_id)
            if chat.archived_at:
                raise ValueError(f"chat is archived: {chat.chat_id}")
            if site and chat.site != site:
                raise ValueError(f"chat belongs to site {chat.site}; request targets {site}")
        else:
            chat = self.store.get_or_create_main_chat(session_id, site=site or "chatgpt")
        return ChatResolution(
            policy=ChatPolicy.CONTINUE,
            site=chat.site,
            session_id=session_id,
            chat_id=chat.chat_id,
            conversation_id=chat.conversation_id,
        )

    def resolve(self, request: RawRequest) -> ChatResolution:
        policy = request.chat_policy
        # Preserve the legacy temporary mode as an explicit opt-out from all
        # session persistence. Persistent legacy requests join the repo's
        # default session unless a session is supplied.
        if request.conversation_mode == "temporary" or policy == ChatPolicy.TEMPORARY:
            return ChatResolution(policy=ChatPolicy.TEMPORARY, site=request.site)

        session = self.store.ensure_session(repo_root=request.repo_root, session_id=request.session_id)
        chat: ChatRecord
        if request.chat_id:
            chat = self.store.get_chat(session.session_id, request.chat_id)
            if chat.archived_at:
                raise ValueError(f"chat is archived: {chat.chat_id}")
        elif policy == ChatPolicy.NEW_CHAT:
            chat = self.store.create_chat(
                session.session_id,
                title=request.objective,
                kind=ChatKind.STANDARD,
                make_active=True,
                site=request.site,
            )
        elif policy == ChatPolicy.INDEPENDENT:
            chat = self.store.create_chat(
                session.session_id,
                title=request.objective,
                kind=ChatKind.INDEPENDENT,
                make_active=False,
                site=request.site,
            )
        else:
            chat = self.store.get_or_create_main_chat(session.session_id, site=request.site)

        if chat.site != request.site:
            raise ValueError(f"chat belongs to site {chat.site}; request targets {request.site}")

        if request.conversation_id:
            if chat.conversation_id and chat.conversation_id != request.conversation_id:
                raise ValueError("explicit conversation_id conflicts with the selected chat")
            if not chat.conversation_id:
                chat = self.store.update_chat(
                    session.session_id,
                    chat.chat_id,
                    conversation_id=request.conversation_id,
                )

        return ChatResolution(
            policy=policy,
            site=request.site,
            session_id=session.session_id,
            chat_id=chat.chat_id,
            conversation_id=chat.conversation_id,
        )

    def attach_request(self, resolution: ChatResolution, request_id: str) -> None:
        if not resolution.session_id or not resolution.chat_id:
            return
        self.store.append_request(resolution.session_id, resolution.chat_id, request_id)

    def bind_conversation(self, resolution: ChatResolution, conversation_id: str | None, tunnel_id: str | None) -> None:
        if not conversation_id or not resolution.session_id or not resolution.chat_id:
            return
        chat = self.store.get_chat(resolution.session_id, resolution.chat_id)
        if chat.conversation_id and chat.conversation_id != conversation_id:
            raise ValueError("provider returned a different conversation for the selected chat")
        self.store.update_chat(
            resolution.session_id,
            resolution.chat_id,
            conversation_id=conversation_id,
            tunnel_id=tunnel_id or chat.tunnel_id,
        )
