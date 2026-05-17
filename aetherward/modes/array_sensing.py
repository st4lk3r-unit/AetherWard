from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from .base import ScanMode
from ..signal.frame import Frame


@dataclass
class SensingEvent:
    """Output event from ArraySensingMode."""
    type: str                               # 'presence', 'motion', 'absence'
    antenna_id: str
    variance: float                         # smoothed variance, the trigger metric
    direction: Optional[np.ndarray] = None  # unit ENU vector; None when single-antenna
    timestamp: float = 0.0
    metadata: dict = field(default_factory=dict)


class ArraySensingMode(ScanMode):
    """
    Mode 3 — Passive array sensing (RF absorption / WiFi sensing).

    Monitors changes in Channel State Information (CSI) or RSSI to infer
    spatial events: presence, motion, or absence.  Multiple antennas
    additionally yield a crude direction estimate.

    Hardware: a NIC that exports CSI (Intel 5300 + linux-80211n-csitool,
    Nexmon-patched Broadcom, etc.).  RSSI-only fallback is supported.

    Pipeline per antenna:
        1. Collect frames into a fixed-length rolling window (deque).
        2. During the first `calibration_frames` frames: build baseline
           variance — no events fired.
        3. After calibration: apply EMA smoothing, then run state machine:
             idle  → active  when smoothed_excess > sensitivity   → 'presence'/'motion'
             active → idle   when smoothed_excess < sensitivity × hysteresis → 'absence'
        4. When ≥2 antennas are calibrated, estimate direction toward the
           most-excited antenna relative to the array centroid.

    Config keys:
        frequency           float     Hz — channel to monitor
        channel             int       alternative to frequency
        history_len         int       rolling window depth per antenna (default 100)
        calibration_frames  int       frames before comparison begins (default 50)
        sensitivity         float     variance increase above baseline to trigger (default 0.05)
        hysteresis          float     return-to-idle fraction of threshold (default 0.4)
        ema_alpha           float     EMA smoothing weight 0–1 (default 0.3)
        on_event            callable  called with SensingEvent on each detection
    """
    name = 'array_sensing'

    def __init__(self, array, config: dict):
        super().__init__(array, config)
        self._on_event:     Optional[Callable[[SensingEvent], None]] = config.get('on_event')
        self._history_len:  int   = config.get('history_len', 100)
        self._calib_frames: int   = config.get('calibration_frames', 50)
        self._sensitivity:  float = config.get('sensitivity', 0.05)
        self._hysteresis:   float = config.get('hysteresis', 0.4)
        self._ema_alpha:    float = config.get('ema_alpha', 0.3)

        self._csi_history:  dict[str, deque] = {}
        self._rssi_history: dict[str, deque] = {}
        self._calib_vars:   dict[str, list]  = {}  # accumulator during calibration phase
        self._baseline:     dict[str, float] = {}  # mean variance after calibration
        self._ema_var:      dict[str, float] = {}  # smoothed current variance
        self._state:        dict[str, str]   = {}  # 'idle' | 'active'
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

    def stop(self) -> None:
        self._running = False
        for ant in self.array.antennas:
            if ant.backend:
                ant.backend.stop_capture()

    def on_frame(self, frame: Frame) -> None:
        self._handle(frame, frame.antenna_id)

    # ── Sensing pipeline ─────────────────────────────────────────────────

    def _handle(self, frame: Frame, antenna_id: str) -> None:
        csi = frame.metadata.get('csi')
        if csi is not None:
            self._update_csi(antenna_id, np.asarray(csi, dtype=complex), frame.timestamp)
        else:
            self._update_rssi(antenna_id, frame.rssi, frame.timestamp)

    def _update_csi(self, antenna_id: str,
                    csi: np.ndarray, timestamp: float) -> None:
        with self._lock:
            hist = self._csi_history.setdefault(
                antenna_id, deque(maxlen=self._history_len)
            )
            hist.append(csi)
            if len(hist) < 2:
                return
            amps = np.array([np.abs(h) for h in hist])
            var  = float(np.mean(np.var(amps, axis=0)))
            self._process(antenna_id, var, timestamp, mode='csi')

    def _update_rssi(self, antenna_id: str, rssi: float, timestamp: float) -> None:
        with self._lock:
            hist = self._rssi_history.setdefault(
                antenna_id, deque(maxlen=self._history_len)
            )
            hist.append(rssi)
            if len(hist) < 10:
                return
            var = float(np.var(np.array(hist)))
            self._process(antenna_id, var, timestamp, mode='rssi')

    def _process(self, antenna_id: str, var: float,
                 timestamp: float, mode: str) -> None:
        # EMA smoothing
        alpha    = self._ema_alpha
        prev     = self._ema_var.get(antenna_id, var)
        smoothed = alpha * var + (1.0 - alpha) * prev
        self._ema_var[antenna_id] = smoothed

        # Calibration phase: collect baseline samples, hold off on events
        if antenna_id not in self._baseline:
            samples = self._calib_vars.setdefault(antenna_id, [])
            samples.append(smoothed)
            if len(samples) >= self._calib_frames:
                self._baseline[antenna_id] = float(np.mean(samples))
                self._state[antenna_id]    = 'idle'
                del self._calib_vars[antenna_id]
            return

        baseline  = self._baseline[antenna_id]
        excess    = smoothed - baseline
        threshold = self._sensitivity
        state     = self._state.get(antenna_id, 'idle')

        if state == 'idle' and excess > threshold:
            self._state[antenna_id] = 'active'
            self._fire(antenna_id,
                       'presence' if mode == 'csi' else 'motion',
                       smoothed, timestamp)

        elif state == 'active' and excess < threshold * self._hysteresis:
            self._state[antenna_id] = 'idle'
            self._fire(antenna_id, 'absence', smoothed, timestamp)

    def _fire(self, antenna_id: str, event_type: str,
              variance: float, timestamp: float) -> None:
        if self._on_event is None:
            return
        self._on_event(SensingEvent(
            type=event_type,
            antenna_id=antenna_id,
            variance=variance,
            direction=self._estimate_direction(antenna_id),
            timestamp=timestamp,
        ))

    # ── Direction estimation ─────────────────────────────────────────────

    def _estimate_direction(self, trigger_id: str) -> Optional[np.ndarray]:
        """
        Unit ENU vector from the array centroid toward the antenna with the
        highest smoothed variance.  Requires ≥2 calibrated antennas.

        This is a coarse estimate: it points toward the antenna most affected
        by the event, not the emitter itself.  Accuracy improves with antenna
        count and wider array aperture.
        """
        calibrated = {
            aid: v
            for aid, v in self._ema_var.items()
            if aid in self._baseline and self.array.get(aid) is not None
        }
        if len(calibrated) < 2:
            return None

        positions = np.array([
            self.array.get(aid).position.as_array()   # type: ignore[union-attr]
            for aid in calibrated
        ])
        centroid = positions.mean(axis=0)

        best_id  = max(calibrated, key=calibrated.__getitem__)
        best_ant = self.array.get(best_id)
        if best_ant is None:
            return None

        v    = best_ant.position.as_array() - centroid
        norm = float(np.linalg.norm(v))
        return (v / norm) if norm > 1e-9 else None
