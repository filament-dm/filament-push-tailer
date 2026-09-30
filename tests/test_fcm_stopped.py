"""The tailer exits once its FCM client has stopped listening for good.

The library reconnects after a dropped socket on its own, but after enough
consecutive failures it terminates its tasks and never comes back. Nothing
else in the process would notice - the heartbeat keeps the agent looking
online while no push can reach it - so the tailer exits instead and leaves
the restart to whatever supervises it.

The tailer reads its config at import and exits without it, so the two
required vars are set before the import below.
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest
from firebase_messaging.fcmpushclient import FcmPushClientRunState

os.environ.setdefault("FILAMENT_CONNECT_TOKEN", "fmcp_test")
os.environ.setdefault("FILAMENT_HOMESERVER", "https://example.invalid")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import push_tailer  # noqa: E402


class FakeClock:
    """Drives the watcher: each awaited sleep advances a fake monotonic clock
    and steps the client through a scripted sequence of run states. Running
    off the end of the script stops the test instead of looping forever."""

    def __init__(self, fcm, states):
        self.fcm = fcm
        self.states = list(states)
        self.now = 0.0

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        if not self.states:
            raise StopAsyncIteration("watcher outlived the script")
        self.now += seconds
        self.fcm.run_state = self.states.pop(0)


class FakeFcm:
    run_state = FcmPushClientRunState.STARTED


def run_watcher(monkeypatch, states):
    fcm = FakeFcm()
    clock = FakeClock(fcm, states)
    monkeypatch.setattr(push_tailer.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(push_tailer.asyncio, "sleep", clock.sleep)
    return asyncio.run(push_tailer._exit_when_fcm_stops(fcm))


STARTED = FcmPushClientRunState.STARTED
STOPPED = FcmPushClientRunState.STOPPED
STOPPING = FcmPushClientRunState.STOPPING
GRACE_TICKS = push_tailer.FCM_STOPPED_GRACE_SECONDS // 5


def test_a_client_that_stays_stopped_ends_the_process(monkeypatch):
    with pytest.raises(SystemExit, match="FCM client stopped listening"):
        run_watcher(monkeypatch, [STOPPED] * (GRACE_TICKS + 1))


def test_stopping_counts_the_same_as_stopped(monkeypatch):
    with pytest.raises(SystemExit):
        run_watcher(monkeypatch, [STOPPING] * (GRACE_TICKS + 1))


def test_a_running_client_is_left_alone(monkeypatch):
    with pytest.raises(StopAsyncIteration):
        run_watcher(monkeypatch, [STARTED] * (GRACE_TICKS * 3))


def test_a_reset_that_recovers_is_not_a_stop(monkeypatch):
    # RESETTING is what a dropped socket looks like while the library
    # reconnects; it must never count toward the grace period.
    with pytest.raises(StopAsyncIteration):
        run_watcher(
            monkeypatch,
            [FcmPushClientRunState.RESETTING] * (GRACE_TICKS * 3) + [STARTED],
        )


def test_a_brief_stop_that_comes_back_resets_the_clock(monkeypatch):
    # Stopped for less than the grace period, then started again: the timer
    # starts over, so a second short stop does not add up to an exit.
    script = [STOPPED] * (GRACE_TICKS - 1) + [STARTED] + [STOPPED] * (GRACE_TICKS - 1) + [STARTED]
    with pytest.raises(StopAsyncIteration):
        run_watcher(monkeypatch, script)
