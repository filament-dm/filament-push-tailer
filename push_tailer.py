#!/usr/bin/env python3
"""filament push tailer — register for Filament pushes, tail them to a file.

Run this wherever your agent lives. It holds a persistent FCM connection and,
on each relevant push from Filament, appends the event to ``state/inbox.jsonl``.
It does NOT spawn anything or decide how to respond — your agent session
(Claude Code with the bundled skill, or any other harness that can tail a file
and call MCP tools) reads the inbox and handles each event however you like.
The tailer is dumb plumbing; the behaviour lives in your session.

Pipeline:  Firebase register -> FCM token -> register_push_token (MCP) ->
           heartbeat loop (keeps "Connected" green) ->
           listen -> per push: ping->pong, or relevant message->append to inbox.

The Filament agent surface has three shapes, all on one bearer token and one
base URL (default https://api.filament.dm/mcp/agents):
  * MCP tools          POST {base}  (JSON-RPC tools/call). Stateless, plain
                       JSON, no initialize handshake. Used here for
                       register_push_token / get_self; your session uses the
                       same server (state/mcp.json) to read + post.
  * Pong side-channel  POST {base}/pong   {"nonce": ...}.
  * Heartbeat          POST {base}/heartbeat   (no body).
pong + heartbeat are deliberately NOT MCP tools — they are harness plumbing
and never routed through the LLM.

Config (env, or a .env beside this file; env wins):
  FILAMENT_CONNECT_TOKEN       your Filament MCP agent API key (the fmcp_
                               token; REQUIRED) — the agent's MCP bearer.
                               `python push_tailer.py login` gets one by
                               OAuth device sign-in and writes it here.
  FILAMENT_AGENT_API_BASE_URL  default https://api.filament.dm/mcp/agents;
                               override for other clusters.
  FILAMENT_FIREBASE_*          Firebase project config (public; defaults
                               baked in — same values as the other Filament
                               clients).

Commands:
  python push_tailer.py                    run the tailer
  python push_tailer.py login [--no-wait | --resume] [--base-url URL]
                                           sign in by OAuth device flow
  python push_tailer.py mcp-add NAME [--run]
                                           the `claude mcp add` command for
                                           this token, without showing it

Dependency:  pip install firebase-messaging
State:  state/fcm-credentials.json (FCM creds), state/seen.json (dedup),
        state/wake_policy.json (which emoji reactions wake the agent),
        state/mcp.json (the Filament MCP server config for your session),
        state/inbox.jsonl (the append-only event feed your session tails),
        state/tailer.pid (this checkout's tailer — one per state dir),
        state/update_notice.json (which sidecar version was announced),
        state/device_login.json + .lock + login.log (a pending sign-in).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import device_login

ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(ROOT, ".env")
STATE_DIR = os.path.join(ROOT, "state")
CREDS = os.path.join(STATE_DIR, "fcm-credentials.json")
SEEN = os.path.join(STATE_DIR, "seen.json")
MCP_CONFIG = os.path.join(STATE_DIR, "mcp.json")
INBOX = os.path.join(STATE_DIR, "inbox.jsonl")
WAKE_POLICY = os.path.join(STATE_DIR, "wake_policy.json")

# Filament's Firebase project — public config (same values the Electron /
# mobile clients and the hermes-filament-fcm adapter use). Override via env.
FIREBASE_DEFAULTS = {
    "project_id": "filament-8ce44",
    "app_id": "1:143821144946:web:90e517a7f36aa42a6093eb",
    "api_key": "AIzaSyBtYzzP3IRpmIZ57dp1PMS4Y8RPjTB0snk",
    "sender_id": "143821144946",
}

# Heartbeat cadence. Any authenticated traffic marks the agent's Matrix
# presence online and decays when we stop, so this is what makes the
# principal's status dot reflect "this sidecar is actually up".
HEARTBEAT_SECONDS = 240

# Update check (mirrors hermes-filament-fcm's update_check): compare the local
# VERSION file against main on GitHub, once shortly after startup and then
# daily. A newer version is announced as a synthetic INBOX event — the inbox is
# already the one channel the session watches, so the update notice reaches the
# principal the same way any message does: the session relays it in Filament.
# Never load-bearing: any failure is swallowed; the tailer's job is pushes.
VERSION_FILE = os.path.join(ROOT, "VERSION")
REMOTE_VERSION_URL = (
    "https://raw.githubusercontent.com/filament-dm/filament-push-tailer/main/VERSION"
)
UPDATE_NOTICE = os.path.join(STATE_DIR, "update_notice.json")
UPDATE_CHECK_FIRST_S = 60
UPDATE_CHECK_INTERVAL_S = 86400

# Eyeballs ack. The agent session keeps SESSION_ALIVE freshly touched while it
# is watching the inbox; while that file is fresh, the tailer posts the 👀
# processing marker the moment a relevant message lands, instead of waiting for
# the session to wake (several seconds of model latency). The session still
# replies and clears the marker. Without a live session the tailer stays
# silent, so 👀 keeps meaning "an agent is actually looking", not merely "the
# push reached this machine".
SESSION_ALIVE = os.path.join(STATE_DIR, "session-alive")
SESSION_ALIVE_MAX_AGE_S = 20

# One tailer per checkout. The PID file is how "is MY tailer running?" is
# answered — a name match (pgrep -f) sees tailers from other clones/accounts
# and lets a bootstrap skip starting the one that feeds THIS state dir,
# leaving the agent silently deaf.
PID_FILE = os.path.join(STATE_DIR, "tailer.pid")


def env(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and not val:
        sys.exit(f"agent_listen: missing required env {key}")
    return val or ""


def _load_dotenv() -> None:
    """Load a .env beside this file. Real env vars always win."""
    try:
        lines = open(ENV_FILE).read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


# Whether the token comes from the real environment, where it beats .env — so
# `login` can warn that the token it just wrote to .env won't be used.
_TOKEN_FROM_ENV = bool(os.environ.get("FILAMENT_CONNECT_TOKEN"))
_load_dotenv()

DEFAULT_API_BASE_URL = "https://api.filament.dm/mcp/agents"
# Not required at import: `login` runs precisely when there is no token yet.
# The run checks for it in main() and says how to get one.
CONNECT_TOKEN = env("FILAMENT_CONNECT_TOKEN")
API_BASE_URL = env("FILAMENT_AGENT_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/")


def token_missing(token: str) -> bool:
    """No token, or the .env.example placeholder copied over unfilled."""
    return not token or token in ("fmcp_...", "fmcp_…")


MISSING_TOKEN_HINT = """\
agent_listen: no FILAMENT_CONNECT_TOKEN — this tailer isn't signed in to Filament yet.

  Sign in (no token to paste):  python push_tailer.py login
  It prints a link; the owner opens it, approves, and the token is saved to .env.
  An agent that can't wait in the foreground: python push_tailer.py login --no-wait
"""


def _firebase(key: str) -> str:
    return env(f"FILAMENT_FIREBASE_{key.upper()}") or FIREBASE_DEFAULTS[key]


# ---------------------------------------------------------------- Filament client

class FilamentError(Exception):
    """A non-2xx HTTP status or a JSON-RPC error from the agent surface."""


class FilamentClient:
    """Bearer-authenticated client for the agent surface (stdlib only).

    ``base`` is the agent API base URL (MCP endpoint); pong and heartbeat
    hang off it as ``{base}/pong`` and ``{base}/heartbeat``. The API key is
    an ``fmcp_`` bearer scoped to this agent. The MCP endpoint is stateless
    and returns plain JSON, so no ``initialize`` handshake or session header
    is needed — every request stands alone.
    """

    def __init__(self, base: str, token: str) -> None:
        self._base = base
        self._token = token
        self._id = 0

    def _post(self, path: str, payload: dict | None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else b""
        req = urllib.request.Request(
            f"{self._base}{path}",
            data=data,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode() or "{}"
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            raise FilamentError(f"{path} -> HTTP {e.code}: {body}") from e
        return json.loads(raw) if raw.strip() else {}

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Invoke an MCP tool and return its (unwrapped) result dict.

        The endpoint wraps tool output as
        ``{"result": {"content": [{"type": "text", "text": "<json>"}]}}``;
        a tool/transport failure comes back as a JSON-RPC ``error``.
        """
        self._id += 1
        resp = self._post(
            "",
            {
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        if "error" in resp:
            raise FilamentError(f"{name}: {resp['error']}")
        content = (resp.get("result") or {}).get("content") or []
        for block in content:
            if block.get("type") == "text":
                try:
                    return json.loads(block["text"])
                except (ValueError, KeyError):
                    return {"text": block.get("text", "")}
        return {}

    def register_push_token(self, token: str) -> dict:
        return self.call_tool("register_push_token", {"token": token})

    def push_tokens(self) -> set[str]:
        """Pushkeys registered for this agent; empty if they can't be read,
        which falls back to registering."""
        try:
            tokens = self.call_tool("list_push_tokens", {}).get("tokens") or []
        except FilamentError as e:
            print(f"agent_listen: list_push_tokens failed ({e}); registering anyway")
            return set()
        return {t.get("pushkey") for t in tokens}

    def get_self(self) -> dict:
        return self.call_tool("get_self", {})

    def pong(self, nonce: str | None) -> None:
        # Side-channel REST endpoint — proves synapse -> FCM -> here -> back,
        # which is what flips push_round_trip_ok on the principal's detail read.
        self._post("/pong", {"nonce": nonce})

    def heartbeat(self) -> None:
        # Side-channel REST endpoint — keeps Matrix presence online.
        self._post("/heartbeat", None)


client = FilamentClient(API_BASE_URL, CONNECT_TOKEN)

# The agent's C&C backchannel room, learned from get_self at startup. Messages
# here always wake the agent (the room is not flagged is_direct in pushes).
BACKCHANNEL_ROOM_ID: str | None = None


# ---------------------------------------------------------------- dedup state

def _load_seen() -> set[str]:
    try:
        return set(json.load(open(SEEN)))
    except (OSError, ValueError):
        return set()


def _mark_seen(seen: set[str], pid: str) -> None:
    seen.add(pid)
    os.makedirs(STATE_DIR, exist_ok=True)
    # Bound the file; recent ids are all that matter for dedup.
    json.dump(list(seen)[-500:], open(SEEN, "w"))


# ---------------------------------------------------------------- push parsing

def _parse_body(data: dict) -> dict | None:
    """Pull the JSON ``PushPayload`` out of a DirectPusher FCM data message.

    Both liveness pings and room events arrive with their real payload in a
    ``body`` field (a JSON string). Returns the decoded dict, or None.
    """
    inner = data.get("data", data) if isinstance(data, dict) else {}
    body_json = inner.get("body") if isinstance(inner, dict) else None
    if not body_json:
        return None
    try:
        payload = json.loads(body_json)
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def summarize_message(payload: dict) -> dict:
    """Flatten a room-event PushPayload into a friendly summary.

    The payload carries ``room_id``, ``event_id``, ``is_direct``, and a
    ``branch`` with type/sender/channel/content.text/thread_id + routing flags.
    """
    branch = payload.get("branch") or {}
    content = branch.get("content") if isinstance(branch.get("content"), dict) else {}
    return {
        "branch_type": branch.get("type", ""),
        "room_id": payload.get("room_id", ""),
        "room_name": branch.get("channel", branch.get("sender", "")),
        "sender": branch.get("sender", ""),
        "sender_id": branch.get("sender_id", ""),
        "is_direct": payload.get("is_direct", False),
        "thread_id": branch.get("thread_id"),
        "is_mention": branch.get("is_mention_of_recipient", False),
        "is_everyone_mention": branch.get("is_everyone_mention", False),
        "is_reply_to_recipient": branch.get("is_reply_to_recipient", False),
        "is_from_self": branch.get("is_from_self", False),
        "event_id": payload.get("event_id", ""),
        "text": content.get("text") or content.get("body") or branch.get("body") or "",
    }


def is_relevant(summary: dict) -> bool:
    """Should a push wake the agent?

    Always answer in the **backchannel** (the agent's C&C room with its
    principal — matched by room id, since that room is NOT flagged
    ``is_direct`` in the push payload) or any other direct conversation;
    elsewhere only on a direct address. Never wake on the agent's own events.

    TODO: tighten with sender-classification flags (ENG-218).
    """
    if summary.get("is_from_self"):
        return False
    if BACKCHANNEL_ROOM_ID and summary.get("room_id") == BACKCHANNEL_ROOM_ID:
        return True
    if summary.get("is_direct"):  # DM with the principal
        return True
    return bool(
        summary.get("is_mention")
        or summary.get("is_everyone_mention")
        or summary.get("is_reply_to_recipient")
    )


# ---------------------------------------------------------------- reaction wake

# The marker the tailer adds to a message it is handing to the session. Never
# a wake trigger: a policy listing it would make the agent re-wake itself on
# its own ack, without end.
PROCESSING_REACTIONS = ("👀",)


def read_wake_policy() -> dict:
    """Which emoji reactions wake the agent, read fresh per push so an edit
    takes effect without a restart.

        {"trigger_emojis": ["🐞", "🐛"],
         "per_channel": {"<room_id>": {"trigger_emojis": ["🔥"]}}}

    Absent or unreadable means no triggers at all, which is what a tailer
    without a policy does: reactions wake nothing.
    """
    try:
        with open(WAKE_POLICY) as f:
            loaded = json.load(f)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def trigger_emojis(policy: dict, room_id: str) -> list:
    """The triggers for one room. A channel's own list REPLACES the global
    one rather than adding to it, so a channel can narrow as well as widen -
    the same resolution the Hermes plugin's wake policy uses."""
    per_channel = policy.get("per_channel")
    channel = per_channel.get(room_id) if isinstance(per_channel, dict) else None
    scope = channel if isinstance(channel, dict) and "trigger_emojis" in channel else policy
    emojis = scope.get("trigger_emojis")
    return emojis if isinstance(emojis, list) else []


def summarize_reaction(payload: dict) -> dict:
    """Flatten a reaction PushPayload, the way summarize_message does for a
    message. ``event_id`` is the reaction itself; ``target_event_id`` is the
    message it sits on, which is the one the session acts on."""
    branch = payload.get("branch") or {}
    return {
        "branch_type": branch.get("type", ""),
        "room_id": payload.get("room_id", ""),
        "room_name": branch.get("channel", branch.get("sender", "")),
        "sender": branch.get("sender", ""),
        "sender_id": branch.get("sender_id", ""),
        "is_direct": payload.get("is_direct", False),
        "is_from_self": branch.get("is_from_self", False),
        "thread_id": branch.get("thread_id"),
        "event_id": payload.get("event_id", ""),
        "key": branch.get("key", ""),
        "target_event_id": branch.get("target_event_id", ""),
        "removed": bool(branch.get("removed", False)),
    }


def skip_reason(summary: dict, policy: dict) -> str | None:
    """Why this reaction does NOT wake the agent, or None if it does.

    A wake needs an emoji the policy lists as a trigger for that room. Four
    things override the list: the agent's own reaction, an un-react, the
    processing marker, and the backchannel, where a reaction is the principal
    annotating a message rather than asking for a turn.

    The reason is what gets logged, so the configuration is checked FIRST:
    an emoji nobody listed did not wake for that reason, in any room. The
    four overrides are reported only when they suppressed an emoji that was
    a trigger - which makes each of them mean "your trigger was blocked
    because X", the one case where naming the override tells you something.
    """
    if summary.get("key") not in trigger_emojis(policy, summary.get("room_id", "")):
        return "not_a_trigger"
    if summary.get("is_from_self"):
        return "own_reaction"
    if summary.get("removed"):
        return "unreact"
    if summary.get("key") in PROCESSING_REACTIONS:
        return "processing_marker"
    if BACKCHANNEL_ROOM_ID and summary.get("room_id") == BACKCHANNEL_ROOM_ID:
        return "backchannel"
    return None


def reaction_wakes(summary: dict, policy: dict) -> bool:
    """Whether this reaction wakes the agent."""
    return skip_reason(summary, policy) is None


# ---------------------------------------------------------------- inbox + MCP config

def write_mcp_config() -> None:
    """Generate the Filament MCP server config for your agent session.

    Describes the Filament agent surface as an HTTP MCP server named
    ``filament``, in the standard ``mcpServers`` shape most harnesses accept
    (e.g. ``claude --mcp-config state/mcp.json``). The bearer lives only in
    this gitignored state file, never in the repo.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    config = {
        "mcpServers": {
            "filament": {
                "type": "http",
                "url": API_BASE_URL,
                "headers": {"Authorization": f"Bearer {CONNECT_TOKEN}"},
            }
        }
    }
    with open(MCP_CONFIG, "w") as f:
        json.dump(config, f, indent=2)


def _session_is_live() -> bool:
    try:
        return (time.time() - os.path.getmtime(SESSION_ALIVE)) < SESSION_ALIVE_MAX_AGE_S
    except OSError:
        return False


def _ack_eyes(event_id: str) -> bool:
    """Post the 👀 marker for a message the session is about to handle.

    Only when a live session is attached (see SESSION_ALIVE). Returns whether
    the reaction was posted, so the inbox record can carry ``acked`` and the
    session knows to skip its own react (it still unreacts after replying).
    """
    if not _session_is_live():
        return False
    try:
        client.call_tool("react", {"message_id": event_id, "key": "👀"})
        print(f"agent_listen: eyes ack on {event_id}")
        return True
    except Exception as e:  # noqa: BLE001 — the ack is best-effort
        print(f"agent_listen: eyes ack failed: {e}")
        return False


def append_to_inbox(summary: dict) -> None:
    """Append one event to the inbox feed your session tails.

    A newline-delimited JSON record per relevant push — the raw event, nothing
    decided. Your agent session watches this file (tail, or Claude Code's
    Monitor tool) and chooses whether and how to respond via the Filament MCP
    tools, keeping its own context across events. We don't spawn anything: the
    tailer is plumbing, the behaviour is yours.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    record = {"received_ms": int(time.time() * 1000), **summary}
    with open(INBOX, "a") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------- push handler

_seen: set[str] = set()


def _handle_reaction(payload: dict, persistent_id) -> None:
    """A reaction push: wake the session only if the policy says this emoji
    is a trigger here."""
    summary = summarize_reaction(payload)
    reason = skip_reason(summary, read_wake_policy())
    where = summary["room_name"] or summary["room_id"]
    print(
        f"agent_listen: reaction push - key={summary.get('key')!r} in {where!r} "
        + ("wakes=True" if reason is None else f"wakes=False ({reason})")
    )
    if reason is not None or persistent_id in _seen:
        return
    _mark_seen(_seen, persistent_id)
    print(f"agent_listen: inbox <- reaction {summary['key']} in {where!r}")
    # The marker goes on the message that was reacted to, not on the reaction:
    # that message is what the session reads and answers, and what the person
    # who reacted is watching.
    if summary.get("target_event_id"):
        summary["acked"] = _ack_eyes(summary["target_event_id"])
    try:
        append_to_inbox(summary)
    except Exception as e:  # noqa: BLE001 - one bad event must not kill the loop
        print(f"agent_listen: inbox append failed: {e}")


def on_push(data, persistent_id, _obj=None) -> None:
    """firebase-messaging callback — fires on every received push."""
    payload = _parse_body(data)
    if payload is None:
        inner = data.get("data", data) if isinstance(data, dict) else {}
        keys = list(inner) if isinstance(inner, dict) else type(inner).__name__
        print(f"agent_listen: push with no parseable body (keys={keys})")
        return

    # (a) Liveness ping -> pong, nothing else. This is what makes the
    # principal-side "Connected" real (push_round_trip_ok): it proves
    # Filament can wake us end to end. Mirrors the Hermes gateway.
    if payload.get("type") == "io.filament.ping":
        nonce = payload.get("nonce")
        try:
            client.pong(nonce)
            print(f"agent_listen: pong (nonce={nonce})")
        except Exception as e:  # noqa: BLE001 — never let pong crash the loop
            print(f"agent_listen: pong failed: {e}")
        return

    # (b) A reaction -> its own gate. Reactions carry no text, so the message
    # path below would either drop them or file empty events; whether one is
    # worth a turn is a question about the emoji, not about addressing.
    branch = payload.get("branch")
    if isinstance(branch, dict) and branch.get("type") == "reaction":
        _handle_reaction(payload, persistent_id)
        return

    # (c) Real activity -> append to the inbox your session tails.
    summary = summarize_message(payload)
    relevant = is_relevant(summary)
    print(
        f"agent_listen: message push — direct={summary.get('is_direct')} "
        f"mention={summary.get('is_mention')} reply={summary.get('is_reply_to_recipient')} "
        f"from_self={summary.get('is_from_self')} relevant={relevant} "
        f"text={(summary.get('text') or '')[:40]!r}"
    )
    if not relevant:
        return
    if persistent_id in _seen:
        return
    _mark_seen(_seen, persistent_id)
    where = summary["room_name"] or summary["room_id"]
    print(f"agent_listen: inbox <- {summary['branch_type']} in {where!r}")
    if summary.get("event_id"):
        summary["acked"] = _ack_eyes(summary["event_id"])
    try:
        append_to_inbox(summary)
    except Exception as e:  # noqa: BLE001 — one bad event must not kill the loop
        print(f"agent_listen: inbox append failed: {e}")


# ---------------------------------------------------------------- heartbeat

def _heartbeat_loop(stop: threading.Event) -> None:
    """Keep the agent's presence online while the sidecar runs."""
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            client.heartbeat()
            print("agent_listen: heartbeat")
        except Exception as e:  # noqa: BLE001
            print(f"agent_listen: heartbeat failed: {e}")


# ---------------------------------------------------------------- update check

def _parse_version(text: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(p) for p in text.strip().split("."))
    except ValueError:
        return None


def _check_for_update() -> None:
    """One comparison of local VERSION vs main; announce a newer one ONCE per
    version, as an inbox event the session will relay to the principal."""
    import urllib.request

    with open(VERSION_FILE) as f:
        local_txt = f.read().strip()
    with urllib.request.urlopen(REMOTE_VERSION_URL, timeout=15) as r:
        remote_txt = r.read().decode().strip()
    local, remote = _parse_version(local_txt), _parse_version(remote_txt)
    if not local or not remote or remote <= local:
        return
    try:
        with open(UPDATE_NOTICE) as f:
            if json.load(f).get("announced") == remote_txt:
                return  # this version was already announced — stay quiet
    except (FileNotFoundError, ValueError):
        pass
    append_to_inbox(
        {
            "type": "sidecar_update",
            "text": (
                f"Sidecar update available: v{remote_txt} is out, this tailer "
                f"runs v{local_txt}. Tell your principal (message_principal) "
                "to update: git pull in the sidecar repo, then restart the "
                "tailer. Mention it once — don't nag."
            ),
            "current_version": local_txt,
            "latest_version": remote_txt,
        }
    )
    with open(UPDATE_NOTICE, "w") as f:
        json.dump({"announced": remote_txt, "at_ms": int(time.time() * 1000)}, f)
    print(f"agent_listen: update available — v{remote_txt} (running v{local_txt})")


def _update_check_loop(stop: threading.Event) -> None:
    if env("FILAMENT_SIDECAR_UPDATE_CHECK", "on").lower() in ("off", "0", "false"):
        return
    delay = UPDATE_CHECK_FIRST_S
    while not stop.wait(delay):
        delay = UPDATE_CHECK_INTERVAL_S
        try:
            _check_for_update()
        except Exception as e:  # noqa: BLE001 — never load-bearing
            print(f"agent_listen: update check failed (ignored): {e}")


# ---------------------------------------------------------------- FCM register + run

async def _fcm_register():
    try:
        from firebase_messaging import FcmPushClient, FcmRegisterConfig
    except ImportError:
        sys.exit("agent_listen: missing dep — run: pip install firebase-messaging")

    def _load_creds():
        try:
            return json.load(open(CREDS))
        except (OSError, ValueError):
            return None

    def _save_creds(creds):
        os.makedirs(STATE_DIR, exist_ok=True)
        json.dump(creds, open(CREDS, "w"), indent=2)

    config = FcmRegisterConfig(
        _firebase("project_id"),
        _firebase("app_id"),
        _firebase("api_key"),
        _firebase("sender_id"),
    )
    # A *fresh* GCM registration is flaky — Google intermittently returns
    # PHONE_REGISTRATION_ERROR, and the library only retries twice internally.
    # Retry with backoff; once it succeeds the creds are cached, so subsequent
    # runs skip registration entirely.
    creds = _load_creds()
    last_err: object = None
    for attempt in range(1, 7):
        fcm = FcmPushClient(
            callback=on_push,
            fcm_config=config,
            credentials=creds,
            credentials_updated_callback=_save_creds,
        )
        try:
            token = await fcm.checkin_or_register()
        except Exception as e:  # noqa: BLE001 — GCM register is flaky
            last_err = e
        else:
            if token:
                return fcm, token
            last_err = "registration returned no token"
        print(
            f"agent_listen: FCM registration attempt {attempt} failed "
            f"({last_err}); retrying…"
        )
        await asyncio.sleep(min(4 * attempt, 20))
    sys.exit(f"agent_listen: FCM registration failed after retries — {last_err}")


async def run() -> None:
    global _seen
    _seen = _load_seen()

    # Identify ourselves up front — a clear failure here means a bad API key
    # or base URL, caught before we wait on pushes.
    try:
        me = client.get_self()
        print(f"agent_listen: connected as {me.get('user_id') or me}")
    except FilamentError as e:
        sys.exit(f"agent_listen: could not reach the agent surface — {e}")

    global BACKCHANNEL_ROOM_ID
    BACKCHANNEL_ROOM_ID = me.get("cc_room_id") if isinstance(me, dict) else None
    print(f"agent_listen: backchannel = {BACKCHANNEL_ROOM_ID}")

    write_mcp_config()

    fcm, fcm_token = await _fcm_register()
    print(f"agent_listen: FCM token = {fcm_token[:24]}…")

    # Register the token with Filament so DirectPusher routes pushes here —
    # once. The cached creds keep the same token across restarts, and the
    # server holds one active token per agent, so re-registering is never needed.
    if fcm_token in client.push_tokens():
        print("agent_listen: token already registered with Filament")
    else:
        result = client.register_push_token(fcm_token)
        if not result.get("success"):
            sys.exit(f"agent_listen: register_push_token failed — {result}")
        print("agent_listen: registered with Filament")

    stop = threading.Event()
    threading.Thread(target=_heartbeat_loop, args=(stop,), daemon=True).start()
    threading.Thread(target=_update_check_loop, args=(stop,), daemon=True).start()

    print(f"agent_listen: listening — inbox={INBOX}. Ctrl-C to stop.")
    await fcm.start()
    try:
        await asyncio.Event().wait()
    finally:
        stop.set()


def _proc_started(pid: int) -> str | None:
    """The process's start time per ps (POSIX `lstart`), or None if it can't
    be read. Works across users, unlike signalling."""
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        started = out.stdout.strip()
        return started or None
    except Exception:  # noqa: BLE001 — identity check is best-effort
        return None


def _pid_alive(pid: int, claimed_start: str | None = None) -> bool:
    """Does `pid` exist AND still refer to the claimant?

    Existence alone isn't identity: after an unclean exit the OS can reuse
    the PID for an unrelated process, which would wedge the claim ("another
    tailer owns this") until hand-deleted. The claim records the process
    start time; a live PID whose start time differs is a reused PID, not the
    tailer. PermissionError = exists under another user (still comparable
    via ps). Unknown start times fall back to existence — conservative."""
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass  # exists, another user's — identity check below still applies
    except (ProcessLookupError, ValueError):
        return False
    if claimed_start:
        live_start = _proc_started(pid)
        if live_start and live_start != claimed_start:
            return False  # PID reused by a different process
    return True


def _claim_pid_file() -> None:
    """Refuse a second tailer on the same checkout (double-appended inboxes),
    then record ourselves for `is my tailer running?` checks.

    The claim is an O_CREAT|O_EXCL create, so concurrent starters serialize on
    the filesystem: exactly one wins the create; losers read the winner's PID
    and exit. A stale claim (dead PID) is removed and the exclusive create
    retried — two starters can both remove a stale file, but the retried
    O_EXCL still admits only one."""
    os.makedirs(STATE_DIR, exist_ok=True)
    # The claim appears atomically COMPLETE or not at all: the PID is written
    # to a private temp file first, and os.link() publishes it — an atomic
    # succeed-or-FileExistsError. No observer can ever see a partial claim,
    # which is what previously forced heuristics for empty files (and their
    # races). Any unparseable claim file is therefore garbage by construction
    # and safe to clear immediately.
    # Line 1: PID (shell checks stay `head -1`-simple). Line 2: the process
    # start time — the identity that survives PID reuse.
    tmp = f"{PID_FILE}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        f.write(f"{os.getpid()}\n{_proc_started(os.getpid()) or ''}\n")
    try:
        for _ in range(3):
            try:
                os.link(tmp, PID_FILE)
                return
            except FileExistsError:
                pass
            claimed_start: str | None = None
            try:
                with open(PID_FILE) as f:
                    lines = f.read().splitlines()
                pid = int(lines[0].strip())
                claimed_start = (lines[1].strip() or None) if len(lines) > 1 else None
            except FileNotFoundError:
                continue  # holder vanished between link-attempt and read
            except (ValueError, IndexError):
                pid = None  # can't be a mid-write claimant — garbage
            if pid is not None and _pid_alive(pid, claimed_start):
                sys.exit(
                    f"agent_listen: another tailer (pid {pid}) already owns "
                    f"this checkout — one per state dir. Stop it or remove "
                    f"{PID_FILE}."
                )
            try:  # stale/garbage claim — clear it; the retried atomic link
                os.remove(PID_FILE)  # serializes whoever's left
            except FileNotFoundError:
                pass
        sys.exit(f"agent_listen: could not claim {PID_FILE} after retries")
    finally:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------- login (OAuth device flow)

LOGIN_STATE = os.path.join(STATE_DIR, "device_login.json")
LOGIN_LOCK = os.path.join(STATE_DIR, "device_login.lock")
LOGIN_LOG = os.path.join(STATE_DIR, "login.log")
_TOKEN_RE = re.compile(r"fmcp_[A-Za-z0-9_\-]+")


def _say(msg: str) -> None:
    print(msg, flush=True)


def _redact(text: str) -> str:
    return _TOKEN_RE.sub("fmcp_<redacted>", text)


def _env_file_value(key: str) -> str:
    """*key* as .env has it NOW — read fresh, since another process (the
    detached poller) may have written it after this one started."""
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[len("export "):]
                name, sep, value = line.partition("=")
                if sep and name.strip() == key:
                    return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _report_signed_in(base: str, token: str) -> int:
    try:
        me = FilamentClient(base, token).get_self()
    except Exception as e:  # noqa: BLE001
        _say(f"login: got a token, but get_self failed: {_redact(str(e))}")
        return 1
    name = me.get("display_name") or me.get("displayname") or me.get("name")
    uid = me.get("user_id") or "?"
    _say(f"Signed in as {name} ({uid})" if name else f"Signed in as {uid}")
    _say(f"Token saved to {ENV_FILE} (FILAMENT_CONNECT_TOKEN). Start the tailer: python push_tailer.py")
    if _TOKEN_FROM_ENV:
        _say(
            "login: note — FILAMENT_CONNECT_TOKEN is also set in your environment, "
            "which wins over .env. Unset it so the tailer uses the new token."
        )
    return 0


def _poll_and_finish(pending: dict, *, immediate: bool) -> int:
    """Poll one saved sign-in to the end; on approval write .env."""
    started = time.time()

    def still_wanted() -> bool:
        # A newer `login` replaces the saved code; a poller on the old one stops.
        current = device_login.load_pending(LOGIN_STATE) or {}
        return current.get("device_code") == pending.get("device_code")

    try:
        doc = device_login.poll(
            pending,
            immediate=immediate,
            still_wanted=still_wanted,
            on_wait=lambda m: _say(f"login: {_redact(m)}"),
        )
    except device_login.LoginSuperseded as e:
        _say(f"login: {e}")
        return 1
    except device_login.LoginError as e:
        status = (
            "denied" if isinstance(e, device_login.LoginDenied)
            else "expired" if isinstance(e, device_login.LoginExpired)
            else "error"
        )
        pending.pop("device_code", None)
        pending.update(status=status, message=str(e), finished_at=time.time())
        device_login.save_pending(LOGIN_STATE, pending)
        _say(f"login: {e}")
        return 1
    token = doc["access_token"]
    device_login.set_env_var(ENV_FILE, "FILAMENT_CONNECT_TOKEN", token)
    if pending.get("write_base_url"):
        device_login.set_env_var(ENV_FILE, "FILAMENT_AGENT_API_BASE_URL", pending["base_url"])
    pending.pop("device_code", None)  # spent; the token is the secret now
    pending.update(
        status="approved",
        agent_user_id=doc.get("agent_user_id"),
        principal_user_id=doc.get("principal_user_id"),
        finished_at=time.time(),
    )
    device_login.save_pending(LOGIN_STATE, pending)
    _say(f"login: approved after {time.time() - started:.1f}s of polling")
    return _report_signed_in(pending["base_url"], token)


def _finish_saved() -> int:
    """Report or continue whatever sign-in state/ holds. Caller holds the lock."""
    pending = device_login.load_pending(LOGIN_STATE)
    if not pending:
        _say("login: no sign-in in progress. Run: python push_tailer.py login")
        return 1
    status = pending.get("status")
    if status == "approved":
        token = _env_file_value("FILAMENT_CONNECT_TOKEN")
        if token_missing(token):
            _say("login: the sign-in was approved but .env has no token. Run login again.")
            return 1
        return _report_signed_in(pending["base_url"], token)
    if status != "pending" or not pending.get("device_code"):
        _say(f"login: the last sign-in ended: {pending.get('message') or status}. Run login again.")
        return 1
    _say(f"login: waiting for approval of {pending['link']}")
    return _poll_and_finish(pending, immediate=True)


def _login_resume() -> int:
    with device_login.poll_lock(LOGIN_LOCK, block=False) as got:
        if got:
            return _finish_saved()
    holder = device_login.lock_holder(LOGIN_LOCK)
    _say(f"login: another poller (pid {holder}) is waiting on this sign-in; waiting for it…")
    with device_login.poll_lock(LOGIN_LOCK, block=True):
        return _finish_saved()


def _spawn_poller() -> int:
    """`login --resume` in its own session, so it outlives this process, the
    terminal and the harness turn that started it."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOGIN_LOG, "a") as log:
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "login", "--resume"],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    return proc.pid


def cmd_login(args: argparse.Namespace) -> int:
    if args.resume:
        return _login_resume()
    base = (args.base_url or API_BASE_URL).rstrip("/")
    try:
        pending = device_login.begin(base, args.client_name)
    except device_login.LoginError as e:
        _say(f"login: {e}")
        return 1
    pending["write_base_url"] = bool(args.base_url)
    device_login.save_pending(LOGIN_STATE, pending)
    minutes = max(1, round((pending["expires_at"] - time.time()) / 60))
    _say(f"Open this link to approve: {pending['link']}")
    _say(f"(It asks the owner to sign in to Filament; code {pending['user_code']}; expires in {minutes} min.)")
    if args.no_wait:
        pid = _spawn_poller()
        _say(
            f"login: a background poller (pid {pid}) saves the token to .env once "
            f"approved (log: {LOGIN_LOG}). Check or wait with: python push_tailer.py login --resume"
        )
        return 0
    with device_login.poll_lock(LOGIN_LOCK, block=True):
        return _poll_and_finish(pending, immediate=False)


# ---------------------------------------------------------------- mcp-add

def mcp_add_command(name: str, base: str, scope: str = "local") -> str:
    """The `claude mcp add` line, with the token read from .env by the shell
    when it runs — so the line itself never shows the token."""
    read_token = f"$(sed -n 's/^FILAMENT_CONNECT_TOKEN=//p' {shlex.quote(ENV_FILE)})"
    return (
        f"claude mcp add --scope {shlex.quote(scope)} --transport http "
        f"{shlex.quote(name)} {shlex.quote(base)} "
        f'--header "Authorization: Bearer {read_token}"'
    )


def cmd_mcp_add(args: argparse.Namespace) -> int:
    token = _env_file_value("FILAMENT_CONNECT_TOKEN")
    if token_missing(token):
        _say(MISSING_TOKEN_HINT)
        return 1
    base = (args.base_url or _env_file_value("FILAMENT_AGENT_API_BASE_URL") or API_BASE_URL).rstrip("/")
    if not args.run:
        print(mcp_add_command(args.name, base, args.scope))
        return 0
    cmd = [
        "claude", "mcp", "add", "--scope", args.scope, "--transport", "http",
        args.name, base, "--header", f"Authorization: Bearer {token}",
    ]
    _say("Running: " + _redact(shlex.join(cmd)))
    try:
        done = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        _say("mcp-add: `claude` is not on PATH")
        return 127
    for stream in (done.stdout, done.stderr):  # it may echo the header back
        if stream:
            _say(_redact(stream.rstrip()))
    return done.returncode


# ---------------------------------------------------------------- main

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="push_tailer.py",
        description="Register for Filament pushes and tail them to state/inbox.jsonl.",
    )
    sub = parser.add_subparsers(dest="command")
    login = sub.add_parser("login", help="sign in to Filament (OAuth device flow); saves the token to .env")
    login.add_argument("--base-url", help=f"agent API base URL (default: FILAMENT_AGENT_API_BASE_URL or {DEFAULT_API_BASE_URL}); saved to .env")
    login.add_argument("--client-name", default=device_login.DEFAULT_CLIENT_NAME,
                       help="OAuth client name; it decides the agent type (default: %(default)r)")
    mode = login.add_mutually_exclusive_group()
    mode.add_argument("--no-wait", action="store_true",
                      help="print the link, leave a background poller to save the token, exit")
    mode.add_argument("--resume", action="store_true",
                      help="wait for (or report) the sign-in saved in state/")
    add = sub.add_parser("mcp-add", help="print (or --run) the `claude mcp add` command for this token")
    add.add_argument("name", nargs="?", default="filament")
    add.add_argument("--run", action="store_true", help="run it instead of printing it")
    add.add_argument("--scope", default="local")
    add.add_argument("--base-url")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.command == "login":
        sys.exit(cmd_login(args))
    if args.command == "mcp-add":
        sys.exit(cmd_mcp_add(args))
    if token_missing(CONNECT_TOKEN):
        sys.exit(MISSING_TOKEN_HINT)
    _claim_pid_file()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print(f"\nagent_listen: stopped ({time.strftime('%H:%M:%S')})")
    finally:
        try:  # only our own claim — a newer tailer may have re-claimed
            with open(PID_FILE) as f:
                first = f.read().splitlines()[0].strip()
            if int(first) == os.getpid():
                os.remove(PID_FILE)
        except (FileNotFoundError, ValueError, IndexError):
            pass


if __name__ == "__main__":
    main()
