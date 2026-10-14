"""Sign the tailer in to Filament with the OAuth device grant (RFC 8628).

The owner never sees or pastes an ``fmcp_`` token: the tailer registers an
OAuth client (DCR), asks for a device code, prints a link, and polls the token
endpoint until the owner approves it in Filament. The token lands in ``.env``.

Stdlib only, and nothing here reads the tailer's config at import, so it is
unit-testable on its own. Request shapes follow the Hermes plugin's
``oauth_login.py`` and synapse's ``agents_mcp/oauth_device.py``.

Sign-in state lives in ``state/device_login.json`` so the poll can be resumed
by a later process (``login --resume``) or run by a detached poller
(``login --no-wait``): an agent harness that can't keep a foreground process
alive across turns can print the link at once and let the poll finish
elsewhere. A lock file makes sure only one process polls a code at a time.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterator

SCOPE = "filament:agent:control"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
# A name matching /claude/i makes the server give the agent type ``claude``.
DEFAULT_CLIENT_NAME = "Claude Code (push tailer)"
HTTP_TIMEOUT_S = 20
SLOW_DOWN_STEP_S = 5


class LoginError(Exception):
    """A sign-in step failed; the message is fit to show the owner."""


class TransportError(LoginError):
    """The server could not be reached, or answered 5xx/429 — worth retrying."""


class LoginDenied(LoginError):
    pass


class LoginExpired(LoginError):
    pass


class LoginSuperseded(LoginError):
    """A newer sign-in replaced the one this poller was waiting on."""


# ---------------------------------------------------------------- HTTP

def request_json(
    url: str, *, form: dict[str, str] | None = None, body: Any = None
) -> tuple[int, Any]:
    """POST a form or JSON body (GET with neither); return (status, JSON)."""
    headers = {"Accept": "application/json"}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransportError(f"Could not reach {url}: {exc}") from exc
    try:
        doc = json.loads(raw or b"null")
    except ValueError:
        doc = {"error": raw.decode(errors="replace")[:200]}
    if status >= 500 or status == 429:
        raise TransportError(f"{url} answered HTTP {status}")
    return status, doc


# ---------------------------------------------------------------- OAuth steps

def _well_known(base: str, name: str) -> list[str]:
    """Where RFC 8414 / 9728 metadata for *base* may live, most specific first."""
    parts = urllib.parse.urlsplit(base)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    return [f"{origin}/.well-known/{name}{path}", f"{base.rstrip('/')}/.well-known/{name}"]


def _first_doc(urls: list[str], request: Callable) -> dict | None:
    for url in urls:
        try:
            status, doc = request(url)
        except TransportError:
            continue
        if status == 200 and isinstance(doc, dict):
            return doc
    return None


def discover(mcp_url: str, request: Callable = request_json) -> dict[str, str]:
    """Protected-resource metadata, then the authorization server's."""
    prm = _first_doc(_well_known(mcp_url, "oauth-protected-resource"), request)
    if not prm or not prm.get("authorization_servers"):
        raise LoginError(f"{mcp_url} does not offer OAuth sign-in.")
    issuer = prm["authorization_servers"][0]
    meta = _first_doc(_well_known(issuer, "oauth-authorization-server"), request)
    if not meta:
        raise LoginError(f"Could not read the sign-in settings of {issuer}.")
    if not meta.get("device_authorization_endpoint"):
        raise LoginError("This Filament server does not offer device sign-in.")
    try:
        return {
            "resource": prm.get("resource") or mcp_url,
            "token_endpoint": meta["token_endpoint"],
            "registration_endpoint": meta["registration_endpoint"],
            "device_authorization_endpoint": meta["device_authorization_endpoint"],
        }
    except KeyError as exc:
        raise LoginError(f"Filament's sign-in settings lack {exc}.") from exc


def register_client(meta: dict, client_name: str, request: Callable = request_json) -> str:
    status, doc = request(
        meta["registration_endpoint"],
        body={
            "client_name": client_name,
            "token_endpoint_auth_method": "none",
            "grant_types": [DEVICE_GRANT],
            "redirect_uris": [],
            "response_types": [],
        },
    )
    if status not in (200, 201) or not isinstance(doc, dict) or "client_id" not in doc:
        raise LoginError(f"Filament refused to register this client ({status}): {doc}")
    return str(doc["client_id"])


def start_device_authorization(
    meta: dict, client_id: str, request: Callable = request_json
) -> dict:
    status, doc = request(
        meta["device_authorization_endpoint"],
        form={"client_id": client_id, "scope": SCOPE, "resource": meta["resource"]},
    )
    if (
        status != 200
        or not isinstance(doc, dict)
        or not {"device_code", "user_code", "verification_uri"} <= doc.keys()
    ):
        raise LoginError(f"Filament refused device sign-in ({status}): {doc}")
    return doc


def begin(
    mcp_url: str,
    client_name: str = DEFAULT_CLIENT_NAME,
    *,
    request: Callable = request_json,
    now: Callable[[], float] = time.time,
) -> dict:
    """DCR + device authorization. Returns the pending sign-in to save."""
    meta = discover(mcp_url, request)
    client_id = register_client(meta, client_name, request)
    dev = start_device_authorization(meta, client_id, request)
    return {
        "status": "pending",
        "base_url": mcp_url,
        "client_name": client_name,
        "client_id": client_id,
        "token_endpoint": meta["token_endpoint"],
        "resource": meta["resource"],
        "device_code": dev["device_code"],
        "user_code": dev["user_code"],
        "link": dev.get("verification_uri_complete") or dev["verification_uri"],
        "verification_uri": dev["verification_uri"],
        "interval": float(dev.get("interval", 5)),
        # Wall clock, not monotonic: a different process may resume the poll.
        "expires_at": now() + float(dev.get("expires_in", 600)),
        "started_at": now(),
    }


def poll(
    pending: dict,
    *,
    request: Callable = request_json,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    immediate: bool = False,
    on_wait: Callable[[str], None] | None = None,
    still_wanted: Callable[[], bool] = lambda: True,
) -> dict:
    """Poll the token endpoint until the code is approved, denied or expires.

    Honours ``interval`` and grows it by 5s on ``slow_down`` (RFC 8628 §3.5).
    A network blip or 5xx doesn't end the wait — it backs off and retries
    until the code expires. Returns the token response (``access_token`` plus
    the server's ``agent_user_id`` / ``principal_user_id``).
    """
    interval = float(pending.get("interval", 5))
    first = True
    while True:
        if now() >= float(pending["expires_at"]):
            raise LoginExpired("The sign-in link expired. Run `login` again.")
        if not (first and immediate):
            sleep(interval)
        first = False
        if not still_wanted():
            raise LoginSuperseded("A newer sign-in replaced this one; this poller stops.")
        try:
            status, doc = request(
                pending["token_endpoint"],
                form={
                    "grant_type": DEVICE_GRANT,
                    "device_code": pending["device_code"],
                    "client_id": pending["client_id"],
                    "resource": pending["resource"],
                },
            )
        except TransportError as exc:
            if on_wait:
                on_wait(f"retrying: {exc}")
            interval = min(interval * 2, 60)
            continue
        if status == 200 and isinstance(doc, dict) and doc.get("access_token"):
            return doc
        error = doc.get("error") if isinstance(doc, dict) else None
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += SLOW_DOWN_STEP_S
            if on_wait:
                on_wait(f"server asked to slow down; polling every {interval:g}s")
            continue
        if error == "access_denied":
            raise LoginDenied("The owner declined the sign-in.")
        if error == "expired_token":
            raise LoginExpired("The sign-in link expired. Run `login` again.")
        detail = (doc.get("error_description") or error) if isinstance(doc, dict) else doc
        raise LoginError(f"Filament did not issue a token ({status}): {detail}")


# ---------------------------------------------------------------- files

def _write_private(path: str, text: str) -> None:
    """Atomically replace *path* with *text*, mode 600 from the first byte."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")  # mkstemp => 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp)
        raise


def set_env_var(path: str, key: str, value: str) -> None:
    """Set ``key=value`` in a .env file, touching only that key's line.

    Every other line — comments, blanks, other keys, a commented-out
    ``# KEY=`` — is kept byte for byte. An ``export KEY=`` line keeps its
    ``export``. Extra copies of the key are dropped (the first one wins in
    the tailer's loader, so leaving a stale second copy would only confuse).
    The key is appended if absent; the file ends up mode 600.
    """
    try:
        with open(path) as f:
            lines = f.read().splitlines(keepends=True)
    except FileNotFoundError:
        lines = []
    out: list[str] = []
    done = False
    for line in lines:
        stripped = line.lstrip()
        prefix = "export " if stripped.startswith("export ") else ""
        name = stripped[len(prefix):].split("=", 1)[0].strip() if "=" in stripped else None
        if name == key:
            if not done:
                indent = line[: len(line) - len(stripped)]
                out.append(f"{indent}{prefix}{key}={value}\n")
                done = True
            continue
        out.append(line)
    if not done:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(f"{key}={value}\n")
    _write_private(path, "".join(out))


def save_pending(path: str, pending: dict) -> None:
    _write_private(path, json.dumps(pending, indent=2) + "\n")


def load_pending(path: str) -> dict | None:
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


@contextlib.contextmanager
def poll_lock(path: str, *, block: bool) -> Iterator[bool]:
    """Hold an exclusive lock on *path*; yields whether it was acquired.

    One poller per device code: two would trip the server's slow_down, and
    only one can consume the code. The OS drops the lock if the holder dies.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def lock_holder(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip() or "?"
    except OSError:
        return "?"
