"""The tailer's `login` / `mcp-add` commands and the missing-token hint,
with the OAuth HTTP and get_self faked out."""

import argparse
import json
import os
import stat
import sys
from pathlib import Path

import pytest

os.environ.setdefault("FILAMENT_CONNECT_TOKEN", "fmcp_test")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import device_login  # noqa: E402
import push_tailer  # noqa: E402


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(push_tailer, "ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setattr(push_tailer, "LOGIN_STATE", str(state / "device_login.json"))
    monkeypatch.setattr(push_tailer, "LOGIN_LOCK", str(state / "device_login.lock"))
    monkeypatch.setattr(push_tailer, "LOGIN_LOG", str(state / "login.log"))
    monkeypatch.setattr(push_tailer, "_TOKEN_FROM_ENV", False)
    monkeypatch.setattr(
        push_tailer.FilamentClient, "get_self",
        lambda self: {"user_id": "@claude-test:srv", "display_name": "Claude Test"},
    )
    return tmp_path


def _fake_begin(base, client_name):
    return {
        "status": "pending", "base_url": base, "client_name": client_name,
        "client_id": "c1", "token_endpoint": "http://srv/token", "resource": base,
        "device_code": "dev_1", "user_code": "BCDF-GHJK",
        "link": "http://srv/device?user_code=BCDF-GHJK",
        "verification_uri": "http://srv/device", "interval": 5,
        "expires_at": 9e12, "started_at": 0,
    }


def _args(**kw):
    base = {"resume": False, "no_wait": False, "base_url": None,
            "client_name": device_login.DEFAULT_CLIENT_NAME}
    return argparse.Namespace(**{**base, **kw})


def test_run_without_token_prints_login_hint(monkeypatch):
    monkeypatch.setattr(push_tailer, "CONNECT_TOKEN", "")
    with pytest.raises(SystemExit) as exc:
        push_tailer.main([])
    assert "python push_tailer.py login" in str(exc.value.code)


def test_placeholder_token_counts_as_missing():
    assert push_tailer.token_missing("fmcp_...")
    assert not push_tailer.token_missing("fmcp_real")


def test_login_writes_env_and_reports(sandbox, monkeypatch, capsys):
    (sandbox / ".env").write_text("# keep\nFILAMENT_CONNECT_TOKEN=fmcp_old\nOTHER=1\n")
    monkeypatch.setattr(device_login, "begin", _fake_begin)
    monkeypatch.setattr(
        device_login, "poll",
        lambda pending, **kw: {"access_token": "fmcp_new", "agent_user_id": "@claude-test:srv"},
    )
    code = push_tailer.cmd_login(_args(base_url="http://srv:8448/mcp/agents"))
    out = capsys.readouterr().out
    assert code == 0
    assert "Open this link to approve: http://srv/device?user_code=BCDF-GHJK\n" in out
    assert "Signed in as Claude Test (@claude-test:srv)" in out
    assert "fmcp_new" not in out
    env = (sandbox / ".env").read_text()
    assert env == (
        "# keep\nFILAMENT_CONNECT_TOKEN=fmcp_new\nOTHER=1\n"
        "FILAMENT_AGENT_API_BASE_URL=http://srv:8448/mcp/agents\n"
    )
    assert stat.S_IMODE(os.stat(sandbox / ".env").st_mode) == 0o600
    saved = json.loads((sandbox / "state" / "device_login.json").read_text())
    assert saved["status"] == "approved" and "device_code" not in saved


def test_no_wait_prints_link_and_spawns_poller(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(device_login, "begin", _fake_begin)
    monkeypatch.setattr(push_tailer, "_spawn_poller", lambda: 4242)
    assert push_tailer.cmd_login(_args(no_wait=True)) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "Open this link to approve: http://srv/device?user_code=BCDF-GHJK"
    assert "pid 4242" in out
    saved = json.loads((sandbox / "state" / "device_login.json").read_text())
    assert saved["status"] == "pending" and saved["device_code"] == "dev_1"


def test_resume_polls_the_saved_code_immediately(sandbox, monkeypatch, capsys):
    device_login.save_pending(push_tailer.LOGIN_STATE, _fake_begin("http://srv/mcp/agents", "x"))
    seen = {}

    def fake_poll(pending, **kw):
        seen.update(kw, device_code=pending["device_code"])
        return {"access_token": "fmcp_r"}

    monkeypatch.setattr(device_login, "poll", fake_poll)
    assert push_tailer.cmd_login(_args(resume=True)) == 0
    assert seen["immediate"] is True and seen["device_code"] == "dev_1"
    assert "FILAMENT_CONNECT_TOKEN=fmcp_r" in (sandbox / ".env").read_text()
    # A second resume just reports the finished sign-in.
    assert push_tailer.cmd_login(_args(resume=True)) == 0
    assert capsys.readouterr().out.count("Signed in as Claude Test") == 2


def test_resume_reports_an_expired_sign_in(sandbox, monkeypatch, capsys):
    device_login.save_pending(push_tailer.LOGIN_STATE, _fake_begin("http://srv/mcp/agents", "x"))

    def expired(pending, **kw):
        raise device_login.LoginExpired("The sign-in link expired. Run `login` again.")

    monkeypatch.setattr(device_login, "poll", expired)
    assert push_tailer.cmd_login(_args(resume=True)) == 1
    assert push_tailer.cmd_login(_args(resume=True)) == 1
    assert "expired" in capsys.readouterr().out
    assert not (sandbox / ".env").exists()


def test_mcp_add_prints_a_command_without_the_token(sandbox, capsys):
    (sandbox / ".env").write_text(
        "FILAMENT_CONNECT_TOKEN=fmcp_secret\nFILAMENT_AGENT_API_BASE_URL=http://srv/mcp/agents\n"
    )
    args = argparse.Namespace(name="filament", run=False, scope="local", base_url=None)
    assert push_tailer.cmd_mcp_add(args) == 0
    out = capsys.readouterr().out
    assert "fmcp_secret" not in out
    assert out.startswith(
        "claude mcp add --scope local --transport http filament http://srv/mcp/agents "
        '--header "Authorization: Bearer $(sed -n'
    )


def test_mcp_add_run_passes_the_token_but_redacts_output(sandbox, monkeypatch, capsys):
    (sandbox / ".env").write_text("FILAMENT_CONNECT_TOKEN=fmcp_secret\n")
    ran = {}

    class Done:
        returncode = 0
        stdout = "Added HTTP MCP server filament with headers Authorization: Bearer fmcp_secret\n"
        stderr = ""

    def fake_run(cmd, **kw):
        ran["cmd"] = cmd
        return Done()

    monkeypatch.setattr(push_tailer.subprocess, "run", fake_run)
    args = argparse.Namespace(name="filament", run=True, scope="local", base_url="http://srv/mcp/agents")
    assert push_tailer.cmd_mcp_add(args) == 0
    assert ran["cmd"][-1] == "Authorization: Bearer fmcp_secret"
    assert ran["cmd"][:2] == ["claude", "mcp"]
    assert "fmcp_secret" not in capsys.readouterr().out
