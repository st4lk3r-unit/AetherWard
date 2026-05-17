"""
NL80211Backend — Linux 802.11 monitor-mode capture backend.

Puts a wireless interface into monitor mode using iw/ip and captures
802.11 frames with Scapy's AsyncSniffer.  Extracts RSSI and frequency
from the radiotap header; parses beacon and probe-response frames for
BSSID and SSID.

Requires:
    scapy >= 2.5          pip install scapy
    iw + ip               iproute2 / wireless-tools (standard on Linux)
    CAP_NET_RAW + CAP_NET_ADMIN  (or run as root)
"""
from __future__ import annotations

import subprocess
import time
from typing import Callable, Optional

from aetherward.hardware.backend import BackendCapabilities, HardwareBackend
from aetherward.signal.frame import Frame


# ── Channel ↔ frequency tables ────────────────────────────────────────────────

def _build_tables() -> tuple[dict[int, int], dict[int, int]]:
    c2f: dict[int, int] = {}
    # 2.4 GHz — channels 1-13: 2412 + (ch-1)*5 MHz; ch 14 = 2484
    for ch in range(1, 14):
        c2f[ch] = 2412 + (ch - 1) * 5
    c2f[14] = 2484
    # 5 GHz — UNII-1/2/2e/3 (channels 32-177): 5000 + ch*5
    for ch in range(32, 178):
        c2f[ch] = 5000 + ch * 5
    f2c = {v: k for k, v in c2f.items()}
    return c2f, f2c


_CHAN_TO_FREQ, _FREQ_TO_CHAN = _build_tables()


# ── Backend ───────────────────────────────────────────────────────────────────

class NL80211Backend(HardwareBackend):
    """
    Linux nl80211/cfg80211 WiFi capture backend.

    Puts a wireless interface into monitor mode and captures 802.11 frames
    using Scapy's AsyncSniffer.  RSSI and channel frequency come from the
    radiotap header; beacon and probe-response frames are parsed for BSSID
    and SSID.

    Config keys:
        interface   str   wireless interface name (e.g. 'wlan0')
        restore     bool  restore managed mode on close (default True)
    """

    def __init__(self, interface: str = 'wlan0', restore: bool = True) -> None:
        self._iface   = interface
        self._restore = restore
        self._ant_id  = interface
        self._sniffer = None
        self._callback: Optional[Callable[[Frame], None]] = None

    # ── HardwareBackend interface ─────────────────────────────────────────────

    def initialize(self) -> None:
        try:
            from scapy.all import AsyncSniffer  # noqa: F401 — presence check
        except ImportError:
            raise RuntimeError(
                "NL80211Backend requires scapy: pip install scapy"
            )
        self._set_monitor(enable=True)

    def configure(self, config: dict) -> None:
        self._iface   = config.get('interface', self._iface)
        self._restore = config.get('restore', self._restore)
        self._ant_id  = config.get('antenna_id', self._iface)

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            frequency_min=2.412e9,
            frequency_max=5.825e9,
            bandwidth_max=160e6,
            supports_channel_hop=True,
            supports_csi=False,
            supports_hw_timestamp=False,
            supports_tdoa_sync=False,
            max_antennas=1,
        )

    def start_capture(self, callback: Callable[[Frame], None]) -> None:
        from scapy.all import AsyncSniffer
        self._callback = callback
        self._sniffer = AsyncSniffer(
            iface=self._iface,
            prn=self._handle_packet,
            store=False,
        )
        self._sniffer.start()

    def stop_capture(self) -> None:
        if self._sniffer is not None:
            try:
                self._sniffer.stop()
            except Exception:
                pass
            self._sniffer = None

    def set_frequency(self, hz: float) -> None:
        mhz  = round(hz / 1e6)
        chan = _FREQ_TO_CHAN.get(mhz)
        if chan is None:
            # Nearest match
            best = min(_FREQ_TO_CHAN.keys(), key=lambda f: abs(f - mhz))
            chan = _FREQ_TO_CHAN[best]
        self.set_channel(chan)

    def set_channel(self, channel: int) -> None:
        try:
            subprocess.run(
                ['iw', 'dev', self._iface, 'set', 'channel', str(channel)],
                capture_output=True, timeout=3,
            )
        except Exception:
            pass

    def close(self) -> None:
        self.stop_capture()
        if self._restore:
            self._set_monitor(enable=False)

    # ── Monitor mode ──────────────────────────────────────────────────────────

    def _set_monitor(self, enable: bool) -> None:
        mode = 'monitor' if enable else 'managed'
        steps = [
            ['ip',  'link', 'set', self._iface, 'down'],
            ['iw',  'dev',  self._iface, 'set', 'type', mode],
            ['ip',  'link', 'set', self._iface, 'up'],
        ]
        for cmd in steps:
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=5)
                if r.returncode != 0 and enable:
                    err = r.stderr.decode(errors='replace').strip()
                    raise RuntimeError(
                        f"Cannot set {self._iface} to {mode} mode: {err}\n"
                        f"  Run as root or grant CAP_NET_ADMIN, and ensure\n"
                        f"  'iw' and 'ip' are installed (iw iproute2)."
                    )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"Command not found: {exc.filename}\n"
                    f"  Install: sudo apt install iw iproute2"
                ) from exc

    # ── Frame parsing ─────────────────────────────────────────────────────────

    def _handle_packet(self, pkt) -> None:
        if self._callback is None:
            return
        try:
            frame = self._parse(pkt)
        except Exception:
            return
        if frame is not None:
            self._callback(frame)

    def _parse(self, pkt) -> Optional[Frame]:
        try:
            from scapy.layers.dot11 import (
                RadioTap, Dot11, Dot11Beacon, Dot11ProbeResp, Dot11Elt,
            )
        except ImportError:
            return None

        if not pkt.haslayer(RadioTap):
            return None

        rt = pkt[RadioTap]
        rssi     = float(getattr(rt, 'dBm_AntSignal',   None) or -100.0)
        freq_mhz = float(getattr(rt, 'ChannelFrequency', None) or 0.0)

        if not pkt.haslayer(Dot11):
            return None
        d11 = pkt[Dot11]

        meta: dict = {'protocol': '802.11'}

        if pkt.haslayer(Dot11Beacon) or pkt.haslayer(Dot11ProbeResp):
            # addr2 = transmitter (BSSID for AP-originated frames)
            bssid = d11.addr3 or d11.addr2
            if bssid:
                meta['bssid']      = bssid
                meta['identifier'] = bssid   # wardriver source key

            # Walk the information element chain for SSID (element ID 0)
            elt = pkt.getlayer(Dot11Elt)
            while elt is not None:
                if elt.ID == 0:
                    raw = bytes(elt.info) if elt.info else b''
                    if raw:
                        meta['ssid'] = raw.decode('utf-8', errors='replace')
                    break
                elt = (elt.payload.getlayer(Dot11Elt)
                       if elt.payload else None)

        return Frame(
            data=bytes(pkt),
            frequency=freq_mhz * 1e6,
            bandwidth=20e6,
            timestamp=time.time(),
            rssi=rssi,
            antenna_id=self._ant_id,
            metadata=meta,
        )
