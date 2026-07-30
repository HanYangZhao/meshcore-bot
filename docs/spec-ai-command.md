# Spec: AI Command (`ai`)

## Problem Statement

MeshCore Bot operators who also run a local LLM (Ollama, llama.cpp, LM Studio, etc.) must
currently run a completely separate process — the meshcore-llm-bridge — connected to its own
dedicated MeshCore node, just to offer AI-assisted responses on the mesh. This doubles the
hardware and operational overhead. Operators want to offer on-demand AI assistance from the
same bot node they already have running, triggered by a simple command alongside all other
bot commands.

## Solution

A new `ai` command is added to meshcore-bot. Users send `ai <question>` in a DM or a monitored
channel. The bot sends an immediate acknowledgement, calls a configurable OpenAI-compatible LLM
endpoint, and sends the response back chunked to fit mesh packet limits. DM conversations
maintain per-sender history across turns (stateful). Channel invocations are one-shot (stateless).
Users can clear their DM history with `ai reset`.

## User Stories

1. As a bot operator, I want to enable an `ai` command in my existing meshcore-bot without
   running a second process, so that I can offer AI responses with no extra hardware.
2. As a bot operator, I want to configure the LLM endpoint, API key, model, and timeout in
   `config.ini`, so that I can point the bot at any OpenAI-compatible backend (Ollama, llama.cpp,
   LM Studio).
3. As a bot operator, I want the `ai` command disabled by default, so that operators who don't
   have a local LLM are not affected.
4. As a bot operator, I want to override the default system prompt in `config.ini`, so that I can
   set a custom persona ("You are a HAM radio expert…") without touching code.
5. As a bot operator, I want the `ai` command keyword to be configurable via `aliases =` in
   `config.ini`, so that I can rename it to `llm`, `bot`, or anything else.
6. As a bot operator, I want the `ai` command to respect the standard `channels =` config key,
   so that I can restrict it to specific channels or DMs-only without custom ACL code.
7. As a bot operator, I want the AI config section to appear in `make config` (the ncurses TUI),
   so that I can configure it interactively without editing the file directly.
8. As a mesh user, I want to type `ai <question>` and receive an AI-generated response, so that
   I can get answers while off-grid.
9. As a mesh user, I want to receive a short acknowledgement immediately after sending my
   question, so that I know the bot received my message and is working on it.
10. As a mesh user sending a DM, I want the bot to remember my previous questions and answers
    within the same session, so that I can ask follow-up questions without repeating context.
11. As a mesh user on a channel, I want each `ai` invocation to be independent, so that other
    users' questions do not bleed into my responses.
12. As a mesh user, I want AI responses to fit within mesh packet limits (auto-chunked at word
    boundaries), so that I receive complete answers without truncation.
13. As a mesh user, I want to type `ai reset` in a DM to clear my conversation history, so that
    I can start a fresh session.
14. As a mesh user, I want to type `ai reset <question>` in a DM to clear history and immediately
    ask a new question, so that I can reset and query in one step.
15. As a mesh user, I want my DM conversation history to expire automatically after 24 hours of
    inactivity, so that stale context does not affect future responses.
16. As a mesh user, I want to receive a clear error message if the LLM is unavailable or times
    out, so that I know my request was not processed.
17. As a mesh user, I want AI responses to use a compressed, terse writing style by default (no
    filler, no formatting, short synonyms), so that more information fits in fewer mesh packets.
18. As a mesh user, I want the `ai` command to work from both DMs and monitored channels, so
    that I have flexibility in how I reach the AI.

## Implementation Decisions

### New module: `modules/commands/ai_command.py`

A standard `BaseCommand` subclass. Uses `AsyncOpenAI` (from the `openai` package) directly
inside `execute()`. No background queues or persistent processors — the async `execute()` method
itself awaits the LLM call, which is safe because the bot's event loop handles concurrency.

### New module: `modules/ai_session_store.py`

A thin class wrapping a new `ai_sessions` SQLite table. Responsible for:
- `get(pubkey) -> list[dict]` — load history for a sender (empty list if expired or absent)
- `append(pubkey, role, content)` — append a turn, trim to `max_history`
- `reset(pubkey)` — delete all rows for a sender
- `prune_expired()` — delete rows where `updated_at < now - expire_after`

Session expiry is checked on `get()` (lazy) and optionally pruned on bot startup.

### New SQLite table: `ai_sessions`

Added via a new migration step in `db_migrations.py`:

```
ai_sessions (
  pubkey      TEXT NOT NULL,
  role        TEXT NOT NULL,        -- 'user' or 'assistant'
  content     TEXT NOT NULL,
  updated_at  INTEGER NOT NULL,     -- Unix timestamp of the turn
  turn_order  INTEGER NOT NULL      -- monotonically increasing per pubkey
)
INDEX: (pubkey, turn_order)
```

Session key is `sender_pubkey`. Channel invocations pass `None` as pubkey — the store is never
written for channel messages.

### Statefulness rules

- `message.is_dm == True` → load history from `AiSessionStore`, append user turn, call LLM,
  append assistant turn, save.
- `message.is_dm == False` → history is an empty list; nothing is read from or written to the
  store.

### Acknowledgement

The first call inside `execute()` (after `can_execute` passes) is `send_response(message, "...")`.
This fires immediately before the LLM `await`. The ack text is configurable via `ack_message =`
in `[AI_Command]` (default: `...`).

### Reset sub-command

If the stripped message body is exactly `reset`, reply `"History cleared."` and return.
If it is `reset <text>`, clear history then proceed with `<text>` as the query.
Reset is only meaningful for DMs; in a channel it is a no-op (replies "History cleared." but
there is nothing to clear).

### System prompt

Default (caveman style, mesh-optimised):

```
You are an AI assistant on a low-bandwidth mesh network. Rules:
- Keep replies under 240 characters. Target 1 message. Max 2.
- No greetings, no sign-offs, no formatting
- Be direct and factual. Fragments OK.
- Drop articles, filler, pleasantries. Use short synonyms.
- Abbreviate common terms (DB/auth/config/req/res/fn)
- Use arrows for causality (X -> Y)
- Technical terms stay exact. Code stays exact.
- If you must explain complex things, use short bullet points.
```

Overridden entirely if `system_prompt =` is set in `[AI_Command]`.

### LLM client

`AsyncOpenAI(base_url=..., api_key=..., timeout=...)` called directly in `execute()`.
On `APIError`, `APITimeoutError`, or any other exception: log the error and send the error
message (`"AI unavailable, try later."`, configurable via `error_message =`).

### `config.ini` section: `[AI_Command]`

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable/disable the command |
| `base_url` | `http://localhost:11434/v1` | OpenAI-compatible endpoint |
| `api_key` | `local` | API key (use `local` for Ollama/llama.cpp) |
| `model` | `llama3` | Model name |
| `timeout` | `60` | LLM request timeout in seconds |
| `max_history` | `20` | Max DM turns (user + assistant) kept per sender |
| `expire_after` | `86400` | DM session expiry in seconds (default 24h) |
| `ack_message` | `...` | Sent immediately before the LLM call |
| `error_message` | `AI unavailable, try later.` | Sent on LLM failure |
| `system_prompt` | *(hardcoded default)* | Override the system prompt entirely |
| `channels` | *(all)* | Restrict to specific channels (standard override) |
| `aliases` | *(none)* | Additional trigger keywords |

### Dependency

`openai>=1.0.0` added to `pyproject.toml` as a mandatory dependency.

## Testing Decisions

A good test for this feature tests **external behaviour only**: what messages are sent in response
to what inputs. It does not assert on internal method calls, SQL query shapes, or OpenAI request
payloads beyond what is observable from the outside.

### Seam 1: `AiCommand.execute()`

Prior art: `tests/commands/test_ping_command.py`, `tests/commands/` broadly.

Tests use `command_mock_bot` fixture and an in-memory SQLite db. `AsyncOpenAI` is patched.

Scenarios to cover:

- `ai what is DNS?` in a DM → ack sent first, then LLM response sent
- `ai what is DNS?` in a channel → no history written or read, LLM called with empty history
- Second DM turn → history from first turn is included in the LLM messages list
- `ai reset` in a DM → replies "History cleared.", does not call LLM
- `ai reset explain TCP` in a DM → history cleared, LLM called with clean history
- LLM raises `APITimeoutError` → error message sent, no crash
- Command disabled in config → `can_execute` returns `False`
- `channels =` restricts to a channel the message is not from → `can_execute` returns `False`
- Response longer than one packet → chunked into multiple `send_response` calls
- DM session older than `expire_after` → treated as empty history (no stale context)

### Seam 2: `AiSessionStore`

Tested independently with `sqlite3.connect(":memory:")`. No bot mock needed.

Scenarios to cover:

- `get()` on empty db returns `[]`
- `append()` then `get()` returns the appended turn
- `append()` beyond `max_history` evicts oldest turns (FIFO)
- `reset()` clears all turns for the sender, not other senders
- `get()` after `expire_after` seconds returns `[]` (lazy expiry)
- `prune_expired()` removes only expired rows

## Out of Scope

- `/long` mode (detailed, length-uncapped responses) — not implemented
- Per-user allow-lists or ACLs beyond the standard `channels =` config key
- Streaming responses (LLM response is awaited in full before sending)
- Multi-turn history for channel invocations
- Retry on LLM failure
- Rate limiting beyond the bot's existing per-user rate limiter

## Further Notes

- The `openai` package handles `APIError`, `APITimeoutError`, and `RateLimitError` as typed
  exceptions — use these in error handling rather than bare `except Exception`.
- Session pruning (`prune_expired`) should be called once at bot startup via the migration/init
  path to avoid accumulating dead rows indefinitely.
- The existing `send_response_chunked` on `BaseCommand` handles packet splitting using
  `get_max_message_length()` — the AI command should use this rather than implementing its own
  chunker.
