# Session and chat architecture

FancyGPT separates a work session, a provider conversation, a request run,
and an individual model turn. This keeps planning out of the user's durable
discussion while allowing one session to own several intentional discussions.

## Layers

```text
Adapters             MCP tools / CLI commands
                         |
Application          ReviewEngine + ConversationManager
                         |
Domain               RawRequest, SessionRecord, ChatRecord, ChatPolicy
                         |
Ports                SessionRepository protocol
                         |
Infrastructure       RequestStore + filesystem SessionStore
                         |
Provider boundary    AutomaticModelProvider / browser tunnel
```

The application layer depends on `SessionRepository`, not filesystem details.
MCP and CLI adapters contain no chat-selection rules. Browser code receives
only a resolved `temporary`, `persistent`, or `continue` instruction.

## Ownership model

```text
Session 1 --- * Chat 1 --- * RequestRun 1 --- 2 Turn
```

- A session belongs to exactly one canonical `repo_root`.
- A session may have one active chat and any number of inactive or independent chats.
- A chat owns the external ChatGPT `conversation_id` and its ordered request ids.
- A request records the session/chat selected when the run began.
- Planner turns are always temporary and never bind a provider conversation.
- Final turns use the resolved chat. The provider conversation id is persisted
  before final JSON parsing and report validation.

## Policies

| Policy | Behavior | Changes active chat |
|---|---|---|
| `continue` | Continue the selected or active chat; create main chat if absent | No |
| `new_chat` | Create a durable chat for the request | Yes |
| `independent` | Create an isolated durable review chat | No |
| `temporary` | Create neither session nor chat | No |

When no `session_id` is supplied, persistent requests use a deterministic
default session scoped to the canonical repository path. `create_session`
provides explicit isolation. Supplying a session for another repository fails
closed.

## Storage

```text
<workdir>/
  requests/<request_id>/status.json
  sessions/<session_id>/session.json
  sessions/<session_id>/chats/<chat_id>.json
```

All records use atomic replacement and advisory locks. Final execution is
serialized per chat, allowing planners and unrelated chats to progress without
interleaving messages in one provider conversation.

## Failure and recovery

- A failed parser or report validator does not discard the provider conversation.
- Request status retains `session_id`, `chat_id`, `chat_policy`, and any observed
  `conversation_id`.
- Session and chat records survive MCP server restarts.
- An archived chat cannot be selected; the active chat must be changed before
  it can be archived.
- Closing a session prevents it from being resolved for new requests but does
  not delete local history or remote ChatGPT conversations.

## Compatibility

Legacy `conversation_mode: temporary` maps to `chat_policy: temporary`.
Legacy explicit `conversation_id` is bound to the resolved chat. Persistent
requests without new fields join the repository's default session.
