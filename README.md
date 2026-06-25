# filament-push-tailer

**Register your Claude Code session for Filament pushes, then tail them.**

A small process you run wherever your agent lives. It holds a persistent FCM
connection and, on each relevant push from Filament, appends the event to
`state/inbox.jsonl`. That's it — it doesn't spawn anything or decide how to
respond. **Your own Claude Code session tails the inbox and handles each event
however you like**, with full session continuity (the bundled
`filament-agent` skill drives that loop via the Monitor tool).

Why this shape: your live session *is* the agent. It keeps context across
messages — not a fresh, contextless `claude -p` per push. The tailer is
plumbing; the behaviour is yours, and lives in the skill (so updating it is a
`git push`, not an app change).

It rides the same FCM / `DirectPusher` transport as the Hermes gateway
(`hermes-filament-fcm`), talking to the agent surface on one bearer token:

- **MCP tools** — `POST {homeserver}/mcp/agents` (JSON-RPC `tools/call`). Used
  here for `get_self` / `register_push_token`, and by your session (same
  `filament` server) for reading + posting.
- **Pong** — `POST {homeserver}/mcp/agents/pong` `{"nonce": …}`.
- **Heartbeat** — `POST {homeserver}/mcp/agents/heartbeat` (no body).

`pong` and `heartbeat` are deliberately *not* MCP tools — they are plumbing the
tailer handles itself, so "Connected" stays green without the LLM involved.

## Why FCM (not `/sync`)

- **No MAS-visible-credential dependency.** Authenticates with the `fmcp_`
  connect token via `register_push_token` — no MAS-introspectable Matrix client
  token needed.
- **Unifies the transport** with Hermes (both ride FCM / `DirectPusher`).
- **NAT-friendly** — FCM is an outbound connection.

Tradeoff vs `/sync` presence: we gain the stronger **ping/pong** signal — the
tailer answers `io.filament.ping` pushes with `pong`, so the app's "Connected"
state reflects a real synapse → FCM → here → `pong` round trip
(`push_round_trip_ok`).

## Run

Prereqs: Python 3.10+, and the [Claude Code CLI](https://claude.com/claude-code)
installed and logged in.

**1. Start the tailer** (registers for pushes, writes the inbox):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in FILAMENT_CONNECT_TOKEN + FILAMENT_HOMESERVER
python push_tailer.py
```

On start it identifies itself (`get_self`), registers for FCM and
`register_push_token`, writes `state/mcp.json` (the Filament MCP config for your
session), starts a presence heartbeat, then listens — `io.filament.ping` →
`pong`, relevant message → a line appended to `state/inbox.jsonl`.

(A venv keeps `firebase-messaging` off your system Python. Use a stable Python —
3.12 or 3.13; the free-threaded `3.14t` build lacks wheels for some deps.)

**2. Be the agent** — in another terminal, start a Claude Code session here
with the Filament tools, and invoke the skill:

```bash
claude --mcp-config state/mcp.json
# then: use the `filament-agent` skill
```

The skill (`.claude/skills/filament-agent`) has your session watch
`state/inbox.jsonl` (Monitor tool), read each new event, and respond through the
`mcp__filament__*` tools — keeping context across messages. Edit the skill to
change persona, what to ignore, how chatty to be: that's the point of tailing a
file instead of running a fixed bot.

## The inbox

`state/inbox.jsonl` is append-only, one JSON event per line (`room_id`,
`event_id`, `thread_id?`, `sender`, `text`, `is_direct`, `is_mention`, …, plus
`received_ms`). The tailer de-dups pushes; your session just respects its own
cursor (last line processed). Nothing decided — the raw event, for you to act on.

## Provenance

The FCM intake + push parsing + token registration is genericized from
`lord-gnomington/scripts/fcm_listen.py`. The earlier design spawned a
contextless `claude -p` per event (`scripts/watch.py`'s `fire_trigger`); this
replaces that with tail-an-inbox so a single continuous session can be the
agent. Tracked by ENG-155.
