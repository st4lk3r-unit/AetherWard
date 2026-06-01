"""
NL80211Backend — Linux 802.11 monitor-mode capture backend.

Puts a wireless interface into monitor mode using iw/ip and captures
802.11 frames with Scapy's AsyncSniffer.  Extracts RSSI/frequency from
radiotap and preserves useful 802.11 metadata: addresses, frame type,
SSID/BSSID, channel, beacon capabilities, WPA/WPA2/WPA3 security, ciphers,
AKMs, and vendor information elements.

Requires:
    scapy >= 2.5          pip install scapy
    iw + ip               iproute2 / wireless-tools (standard on Linux)
    CAP_NET_RAW + CAP_NET_ADMIN  (or run as root)
"""
from __future__ import annotations

import subprocess
import threading
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
    # 6 GHz — IEEE 802.11ax channel numbering, centre MHz = 5950 + 5*ch
    for ch in range(1, 234):
        c2f.setdefault(ch, 5950 + ch * 5)
    f2c = {v: k for k, v in c2f.items()}
    return c2f, f2c


_CHAN_TO_FREQ, _FREQ_TO_CHAN = _build_tables()


def _bool_config(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ('0', 'false', 'no', 'off', 'disabled')
    return bool(value)


# ── 802.11 parsing helpers ───────────────────────────────────────────────────

_FRAME_TYPES = {0: 'management', 1: 'control', 2: 'data', 3: 'extension'}
_MGMT_SUBTYPES = {
    0: 'association_req', 1: 'association_resp', 2: 'reassociation_req',
    3: 'reassociation_resp', 4: 'probe_req', 5: 'probe_resp', 8: 'beacon',
    9: 'atim', 10: 'disassociation', 11: 'authentication',
    12: 'deauthentication', 13: 'action',
}
_DATA_SUBTYPES = {
    0: 'data', 4: 'null', 8: 'qos_data', 12: 'qos_null',
}
_EID_NAMES = {
    0: 'ssid', 1: 'supported_rates', 3: 'ds_parameter_set', 5: 'tim',
    7: 'country', 32: 'power_constraint', 42: 'erp', 45: 'ht_capabilities',
    48: 'rsn', 50: 'extended_supported_rates', 61: 'ht_operation',
    127: 'extended_capabilities', 191: 'vht_capabilities', 192: 'vht_operation',
    221: 'vendor_specific', 255: 'extension',
}
_CIPHERS = {
    0: 'USE-GROUP', 1: 'WEP-40', 2: 'TKIP', 4: 'CCMP', 5: 'WEP-104',
    6: 'BIP-CMAC-128', 8: 'GCMP-128', 9: 'GCMP-256', 10: 'CCMP-256',
    11: 'BIP-GMAC-128', 12: 'BIP-GMAC-256', 13: 'BIP-CMAC-256',
}
_AKMS = {
    0: 'RESERVED', 1: '802.1X', 2: 'PSK', 3: 'FT-802.1X', 4: 'FT-PSK',
    5: '802.1X-SHA256', 6: 'PSK-SHA256', 8: 'SAE', 9: 'FT-SAE',
    11: '802.1X-SUITE-B', 12: '802.1X-SUITE-B-192', 18: 'OWE',
}


def _u16le(buf: bytes, off: int) -> tuple[int, int]:
    if off + 2 > len(buf):
        return 0, len(buf)
    return int.from_bytes(buf[off:off + 2], 'little'), off + 2


def _suite(buf: bytes, off: int, table: dict[int, str]) -> tuple[str, int]:
    if off + 4 > len(buf):
        return 'TRUNCATED', len(buf)
    oui = ':'.join(f'{b:02x}' for b in buf[off:off + 3])
    stype = buf[off + 3]
    name = table.get(stype, f'{oui}:{stype}')
    return name, off + 4


def _parse_rsn(info: bytes) -> dict:
    """Parse RSN IE (EID 48) into ciphers/AKMs."""
    out: dict = {'version': None, 'group_cipher': None,
                 'pairwise_ciphers': [], 'akm_suites': []}
    off = 0
    out['version'], off = _u16le(info, off)
    if off >= len(info):
        return out
    out['group_cipher'], off = _suite(info, off, _CIPHERS)
    count, off = _u16le(info, off)
    for _ in range(count):
        c, off = _suite(info, off, _CIPHERS)
        out['pairwise_ciphers'].append(c)
    count, off = _u16le(info, off)
    for _ in range(count):
        a, off = _suite(info, off, _AKMS)
        out['akm_suites'].append(a)
    if off + 2 <= len(info):
        caps, off = _u16le(info, off)
        out['rsn_capabilities'] = caps
    return out


def _parse_wpa_vendor(info: bytes) -> Optional[dict]:
    """Parse WPA vendor IE: OUI 00:50:f2, type 1."""
    if len(info) < 8 or info[:4] != b'\x00\x50\xf2\x01':
        return None
    out: dict = {'version': None, 'group_cipher': None,
                 'pairwise_ciphers': [], 'akm_suites': []}
    off = 4
    out['version'], off = _u16le(info, off)
    out['group_cipher'], off = _suite(info, off, _CIPHERS)
    count, off = _u16le(info, off)
    for _ in range(count):
        c, off = _suite(info, off, _CIPHERS)
        out['pairwise_ciphers'].append(c)
    count, off = _u16le(info, off)
    for _ in range(count):
        a, off = _suite(info, off, _AKMS)
        out['akm_suites'].append(a)
    return out


def _decode_rates(info: bytes) -> list[float]:
    rates = []
    for b in info:
        rates.append(round((b & 0x7f) * 0.5, 1))
    return rates


def _cap_to_int(cap) -> int:
    try:
        return int(cap)
    except Exception:
        s = str(cap).lower()
        val = 0
        if 'ess' in s:
            val |= 0x0001
        if 'privacy' in s:
            val |= 0x0010
        return val


def _auth_mode(privacy: bool, rsn: Optional[dict], wpa: Optional[dict]) -> tuple[str, str]:
    blocks: list[str] = []
    security: list[str] = []

    if wpa:
        ciphers = '+'.join(wpa.get('pairwise_ciphers') or [wpa.get('group_cipher') or ''])
        akms = wpa.get('akm_suites') or ['PSK']
        if any(a in ('802.1X', 'FT-802.1X') for a in akms):
            blocks.append(f'WPA-EAP-{ciphers}')
        else:
            blocks.append(f'WPA-PSK-{ciphers}')
        security.append('WPA')

    if rsn:
        ciphers = '+'.join(rsn.get('pairwise_ciphers') or [rsn.get('group_cipher') or 'CCMP'])
        akms = rsn.get('akm_suites') or []
        if any(a in ('SAE', 'FT-SAE') for a in akms):
            blocks.append(f'WPA3-SAE-{ciphers}')
            security.append('WPA3')
        if any(a == 'OWE' for a in akms):
            blocks.append(f'WPA3-OWE-{ciphers}')
            security.append('WPA3')
        if any('802.1X' in a for a in akms):
            blocks.append(f'WPA2-EAP-{ciphers}')
            security.append('WPA2')
        if any(a in ('PSK', 'FT-PSK', 'PSK-SHA256') for a in akms) or not akms:
            blocks.append(f'WPA2-PSK-{ciphers}')
            security.append('WPA2')

    if not blocks:
        if privacy:
            blocks.append('WEP')
            security.append('WEP')
        else:
            blocks.append('OPEN')
            security.append('OPEN')

    # Wigle-style AuthMode, with ESS appended when it is an infrastructure AP.
    auth = ''.join(f'[{b}]' for b in dict.fromkeys(blocks)) + '[ESS]'
    sec = '+'.join(dict.fromkeys(security))
    return auth, sec


def _walk_ies(pkt, Dot11Elt) -> list[dict]:
    ies: list[dict] = []
    elt = pkt.getlayer(Dot11Elt)
    while elt is not None:
        try:
            eid = int(elt.ID)
        except Exception:
            eid = -1
        raw = bytes(elt.info) if getattr(elt, 'info', None) else b''
        ies.append({'id': eid, 'name': _EID_NAMES.get(eid, f'ie_{eid}'),
                    'len': len(raw), 'info': raw})
        elt = elt.payload.getlayer(Dot11Elt) if getattr(elt, 'payload', None) else None
    return ies


def _channel_from_freq_mhz(freq_mhz: float) -> int | None:
    if not freq_mhz:
        return None
    return _FREQ_TO_CHAN.get(round(freq_mhz))


def _band_from_freq_mhz(freq_mhz: float) -> str:
    if 2400 <= freq_mhz < 2500:
        return '2.4GHz'
    if 4900 <= freq_mhz < 5900:
        return '5GHz'
    if 5925 <= freq_mhz < 7125:
        return '6GHz'
    return ''


# ── Backend ───────────────────────────────────────────────────────────────────

class NL80211Backend(HardwareBackend):
    """
    Linux nl80211/cfg80211 WiFi capture backend.

    Puts a wireless interface into monitor mode and captures 802.11 frames
    using Scapy's AsyncSniffer.  RSSI and channel frequency come from the
    radiotap header; beacon and probe-response frames are parsed for AP identity
    and security metadata.

    Config keys:
        interface        str    wireless interface name (e.g. 'wlan0')
        restore          bool   restore managed mode on close (default True)
        auto_recover     bool   reset interface on channel-set failure (default True)
        recover_cooldown float  min seconds between resets (default 2.0)
    """

    def __init__(self, interface: str = 'wlan0', restore: bool = True) -> None:
        self._iface   = interface
        self._restore = restore
        self._ant_id  = interface
        self._sniffer = None
        self._callback: Optional[Callable[[Frame], None]] = None
        self._capture_running = False
        self._auto_recover = True
        self._recover_cooldown = 2.0
        self._last_recover = 0.0
        self._last_channel: Optional[int] = None
        self._lock = threading.RLock()

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
        self._restore = _bool_config(config.get('restore', self._restore))
        self._ant_id  = config.get('antenna_id', self._iface)
        self._auto_recover = _bool_config(config.get('auto_recover', self._auto_recover))
        self._recover_cooldown = float(config.get('recover_cooldown', self._recover_cooldown))

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            frequency_min=2.412e9,
            frequency_max=7.125e9,
            bandwidth_max=160e6,
            supports_channel_hop=True,
            supports_csi=False,
            supports_hw_timestamp=False,
            supports_tdoa_sync=False,
            max_antennas=1,
        )

    def start_capture(self, callback: Callable[[Frame], None]) -> None:
        with self._lock:
            self._callback = callback
            self._capture_running = True
            self._restart_sniffer_locked()

    def stop_capture(self) -> None:
        with self._lock:
            self._capture_running = False
            self._stop_sniffer_locked()

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
            channel = int(channel)
        except (TypeError, ValueError):
            return

        with self._lock:
            self._last_channel = channel
            if self._set_channel_locked(channel):
                return

            if not self._recover_interface_locked():
                return

            # A down/up reset clears the channel on many adapters; retry once.
            self._set_channel_locked(channel)

    def close(self) -> None:
        self.stop_capture()
        if self._restore:
            self._set_monitor(enable=False)

    # ── Monitor mode / recovery ───────────────────────────────────────────────

    def _set_channel_locked(self, channel: int) -> bool:
        try:
            r = subprocess.run(
                ['iw', 'dev', self._iface, 'set', 'channel', str(channel)],
                capture_output=True, timeout=3,
            )
        except Exception:
            return False
        return r.returncode == 0

    def _stop_sniffer_locked(self) -> None:
        if self._sniffer is not None:
            try:
                self._sniffer.stop()
            except Exception:
                pass
            self._sniffer = None

    def _restart_sniffer_locked(self) -> None:
        self._stop_sniffer_locked()
        if not self._capture_running or self._callback is None:
            return
        from scapy.all import AsyncSniffer
        self._sniffer = AsyncSniffer(
            iface=self._iface,
            prn=self._handle_packet,
            store=False,
        )
        self._sniffer.start()

    def _recover_interface_locked(self) -> bool:
        if not self._auto_recover:
            return False
        now = time.monotonic()
        if self._recover_cooldown > 0 and now - self._last_recover < self._recover_cooldown:
            return False
        self._last_recover = now

        self._stop_sniffer_locked()
        ok = self._set_monitor(enable=True, strict=False)
        try:
            self._restart_sniffer_locked()
        except Exception:
            ok = False
        return ok

    def _set_monitor(self, enable: bool, strict: bool = True) -> bool:
        mode = 'monitor' if enable else 'managed'
        steps = [
            ['ip',  'link', 'set', self._iface, 'down'],
            ['iw',  'dev',  self._iface, 'set', 'type', mode],
            ['ip',  'link', 'set', self._iface, 'up'],
        ]
        ok = True
        for cmd in steps:
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=5)
                if r.returncode != 0:
                    ok = False
                    if strict and enable:
                        err = r.stderr.decode(errors='replace').strip()
                        raise RuntimeError(
                            f"Cannot set {self._iface} to {mode} mode: {err}\n"
                            f"  Run as root or grant CAP_NET_ADMIN, and ensure\n"
                            f"  'iw' and 'ip' are installed (iw iproute2)."
                        )
            except FileNotFoundError as exc:
                if strict:
                    raise RuntimeError(
                        f"Command not found: {exc.filename}\n"
                        f"  Install: sudo apt install iw iproute2"
                    ) from exc
                return False
        return ok

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
                RadioTap, Dot11, Dot11Beacon, Dot11ProbeResp, Dot11ProbeReq,
                Dot11Elt,
            )
        except ImportError:
            return None

        if not pkt.haslayer(RadioTap):
            return None

        rt = pkt[RadioTap]
        rssi     = float(getattr(rt, 'dBm_AntSignal',   None) or -100.0)
        freq_mhz = float(getattr(rt, 'ChannelFrequency', None) or 0.0)
        channel  = _channel_from_freq_mhz(freq_mhz)

        if not pkt.haslayer(Dot11):
            return None
        d11 = pkt[Dot11]

        ftype = int(getattr(d11, 'type', 0) or 0)
        stype = int(getattr(d11, 'subtype', 0) or 0)
        subtype_name = (_MGMT_SUBTYPES.get(stype, f'mgmt_{stype}') if ftype == 0
                        else _DATA_SUBTYPES.get(stype, f'subtype_{stype}'))

        meta: dict = {
            'protocol': '802.11',
            'frame_type': _FRAME_TYPES.get(ftype, str(ftype)),
            'frame_subtype': subtype_name,
            'type': ftype,
            'subtype': stype,
            'addr1': getattr(d11, 'addr1', None),
            'addr2': getattr(d11, 'addr2', None),
            'addr3': getattr(d11, 'addr3', None),
        }
        if channel:
            meta['channel'] = channel
        if freq_mhz:
            band = _band_from_freq_mhz(freq_mhz)
            if band:
                meta['band'] = band

        is_ap_advert = pkt.haslayer(Dot11Beacon) or pkt.haslayer(Dot11ProbeResp)
        is_probe_req = pkt.haslayer(Dot11ProbeReq)

        # Management AP frames: addr3 is the BSSID; addr2 is transmitter.
        if is_ap_advert:
            bssid = d11.addr3 or d11.addr2
            if bssid:
                meta['bssid'] = bssid
                meta['identifier'] = bssid
        elif is_probe_req and d11.addr2:
            meta['station'] = d11.addr2
            meta['identifier'] = d11.addr2
        elif d11.addr3:
            meta['bssid'] = d11.addr3
            meta['identifier'] = d11.addr3
        elif d11.addr2:
            meta['identifier'] = d11.addr2

        rsn = None
        wpa = None
        ies = _walk_ies(pkt, Dot11Elt)
        slim_ies: list[dict] = []
        rates: list[float] = []
        vendor_ouis: list[str] = []
        ssid_seen = False

        for ie in ies:
            eid = ie['id']
            raw = ie['info']
            slim_ies.append({'id': eid, 'name': ie['name'], 'len': ie['len'],
                             'info_hex': raw.hex()})
            if eid == 0:
                ssid_seen = True
                meta['ssid'] = raw.decode('utf-8', errors='replace') if raw else ''
            elif eid == 1 or eid == 50:
                rates.extend(_decode_rates(raw))
            elif eid == 3 and raw:
                meta['channel'] = raw[0]
            elif eid == 48:
                rsn = _parse_rsn(raw)
                meta['rsn'] = rsn
            elif eid == 61 and len(raw) >= 1:
                meta.setdefault('channel', raw[0])
            elif eid == 192 and len(raw) >= 1:
                meta.setdefault('vht_channel_width', raw[0])
            elif eid == 221 and len(raw) >= 4:
                vendor_ouis.append(':'.join(f'{b:02x}' for b in raw[:3]))
                parsed_wpa = _parse_wpa_vendor(raw)
                if parsed_wpa:
                    wpa = parsed_wpa
                    meta['wpa'] = wpa

        if rates:
            meta['supported_rates_mbps'] = sorted(set(rates))
        if vendor_ouis:
            meta['vendor_ouis'] = sorted(set(vendor_ouis))
        if slim_ies:
            meta['ies'] = slim_ies

        if is_ap_advert:
            beacon = pkt[Dot11Beacon] if pkt.haslayer(Dot11Beacon) else pkt[Dot11ProbeResp]
            cap_int = _cap_to_int(getattr(beacon, 'cap', 0))
            meta['capabilities'] = cap_int
            meta['privacy'] = bool(cap_int & 0x0010)
            meta['ess'] = bool(cap_int & 0x0001)
            if getattr(beacon, 'beacon_interval', None) is not None:
                meta['beacon_interval'] = getattr(beacon, 'beacon_interval')
            auth, sec = _auth_mode(meta['privacy'], rsn, wpa)
            meta['auth_mode'] = auth
            meta['security'] = sec
            if rsn:
                meta['akm_suites'] = rsn.get('akm_suites', [])
                meta['pairwise_ciphers'] = rsn.get('pairwise_ciphers', [])
                meta['group_cipher'] = rsn.get('group_cipher')
            elif wpa:
                meta['akm_suites'] = wpa.get('akm_suites', [])
                meta['pairwise_ciphers'] = wpa.get('pairwise_ciphers', [])
                meta['group_cipher'] = wpa.get('group_cipher')
        elif not ssid_seen and is_probe_req:
            meta['ssid'] = ''

        if not freq_mhz and meta.get('channel') in _CHAN_TO_FREQ:
            freq_mhz = float(_CHAN_TO_FREQ[int(meta['channel'])])
            band = _band_from_freq_mhz(freq_mhz)
            if band:
                meta['band'] = band

        # Remove null address fields while keeping explicitly-empty SSIDs.
        meta = {k: v for k, v in meta.items() if v is not None}

        return Frame(
            data=bytes(pkt),
            frequency=freq_mhz * 1e6,
            bandwidth=20e6,
            timestamp=time.time(),
            rssi=rssi,
            antenna_id=self._ant_id,
            metadata=meta,
        )
