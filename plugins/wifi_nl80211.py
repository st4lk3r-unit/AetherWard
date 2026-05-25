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
import re
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


def channel_to_frequency_mhz(channel: int) -> Optional[int]:
    return _CHAN_TO_FREQ.get(int(channel))


def frequency_to_channel(freq_mhz: float) -> Optional[int]:
    if not freq_mhz:
        return None
    mhz = int(round(freq_mhz))
    if mhz in _FREQ_TO_CHAN:
        return _FREQ_TO_CHAN[mhz]
    # Common formulas for channels not explicitly in the table.
    if 2412 <= mhz <= 2472:
        return int(round((mhz - 2412) / 5) + 1)
    if mhz == 2484:
        return 14
    if 5000 <= mhz <= 5900:
        return int(round((mhz - 5000) / 5))
    if 5955 <= mhz <= 7115:
        return int(round((mhz - 5950) / 5))
    return None


def _iter_elts(pkt, dot11elt_cls):
    elt = pkt.getlayer(dot11elt_cls)
    seen = 0
    while elt is not None and seen < 256:
        yield elt
        seen += 1
        try:
            elt = elt.payload.getlayer(dot11elt_cls) if elt.payload else None
        except Exception:
            break


def _safe_info(elt) -> bytes:
    try:
        return bytes(elt.info) if elt.info is not None else b''
    except Exception:
        return b''


def _parse_rsn(info: bytes) -> dict:
    # RSN IE (ID 48). Enough for metadata; not a full validator.
    out = {'auth_mode': 'WPA2', 'encryption': 'WPA2', 'akm': [], 'cipher': []}
    if len(info) < 8:
        return out
    pos = 2  # version
    cipher_map = {0: 'USE-GROUP', 1: 'WEP40', 2: 'TKIP', 4: 'CCMP', 5: 'WEP104', 8: 'GCMP', 9: 'GCMP-256', 10: 'CCMP-256'}
    akm_map = {1: '802.1X', 2: 'PSK', 3: 'FT-802.1X', 4: 'FT-PSK', 6: 'PSK-SHA256', 8: 'SAE', 18: 'OWE'}
    try:
        # group cipher suite
        if pos + 4 <= len(info):
            out['cipher'].append(cipher_map.get(info[pos + 3], f'cipher-{info[pos + 3]}'))
        pos += 4
        pairwise_n = int.from_bytes(info[pos:pos+2], 'little'); pos += 2
        for _ in range(pairwise_n):
            if pos + 4 > len(info): break
            out['cipher'].append(cipher_map.get(info[pos + 3], f'cipher-{info[pos + 3]}'))
            pos += 4
        akm_n = int.from_bytes(info[pos:pos+2], 'little'); pos += 2
        for _ in range(akm_n):
            if pos + 4 > len(info): break
            out['akm'].append(akm_map.get(info[pos + 3], f'akm-{info[pos + 3]}'))
            pos += 4
    except Exception:
        pass
    if 'SAE' in out['akm']:
        out['auth_mode'] = 'WPA3-SAE'
    elif 'PSK' in out['akm'] or 'FT-PSK' in out['akm'] or 'PSK-SHA256' in out['akm']:
        out['auth_mode'] = 'WPA2-PSK'
    elif out['akm']:
        out['auth_mode'] = 'WPA2-Enterprise'
    out['akm'] = '+'.join(dict.fromkeys(out['akm']))
    out['cipher'] = '+'.join(dict.fromkeys(out['cipher']))
    return out


def _parse_wpa_vendor(info: bytes) -> dict:
    # WPA vendor IE: 00:50:f2:01 ...
    if len(info) >= 4 and info[:4] == b'\x00\x50\xf2\x01':
        return {'auth_mode': 'WPA', 'encryption': 'WPA'}
    return {}


def _cap_privacy(pkt) -> bool:
    try:
        st = pkt.sprintf('{Dot11Beacon:%Dot11Beacon.cap%}{Dot11ProbeResp:%Dot11ProbeResp.cap%}')
        return 'privacy' in st.lower()
    except Exception:
        return False


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
                RadioTap, Dot11, Dot11Beacon, Dot11ProbeResp, Dot11ProbeReq, Dot11Elt,
            )
        except ImportError:
            return None

        if not pkt.haslayer(RadioTap):
            return None

        rt = pkt[RadioTap]
        rssi = getattr(rt, 'dBm_AntSignal', None)
        try:
            rssi = float(rssi)
        except Exception:
            rssi = -100.0
        freq_mhz = getattr(rt, 'ChannelFrequency', None)
        try:
            freq_mhz = float(freq_mhz or 0.0)
        except Exception:
            freq_mhz = 0.0

        if not pkt.haslayer(Dot11):
            return None
        d11 = pkt[Dot11]

        meta: dict = {
            'protocol': '802.11',
            'frame_type': f'{getattr(d11, "type", "?")}/{getattr(d11, "subtype", "?")}',
        }

        is_ap_frame = pkt.haslayer(Dot11Beacon) or pkt.haslayer(Dot11ProbeResp)
        is_probe_req = pkt.haslayer(Dot11ProbeReq)
        if is_ap_frame or is_probe_req:
            # AP-originated management frames normally carry BSSID in addr3;
            # probe requests use the station MAC as transmitter/identifier.
            bssid = (d11.addr3 or d11.addr2) if is_ap_frame else (d11.addr2 or d11.addr3)
            if bssid:
                meta['bssid'] = bssid
                meta['identifier'] = bssid

            ssid_seen = False
            channel = None
            auth_bits: dict = {}
            for elt in _iter_elts(pkt, Dot11Elt):
                eid = getattr(elt, 'ID', None)
                raw = _safe_info(elt)
                if eid == 0 and not ssid_seen:
                    ssid_seen = True
                    meta['ssid'] = raw.decode('utf-8', errors='replace') if raw else '<hidden>'
                elif eid == 3 and raw:
                    channel = raw[0]
                elif eid == 48:
                    auth_bits.update(_parse_rsn(raw))
                elif eid == 221:
                    auth_bits.update(_parse_wpa_vendor(raw))

            # Scapy's network_stats() often extracts channel/crypto already.
            try:
                st = pkt[Dot11Beacon if pkt.haslayer(Dot11Beacon) else Dot11ProbeResp].network_stats()
                if st.get('channel') and channel is None:
                    channel = int(st['channel'])
                crypto = st.get('crypto')
                if crypto and not auth_bits:
                    if isinstance(crypto, set):
                        crypto_s = '+'.join(sorted(map(str, crypto)))
                    else:
                        crypto_s = str(crypto)
                    auth_bits = {'auth_mode': crypto_s, 'encryption': crypto_s}
            except Exception:
                pass

            if channel is None and freq_mhz:
                channel = frequency_to_channel(freq_mhz)
            if channel is not None:
                meta['channel'] = int(channel)
                if not freq_mhz:
                    mhz = channel_to_frequency_mhz(int(channel))
                    if mhz:
                        freq_mhz = float(mhz)

            privacy = _cap_privacy(pkt)
            meta['privacy'] = bool(privacy)
            meta.update(auth_bits)
            if privacy and not meta.get('auth_mode'):
                meta['auth_mode'] = 'WEP/unknown'
                meta['encryption'] = 'WEP/unknown'
            elif not privacy and not meta.get('auth_mode'):
                meta['auth_mode'] = 'OPEN'
                meta['encryption'] = 'OPEN'

        if not freq_mhz:
            # Unknown channel/frequency is not useful for wardrive source keys.
            return None

        return Frame(
            data=bytes(pkt),
            frequency=freq_mhz * 1e6,
            bandwidth=20e6,
            timestamp=time.time(),
            rssi=rssi,
            antenna_id=self._ant_id,
            metadata=meta,
        )
