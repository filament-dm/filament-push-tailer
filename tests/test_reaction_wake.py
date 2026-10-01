"""Which emoji reactions wake the session.

A reaction carries no text, so nothing about who it addresses can decide
whether it is worth a turn - only the emoji can. These cover the resolution
and the four things that must never wake the agent however the policy is
written: its own reaction, an un-react, the processing marker, and anything
in the backchannel.

The tailer reads its config at import and exits without it, so the two
required vars are set before the import below.
"""

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("FILAMENT_CONNECT_TOKEN", "fmcp_test")
os.environ.setdefault("FILAMENT_HOMESERVER", "https://example.invalid")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import push_tailer  # noqa: E402


ROOM = "!channel:server"
BACKCHANNEL = "!backchannel:server"


def _reaction(key="\N{LADY BEETLE}", room_id=ROOM, **branch):
    payload = {
        "room_id": room_id,
        "event_id": "$reaction",
        "branch": {
            "type": "reaction",
            "key": key,
            "target_event_id": "$message",
            "sender": "Someone",
            "sender_id": "@someone:server",
            "channel": "bugs",
            **branch,
        },
    }
    return push_tailer.summarize_reaction(payload)


class TestTriggerResolution:
    def test_global_list_applies_to_any_room(self):
        policy = {"trigger_emojis": ["\N{LADY BEETLE}", "\N{BUG}"]}
        assert push_tailer.trigger_emojis(policy, ROOM) == [
            "\N{LADY BEETLE}",
            "\N{BUG}",
        ]

    def test_channel_list_replaces_the_global_one(self):
        # Replace, not union - so a channel can narrow as well as widen.
        policy = {
            "trigger_emojis": ["\N{LADY BEETLE}"],
            "per_channel": {ROOM: {"trigger_emojis": ["\N{FIRE}"]}},
        }
        assert push_tailer.trigger_emojis(policy, ROOM) == ["\N{FIRE}"]

    def test_channel_entry_without_triggers_falls_back(self):
        policy = {
            "trigger_emojis": ["\N{LADY BEETLE}"],
            "per_channel": {ROOM: {"something_else": True}},
        }
        assert push_tailer.trigger_emojis(policy, ROOM) == ["\N{LADY BEETLE}"]

    @pytest.mark.parametrize("policy", [{}, {"trigger_emojis": "not a list"}])
    def test_nothing_usable_means_no_triggers(self, policy):
        assert push_tailer.trigger_emojis(policy, ROOM) == []


class TestReactionWakes:
    """Every skip carries the reason it was skipped: a configured trigger
    suppressed by the room and an emoji nobody configured are the same
    "did not wake" from the outside, and telling them apart is the whole
    of debugging a quiet agent."""

    POLICY = {"trigger_emojis": ["\N{LADY BEETLE}", "\N{EYES}"]}

    def test_a_listed_emoji_wakes(self):
        assert push_tailer.skip_reason(_reaction(), self.POLICY) is None
        assert push_tailer.reaction_wakes(_reaction(), self.POLICY) is True

    def test_an_unlisted_emoji_does_not(self):
        summary = _reaction("\N{PARTY POPPER}")
        assert push_tailer.skip_reason(summary, self.POLICY) == "not_a_trigger"

    def test_our_own_reaction_never_wakes(self):
        summary = _reaction(is_from_self=True)
        assert push_tailer.skip_reason(summary, self.POLICY) == "own_reaction"

    def test_an_unreact_never_wakes(self):
        summary = _reaction(removed=True)
        assert push_tailer.skip_reason(summary, self.POLICY) == "unreact"

    def test_a_listed_eyes_reaction_wakes(self):
        # Nothing the agent posts is 👀, so a listed 👀 is a person's reaction
        # like any other trigger.
        summary = _reaction("\N{EYES}")
        assert push_tailer.skip_reason(summary, self.POLICY) is None

    def test_the_backchannel_never_wakes(self, monkeypatch):
        # A reaction there is the principal annotating, not asking - and a
        # listed trigger is exactly the case that reads as broken without
        # the reason, since the policy plainly names the emoji.
        monkeypatch.setattr(push_tailer, "BACKCHANNEL_ROOM_ID", BACKCHANNEL)
        summary = _reaction(room_id=BACKCHANNEL)
        assert push_tailer.skip_reason(summary, self.POLICY) == "backchannel"


class TestSummary:
    def test_carries_the_message_the_session_must_answer(self):
        # event_id is the reaction; target_event_id is the message under it.
        summary = _reaction()
        assert summary["event_id"] == "$reaction"
        assert summary["target_event_id"] == "$message"
        assert summary["key"] == "\N{LADY BEETLE}"
        assert summary["branch_type"] == "reaction"


class TestPolicyFile:
    def test_absent_file_reads_as_no_triggers(self, tmp_path, monkeypatch):
        monkeypatch.setattr(push_tailer, "WAKE_POLICY", str(tmp_path / "nope.json"))
        assert push_tailer.read_wake_policy() == {}

    def test_unparseable_file_reads_as_no_triggers(self, tmp_path, monkeypatch):
        path = tmp_path / "wake_policy.json"
        path.write_text("{ not json")
        monkeypatch.setattr(push_tailer, "WAKE_POLICY", str(path))
        assert push_tailer.read_wake_policy() == {}

    def test_a_policy_is_read_fresh(self, tmp_path, monkeypatch):
        path = tmp_path / "wake_policy.json"
        monkeypatch.setattr(push_tailer, "WAKE_POLICY", str(path))
        path.write_text(json.dumps({"trigger_emojis": ["\N{LADY BEETLE}"]}))
        assert push_tailer.read_wake_policy()["trigger_emojis"] == ["\N{LADY BEETLE}"]
        # Edited while the tailer runs: the next push sees the new list.
        path.write_text(json.dumps({"trigger_emojis": ["\N{FIRE}"]}))
        assert push_tailer.read_wake_policy()["trigger_emojis"] == ["\N{FIRE}"]


class TestDispatch:
    """on_push routes a reaction branch to the reaction gate, not the message
    path - where it would arrive with empty text and be judged on addressing."""

    def _push(self, payload):
        return {"data": {"body": json.dumps(payload)}}

    def _run(self, tmp_path, monkeypatch, key):
        inbox = tmp_path / "inbox.jsonl"
        monkeypatch.setattr(push_tailer, "STATE_DIR", str(tmp_path))
        monkeypatch.setattr(push_tailer, "INBOX", str(inbox))
        monkeypatch.setattr(push_tailer, "SEEN", str(tmp_path / "seen.json"))
        monkeypatch.setattr(push_tailer, "BACKCHANNEL_ROOM_ID", BACKCHANNEL)
        monkeypatch.setattr(push_tailer, "_seen", set())
        monkeypatch.setattr(
            push_tailer,
            "read_wake_policy",
            lambda: {"trigger_emojis": ["\N{LADY BEETLE}"]},
        )
        payload = {
            "room_id": ROOM,
            "event_id": "$reaction",
            "branch": {
                "type": "reaction",
                "key": key,
                "target_event_id": "$message",
                "sender": "Someone",
                "channel": "bugs",
            },
        }
        push_tailer.on_push(self._push(payload), "pid-1")
        if not inbox.exists():
            return []
        return [json.loads(line) for line in inbox.read_text().splitlines()]

    def test_a_trigger_reaction_reaches_the_inbox(self, tmp_path, monkeypatch):
        records = self._run(tmp_path, monkeypatch, "\N{LADY BEETLE}")
        assert len(records) == 1
        assert records[0]["branch_type"] == "reaction"
        assert records[0]["target_event_id"] == "$message"

    def test_an_unlisted_reaction_files_nothing(self, tmp_path, monkeypatch):
        assert self._run(tmp_path, monkeypatch, "\N{PARTY POPPER}") == []


class TestReasonOrdering:
    """An override is only worth naming when it blocked something that would
    otherwise have woken the agent. An emoji nobody configured did not wake
    for that reason, wherever it arrived - saying "backchannel" there implies
    it would have worked in another channel, which is false."""

    POLICY = {"trigger_emojis": ["\N{LADY BEETLE}"]}

    def test_unconfigured_emoji_in_the_backchannel_is_not_a_trigger(self, monkeypatch):
        monkeypatch.setattr(push_tailer, "BACKCHANNEL_ROOM_ID", BACKCHANNEL)
        summary = _reaction("\N{HEAVY BLACK HEART}", room_id=BACKCHANNEL)
        assert push_tailer.skip_reason(summary, self.POLICY) == "not_a_trigger"

    def test_configured_emoji_in_the_backchannel_names_the_room(self, monkeypatch):
        monkeypatch.setattr(push_tailer, "BACKCHANNEL_ROOM_ID", BACKCHANNEL)
        summary = _reaction("\N{LADY BEETLE}", room_id=BACKCHANNEL)
        assert push_tailer.skip_reason(summary, self.POLICY) == "backchannel"

    def test_unconfigured_emoji_from_self_is_not_a_trigger(self):
        summary = _reaction("\N{HEAVY BLACK HEART}", is_from_self=True)
        assert push_tailer.skip_reason(summary, self.POLICY) == "not_a_trigger"
