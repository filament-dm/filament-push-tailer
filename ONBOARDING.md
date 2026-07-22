# Claude Code as your Filament agent — validated walkthrough

The exact path, as followed end-to-end on 2026-07-22 against `api.filament.dm`
(prod). Two one-time setup phases, then a single command starts everything.

**What you end up with:** your live Claude Code session *is* your Filament
agent — it answers DMs and mentions with full session memory. A small
background process (the tailer) holds the push connection and feeds events to
a file your session watches. No gateway daemon, no Hermes install.

## One-time setup

### 0. Prerequisites

- Claude Code installed and logged in
- Python 3.12 or 3.13 (**not** 3.14t — missing wheels for the FCM dependency)
- A Filament account

### 1. Get your connect token — do NOT run the command the app shows

In the Filament app, run the agent connect flow. It will show a Hermes
install one-liner:

```
curl -fsSL https://raw.githubusercontent.com/filament-dm/filament-hermes/main/install.sh | CONNECT_TOKEN=fmcp_… bash
```

**Don't run it** — that installs the Hermes runtime, a different product.
Copy the `fmcp_…` value out of the `CONNECT_TOKEN=` part. That token is the
credential; the rest is Hermes packaging. (Verified: the token works here
directly — no Hermes install or extra finalize step needed.)

### 2. Configure the sidecar

```bash
git clone https://github.com/filament-dm/filament-push-tailer
cd filament-push-tailer
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```
FILAMENT_CONNECT_TOKEN=fmcp_…            # from step 1
FILAMENT_HOMESERVER=https://api.filament.dm
```

(Dev cluster instead? Also set the four `FILAMENT_FIREBASE_*` values to the
dev project's — see `.env.example` — or pushes will be rejected.)

### 3. Register the MCP server (so plain `claude` has the tools)

```bash
set -a; source .env; set +a     # keeps the token out of shell history
claude mcp add --scope local --transport http <agent-name> \
  "$FILAMENT_HOMESERVER/mcp/agents" \
  --header "Authorization: Bearer $FILAMENT_CONNECT_TOKEN"
```

`<agent-name>` is the client-side label and becomes your tool prefix
(`mcp__<agent-name>__post_message`); match your agent's name for sanity. The
registration is per-directory (`--scope local`, stored privately in
`~/.claude.json`) — sessions started in this directory get the tools
automatically. If you ever rotate the token (disconnect/reconnect in the
app), update `.env` **and** re-run this command.

## Every time: one command, one tab

```bash
cd filament-push-tailer && claude
```

First message to the session:

> Bootstrap yourself as my Filament agent:
> 1. If no `push_tailer.py` process is running (`pgrep -f push_tailer`),
>    start it: `source .venv/bin/activate && nohup python push_tailer.py >
>    tailer.log 2>&1 &`, then wait until `tailer.log` shows "registered with
>    Filament".
> 2. Post a short hello to my backchannel with `post_message` (get the room
>    id from `get_self`).
> 3. Then follow the filament-agent skill's loop: cursor, Monitor,
>    👀-react on pickup, respond, unreact, advance cursor.
> 4. After EVERY reply, immediately start the next Monitor wait. Never end
>    your turn without a Monitor call running.

Then verify: the last thing in the session should be a **running Monitor
call**. If it wrapped up with a summary instead, it dropped out of the loop —
say "resume the watch loop" and check again.

## Test it

Message your agent from the Filament app (DM/backchannel — no @-mention
needed there). Within a few seconds: the tailer logs the push, the session
wakes, 👀 appears on your message, the reply arrives, the 👀 clears.

## Known rough edges

- **Web client may not render the agent's reply until a hard reload**
  (ENG-591, fix in QA). The message is delivered — it's a client rendering
  wedge, and it affects Hermes agents identically.
- **The agent is only live while the session is watching.** The tailer keeps
  the "Connected" dot green on its own (it answers pings), so a closed CC
  session with a running tailer looks online but answers nothing. `pkill -f
  push_tailer` when you're done, or expect the green dot to overpromise.
- **Update reminders:** the tailer checks GitHub daily; when a newer sidecar
  ships, your agent tells you in Filament (once). The fix is `git pull` +
  restart the tailer.
