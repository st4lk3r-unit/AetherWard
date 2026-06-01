"""Focused wardrive regression tests for config, storage, parser and exports."""
from __future__ import annotations

import base64
import csv
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aetherward.config.schema import AWConfig
from aetherward.modes.wardriver import WardriverMode
from aetherward.position.absolute import AbsolutePosition, FixType
from aetherward.signal.frame import Frame
from aetherward.session import record_source_id, source_meta_from_record
from cli._commands import _proc_wardrive_map
from plugins import wifi_nl80211

SESSIONS_DIR = Path.home() / '.aetherward' / 'sessions' / 'tests' / 'regressions'


def _session_path(name: str) -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR / name


def _make_array(freq_ranges: list[tuple[float, float]] | None = None, gps=None):
    array = MagicMock()
    antennas = []
    freq_ranges = freq_ranges or [(0.0, 7.2e9)]
    for i, fr in enumerate(freq_ranges):
        ant = MagicMock()
        ant.id = f'wlan{i}'
        ant.backend = MagicMock()
        lo, hi = fr
        ant.covers_frequency = lambda hz, lo=lo, hi=hi: lo <= hz <= hi
        antennas.append(ant)
    array.antennas = antennas
    array.absolute_position = gps
    return array


def _ap_frame(raw: bytes = b'\x80\x00wardrive-test') -> Frame:
    return Frame(
        data=raw,
        frequency=2_437_000_000.0,
        bandwidth=20_000_000.0,
        timestamp=1_700_000_000.25,
        rssi=-47.5,
        antenna_id='wlan0',
        metadata={
            'protocol': '802.11',
            'identifier': 'aa:bb:cc:dd:ee:ff',
            'bssid': 'aa:bb:cc:dd:ee:ff',
            'ssid': 'TestAP',
            'auth_mode': '[WPA2-PSK-CCMP][ESS]',
            'security': 'WPA2',
            'channel': 6,
            'band': '2.4GHz',
            'frame_type': 'management',
            'frame_subtype': 'beacon',
            'privacy': True,
            'akm_suites': ['PSK'],
            'pairwise_ciphers': ['CCMP'],
            'group_cipher': 'CCMP',
            'capabilities': 0x0411,
            'beacon_interval': 100,
            'nested_bytes': b'\x01\x02',
        },
    )


class TestOutputConfigNormalization:
    def test_output_path_table_is_copied_to_mode_config(self):
        cfg = AWConfig.from_dict({
            'mode': 'wardriver',
            'mode_config': {'channels': [1, 6, 11]},
            'output': {'format': 'jsonl', 'path': str(_session_path('normalised.jsonl'))},
        })
        assert cfg.mode_config['output_path'].endswith('normalised.jsonl')

    def test_mode_config_output_path_wins_over_output_table(self):
        cfg = AWConfig.from_dict({
            'mode': 'wardriver',
            'mode_config': {'output_path': 'explicit.jsonl'},
            'output': {'format': 'jsonl', 'path': 'from-output.jsonl'},
        })
        assert cfg.mode_config['output_path'] == 'explicit.jsonl'


    def test_missing_output_path_defaults_to_sessions_dir(self):
        cfg = AWConfig.from_dict({
            'mode': 'wardriver',
            'array_id': 'rig-test',
            'mode_config': {'channels': [1, 6, 11]},
            'output': {'format': 'jsonl'},
        })
        out = Path(cfg.mode_config['output_path'])
        assert out.parent == Path.home() / '.aetherward' / 'sessions'
        assert out.name.startswith('rig-test-')
        assert out.suffix == '.jsonl'
        assert cfg.output['path'] == cfg.mode_config['output_path']

    def test_output_none_does_not_create_default_path(self):
        cfg = AWConfig.from_dict({
            'mode': 'wardriver',
            'mode_config': {'channels': [1, 6, 11]},
            'output': {'format': 'none'},
        })
        assert 'output_path' not in cfg.mode_config
        assert 'path' not in cfg.output


class TestWardriverFullFidelityJsonl:
    def test_jsonl_preserves_metadata_raw_frame_and_ap_fields(self):
        out = _session_path('full_fidelity.jsonl')
        out.unlink(missing_ok=True)
        gps = AbsolutePosition(lat=48.8566, lon=2.3522, alt=35.0,
                               fix_type=FixType.FIX_3D)
        mode = WardriverMode(_make_array(gps=gps), {'output_path': str(out)})
        mode.start()
        mode._handle(_ap_frame(), 'wlan0')
        mode.stop()

        rec = json.loads(out.read_text().strip())
        for field in ('metadata', 'raw_frame_hex', 'raw_frame_b64', 'frame_len',
                      'auth_mode', 'security', 'channel', 'bssid', 'ssid'):
            assert field in rec, f'{field} missing from wardrive JSONL'
        assert rec['metadata']['nested_bytes'] == {'encoding': 'hex', 'data': '0102'}
        assert rec['raw_frame_hex'] == _ap_frame().data.hex()
        assert base64.b64decode(rec['raw_frame_b64']) == _ap_frame().data
        assert rec['lat'] == pytest.approx(48.8566)
        assert rec['fix'] == int(FixType.FIX_3D)

    def test_store_raw_frames_false_removes_raw_payload_fields(self):
        out = _session_path('no_raw.jsonl')
        out.unlink(missing_ok=True)
        mode = WardriverMode(_make_array(), {
            'output_path': str(out),
            'store_raw_frames': False,
        })
        mode.start()
        mode._handle(_ap_frame(), 'wlan0')
        mode.stop()

        rec = json.loads(out.read_text().strip())
        assert rec['frame_len'] == len(_ap_frame().data)
        assert 'raw_frame_hex' not in rec
        assert 'raw_frame_b64' not in rec


class TestFrequencyAwareChannelSplit:
    def test_24_and_5ghz_antennas_get_only_supported_channels(self):
        array = _make_array([
            (2.400e9, 2.500e9),
            (5.000e9, 5.900e9),
        ])
        mode = WardriverMode(array, {'channels': [1, 6, 11, 36, 40, 149]})
        mode._assign_channels()
        assert mode._channel_map['wlan0'] == [1, 6, 11]
        assert mode._channel_map['wlan1'] == [36, 40, 149]

    def test_unknown_channel_falls_back_to_balanced_assignment(self):
        mode = WardriverMode(_make_array([(2.4e9, 2.5e9), (5.0e9, 5.9e9)]),
                             {'channels': [999, 1000]})
        mode._assign_channels()
        assigned = mode._channel_map['wlan0'] + mode._channel_map['wlan1']
        assert sorted(assigned) == [999, 1000]


class TestWifiParserHelpers:
    def test_rsn_wpa2_psk_ccmp_metadata(self):
        # RSN v1, group CCMP, pairwise CCMP, AKM PSK, capabilities 0.
        rsn = bytes.fromhex(
            '0100'          # version
            '000fac04'      # group cipher CCMP
            '0100'          # pairwise count
            '000fac04'      # pairwise CCMP
            '0100'          # AKM count
            '000fac02'      # PSK
            '0000'          # RSN capabilities
        )
        parsed = wifi_nl80211._parse_rsn(rsn)
        auth, security = wifi_nl80211._auth_mode(True, parsed, None)
        assert parsed['group_cipher'] == 'CCMP'
        assert parsed['pairwise_ciphers'] == ['CCMP']
        assert parsed['akm_suites'] == ['PSK']
        assert auth == '[WPA2-PSK-CCMP][ESS]'
        assert security == 'WPA2'

    def test_rsn_wpa3_sae_metadata(self):
        rsn = bytes.fromhex(
            '0100'          # version
            '000fac04'      # group CCMP
            '0100'          # pairwise count
            '000fac04'      # pairwise CCMP
            '0100'          # AKM count
            '000fac08'      # SAE
            '0000'
        )
        parsed = wifi_nl80211._parse_rsn(rsn)
        auth, security = wifi_nl80211._auth_mode(True, parsed, None)
        assert parsed['akm_suites'] == ['SAE']
        assert auth == '[WPA3-SAE-CCMP][ESS]'
        assert security == 'WPA3'

    def test_open_and_wep_auth_modes(self):
        assert wifi_nl80211._auth_mode(False, None, None) == ('[OPEN][ESS]', 'OPEN')
        assert wifi_nl80211._auth_mode(True, None, None) == ('[WEP][ESS]', 'WEP')

    def test_channel_frequency_and_band_helpers(self):
        assert wifi_nl80211._CHAN_TO_FREQ[6] == 2437
        assert wifi_nl80211._FREQ_TO_CHAN[5180] == 36
        assert wifi_nl80211._band_from_freq_mhz(2437) == '2.4GHz'
        assert wifi_nl80211._band_from_freq_mhz(5180) == '5GHz'


class TestProcessAndExportRegressions:
    def _records(self) -> list[dict]:
        base = {
            'id': 'aa:bb:cc:dd:ee:ff',
            'bssid': 'aa:bb:cc:dd:ee:ff',
            'ssid': 'TestAP',
            'auth_mode': '[WPA2-PSK-CCMP][ESS]',
            'security': 'WPA2',
            'channel': 6,
            'freq': 2_437_000_000,
            'protocol': '802.11',
            'metadata': {'pairwise_ciphers': ['CCMP'], 'akm_suites': ['PSK']},
        }
        coords = [(48.85650, 2.35210, -56), (48.85670, 2.35230, -54),
                  (48.85660, 2.35220, -40)]
        return [dict(base, t=1_700_000_000 + i, lat=lat, lon=lon, alt=35.0,
                     rssi=rssi, ant=f'wlan{i % 2}')
                for i, (lat, lon, rssi) in enumerate(coords)]

    def test_process_default_output_writes_next_to_session(self):
        session = _session_path('process_default.jsonl')
        session.write_text('\n'.join(json.dumps(r) for r in self._records()) + '\n')
        out = session.with_suffix('.geojson')
        out.unlink(missing_ok=True)
        _proc_wardrive_map(self._records(), 'geojson', None, str(session))
        doc = json.loads(out.read_text())
        props = doc['features'][0]['properties']
        assert props['auth_mode'] == '[WPA2-PSK-CCMP][ESS]'
        assert props['security'] == 'WPA2'
        assert props['pairwise_ciphers'] == ['CCMP']

    def test_wigle_export_keeps_auth_mode(self):
        out = _session_path('process_wigle.wigle.csv')
        out.unlink(missing_ok=True)
        _proc_wardrive_map(self._records(), 'wigle', str(out), 'ignored.jsonl')
        rows = list(csv.DictReader(out.read_text().splitlines()[1:]))
        assert rows[0]['MAC'] == 'aa:bb:cc:dd:ee:ff'
        assert rows[0]['AuthMode'] == '[WPA2-PSK-CCMP][ESS]'
        assert rows[0]['Channel'] == '6'

    def test_metadata_merge_keeps_fragments_from_multiple_records(self):
        rec1 = {
            'id': 'aa:bb:cc:dd:ee:ff',
            'metadata': {'ssid': 'TestAP', 'auth_mode': '[WPA2-PSK-CCMP][ESS]'},
        }
        rec2 = {
            'id': 'aa:bb:cc:dd:ee:ff',
            'metadata': {'channel': 6, 'pairwise_ciphers': ['CCMP']},
        }
        merged = {}
        for rec in (rec1, rec2):
            for k, v in source_meta_from_record(rec).items():
                if isinstance(v, list):
                    merged[k] = sorted(set(merged.get(k, []) + v))
                elif k not in merged:
                    merged[k] = v
        assert record_source_id(rec1) == 'aa:bb:cc:dd:ee:ff'
        assert merged['ssid'] == 'TestAP'
        assert merged['auth_mode'] == '[WPA2-PSK-CCMP][ESS]'
        assert merged['channel'] == 6
        assert merged['pairwise_ciphers'] == ['CCMP']


class TestNL80211Recovery:
    def test_channel_failure_resets_interface_and_retries(self, monkeypatch):
        calls = []
        channel_attempts = {'n': 0}

        class Result:
            def __init__(self, returncode=0, stderr=b''):
                self.returncode = returncode
                self.stderr = stderr

        def fake_run(cmd, capture_output=True, timeout=None):
            calls.append(list(cmd))
            if cmd[:5] == ['iw', 'dev', 'wlan0', 'set', 'channel']:
                channel_attempts['n'] += 1
                if channel_attempts['n'] == 1:
                    return Result(1, b'Network is down')
            return Result(0, b'')

        monkeypatch.setattr(wifi_nl80211.subprocess, 'run', fake_run)

        b = wifi_nl80211.NL80211Backend(interface='wlan0')
        b.set_channel(6)

        assert calls == [
            ['iw', 'dev', 'wlan0', 'set', 'channel', '6'],
            ['ip', 'link', 'set', 'wlan0', 'down'],
            ['iw', 'dev', 'wlan0', 'set', 'type', 'monitor'],
            ['ip', 'link', 'set', 'wlan0', 'up'],
            ['iw', 'dev', 'wlan0', 'set', 'channel', '6'],
        ]

    def test_auto_recover_can_be_disabled(self, monkeypatch):
        calls = []

        class Result:
            returncode = 1
            stderr = b'Network is down'

        def fake_run(cmd, capture_output=True, timeout=None):
            calls.append(list(cmd))
            return Result()

        monkeypatch.setattr(wifi_nl80211.subprocess, 'run', fake_run)

        b = wifi_nl80211.NL80211Backend(interface='wlan0')
        b.configure({'auto_recover': False})
        b.set_channel(6)

        assert calls == [['iw', 'dev', 'wlan0', 'set', 'channel', '6']]

    def test_recovery_is_throttled_to_avoid_reset_storms(self, monkeypatch):
        calls = []

        class Result:
            returncode = 1
            stderr = b'Network is down'

        def fake_run(cmd, capture_output=True, timeout=None):
            calls.append(list(cmd))
            return Result()

        monkeypatch.setattr(wifi_nl80211.subprocess, 'run', fake_run)
        monkeypatch.setattr(wifi_nl80211.time, 'monotonic', lambda: 100.0)

        b = wifi_nl80211.NL80211Backend(interface='wlan0')
        b.configure({'recover_cooldown': 10.0})
        b.set_channel(6)
        b.set_channel(11)

        assert calls == [
            ['iw', 'dev', 'wlan0', 'set', 'channel', '6'],
            ['ip', 'link', 'set', 'wlan0', 'down'],
            ['iw', 'dev', 'wlan0', 'set', 'type', 'monitor'],
            ['ip', 'link', 'set', 'wlan0', 'up'],
            ['iw', 'dev', 'wlan0', 'set', 'channel', '11'],
        ]
