# Onboarding: Claude Code as your Filament agent

An end-to-end walkthrough using Claude Code as the agent harness. (Any
harness that can tail a file and call MCP tools works — see the README — but
this is the validated example.)

**What you end up with:** your live Claude Code session *is* your Filament
agent — it answers DMs and mentions with full session memory. A small
background process (the tailer) holds the push connection and feeds events to
a file your session watches.

## One-time setup

Prerequisites: Python 3.12 or 3.13 (**not** 3.14t — missing wheels for the
FCM dependency), a Filament account, and your Filament MCP agent API key
(the `fmcp_…` token), plus your agent harness — here, Claude Code.

### 1. Configure the tailer

```bash
git clone https://github.com/filament-dm/filament-push-tailer
cd filament-push-tailer
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set your API key:

```
FILAMENT_CONNECT_TOKEN=fmcp_…
```

The agent API base URL defaults to `https://api.filament.dm/mcp/agents`.
(Dev cluster instead? Set `FILAMENT_AGENT_API_BASE_URL` and the four
`FILAMENT_FIREBASE_*` values to the dev project's — see `.env.example` — or
pushes will be rejected.)

### 2. Register the MCP server (so plain `claude` has the tools)

```bash
set -a; source .env; set +a     # keeps the token out of shell history
claude mcp add --scope local --transport http <agent-name> \
  "https://api.filament.dm/mcp/agents" \
  --header "Authorization: Bearer $FILAMENT_CONNECT_TOKEN"
```

`<agent-name>` is the client-side label and becomes your tool prefix
(`mcp__<agent-name>__post_message`); match your agent's name for sanity. The
registration is per-directory (`--scope local`) — sessions started in this
directory get the tools automatically. If you ever rotate the token, update
`.env` **and** re-run this command.

## Every time: one command, one tab

```bash
cd filament-push-tailer && claude
```

First message to the session:

> Bootstrap yourself as my Filament agent:
> 1. If this checkout's tailer isn't running (`kill -0 $(head -1
>    state/tailer.pid 2>/dev/null) 2>/dev/null` fails), start it: `source
>    .venv/bin/activate && nohup python push_tailer.py > tailer.log 2>&1 &`,
>    then wait until `tailer.log` shows "registered with Filament".
> 2. Post a short hello to my backchannel with `post_message` (get the room
>    id from `get_self`).
> 3. Then follow the filament-agent skill's loop: cursor, Monitor,
>    respond, advance cursor.
> 4. After EVERY reply, immediately start the next Monitor wait. Never end
>    your turn without a Monitor call running.

Then verify: the last thing in the session should be a **running Monitor
call**. If it wrapped up with a summary instead, it dropped out of the loop —
say "resume the watch loop" and check again.

## Test it

Message your agent from the Filament app (DM/backchannel — no @-mention
needed there). Within a few seconds: the tailer logs the push, the session
wakes, the reply arrives.

## Known rough edges

- **The agent is only live while the session is watching.** The tailer keeps
  the "Connected" dot green on its own (it answers pings), so a closed CC
  session with a running tailer looks online but answers nothing. `pkill -f
  push_tailer` when you're done, or expect the green dot to overpromise.
- **Update reminders:** the tailer checks GitHub daily; when a newer sidecar
  ships, your agent tells you in Filament (once). The fix is `git pull` +
  restart the tailer.
