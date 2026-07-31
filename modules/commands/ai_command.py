#!/usr/bin/env python3
"""AI command — routes mesh messages to an OpenAI-compatible LLM."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

import requests
from openai import APIError, APITimeoutError, AsyncOpenAI

# Strip <think>...</think> blocks produced by reasoning models (Qwen3, DeepSeek-R1, etc.)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

from ..ai_session_store import AiSessionStore
from ..models import MeshMessage
from .base_command import BaseCommand

_DEFAULT_SYSTEM_PROMPT = (
    "You are an AI assistant on a low-bandwidth mesh network. Rules:\n"
    "- Keep replies under 390 characters. Target 1 message. Max 3.\n"
    "- No greetings, no sign-offs, no formatting\n"
    "- Be direct and factual. Fragments OK.\n"
    "- Drop articles, filler, pleasantries. Use short synonyms.\n"
    "- Abbreviate common terms (DB/auth/config/req/res/fn)\n"
    "- Use arrows for causality (X -> Y)\n"
    "- Technical terms stay exact. Code stays exact.\n"
    "- If you must explain complex things, use short bullet points."
)

_SECTION = "AI_Command"


def _split_chunks(text: str, max_len: int) -> list[str]:
    """Split text at word boundaries into chunks of at most max_len bytes (UTF-8)."""
    if not text:
        return []
    chunks: list[str] = []
    while text:
        if len(text.encode()) <= max_len:
            chunks.append(text)
            break
        # Find the largest prefix whose UTF-8 byte length fits
        cut = text
        while len(cut.encode()) > max_len:
            cut = cut[: len(cut) - 1]
        # Walk back to last word boundary
        last_space = cut.rfind(" ")
        if last_space > 0:
            chunks.append(cut[:last_space])
            text = text[len(cut[:last_space]):].lstrip(" ")
        else:
            chunks.append(cut)
            text = text[len(cut):]
    return [c for c in chunks if c]


class AiCommand(BaseCommand):
    """Query an OpenAI-compatible LLM from the mesh.

    DM conversations are stateful (per-sender history stored in SQLite).
    Channel invocations are stateless (no history read or written).
    """

    name = "ai"
    keywords = ["ai"]
    description = "Ask the AI a question (usage: ai <question>)"
    category = "utility"
    requires_internet = False

    short_description = "Ask an AI assistant a question"
    usage = "ai <question>"
    examples = ["ai what is DNS?", "ai reset", "ai reset explain TCP"]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)

        self._enabled = self.get_config_value(_SECTION, "enabled", fallback=False, value_type="bool")
        base_url: str = self.get_config_value(_SECTION, "base_url", fallback="http://localhost:11434/v1")
        api_key: str = self.get_config_value(_SECTION, "api_key", fallback="local")
        model: str = self.get_config_value(_SECTION, "model", fallback="llama3")
        timeout: int = self.get_config_value(_SECTION, "timeout", fallback=60, value_type="int")
        max_history: int = self.get_config_value(_SECTION, "max_history", fallback=20, value_type="int")
        expire_after: int = self.get_config_value(_SECTION, "expire_after", fallback=86400, value_type="int")
        self._ack: str = self.get_config_value(_SECTION, "ack_message", fallback="...")
        self._error_msg: str = self.get_config_value(
            _SECTION, "error_message", fallback="AI unavailable, try later."
        )
        custom_prompt: Optional[str] = self.get_config_value(_SECTION, "system_prompt", fallback=None)
        self._system_prompt: str = custom_prompt if custom_prompt else _DEFAULT_SYSTEM_PROMPT

        self._tool_ids: list[str] = []
        self._searxng_url: str = ""
        self._searxng_results: int = 3
        self._client: Optional[AsyncOpenAI] = None
        self._sessions: Optional[AiSessionStore] = None

        if self._enabled:
            self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=float(timeout))
            self._model = model
            enable_tools: bool = self.get_config_value(_SECTION, "enable_tools", fallback=False, value_type="bool")
            if enable_tools:
                # OWUI tool IDs — find them at GET /api/v1/tools in your Open WebUI instance
                raw_ids: str = self.get_config_value(_SECTION, "tool_ids", fallback="")
                self._tool_ids = [t.strip() for t in raw_ids.split(",") if t.strip()]
            self._searxng_url = self.get_config_value(_SECTION, "searxng_url", fallback="").rstrip("/")
            self._searxng_results = self.get_config_value(_SECTION, "searxng_results", fallback=3, value_type="int")
            db_path = str(self.bot.db_manager.db_path)
            self._sessions = AiSessionStore(db_path, max_history=max_history, expire_after=expire_after)
            # Clean up stale sessions from previous runs
            try:
                pruned = self._sessions.prune_expired()
                if pruned:
                    self.logger.debug("AI: pruned %d expired session rows", pruned)
            except Exception as e:
                self.logger.warning("AI: session prune failed: %s", e)

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self._enabled:
            return False
        return super().can_execute(message, skip_channel_check=skip_channel_check)

    async def execute(self, message: MeshMessage) -> bool:
        # message.content has already had the global command prefix stripped by cleanup_message_for_matching
        content = message.content.strip()

        # Strip the matched keyword from the front to get the body
        body = content
        for kw in self.keywords:
            if content.lower().startswith(kw.lower()):
                body = content[len(kw):].lstrip()
                break

        # --- reset sub-command ---
        if body.lower() == "reset" or body.lower().startswith("reset "):
            query = body[5:].lstrip() if body.lower().startswith("reset ") else ""
            if message.is_dm and message.sender_pubkey and self._sessions:
                self._sessions.reset(message.sender_pubkey)
            if not query:
                return await self.send_response(message, "History cleared.")
            # reset + query: fall through with cleared history and the remainder as the query
            body = query

        if not body:
            return await self.send_response(message, "Usage: ai <question>")

        # Acknowledge immediately so the user knows we're working on it
        await self.send_response(message, self._ack, skip_user_rate_limit=True)

        # Build LLM message list
        history: list[dict] = []
        if message.is_dm and message.sender_pubkey and self._sessions:
            history = self._sessions.get(message.sender_pubkey)

        llm_messages = [{"role": "system", "content": self._system_prompt}]
        llm_messages.extend(history)

        # Prepend SearXNG results as context if configured
        user_content = body
        if self._searxng_url:
            search_context = await self._search_web(body)
            if search_context:
                user_content = f"{search_context}\n\nUsing the search results above, answer: {body}"

        llm_messages.append({"role": "user", "content": user_content})

        # Call OWUI — tool execution (SearXNG etc.) is handled server-side via tool_ids
        try:
            create_kwargs: dict = {
                "model": self._model,
                "messages": llm_messages,
                "temperature": 0.3,
                "max_tokens": 4096,
            }
            if self._tool_ids:
                create_kwargs["extra_body"] = {"tool_ids": self._tool_ids}
            resp = await self._client.chat.completions.create(**create_kwargs)
            resp_text = _THINK_RE.sub("", resp.choices[0].message.content or "").strip()
        except (APIError, APITimeoutError, Exception) as e:
            self.logger.error("AI command error for %s: %s", message.sender_id, e)
            return await self.send_response(message, self._error_msg, skip_user_rate_limit=True)

        # Persist the exchange for DM sessions only
        if message.is_dm and message.sender_pubkey and self._sessions:
            try:
                self._sessions.append(message.sender_pubkey, "user", body)
                self._sessions.append(message.sender_pubkey, "assistant", resp_text)
            except Exception as e:
                self.logger.warning("AI: failed to persist session for %s: %s", message.sender_id, e)

        # Send response, chunked to fit mesh packet limits
        max_len = self.get_max_message_length(message)
        chunks = _split_chunks(resp_text, max_len)
        if not chunks:
            return True
        return await self.send_response_chunked(message, chunks)

    async def _search_web(self, query: str) -> str:
        """Query SearXNG and return top results formatted for LLM context injection."""
        try:
            resp = await asyncio.to_thread(
                requests.get,
                f"{self._searxng_url}/search",
                params={"q": query, "format": "json"},
                timeout=5,
            )
            if not resp.ok:
                self.logger.warning("AI: SearXNG returned %s", resp.status_code)
                return ""
            results = resp.json().get("results", "")[:self._searxng_results]
            if not results:
                return ""
            lines = []
            for i, r in enumerate(results, 1):
                title = r.get("title", "")
                snippet = (r.get("content") or "")[:200].strip()
                lines.append(f"{i}. {title}: {snippet}")
            return "Web search results:\n" + "\n".join(lines)
        except Exception as e:
            self.logger.warning("AI: SearXNG search failed: %s", e)
            return ""
