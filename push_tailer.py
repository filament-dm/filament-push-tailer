#!/usr/bin/env python3
"""filament push tailer — register for Filament pushes, tail them to a file.

Run this wherever your agent lives. It holds a persistent FCM connection and,
on each relevant push from Filament, appends the event to ``state/inbox.jsonl``.
It does NOT spawn anything or decide how to respond — your own Claude Code
session tails the inbox (e.g. via the Monitor tool, see the bundled skill) and
handles each event however you like, with full session continuity. The tailer
is dumb plumbing; the behaviour lives in your session.

This is the listen half of the Filament agent loop. It rides the same FCM /
DirectPusher transport as the Hermes gateway (hermes-filament-fcm). Genericized
from lord-gnomington's ``scripts/fcm_listen.py`` (FCM intake + push parsing +
token registration). Tracked by ENG-155.

Pipeline:  Firebase register -> FCM token -> register_push_token (MCP) ->
           heartbeat loop (keeps "Connected" green) ->
           listen -> per push: ping->pong, or relevant message->append to inbox.

The Filament agent surface has three shapes, all on one bearer token:
  * MCP tools          POST {homeserver}/mcp/agents  (JSON-RPC tools/call).
                       Stateless, plain JSON, no initialize handshake. Used
                       here for register_push_token / get_self; your session
                       uses the same server (state/mcp.json) to read + post.
  * Pong side-channel  POST {homeserver}/mcp/agents/pong   {"nonce": ...}.
  * Heartbeat          POST {homeserver}/mcp/agents/heartbeat   (no body).
pong + heartbeat are deliberately NOT MCP tools — they are harness plumbing
and never routed through the LLM.

Config (env, or a .env beside this file; env wins):
  FILAMENT_CONNECT_TOKEN   the fmcp_ connect token from the Filament app
                           connect flow (REQUIRED) — the agent's MCP bearer.
  FILAMENT_HOMESERVER      e.g. https://api.filament.dm (REQUIRED).
  FILAMENT_PUSH_PLATFORM   android | macos | ios; default android.
  FILAMENT_FIREBASE_*      Firebase project config (public; defaults baked
                           in — same values as the other Filament clients).

Dependency:  pip install firebase-messaging
State:  state/fcm-credentials.json (FCM creds), state/seen.json (dedup),
        state/mcp.json (the Filament MCP server config for your session),
        state/inbox.jsonl (the append-only event feed your session tails),
        state/tailer.pid (this checkout's tailer — one per state dir),
        state/update_notice.json (which sidecar version was announced).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, "state")
CREDS = os.path.join(STATE_DIR, "fcm-credentials.json")
SEEN = os.path.join(STATE_DIR, "seen.json")
MCP_CONFIG = os.path.join(STATE_DIR, "mcp.json")
INBOX = os.path.join(STATE_DIR, "inbox.jsonl")

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
    path = os.path.join(ROOT, ".env")
    try:
        lines = open(path).read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()

CONNECT_TOKEN = env("FILAMENT_CONNECT_TOKEN", required=True)
HOMESERVER = env("FILAMENT_HOMESERVER", required=True).rstrip("/")
PLATFORM = env("FILAMENT_PUSH_PLATFORM", "android")


def _firebase(key: str) -> str:
    return env(f"FILAMENT_FIREBASE_{key.upper()}") or FIREBASE_DEFAULTS[key]


# ---------------------------------------------------------------- Filament client

class FilamentError(Exception):
    """A non-2xx HTTP status or a JSON-RPC error from the agent surface."""


class FilamentClient:
    """Bearer-authenticated client for the agent surface (stdlib only).

    The connect token is an ``fmcp_`` bearer scoped to this agent. The MCP
    endpoint is stateless and returns plain JSON, so no ``initialize``
    handshake or session header is needed — every request stands alone.
    """

    def __init__(self, homeserver: str, token: str) -> None:
        self._base = homeserver
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
            "/mcp/agents",
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

    def register_push_token(self, token: str, platform: str) -> dict:
        return self.call_tool(
            "register_push_token", {"token": token, "platform": platform}
        )

    def get_self(self) -> dict:
        return self.call_tool("get_self", {})

    def pong(self, nonce: str | None) -> None:
        # Side-channel REST endpoint — proves synapse -> FCM -> here -> back,
        # which is what flips push_round_trip_ok on the principal's detail read.
        self._post("/mcp/agents/pong", {"nonce": nonce})

    def heartbeat(self) -> None:
        # Side-channel REST endpoint — keeps Matrix presence online.
        self._post("/mcp/agents/heartbeat", None)


client = FilamentClient(HOMESERVER, CONNECT_TOKEN)

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

    Lifted from lord-gnomington/fcm_listen.py. The payload carries
    ``room_id``, ``event_id``, ``is_direct``, and a ``branch`` with
    type/sender/channel/content.text/thread_id + routing flags.
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


# ---------------------------------------------------------------- inbox + MCP config

def write_mcp_config() -> None:
    """Generate the Filament MCP server config for your Claude Code session.

    Registers the Filament agent surface as an HTTP MCP server named
    ``filament``, so its tools appear to claude as ``mcp__filament__*``. Point
    your session at it with ``claude --mcp-config state/mcp.json`` (or
    ``claude mcp add``). The bearer lives only in this gitignored state file,
    never in the repo.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    config = {
        "mcpServers": {
            "filament": {
                "type": "http",
                "url": f"{HOMESERVER}/mcp/agents",
                "headers": {"Authorization": f"Bearer {CONNECT_TOKEN}"},
            }
        }
    }
    with open(MCP_CONFIG, "w") as f:
        json.dump(config, f, indent=2)


def append_to_inbox(summary: dict) -> None:
    """Append one event to the inbox feed your session tails.

    A newline-delimited JSON record per relevant push — the raw event, nothing
    decided. Your Claude Code session watches this file (Monitor / tail) and
    chooses whether and how to respond via the Filament MCP tools, keeping its
    own context across events. We don't spawn anything: the tailer is plumbing,
    the behaviour is yours.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    record = {"received_ms": int(time.time() * 1000), **summary}
    with open(INBOX, "a") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------- push handler

_seen: set[str] = set()


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

    # (b) Real activity -> append to the inbox your session tails.
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

    # Identify ourselves up front — a clear failure here means a bad token or
    # homeserver, caught before we wait on pushes.
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

    # Register the token with Filament so DirectPusher routes pushes here.
    result = client.register_push_token(fcm_token, PLATFORM)
    if not result.get("success"):
        sys.exit(f"agent_listen: register_push_token failed — {result}")
    print(f"agent_listen: registered with Filament (platform={PLATFORM})")

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


def main() -> None:
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
