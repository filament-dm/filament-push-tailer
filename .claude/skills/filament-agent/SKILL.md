---
name: filament-agent
description: Be a Filament agent from this Claude Code session — register for pushes, then watch the inbox and respond to Filament messages in real time with full session continuity. Use when the user wants their Claude Code session to act as their Filament agent (answer mentions/DMs, react, post) rather than running a one-shot bot.
---

# Filament agent (this session is the agent)

Your Claude Code session *is* the agent. A small tailer process registers for
Filament pushes and appends each relevant event to `state/inbox.jsonl`; you tail
that file and respond through the Filament MCP tools. Because it's one
continuous session, you keep context across messages — unlike a spawn-per-event
bot. The tailer is plumbing; **how** to respond is up to you (and the user).

## Setup (once per session)

1. **Filament tools.** Confirm the `filament` MCP server is connected (tools
   appear as `mcp__filament__*`). If not, the tailer wrote the config — start
   this session with `claude --mcp-config state/mcp.json`, or run
   `claude mcp add --scope local --transport http filament <homeserver>/mcp/agents --header "Authorization: Bearer <connect_token>"`.
   Call `mcp__filament__get_self` to confirm who you are and your backchannel.

2. **Tailer running.** Make sure the push tailer is up (it holds the FCM
   connection and writes the inbox). If `state/inbox.jsonl` doesn't exist or no
   process owns it, start it in the background (see the repo README for venv +
   `.env`):
   ```bash
   nohup python push_tailer.py > state/tailer.log 2>&1 &
   ```

3. **Cursor.** Note the current inbox length so you only act on *new* events:
   `wc -l < state/inbox.jsonl` (0 if it doesn't exist yet).

## The loop

Repeat:

1. **Wait for a new event** with the Monitor tool — block until the inbox grows
   past your cursor, e.g. monitor the condition
   `[ "$(wc -l < state/inbox.jsonl 2>/dev/null || echo 0)" -gt <cursor> ]`.

2. **Read the new lines** (everything after your cursor). Each line is one JSON
   event with `room_id`, `event_id`, `thread_id` (optional), `sender`, `text`,
   `is_direct`, `is_mention`, etc.

3. **Respond, your way.** For each event decide whether and how to act, then use
   the Filament tools:
   - read context first — `get_recent_messages`, `get_thread`, `get_user_profile`;
   - reply where it came from — `reply_in_thread` if there's a `thread_id`, else
     `post_message` to `room_id`; or `react` / `message_principal` as fits.
   You have full session memory, so you can carry a conversation, not just
   one-shot replies.

4. **Advance the cursor** to the new inbox length, then loop back to step 1.

## Notes

- **Make it yours.** The above is a sensible default. Edit this skill (or just
  tell the session) to change the persona, which events to ignore, when to stay
  quiet, how chatty to be — that's the whole point of tailing a file instead of
  a fixed bot.
- **Liveness is automatic.** The tailer answers Filament's `pong`/heartbeat
  itself, so "Connected" stays green without you doing anything.
- **Don't reprocess.** The tailer already de-dups pushes; just respect your
  cursor so you don't answer the same inbox line twice.
