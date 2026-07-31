#!/usr/bin/env python3
"""
Home Assistant command for the MeshCore Bot
Queries entity states from a Home Assistant instance via the REST API.
"""

from __future__ import annotations

from typing import Any, Optional

import requests

from ..models import MeshMessage
from .base_command import BaseCommand

_HA_TIMEOUT = 5  # seconds


class HaCommand(BaseCommand):
    """Fetch Home Assistant entity states over the local REST API."""

    name = "ha"
    keywords = ["ha"]
    description = "Get Home Assistant entity states (admin only, usage: ha get [entity_id])"
    cooldown_seconds = 10
    category = "admin"

    short_description = "Get Home Assistant entity states"
    usage = "ha get [entity_id]"
    examples = ["ha get", "ha get sensor.living_room_temp"]
    parameters = [
        {"name": "entity_id", "description": "Optional entity ID to query; omit for all configured entities"},
    ]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.ha_enabled = self.get_config_value("Home_Assistant", "enabled", fallback=False, value_type="bool")
        self.ha_url = self.get_config_value("Home_Assistant", "url", fallback="").rstrip("/")
        self.ha_token = self.get_config_value("Home_Assistant", "token", fallback="")
        self.ha_admin_only = self.get_config_value("Home_Assistant", "admin_only", fallback=True, value_type="bool")
        raw_entities = self.get_config_value("Home_Assistant", "entities", fallback="")
        self.ha_entities: list[str] = [e.strip() for e in raw_entities.split(",") if e.strip()]

    def requires_admin_access(self) -> bool:
        return self.ha_admin_only

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self.ha_enabled:
            return False
        return super().can_execute(message, skip_channel_check=skip_channel_check)

    async def execute(self, message: MeshMessage) -> bool:
        args = (message.content or "").split()
        # args[0] is the trigger keyword ("ha"); args[1] should be the subcommand
        if len(args) < 2 or args[1].lower() != "get":
            await self.send_response(message, "Usage: ha get [entity_id]")
            return True

        target: Optional[str] = args[2] if len(args) >= 3 else None

        if target:
            entity_ids = [target]
        elif self.ha_entities:
            entity_ids = self.ha_entities
        else:
            await self.send_response(message, "No HA entities configured")
            return True

        lines = []
        for entity_id in entity_ids:
            line = self._fetch_entity(entity_id)
            lines.append(line)

        chunks = self._pack_into_chunks(lines, self.get_max_message_length(message))
        if len(chunks) == 1:
            await self.send_response(message, chunks[0])
        else:
            await self.send_response_chunked(message, chunks)
        return True

    def _pack_into_chunks(self, lines: list[str], max_bytes: int) -> list[str]:
        """Pack lines into as few messages as possible without exceeding max_bytes each."""
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for line in lines:
            line_bytes = len(line.encode("utf-8"))
            # +1 for the \n separator (except first line in a chunk)
            needed = line_bytes + (1 if current else 0)
            if current and current_len + needed > max_bytes:
                chunks.append("\n".join(current))
                current = [line]
                current_len = line_bytes
            else:
                current.append(line)
                current_len += needed
        if current:
            chunks.append("\n".join(current))
        return chunks

    def _fetch_entity(self, entity_id: str) -> str:
        """Return a single formatted line for one entity, or an error string."""
        if not self.ha_url or not self.ha_token:
            return "HA not configured"

        url = f"{self.ha_url}/api/states/{entity_id}"
        headers = {
            "Authorization": f"Bearer {self.ha_token}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.get(url, headers=headers, timeout=_HA_TIMEOUT)
        except requests.exceptions.ConnectionError:
            return "HA unavailable"
        except requests.exceptions.Timeout:
            return "HA timeout"

        if resp.status_code == 401:
            return "HA auth failed"
        if resp.status_code == 404:
            return f"{entity_id}: not found"
        if not resp.ok:
            return f"HA error {resp.status_code}"

        try:
            data = resp.json()
        except ValueError:
            return "HA bad response"

        state: str = data.get("state", "unknown")
        attrs: dict = data.get("attributes", {})
        friendly = attrs.get("friendly_name") or entity_id
        unit = attrs.get("unit_of_measurement", "")

        if unit:
            return f"{friendly}: {state} {unit}"
        return f"{friendly}: {state}"
