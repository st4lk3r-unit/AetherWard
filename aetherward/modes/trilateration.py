from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from .base import ScanMode
from ..signal.frame import Frame
from ..signal.observation import Observation
from ..signal.source import SignalProperties, SignalSource

_log = logging.getLogger(__name__)


class TrilaterationMode(ScanMode):
    """
    Mode 2 — TDOA trilateration.

    All antennas are tuned to the same frequency/channel and listen
    simultaneously.  When the same transmission is received at multiple
    antennas, the small time differences (TDOA) are fed to a Gauss-Newton
    iterative solver to compute the source position in the local ENU frame.
    That result is then projected to absolute coordinates via the GPS anchor.

    Accuracy note: 1 ns timing error ≈ 30 cm position error.
    Meaningful accuracy requires PPS-disciplined or GPSDO clocks.

    Config keys:
        frequency          float     Hz — all antennas tune here
        channel            int       alternative to frequency (WiFi)
        reference_antenna  str       antenna ID used as t=0 reference
        correlation_window float     max expected TDOA, seconds (default 1 ms)
        group_timeout      float     discard incomplete groups after this, seconds (default 50 ms)
        on_source          callable  called when a source position is updated
    """
    name = 'trilateration'

    def __init__(self, array, config: dict):
        super().__init__(array, config)
        self._sources:       dict[str, SignalSource]      = {}
        self._pending:       dict[str, list[Observation]] = {}
        self._pending_ts:    dict[str, float]             = {}  # monotonic arrival time per group
        self._on_source:     Optional[Callable]           = config.get('on_source')
        self._ref_id:        str                          = config.get(
            'reference_antenna',
            array.antennas[0].id if array.antennas else '',
        )
        self._window:        float = config.get('correlation_window', 1e-3)
        self._group_timeout: float = config.get('group_timeout', 0.05)
        self._lock = threading.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        freq    = self.config.get('frequency')
        channel = self.config.get('channel')
        for ant in self.array.antennas:
            if ant.backend is None:
                continue
            if freq is not None:
                ant.backend.set_frequency(freq)
            elif channel is not None:
                ant.backend.set_channel(channel)
            ant.backend.start_capture(
                lambda frame, aid=ant.id: self._handle(frame, aid)
            )
        t = threading.Thread(target=self._cleanup_worker, daemon=True)
        t.start()

    def stop(self) -> None:
        self._running = False
        for ant in self.array.antennas:
            if ant.backend:
                ant.backend.stop_capture()

    def on_frame(self, frame: Frame) -> None:
        self._handle(frame, frame.antenna_id)

    # ── Frame correlation ────────────────────────────────────────────────

    def _handle(self, frame: Frame, antenna_id: str) -> None:
        obs = Observation(
            frame=frame,
            antenna_id=antenna_id,
            rssi=frame.rssi,
            array_absolute=self.array.absolute_position,
        )
        ready_group = None
        with self._lock:
            key = self._assign_key(frame)
            group = self._pending.setdefault(key, [])
            self._pending_ts.setdefault(key, time.monotonic())
            group.append(obs)

            # Warn when TDOA between this frame and the reference antenna
            # greatly exceeds the correlation window — likely a sync problem.
            if self._ref_id:
                ref_obs = next(
                    (o for o in group if o.antenna_id == self._ref_id), None
                )
                if ref_obs is not None:
                    delta = abs(frame.timestamp - ref_obs.frame.timestamp)
                    if delta > self._window * 100:
                        _log.warning(
                            'TDOA %.3f ms far exceeds window %.3f ms — '
                            'clocks may not be synchronised',
                            delta * 1e3, self._window * 1e3,
                        )

            if len(group) >= self.array.n:
                ready_group = list(group)
                self._pending.pop(key, None)
                self._pending_ts.pop(key, None)

        # Solve outside the lock so frame ingestion is not blocked during GN.
        if ready_group is not None:
            self._solve(ready_group)

    def _assign_key(self, frame: Frame) -> str:
        """
        Assign a pending-group key for this frame.

        Prefers an existing partial group in the adjacent (previous) time
        bucket before creating a new entry.  This prevents a cross-boundary
        split when a frame's timestamp falls right on a bucket edge.
        """
        if 'frame_hash' in frame.metadata:
            return str(frame.metadata['frame_hash'])

        bucket  = int(frame.timestamp / self._window)
        freq    = f"{frame.frequency:.0f}"
        primary  = f"{freq}:{bucket}"
        adjacent = f"{freq}:{bucket - 1}"

        if adjacent in self._pending and primary not in self._pending:
            return adjacent
        return primary

    def _cleanup_worker(self) -> None:
        """Purge pending groups that never accumulated all antennas' frames."""
        interval = max(0.005, self._group_timeout / 4)
        while self._running:
            time.sleep(interval)
            now = time.monotonic()
            with self._lock:
                stale = [
                    k for k, ts in self._pending_ts.items()
                    if now - ts > self._group_timeout
                ]
                for k in stale:
                    n_got = len(self._pending.get(k, []))
                    _log.debug(
                        'Dropping incomplete group %s (%d/%d antennas received)',
                        k, n_got, self.array.n,
                    )
                    self._pending.pop(k, None)
                    self._pending_ts.pop(k, None)

    # ── Solve ────────────────────────────────────────────────────────────

    def _solve(self, observations: list[Observation]) -> None:
        from ..core import tdoa_solve
        ref_obs = next(
            (o for o in observations if o.antenna_id == self._ref_id),
            observations[0],
        )
        measurements = [
            {
                'antenna_id': o.antenna_id,
                'tdoa':       o.frame.timestamp - ref_obs.frame.timestamp,
                'rssi':       o.rssi,
                'timestamp':  o.frame.timestamp,
            }
            for o in observations
        ]
        result = tdoa_solve(self.array, measurements, ref_obs.antenna_id)
        if not result or not result.get('valid'):
            return

        key   = self._assign_key(ref_obs.frame)
        props = SignalProperties(frequency=ref_obs.frame.frequency)
        src   = self._sources.setdefault(key, SignalSource(signal=props))

        for obs, meas in zip(observations, measurements):
            obs.tdoa = meas['tdoa']
            src.add_observation(obs)

        src.position_relative = result['position_relative']
        src.position_absolute = result['position_absolute']

        if self._on_source:
            self._on_source(src)

    @property
    def sources(self) -> dict[str, SignalSource]:
        with self._lock:
            return dict(self._sources)
