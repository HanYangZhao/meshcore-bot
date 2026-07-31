# Spec: Home Assistant Command (`ha get`)

## Problem Statement

Bot operators who run Home Assistant alongside their MeshCore node have no way to check
device and sensor states from the mesh. When they are off-site or in a situation where their
phone has no data but the mesh is reachable, they cannot query whether a door is locked, a
sensor is tripped, or a light is on without a separate connection to their home network. They
want a single, short command from the mesh that returns live entity states from their local
Home Assistant instance.

## Solution

A new `ha` command is added to meshcore-bot. Administrators send `ha get` to receive the
current state of all configured entities, or `ha get <entity_id>` to query a single entity.
The bot calls the Home Assistant REST API with a configured bearer token and returns one
compact line per entity in the format `friendly_name: state unit`. Access is restricted to
admin pubkeys. The entity list is configured in `config.ini`.

## User Stories

1. As a bot operator, I want to enable a `ha get` command in config.ini, so that I can query
   my Home Assistant without running additional software.
2. As a bot operator, I want the command disabled by default, so that operators without Home
   Assistant are not affected.
3. As a bot operator, I want to set the Home Assistant base URL in config.ini, so that the
   bot knows where to reach my HA instance.
4. As a bot operator, I want to store my Long-Lived Access Token in config.ini, so that the
   bot can authenticate to the HA REST API.
5. As a bot operator, I want to configure a list of entity IDs in config.ini, so that `ha get`
   with no argument returns only the entities I care about.
6. As a bot operator, I want `ha get` to be restricted to admin pubkeys, so that my home
   occupancy and security state is not exposed to arbitrary mesh users.
7. As a bot operator, I want the command available from both DMs and monitored channels, so
   that I can use it wherever I am on the mesh.
8. As a bot operator, I want a 10-second per-user cooldown on the command, so that the HA
   instance is not hammered by repeated mesh requests.
9. As an admin mesh user, I want to type `ha get` and receive the state of all configured
   entities, so that I can get a home-status summary at a glance.
10. As an admin mesh user, I want to type `ha get <entity_id>` and receive the state of that
    single entity, so that I can query a specific device without retrieving everything.
11. As an admin mesh user, I want each entity returned on its own line in the format
    `friendly_name: state unit`, so that the response is easy to scan on a small mesh display.
12. As an admin mesh user, I want multiple entities to be returned in a single chunked message
    rather than one message per entity, so that the channel is not spammed.
13. As an admin mesh user, I want to receive `HA unavailable` when the HA instance cannot be
    reached, so that I know the failure is a connectivity issue.
14. As an admin mesh user, I want to receive `HA timeout` when the request takes too long, so
    that I know the bot attempted the call.
15. As an admin mesh user, I want to receive `HA auth failed` when the bearer token is rejected,
    so that I know to update my token in config.ini.
16. As an admin mesh user, I want to receive `<entity_id>: not found` when I query an entity
    that does not exist in HA, so that I can correct the entity ID.
17. As an admin mesh user, I want to receive `HA error <code>` for any other HTTP error, so
    that I have enough information to diagnose the problem.
18. As an admin mesh user, I want responses auto-chunked to fit mesh packet limits, so that
    long entity lists are delivered completely without truncation.

## Implementation Decisions

### New module: `modules/commands/ha_command.py`

A standard `BaseCommand` subclass. `requires_admin_access()` unconditionally returns `True`,
bypassing the `Admin_ACL.admin_commands` list — admin gating is structural, not config-driven,
because exposing HA state to non-admins is always a security risk.

### Config section: `[Home_Assistant]`

Four keys:

```
[Home_Assistant]
enabled = false
url = http://homeassistant.local:8123
token =
entities = sensor.living_room_temp, light.kitchen
```

`entities` is a comma-separated list of HA entity IDs. When `ha get` is called with no
argument, all listed entities are queried. When called with an argument, only that entity is
queried regardless of the configured list.

### REST API contract

- Endpoint: `GET /api/states/<entity_id>` on the configured HA base URL.
- Auth: `Authorization: Bearer <token>` header.
- Response shape used: `data["state"]`, `data["attributes"]["friendly_name"]`,
  `data["attributes"]["unit_of_measurement"]`.
- Timeout: 5 seconds per request.

### Response format

One line per entity: `friendly_name: state unit` (unit omitted when absent).  
All entity lines joined with `\n` and sent as a single response; the bot's existing chunker
handles splitting at word boundaries.

### Error strings (short, mesh-safe)

| Condition | Response |
|-----------|----------|
| `ConnectionError` | `HA unavailable` |
| `Timeout` | `HA timeout` |
| HTTP 401 | `HA auth failed` |
| HTTP 404 | `<entity_id>: not found` |
| Other HTTP error | `HA error <code>` |
| JSON decode failure | `HA bad response` |
| `url`/`token` empty | `HA not configured` |
| No entities configured and no argument | `No HA entities configured` |

### Cooldown

10-second per-user cooldown enforced via `BaseCommand.check_cooldown()` (inherited behaviour).

### No new seams

The command is auto-discovered by the existing plugin loader. No changes to the loader,
core bot, or database are required.

## Testing Decisions

Good tests assert only the externally visible behaviour of `HaCommand.execute()` — the string
returned to the mesh — not the internal HTTP call structure.

**What makes a good test:**
- Mock `requests.get` at the boundary (not internal methods).
- Assert the exact response string sent to the mesh.
- Cover the full error-string matrix above.
- Cover both the "all entities" and "single entity" argument paths.
- Cover the admin-gating path (non-admin receives no response / `can_execute` returns False).

**Module under test:** `modules/commands/ha_command.py`

**Prior art:** `tests/test_status_command.py` for the admin-ACL gating pattern;
`tests/` for the general `BaseCommand` mock-bot fixture pattern used across all command tests.

## Out of Scope

- **Writing entity states** (`ha set`) — read-only integration only.
- **Subscribing to HA events** or push-based state change notifications.
- **WebSocket / long-lived HA connection** — each command invocation is a fresh HTTP request.
- **Automations or scripts** — only `GET /api/states/<entity_id>` is used.
- **Non-admin access** — no `public = true` opt-in flag in this iteration.
- **Caching entity states** between requests.
- **Config hot-reload** — config changes require a bot restart, consistent with all other commands.

## Further Notes

The bearer token in `config.ini` is sensitive. Operators should ensure `config.ini` has
restricted file permissions (`chmod 600`) and is not committed to version control. This is
consistent with the existing guidance for the `[Bot]` section's secrets. A note to this
effect should be added to the configuration documentation when this feature ships.
