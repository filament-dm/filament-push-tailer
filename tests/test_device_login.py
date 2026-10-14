"""OAuth device sign-in: the .env rewrite and the token poll loop.

HTTP is a fake ``request`` callable, so nothing here touches the network.
"""

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import device_login as dl  # noqa: E402


# ---------------------------------------------------------------- .env rewrite


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_set_env_var_replaces_only_that_line(tmp_path):
    env = tmp_path / ".env"
    original = (
        "# Copy to .env and fill in.\n"
        "\n"
        "FILAMENT_CONNECT_TOKEN=fmcp_old\n"
        "# FILAMENT_CONNECT_TOKEN=fmcp_commented\n"
        "FILAMENT_AGENT_API_BASE_URL=http://x/mcp/agents\n"
        'FILAMENT_FIREBASE_API_KEY="quoted value"\n'
    )
    env.write_text(original)
    dl.set_env_var(str(env), "FILAMENT_CONNECT_TOKEN", "fmcp_new")
    assert env.read_text() == original.replace("fmcp_old", "fmcp_new")
    assert _mode(env) == 0o600


def test_set_env_var_appends_when_absent_and_creates_file(tmp_path):
    env = tmp_path / ".env"
    dl.set_env_var(str(env), "FILAMENT_CONNECT_TOKEN", "fmcp_a")
    assert env.read_text() == "FILAMENT_CONNECT_TOKEN=fmcp_a\n"
    assert _mode(env) == 0o600

    env.write_text("OTHER=1")  # no trailing newline
    os.chmod(env, 0o644)
    dl.set_env_var(str(env), "FILAMENT_CONNECT_TOKEN", "fmcp_b")
    assert env.read_text() == "OTHER=1\nFILAMENT_CONNECT_TOKEN=fmcp_b\n"
    assert _mode(env) == 0o600


def test_set_env_var_keeps_export_and_drops_duplicates(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "export FILAMENT_CONNECT_TOKEN=fmcp_1\nA=1\nFILAMENT_CONNECT_TOKEN = fmcp_2\n"
    )
    dl.set_env_var(str(env), "FILAMENT_CONNECT_TOKEN", "fmcp_3")
    assert env.read_text() == "export FILAMENT_CONNECT_TOKEN=fmcp_3\nA=1\n"


def test_set_env_var_does_not_match_a_longer_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FILAMENT_CONNECT_TOKEN_OLD=keep\n")
    dl.set_env_var(str(env), "FILAMENT_CONNECT_TOKEN", "fmcp_x")
    assert env.read_text() == "FILAMENT_CONNECT_TOKEN_OLD=keep\nFILAMENT_CONNECT_TOKEN=fmcp_x\n"


# ---------------------------------------------------------------- poll loop


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def _pending(clock, **kw):
    return {
        "token_endpoint": "http://srv/oauth/token",
        "device_code": "dev_abc",
        "client_id": "client_1",
        "resource": "http://srv/mcp/agents",
        "interval": 5,
        "expires_at": clock.now() + 600,
        **kw,
    }


def _server(*answers):
    """A fake token endpoint answering each poll in turn; records requests."""
    calls = []
    queue = list(answers)

    def request(url, *, form=None, body=None):
        calls.append((url, form))
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return request, calls


PENDING = (400, {"error": "authorization_pending"})
OK = (200, {"access_token": "fmcp_tok", "agent_user_id": "@a:srv", "principal_user_id": "@p:srv"})


def test_poll_waits_for_approval_and_sends_the_device_grant():
    clock = Clock()
    request, calls = _server(PENDING, PENDING, OK)
    doc = dl.poll(_pending(clock), request=request, sleep=clock.sleep, now=clock.now)
    assert doc["access_token"] == "fmcp_tok"
    assert clock.sleeps == [5, 5, 5]
    url, form = calls[0]
    assert url == "http://srv/oauth/token"
    assert form == {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": "dev_abc",
        "client_id": "client_1",
        "resource": "http://srv/mcp/agents",
    }


def test_poll_slow_down_adds_five_seconds_each_time():
    clock = Clock()
    request, _ = _server(
        PENDING, (400, {"error": "slow_down"}), PENDING, (400, {"error": "slow_down"}), OK
    )
    dl.poll(_pending(clock), request=request, sleep=clock.sleep, now=clock.now)
    assert clock.sleeps == [5, 5, 10, 10, 15]


def test_poll_immediate_skips_the_first_sleep():
    clock = Clock()
    request, _ = _server(OK)
    dl.poll(_pending(clock), request=request, sleep=clock.sleep, now=clock.now, immediate=True)
    assert clock.sleeps == []


def test_poll_denied():
    clock = Clock()
    request, _ = _server(PENDING, (400, {"error": "access_denied"}))
    with pytest.raises(dl.LoginDenied):
        dl.poll(_pending(clock), request=request, sleep=clock.sleep, now=clock.now)


def test_poll_expired_by_server():
    clock = Clock()
    request, _ = _server((400, {"error": "expired_token"}))
    with pytest.raises(dl.LoginExpired):
        dl.poll(_pending(clock), request=request, sleep=clock.sleep, now=clock.now)


def test_poll_expires_locally_without_hammering():
    clock = Clock()
    request, calls = _server(*([PENDING] * 200))
    with pytest.raises(dl.LoginExpired):
        dl.poll(_pending(clock, expires_at=clock.now() + 12), request=request,
                sleep=clock.sleep, now=clock.now)
    assert len(calls) == 3  # t=5, 10, 15 then the deadline check stops it


def test_poll_survives_transport_errors_with_backoff():
    clock = Clock()
    request, _ = _server(dl.TransportError("down"), dl.TransportError("down"), PENDING, OK)
    seen = []
    doc = dl.poll(_pending(clock), request=request, sleep=clock.sleep, now=clock.now,
                  on_wait=seen.append)
    assert doc["access_token"] == "fmcp_tok"
    assert clock.sleeps == [5, 10, 20, 20]
    assert len(seen) == 2


def test_poll_other_errors_are_fatal():
    clock = Clock()
    request, _ = _server((400, {"error": "invalid_grant", "error_description": "already used"}))
    with pytest.raises(dl.LoginError, match="already used"):
        dl.poll(_pending(clock), request=request, sleep=clock.sleep, now=clock.now)


def test_poll_stops_when_superseded():
    clock = Clock()
    request, calls = _server(PENDING, OK)
    wanted = iter([True, False])
    with pytest.raises(dl.LoginSuperseded):
        dl.poll(_pending(clock), request=request, sleep=clock.sleep, now=clock.now,
                still_wanted=lambda: next(wanted))
    assert len(calls) == 1


# ---------------------------------------------------------------- begin (DCR + device authorization)


def test_begin_registers_then_asks_for_a_device_code():
    base = "http://srv:8448/mcp/agents"
    issuer = "http://srv:8448/mcp/agents/oauth"
    docs = {
        "http://srv:8448/.well-known/oauth-protected-resource/mcp/agents": (
            200, {"resource": base, "authorization_servers": [issuer]}),
        "http://srv:8448/.well-known/oauth-authorization-server/mcp/agents/oauth": (
            200, {
                "token_endpoint": f"{issuer}/token",
                "registration_endpoint": f"{issuer}/register",
                "device_authorization_endpoint": f"{issuer}/device_authorization",
            }),
        f"{issuer}/register": (201, {"client_id": "client_9"}),
        f"{issuer}/device_authorization": (200, {
            "device_code": "dev_1", "user_code": "BCDF-GHJK",
            "verification_uri": "http://srv:8448/mcp/agents/oauth/device",
            "verification_uri_complete": "http://srv:8448/mcp/agents/oauth/device?user_code=BCDF-GHJK",
            "expires_in": 600, "interval": 5}),
    }
    calls = []

    def request(url, *, form=None, body=None):
        calls.append((url, form, body))
        return docs.get(url, (404, {}))

    pending = dl.begin(base, "Claude Code (push tailer)", request=request, now=lambda: 50.0)
    assert pending["link"].endswith("?user_code=BCDF-GHJK")
    assert pending["client_id"] == "client_9"
    assert pending["expires_at"] == 650.0
    reg = next(b for u, _, b in calls if u.endswith("/register"))
    assert reg["client_name"] == "Claude Code (push tailer)"
    assert reg["grant_types"] == ["urn:ietf:params:oauth:grant-type:device_code"]
    dev = next(f for u, f, _ in calls if u.endswith("/device_authorization"))
    assert dev == {"client_id": "client_9", "scope": "filament:agent:control", "resource": base}


def test_poll_lock_is_exclusive(tmp_path):
    lock = str(tmp_path / "x.lock")
    with dl.poll_lock(lock, block=False) as first:
        assert first
        with dl.poll_lock(lock, block=False) as second:
            assert not second
    with dl.poll_lock(lock, block=False) as again:
        assert again
