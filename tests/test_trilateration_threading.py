"""
Threading tests for TrilaterationMode.

Verifies:
  - A complete group triggers exactly one _solve call.
  - _solve is called OUTSIDE the lock (regression for lock-held-during-solve fix).
  - Concurrent frame injection from N threads completes without deadlock or drops.
  - Stale incomplete groups are removed by the cleanup worker.
  - A partial group (< n_antennas frames) never triggers _solve.
  - Desynchronised-clock warning fires when TDOA greatly exceeds the window.
"""
from __future__ import annotations

import logging
import threading
import time

from aetherward.antenna.antenna import Antenna
from aetherward.antenna.array import AntennaArray
from aetherward.modes.trilateration import TrilaterationMode
from aetherward.orientation.quaternion import Orientation
from aetherward.position.relative import RelativePosition
from aetherward.signal.frame import Frame


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_array(n: int = 4) -> AntennaArray:
    arr = AntennaArray(id='test-array')
    for i in range(n):
        ant = Antenna(
            id=f'ant{i}',
            position=RelativePosition(x=float(i), y=0.0, z=0.0),
            orientation=Orientation.identity(),
        )
        arr.add(ant)
    return arr


def _frame(antenna_id: str, timestamp: float, freq: float = 2.412e9,
           frame_hash: str | None = None) -> Frame:
    f = Frame(
        data=b'\x00' * 8,
        frequency=freq,
        bandwidth=20e6,
        timestamp=timestamp,
        rssi=-60.0,
        antenna_id=antenna_id,
    )
    if frame_hash is not None:
        f.metadata['frame_hash'] = frame_hash
    return f


def _mode(n: int = 4, **cfg) -> TrilaterationMode:
    arr = _make_array(n)
    defaults = {'reference_antenna': 'ant0'}
    defaults.update(cfg)
    mode = TrilaterationMode(arr, defaults)
    mode._running = True
    return mode


# ── Group completion ──────────────────────────────────────────────────────────

class TestGroupCompletion:
    def test_complete_group_triggers_solve_once(self):
        mode = _mode(4)
        calls = []
        mode._solve = lambda obs: calls.append(len(obs))

        ts = time.time()
        for i in range(4):
            mode._handle(_frame(f'ant{i}', ts, frame_hash='g1'), f'ant{i}')

        assert len(calls) == 1
        assert calls[0] == 4

    def test_partial_group_never_solved(self):
        mode = _mode(4)
        calls = []
        mode._solve = lambda obs: calls.append(obs)

        ts = time.time()
        for i in range(3):      # only 3 of 4 antennas
            mode._handle(_frame(f'ant{i}', ts, frame_hash='partial'), f'ant{i}')

        assert calls == []

    def test_two_independent_groups_each_solved_once(self):
        mode = _mode(4)
        calls: list[str] = []
        mode._solve = lambda obs: calls.append(obs[0].frame.metadata['frame_hash'])

        ts = time.time()
        for grp in ('groupA', 'groupB'):
            for i in range(4):
                mode._handle(_frame(f'ant{i}', ts, frame_hash=grp), f'ant{i}')

        assert sorted(calls) == ['groupA', 'groupB']

    def test_pending_cleared_after_complete(self):
        mode = _mode(4)
        mode._solve = lambda obs: None

        ts = time.time()
        for i in range(4):
            mode._handle(_frame(f'ant{i}', ts, frame_hash='done'), f'ant{i}')

        with mode._lock:
            assert 'done' not in mode._pending


# ── Lock is NOT held during solve ─────────────────────────────────────────────

class TestLockNotHeldDuringSolve:
    def test_solve_called_outside_lock(self):
        """
        _solve must run with self._lock released.
        If it ran inside the lock, a non-blocking acquire from within _solve
        would fail (return False).
        """
        mode = _mode(4)
        lock_was_free: list[bool] = []

        def spy(observations):
            acquired = mode._lock.acquire(blocking=False)
            lock_was_free.append(acquired)
            if acquired:
                mode._lock.release()

        mode._solve = spy

        ts = time.time()
        for i in range(4):
            mode._handle(_frame(f'ant{i}', ts, frame_hash='locktest'), f'ant{i}')

        assert lock_was_free == [True], (
            "_solve was called while the lock was held — fix regressed"
        )

    def test_lock_free_even_under_concurrent_injection(self):
        """
        Same lock check under concurrent load from multiple threads.
        """
        mode = _mode(4)
        violations: list[bool] = []

        def spy(observations):
            acquired = mode._lock.acquire(blocking=False)
            if not acquired:
                violations.append(True)
            else:
                mode._lock.release()

        mode._solve = spy

        ts = time.time()
        threads = [
            threading.Thread(
                target=mode._handle,
                args=(_frame(f'ant{i}', ts, frame_hash='concurrent_lock'), f'ant{i}'),
            )
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        assert violations == [], "lock was held during _solve in concurrent scenario"


# ── Concurrent injection ──────────────────────────────────────────────────────

class TestConcurrentInjection:
    def test_no_deadlock_four_threads(self):
        mode = _mode(4)
        mode._solve = lambda obs: None

        ts = time.time()
        errors: list[Exception] = []

        def inject(i: int) -> None:
            try:
                mode._handle(_frame(f'ant{i}', ts, frame_hash='flood'), f'ant{i}')
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=inject, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        assert not any(t.is_alive() for t in threads), "deadlock detected — threads did not finish"
        assert errors == []

    def test_exactly_one_solve_per_group_under_contention(self):
        """
        Even when all four frames arrive simultaneously from four threads,
        _solve must be called exactly once per group hash.
        """
        mode = _mode(4)
        calls: list[int] = []
        mode._solve = lambda obs: calls.append(1)

        ts = time.time()
        barrier = threading.Barrier(4)

        def inject(i: int) -> None:
            barrier.wait()          # all threads start at the same instant
            mode._handle(_frame(f'ant{i}', ts, frame_hash='race'), f'ant{i}')

        threads = [threading.Thread(target=inject, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        assert sum(calls) == 1, f"Expected 1 solve call, got {sum(calls)}"

    def test_many_groups_concurrent(self):
        """
        Eight groups injected concurrently; each should be solved exactly once.
        """
        n_groups = 8
        mode = _mode(4)
        calls: list[str] = []

        import threading as _th
        lock = _th.Lock()

        def spy(obs):
            with lock:
                calls.append(obs[0].frame.metadata['frame_hash'])

        mode._solve = spy

        ts = time.time()
        threads = []
        for g in range(n_groups):
            for i in range(4):
                threads.append(threading.Thread(
                    target=mode._handle,
                    args=(_frame(f'ant{i}', ts, frame_hash=f'grp{g}'), f'ant{i}'),
                ))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=4.0)

        from collections import Counter
        counts = Counter(calls)
        for g in range(n_groups):
            assert counts[f'grp{g}'] == 1, (
                f"grp{g} solved {counts[f'grp{g}']} times, expected 1"
            )


# ── Stale group cleanup ───────────────────────────────────────────────────────

class TestStaleGroupCleanup:
    def test_incomplete_group_cleaned_after_timeout(self):
        mode = _mode(4, group_timeout=0.05)
        mode._solve = lambda obs: None

        cleanup_thread = threading.Thread(target=mode._cleanup_worker, daemon=True)
        cleanup_thread.start()

        ts = time.time()
        for i in range(2):      # inject only 2 of 4
            mode._handle(_frame(f'ant{i}', ts, frame_hash='stale'), f'ant{i}')

        with mode._lock:
            assert 'stale' in mode._pending

        time.sleep(0.25)        # well past group_timeout=0.05 s
        mode._running = False
        cleanup_thread.join(timeout=1.0)

        with mode._lock:
            assert 'stale' not in mode._pending, "stale group not cleaned up"

    def test_cleanup_does_not_discard_complete_groups(self):
        """
        A group that completed (and was already solved+removed) should not
        re-appear in pending after cleanup runs.
        """
        mode = _mode(4, group_timeout=0.05)
        solved = []
        mode._solve = lambda obs: solved.append(1)

        cleanup_thread = threading.Thread(target=mode._cleanup_worker, daemon=True)
        cleanup_thread.start()

        ts = time.time()
        for i in range(4):
            mode._handle(_frame(f'ant{i}', ts, frame_hash='complete'), f'ant{i}')

        time.sleep(0.25)
        mode._running = False
        cleanup_thread.join(timeout=1.0)

        assert solved == [1]    # solved exactly once, not cleaned as stale


# ── Desynchronised-clock warning ──────────────────────────────────────────────

class TestDesyncWarning:
    def test_warning_logged_when_tdoa_far_exceeds_window(self, caplog):
        """
        When a frame arrives with a timestamp wildly different from the
        reference antenna's frame in the same group, a WARNING should appear.
        """
        mode = _mode(4, correlation_window=1e-3)   # 1 ms window

        ref_ts   = 1_000_000.0
        late_ts  = ref_ts + 1.0   # 1 second late — 1000× the window

        with caplog.at_level(logging.WARNING, logger='aetherward.modes.trilateration'):
            mode._handle(_frame('ant0', ref_ts,  frame_hash='desync'), 'ant0')
            mode._handle(_frame('ant1', late_ts, frame_hash='desync'), 'ant1')

        assert any('clocks may not be synchronised' in r.message or
                   'synchronised' in r.message
                   for r in caplog.records), (
            "expected desync warning not logged"
        )

    def test_no_warning_for_normal_tdoa(self, caplog):
        mode = _mode(4, correlation_window=1e-3)

        ref_ts  = 1_000_000.0
        near_ts = ref_ts + 50e-6   # 50 µs — well inside window

        with caplog.at_level(logging.WARNING, logger='aetherward.modes.trilateration'):
            mode._handle(_frame('ant0', ref_ts,  frame_hash='ok'), 'ant0')
            mode._handle(_frame('ant1', near_ts, frame_hash='ok'), 'ant1')

        assert not any('synchronised' in r.message for r in caplog.records)
