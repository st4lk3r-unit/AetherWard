from __future__ import annotations

import base64
import json
import os
import threading
import time
from typing import Callable, Optional

from .base import ScanMode
from ..signal.frame import Frame
from ..signal.observation import Observation
from ..signal.source import SignalProperties, SignalSource


def _json_safe(value):
    """Return a JSON-serialisable copy of backend metadata."""
    if isinstance(value, bytes):
        return {'encoding': 'hex', 'data': value.hex()}
    if isinstance(value, bytearray):
        return {'encoding': 'hex', 'data': bytes(value).hex()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def _wifi_channel_frequency_hz(channel: int) -> Optional[float]:
    """Best-effort WiFi channel → centre frequency mapping."""
    try:
        ch = int(channel)
    except (TypeError, ValueError):
        return None
    if 1 <= ch <= 13:
        return float((2412 + (ch - 1) * 5) * 1_000_000)
    if ch == 14:
        return 2484_000_000.0
    if 32 <= ch <= 177:
        return float((5000 + ch * 5) * 1_000_000)
    if 1 <= ch <= 233:  # 6 GHz PSC/non-PSC channel numbering
        return float((5950 + ch * 5) * 1_000_000)
    return None


class WardriverMode(ScanMode):
    """
    Mode 1 — Wardriving.

    One or more antennas scan RF channels and correlate captures with GPS.
    Channels are assigned to antennas that cover the channel frequency.  When
    no frequency-aware match is possible, the mode falls back to balanced
    round-robin assignment so every channel still gets covered.

    Config keys:
        channels          list[int]       channels to scan (default: 1-13)
        hop_interval      float           seconds per channel dwell (default: 0.1)
        gps_backend       GPSBackend      if set, polls GPS and updates the array
        gps_interval      float           GPS poll interval, seconds (default: 1.0)
        output_path       str             write JSONL observations here (optional)
        store_raw_frames  bool            store raw frame bytes as hex (default: True)
        on_source         callable        called when a new SignalSource appears
        on_observation    callable        called for every Observation
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
        self._store_raw_frames: bool                  = bool(config.get('store_raw_frames', True))
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
        self._channel_map = {a.id: [] for a in ants}
        if not ants:
            return

        chans = list(self._channels)
        if not chans:
            return

        # Single-channel capture is intentionally broadcast to all receivers;
        # useful for long dwell or later TDOA-style correlation.
        if len(chans) == 1:
            for ant in ants:
                self._channel_map[ant.id] = list(chans)
            return

        for ch in chans:
            freq = _wifi_channel_frequency_hz(ch)
            if freq is None:
                eligible = ants
            else:
                eligible = []
                for ant in ants:
                    covers = getattr(ant, 'covers_frequency', None)
                    try:
                        if covers is None or covers(freq):
                            eligible.append(ant)
                    except Exception:
                        eligible.append(ant)
                if not eligible:
                    eligible = ants

            # Balanced no-overlap assignment within the eligible antenna set.
            ant = min(eligible, key=lambda a: (len(self._channel_map.get(a.id, [])), a.id))
            self._channel_map.setdefault(ant.id, []).append(ch)

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
                    identifier=(frame.metadata.get('identifier') or
                                frame.metadata.get('bssid') or
                                frame.metadata.get('addr2')),
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
        ident = (frame.metadata.get('identifier') or frame.metadata.get('bssid') or
                 frame.metadata.get('addr2') or '')
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
        frame = obs.frame
        gps = obs.array_absolute
        meta = _json_safe(dict(frame.metadata or {}))
        rec: dict = {
            't':         frame.timestamp,
            'freq':      frame.frequency,
            'bw':        frame.bandwidth,
            'rssi':      obs.rssi,
            'ant':       obs.antenna_id,
            'frame_len': len(frame.data),
            'metadata':  meta,
        }
        if frame.sample_rate:
            rec['sample_rate'] = frame.sample_rate

        # Keep common fields at top-level for compact tools and legacy readers,
        # while preserving the full backend metadata dict above.
        for out_key, meta_key in (
            ('protocol', 'protocol'), ('id', 'identifier'), ('bssid', 'bssid'),
            ('ssid', 'ssid'), ('auth_mode', 'auth_mode'), ('security', 'security'),
            ('channel', 'channel'), ('band', 'band'), ('frame_type', 'frame_type'),
            ('frame_subtype', 'frame_subtype'), ('privacy', 'privacy'),
            ('akm_suites', 'akm_suites'), ('pairwise_ciphers', 'pairwise_ciphers'),
            ('group_cipher', 'group_cipher'), ('beacon_interval', 'beacon_interval'),
            ('capabilities', 'capabilities'),
        ):
            value = meta.get(meta_key)
            if value not in (None, '', [], {}):
                rec[out_key] = value
        if 'id' not in rec and rec.get('bssid'):
            rec['id'] = rec['bssid']

        if self._store_raw_frames and frame.data:
            # Hex is larger than base64 but easier to grep, diff, and replay.
            rec['raw_frame_hex'] = frame.data.hex()
            rec['raw_frame_b64'] = base64.b64encode(frame.data).decode('ascii')

        if gps is not None and gps.is_valid():
            rec['lat'] = gps.lat
            rec['lon'] = gps.lon
            rec['alt'] = gps.alt
            rec['fix'] = int(gps.fix_type)
        with self._out_lock:
            self._out_file.write(json.dumps(rec, separators=(',', ':')) + '\n')

    # ── Read access ──────────────────────────────────────────────────────

    @property
    def sources(self) -> dict[str, SignalSource]:
        with self._lock:
            return dict(self._sources)
