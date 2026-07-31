"""Tests for modules.commands.ha_command."""

import asyncio
import configparser
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import requests

from modules.commands.ha_command import HaCommand
from tests.conftest import mock_message

_ADMIN_KEY = "a" * 64
_OTHER_KEY = "b" * 64


def _make_bot(
    *,
    enabled: bool = True,
    url: str = "http://ha.local:8123",
    token: str = "tok",
    entities: str = "sensor.temp, light.kitchen",
    admin_only: bool = True,
):
    bot = MagicMock()
    bot.logger = Mock()

    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    config.add_section("Keywords")
    config.add_section("Home_Assistant")
    config.set("Home_Assistant", "enabled", "true" if enabled else "false")
    config.set("Home_Assistant", "url", url)
    config.set("Home_Assistant", "token", token)
    config.set("Home_Assistant", "entities", entities)
    config.set("Home_Assistant", "admin_only", "true" if admin_only else "false")
    config.add_section("Admin_ACL")
    config.set("Admin_ACL", "admin_pubkeys", _ADMIN_KEY)
    config.set("Admin_ACL", "admin_commands", "")
    bot.config = config

    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.translator.get_value = Mock(return_value=None)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    bot.command_manager.send_response = AsyncMock(return_value=True)
    return bot


def _run(coro):
    return asyncio.run(coro)


def _sent_text(bot):
    """Return the response string passed to send_response."""
    return bot.command_manager.send_response.call_args[0][1]


def _make_resp(state, friendly=None, unit=None, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.ok = (200 <= status < 300)
    data = {"state": state, "attributes": {}}
    if friendly:
        data["attributes"]["friendly_name"] = friendly
    if unit:
        data["attributes"]["unit_of_measurement"] = unit
    resp.json = Mock(return_value=data)
    return resp


# ---------------------------------------------------------------------------
# can_execute — enabled/disabled and admin gating
# ---------------------------------------------------------------------------

class TestHaCommandCanExecute:
    def test_admin_dm_allowed(self):
        cmd = HaCommand(_make_bot())
        msg = mock_message(content="ha get", is_dm=True, sender_id="u1", sender_pubkey=_ADMIN_KEY)
        assert cmd.can_execute(msg) is True

    def test_disabled_blocks(self):
        cmd = HaCommand(_make_bot(enabled=False))
        msg = mock_message(content="ha get", is_dm=True, sender_id="u1", sender_pubkey=_ADMIN_KEY)
        assert cmd.can_execute(msg) is False

    def test_non_admin_blocked(self):
        cmd = HaCommand(_make_bot())
        msg = mock_message(content="ha get", is_dm=True, sender_id="u2", sender_pubkey=_OTHER_KEY)
        assert cmd.can_execute(msg) is False

    def test_admin_channel_allowed(self):
        cmd = HaCommand(_make_bot())
        msg = mock_message(content="ha get", is_dm=False, channel="general", sender_id="u1", sender_pubkey=_ADMIN_KEY)
        assert cmd.can_execute(msg) is True

    def test_non_admin_allowed_when_admin_only_false(self):
        cmd = HaCommand(_make_bot(admin_only=False))
        msg = mock_message(content="ha get", is_dm=True, sender_id="u2", sender_pubkey=_OTHER_KEY)
        assert cmd.can_execute(msg) is True

    def test_non_admin_blocked_when_admin_only_true(self):
        cmd = HaCommand(_make_bot(admin_only=True))
        msg = mock_message(content="ha get", is_dm=True, sender_id="u2", sender_pubkey=_OTHER_KEY)
        assert cmd.can_execute(msg) is False


# ---------------------------------------------------------------------------
# execute — bad subcommand / missing entities
# ---------------------------------------------------------------------------

class TestHaCommandBadInput:
    def test_wrong_subcommand_shows_usage(self):
        bot = _make_bot()
        cmd = HaCommand(bot)
        msg = mock_message(content="ha foo", is_dm=True, sender_pubkey=_ADMIN_KEY)
        _run(cmd.execute(msg))
        assert "Usage:" in _sent_text(bot)

    def test_bare_ha_shows_usage(self):
        bot = _make_bot()
        cmd = HaCommand(bot)
        msg = mock_message(content="ha", is_dm=True, sender_pubkey=_ADMIN_KEY)
        _run(cmd.execute(msg))
        assert "Usage:" in _sent_text(bot)

    def test_no_entities_configured(self):
        bot = _make_bot(entities="")
        cmd = HaCommand(bot)
        msg = mock_message(content="ha get", is_dm=True, sender_pubkey=_ADMIN_KEY)
        _run(cmd.execute(msg))
        assert "No HA entities configured" in _sent_text(bot)


# ---------------------------------------------------------------------------
# execute — successful fetches
# ---------------------------------------------------------------------------

class TestHaCommandSuccessfulFetch:
    def test_single_entity_with_unit(self):
        bot = _make_bot(entities="sensor.temp")
        cmd = HaCommand(bot)
        msg = mock_message(content="ha get", is_dm=True, sender_pubkey=_ADMIN_KEY)
        with patch("modules.commands.ha_command.requests.get",
                   return_value=_make_resp("23.5", friendly="Living Room Temp", unit="°C")):
            _run(cmd.execute(msg))
        assert _sent_text(bot) == "Living Room Temp: 23.5 °C"

    def test_single_entity_without_unit(self):
        bot = _make_bot(entities="light.kitchen")
        cmd = HaCommand(bot)
        msg = mock_message(content="ha get", is_dm=True, sender_pubkey=_ADMIN_KEY)
        with patch("modules.commands.ha_command.requests.get",
                   return_value=_make_resp("on", friendly="Kitchen Light")):
            _run(cmd.execute(msg))
        assert _sent_text(bot) == "Kitchen Light: on"

    def test_entity_falls_back_to_id_when_no_friendly_name(self):
        bot = _make_bot(entities="sensor.mystery")
        cmd = HaCommand(bot)
        msg = mock_message(content="ha get", is_dm=True, sender_pubkey=_ADMIN_KEY)
        with patch("modules.commands.ha_command.requests.get",
                   return_value=_make_resp("42")):
            _run(cmd.execute(msg))
        assert _sent_text(bot) == "sensor.mystery: 42"

    def test_multiple_entities_joined_as_single_message(self):
        bot = _make_bot(entities="sensor.temp, light.kitchen")
        cmd = HaCommand(bot)
        msg = mock_message(content="ha get", is_dm=True, sender_pubkey=_ADMIN_KEY)
        responses = [
            _make_resp("21.0", friendly="Temp", unit="°C"),
            _make_resp("off", friendly="Kitchen"),
        ]
        with patch("modules.commands.ha_command.requests.get", side_effect=responses):
            _run(cmd.execute(msg))
        # Both entities are short enough to fit in one DM chunk
        bot.command_manager.send_response.assert_called_once()
        text = _sent_text(bot)
        assert "Temp: 21.0 °C" in text
        assert "Kitchen: off" in text

    def test_long_response_split_into_multiple_chunks(self):
        # 10 entities whose lines together exceed 158 bytes
        entities = ",".join(f"sensor.e{i}" for i in range(10))
        bot = _make_bot(entities=entities)
        cmd = HaCommand(bot)
        msg = mock_message(content="ha get", is_dm=True, sender_pubkey=_ADMIN_KEY)
        # Each line: "Entity N Name: on" — short, but 10 together will exceed 158 bytes
        side_effects = [_make_resp("on", friendly=f"Entity {i} Name") for i in range(10)]
        with patch("modules.commands.ha_command.requests.get", side_effect=side_effects):
            _run(cmd.execute(msg))
        bot.command_manager.send_response_chunked.assert_called_once()
        all_lines = "\n".join(bot.command_manager.send_response_chunked.call_args[0][1])
        for i in range(10):
            assert f"Entity {i} Name: on" in all_lines

    def test_explicit_entity_id_overrides_configured_list(self):
        bot = _make_bot(entities="sensor.temp, light.kitchen")
        cmd = HaCommand(bot)
        msg = mock_message(content="ha get binary_sensor.door", is_dm=True, sender_pubkey=_ADMIN_KEY)
        with patch("modules.commands.ha_command.requests.get",
                   return_value=_make_resp("on", friendly="Front Door")) as mock_get:
            _run(cmd.execute(msg))
        # Only one request, for the explicit entity
        mock_get.assert_called_once()
        assert "binary_sensor.door" in mock_get.call_args[0][0]
        assert _sent_text(bot) == "Front Door: on"


# ---------------------------------------------------------------------------
# execute — error paths
# ---------------------------------------------------------------------------

class TestHaCommandErrors:
    def test_connection_error(self):
        bot = _make_bot()
        cmd = HaCommand(bot)
        msg = mock_message(content="ha get sensor.temp", is_dm=True, sender_pubkey=_ADMIN_KEY)
        with patch("modules.commands.ha_command.requests.get",
                   side_effect=requests.exceptions.ConnectionError):
            _run(cmd.execute(msg))
        assert _sent_text(bot) == "HA unavailable"

    def test_timeout(self):
        bot = _make_bot()
        cmd = HaCommand(bot)
        msg = mock_message(content="ha get sensor.temp", is_dm=True, sender_pubkey=_ADMIN_KEY)
        with patch("modules.commands.ha_command.requests.get",
                   side_effect=requests.exceptions.Timeout):
            _run(cmd.execute(msg))
        assert _sent_text(bot) == "HA timeout"

    def test_auth_failed(self):
        bot = _make_bot()
        cmd = HaCommand(bot)
        msg = mock_message(content="ha get sensor.temp", is_dm=True, sender_pubkey=_ADMIN_KEY)
        with patch("modules.commands.ha_command.requests.get",
                   return_value=_make_resp("", status=401)):
            _run(cmd.execute(msg))
        assert _sent_text(bot) == "HA auth failed"

    def test_entity_not_found(self):
        bot = _make_bot()
        cmd = HaCommand(bot)
        msg = mock_message(content="ha get sensor.ghost", is_dm=True, sender_pubkey=_ADMIN_KEY)
        with patch("modules.commands.ha_command.requests.get",
                   return_value=_make_resp("", status=404)):
            _run(cmd.execute(msg))
        assert _sent_text(bot) == "sensor.ghost: not found"

    def test_other_http_error(self):
        bot = _make_bot()
        cmd = HaCommand(bot)
        msg = mock_message(content="ha get sensor.temp", is_dm=True, sender_pubkey=_ADMIN_KEY)
        with patch("modules.commands.ha_command.requests.get",
                   return_value=_make_resp("", status=503)):
            _run(cmd.execute(msg))
        assert _sent_text(bot) == "HA error 503"

    def test_bad_json_response(self):
        bot = _make_bot()
        cmd = HaCommand(bot)
        msg = mock_message(content="ha get sensor.temp", is_dm=True, sender_pubkey=_ADMIN_KEY)
        resp = MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.json = Mock(side_effect=ValueError("bad json"))
        with patch("modules.commands.ha_command.requests.get", return_value=resp):
            _run(cmd.execute(msg))
        assert _sent_text(bot) == "HA bad response"

    def test_not_configured_when_url_empty(self):
        bot = _make_bot(url="", entities="sensor.temp")
        cmd = HaCommand(bot)
        msg = mock_message(content="ha get sensor.temp", is_dm=True, sender_pubkey=_ADMIN_KEY)
        _run(cmd.execute(msg))
        assert _sent_text(bot) == "HA not configured"

    def test_not_configured_when_token_empty(self):
        bot = _make_bot(token="", entities="sensor.temp")
        cmd = HaCommand(bot)
        msg = mock_message(content="ha get sensor.temp", is_dm=True, sender_pubkey=_ADMIN_KEY)
        _run(cmd.execute(msg))
        assert _sent_text(bot) == "HA not configured"
