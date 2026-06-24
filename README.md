# filament agent listen (FCM wake sidecar)

**Status: runnable (ENG-155). The Claude Code connect path — the standalone
wake sidecar, split out from the earlier `claude-code-connector-filament`
TS prototype.**

The Claude Code wake path: a small sidecar you run wherever your agent
lives. It holds a persistent FCM connection and, on each relevant push
from Filament, spawns a fresh, scoped `claude -p` that reads and acts
through your agent's MCP tools, then exits. No long-lived context window;
all state materializes back in Filament.

This is the CC mirror of the Hermes gateway (`hermes-filament-fcm`). It
talks to the same agent surface on three shapes of one bearer token:

- **MCP tools** — `POST {homeserver}/mcp/agents` (JSON-RPC `tools/call`).
  Stateless, plain JSON, no `initialize` handshake. Used here for
  `get_self` / `register_push_token`, and — inside the woken `claude` —
  for reading and posting.
- **Pong** — `POST {homeserver}/mcp/agents/pong` `{"nonce": …}`.
- **Heartbeat** — `POST {homeserver}/mcp/agents/heartbeat` (no body).

`pong` and `heartbeat` are deliberately *not* MCP tools — they are harness
plumbing and never routed through the LLM.

## Why FCM (not `/sync`)

ENG-155 was originally scoped as a `/sync` long-poll receiver. FCM is
better here:

- **No MAS-visible-credential dependency.** It authenticates with the
  `fmcp_` connect token via `register_push_token` — no
  MAS-introspectable Matrix client token needed.
- **Unifies the transport** with Hermes (both ride FCM / `DirectPusher`).
- **NAT-friendly** — FCM is an outbound connection, which was the whole
  reason `/sync` was considered for behind-a-firewall agents.

Tradeoff: we give up `/sync`-native presence, but gain the stronger
**ping/pong** signal — the sidecar answers `io.filament.ping` pushes with
`pong`, so the app's "Connected" state reflects a real
synapse → FCM → here → `pong` round trip (`push_round_trip_ok`).

## Run

Prereqs: Python 3.10+, and the [Claude Code CLI](https://claude.com/claude-code)
already installed and logged in (`claude` on your PATH — the woken `claude`
reuses your login).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in FILAMENT_CONNECT_TOKEN + FILAMENT_HOMESERVER
python agent_listen.py
```

(A venv keeps `firebase-messaging` and its deps off your system Python, and
sidesteps the `pip` vs `pip3` / multi-interpreter mess on macOS. Use a
stable Python — 3.12 or 3.13; the free-threaded `3.14t` build has no wheels
for some deps.)

On start it: identifies itself (`get_self`), registers for FCM and
`register_push_token`, starts a 240s presence heartbeat, then listens.
`io.filament.ping` pushes are answered with `pong`; a relevant message
wakes a scoped `claude -p`.

### What the woken `claude` can do

Each event spawns `claude -p` with `--mcp-config state/mcp.json`
(generated on start — registers the Filament surface as MCP server
`filament`) and `--strict-mcp-config`, so the run sees *only* the Filament
tools, none of your host's personal MCP servers. `--allowed-tools` (default
`mcp__filament__*`) is what it's permitted to call; in `-p` mode anything
outside the allow-list is denied, not prompted. Give it a persona with
`FILAMENT_SYSTEM_PROMPT`.

## Provenance

Genericized from `lord-gnomington/scripts/fcm_listen.py` (FCM intake +
push parsing + token registration) and `scripts/watch.py`'s
`fire_trigger` (the `claude -p` spawn-per-event).

## Still rough

- `is_relevant()` keys off the routing flags already in the push payload;
  it wants sender-classification (ENG-218) and explicit
  `io.filament.agent_cc` backchannel identification.
- The woken `claude` gets a generic respond-in-place system prompt; the
  richer triage behaviour `watch.py` kept in `/process-push-notification`
  could move into `FILAMENT_SYSTEM_PROMPT` or a committed slash command.
- Packaging: this is what the app's CC connect flow would point users at.
