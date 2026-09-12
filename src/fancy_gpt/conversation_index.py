from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .project_models import is_chatgpt_conversation
from .project_service import ProjectService
from .project_store import ProjectStore
from .store import SessionStore


class BoundConversation(BaseModel):
    """One ChatGPT thread this runtime created or joined."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    url: str
    origin: str                       # "session" or "project"
    owner: str                        # session id or project id
    label: str                        # chat title, or role/thread for a project
    thread_key: str | None = None
    tunnel_id: str | None = None
    turns: int = 0
    last_used_at: str | None = None


class ConversationIndex:
    """Every ChatGPT thread reachable from local state, in one place.

    The runtime never queries chatgpt.com for a conversation list -- it holds no
    account credentials and calls no private endpoint. What it can account for
    precisely is every thread it opened itself, because each one was recorded
    when the browser reported its id back.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def _from_sessions(self) -> list[BoundConversation]:
        store = SessionStore(self.root)
        found: list[BoundConversation] = []
        for session in store.list_sessions():
            for chat in store.list_chats(session.session_id, include_archived=True):
                if not chat.conversation_id:
                    continue
                found.append(BoundConversation(
                    conversation_id=chat.conversation_id,
                    url=f"https://chatgpt.com/c/{chat.conversation_id}",
                    origin="session",
                    owner=session.session_id,
                    label=chat.title + (" (archived)" if chat.archived_at else ""),
                    tunnel_id=chat.tunnel_id,
                    turns=len(chat.request_ids),
                    last_used_at=chat.updated_at,
                ))
        return found

    def _from_projects(self) -> list[BoundConversation]:
        service = ProjectService(self.root)
        found: dict[str, BoundConversation] = {}
        for project_id in ProjectStore(self.root).list_project_ids():
            for session in service.snapshot(project_id).sessions:
                binding = session.conversation_binding
                if not binding or not is_chatgpt_conversation(binding):
                    continue
                # Several work items can share one thread; count them as turns
                # on a single conversation rather than listing it repeatedly.
                existing = found.get(binding)
                if existing:
                    found[binding] = existing.model_copy(update={
                        "turns": existing.turns + 1,
                        "last_used_at": max(
                            filter(None, [existing.last_used_at, session.ended_at, session.started_at]),
                            default=existing.last_used_at,
                        ),
                    })
                    continue
                found[binding] = BoundConversation(
                    conversation_id=binding,
                    url=f"https://chatgpt.com/c/{binding}",
                    origin="project",
                    owner=project_id,
                    label=session.role.value,
                    thread_key=session.conversation_key,
                    turns=1,
                    last_used_at=session.ended_at or session.started_at,
                )
        return list(found.values())

    def all(self) -> list[BoundConversation]:
        items = self._from_sessions() + self._from_projects()
        items.sort(key=lambda item: (item.last_used_at or "", item.conversation_id), reverse=True)
        return items
