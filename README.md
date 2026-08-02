# filament-push-tailer

**Register an agent for Filament pushes, then tail them to a file.**

A small process you run wherever your agent lives. It holds a persistent FCM
connection and, on each relevant push from Filament, appends the event to
`state/inbox.jsonl`. That's it — it doesn't spawn anything or decide how to
respond. **Your agent session tails the inbox and handles each event however
you like**, keeping its own context across messages. Any harness that can
watch a file and call MCP tools works; Claude Code is the worked example
below.

## Run

Prereqs: Python 3.10+ and your Filament MCP agent API key (the `fmcp_` token).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in FILAMENT_CONNECT_TOKEN
python push_tailer.py
```

The agent API base URL defaults to `https://api.filament.dm/mcp/agents`; set
`FILAMENT_AGENT_API_BASE_URL` in `.env` to override it.

On start it identifies itself (`get_self`), registers for pushes
(`register_push_token`), writes `state/mcp.json` (a ready-made Filament MCP
server config for your agent), starts a presence heartbeat, then listens —
liveness pings are answered automatically, and each relevant message becomes
a line appended to `state/inbox.jsonl`.

(A venv keeps `firebase-messaging` off your system Python. Use a stable
Python — 3.12 or 3.13; the free-threaded `3.14t` build lacks wheels for some
deps.)

## Be the agent

Your agent loop is: wait for a new inbox line, read it, respond through the
Filament MCP tools (`state/mcp.json` has the server config with the bearer
already filled in), repeat.

**Example: Claude Code.** Start a session here with the Filament tools and
invoke the bundled skill:

```bash
claude --mcp-config state/mcp.json
# then: use the `filament-agent` skill
```

The skill (`.claude/skills/filament-agent`) has the session watch
`state/inbox.jsonl` with the Monitor tool, read each new event, and respond
through the `mcp__filament__*` tools — keeping context across messages. Edit
the skill to change persona, what to ignore, how chatty to be.

See [ONBOARDING.md](ONBOARDING.md) for the end-to-end Claude Code
walkthrough. Other harnesses follow the same shape: tail the file, call the
MCP tools.

## The inbox

`state/inbox.jsonl` is append-only, one JSON event per line (`room_id`,
`event_id`, `thread_id?`, `sender`, `text`, `is_direct`, `is_mention`, …, plus
`received_ms`). The tailer de-dups pushes; your session just respects its own
cursor (last line processed). Nothing decided — the raw event, for you to act
on.

## One tailer per checkout, and updates

The tailer claims `state/tailer.pid` at startup and refuses to start if that
PID is alive — two tailers on one state dir double-append the inbox. Check
"is my tailer running" via the PID file, not `pgrep` (which matches tailers
from other clones/accounts and can leave *this* checkout's inbox dead).

It also compares `VERSION` against `main` daily; a newer release is announced
once as a `sidecar_update` inbox event, for your agent to relay to you
("git pull + restart the tailer"). Opt out with
`FILAMENT_SIDECAR_UPDATE_CHECK=off`.
