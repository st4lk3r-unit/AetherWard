from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable, Optional

from .base import ScanMode
from ..signal.frame import Frame
from ..signal.observation import Observation
from ..signal.source import SignalProperties, SignalSource
from ..session import make_observation_record


class WardriverMode(ScanMode):
    """
    Mode 1 — Wardriving.

    One or more antennas scan RF channels and correlate captures with GPS.
    Channels are partitioned across antennas: every channel is covered by
    exactly one antenna — no gaps, no duplicates, remainder distributed.

    Config keys:
        channels         list[int]       channels to scan (default: 1-13)
        hop_interval     float           seconds per channel dwell (default: 0.1)
        gps_backend      GPSBackend      if set, polls GPS and updates the array
        gps_interval     float           GPS poll interval, seconds (default: 1.0)
        output_path      str             write JSONL observations here (optional)
        on_source        callable        called when a new SignalSource appears
        on_observation   callable        called for every Observation
    """
    name = 'wardriver'

    def __init__(self, array, config: dict):
        super().__init__(array, config)
        self._sources:        dict[str, SignalSource] = {}
        self._on_source:      Optional[Callable]      = config.get('on_source')
        self._on_observation: Optional[Callable]      = config.get('on_observation')
        self._channels:       list[int]               = config.get('channels', list(range(1, 14)))
        self._hop_interval:   float                   = config.get('hop_interval', 0.1)
        self._gps_backend                             = config.get('gps_backend')
        self._gps_interval:   float                   = config.get('gps_interval', 1.0)
        self._output_path:    Optional[str]           = config.get('output_path')
        self._session_id:     str                     = str(config.get('session_id') or '')
        self._channel_map:    dict[str, list[int]]    = {}
        self._lock     = threading.Lock()
        self._out_lock = threading.Lock()
        self._out_file = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self._assign_channels()

        if self._output_path:
            path = os.path.expanduser(self._output_path)
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._out_file = open(path, 'a', buffering=1)

        for ant in self.array.antennas:
            if ant.backend is None:
                continue
            ant.backend.start_capture(
                lambda frame, aid=ant.id: self._handle(frame, aid)
            )
            t = threading.Thread(target=self._hop_worker, args=(ant,), daemon=True)
            t.start()

        if self._gps_backend is not None:
            g = threading.Thread(target=self._gps_worker, daemon=True)
            g.start()

    def stop(self) -> None:
        self._running = False
        for ant in self.array.antennas:
            if ant.backend:
                ant.backend.stop_capture()
        if self._out_file:
            self._out_file.close()
            self._out_file = None

    def on_frame(self, frame: Frame) -> None:
        self._handle(frame, frame.antenna_id)

    # ── Channel assignment ───────────────────────────────────────────────

    def _assign_channels(self) -> None:
        ants = [a for a in self.array.antennas if a.backend is not None]
        if not ants:
            return

        # Filter channels per antenna/backend frequency range before splitting.
        # This avoids assigning 2.4 GHz channels to a 5 GHz-only adapter, or
        # channels outside a backend's declared capabilities.
        eligible: dict[str, list[int]] = {a.id: self._eligible_channels(a) for a in ants}
        for ant in ants:
            if not eligible[ant.id]:
                self._channel_map[ant.id] = []

        for ch in self._channels:
            candidates = [a for a in ants if ch in eligible.get(a.id, [])]
            if not candidates:
                continue
            # Assign to the currently least-loaded eligible antenna.
            ant = min(candidates, key=lambda a: len(self._channel_map.get(a.id, [])))
            self._channel_map.setdefault(ant.id, []).append(ch)

        # Edge case: more antennas than eligible channels.  Keep all antennas
        # active by letting empty antennas scan their eligible set, matching the
        # historical "single channel shared by all" behaviour.
        for ant in ants:
            if not self._channel_map.get(ant.id) and eligible.get(ant.id):
                self._channel_map[ant.id] = list(eligible[ant.id])

    @staticmethod
    def _wifi_channel_frequency_hz(channel: int) -> Optional[float]:
        if 1 <= channel <= 13:
            return float((2412 + (channel - 1) * 5) * 1_000_000)
        if channel == 14:
            return 2484e6
        if 32 <= channel <= 177:
            return float((5000 + channel * 5) * 1_000_000)
        return None

    def _eligible_channels(self, ant) -> list[int]:
        out: list[int] = []
        try:
            import numbers
            caps = ant.backend.capabilities() if ant.backend else None
            fmin_raw = getattr(caps, 'frequency_min')
            fmax_raw = getattr(caps, 'frequency_max')
            hop_raw = getattr(caps, 'supports_channel_hop')
            if not isinstance(fmin_raw, numbers.Real) or not isinstance(fmax_raw, numbers.Real):
                raise TypeError
            if not isinstance(hop_raw, bool):
                raise TypeError
            fmin = float(fmin_raw)
            fmax = float(fmax_raw)
            hop = hop_raw
        except Exception:
            caps = None
            fmin, fmax, hop = float('-inf'), float('inf'), True
        for ch in self._channels:
            hz = self._wifi_channel_frequency_hz(int(ch))
            if hz is None:
                continue
            if not ant.covers_frequency(hz):
                continue
            if caps is not None:
                if hz < fmin or hz > fmax:
                    continue
                if not hop:
                    continue
            out.append(int(ch))
        return out

    def _hop_worker(self, ant) -> None:
        channels = self._channel_map.get(ant.id, [])
        idx = 0
        while self._running:
            if channels:
                try:
                    ant.backend.set_channel(channels[idx % len(channels)])
                except Exception:
                    pass
                idx += 1
            time.sleep(self._hop_interval)

    def _gps_worker(self) -> None:
        while self._running:
            try:
                pos = self._gps_backend.get_position()
                if pos is not None:
                    self.array.update_position(pos)
            except Exception:
                pass
            time.sleep(self._gps_interval)

    # ── Frame handling ───────────────────────────────────────────────────

    def _handle(self, frame: Frame, antenna_id: str) -> None:
        obs = Observation(
            frame=frame,
            antenna_id=antenna_id,
            rssi=frame.rssi,
            array_absolute=self.array.absolute_position,
        )
        key = self._source_key(frame)
        with self._lock:
            if key not in self._sources:
                props = SignalProperties(
                    frequency=frame.frequency,
                    protocol=frame.metadata.get('protocol'),
                    identifier=frame.metadata.get('identifier'),
                )
                self._sources[key] = SignalSource(signal=props)
                if self._on_source:
                    self._on_source(self._sources[key])
            self._sources[key].add_observation(obs)

        if self._on_observation:
            self._on_observation(obs)

        self._write_obs(obs)

    @staticmethod
    def _source_key(frame: Frame) -> str:
        ident = frame.metadata.get('identifier') or ''
        if ident:
            return f"{frame.frequency:.0f}:{ident}"
        # Anonymous source: bucket by frequency + 1-second time slot so
        # different anonymous emitters on the same channel don't all merge.
        bucket = int(frame.timestamp)
        return f"{frame.frequency:.0f}:anon:{bucket}"

    # ── Output ───────────────────────────────────────────────────────────

    def _write_obs(self, obs: Observation) -> None:
        if self._out_file is None:
            return
        rec = make_observation_record(
            frame=obs.frame,
            antenna_id=obs.antenna_id,
            gps=obs.array_absolute,
            session_id=self._session_id,
            mode=self.name,
        )
        with self._out_lock:
            self._out_file.write(json.dumps(rec) + '\n')

    # ── Read access ──────────────────────────────────────────────────────

    @property
    def sources(self) -> dict[str, SignalSource]:
        with self._lock:
            return dict(self._sources)
