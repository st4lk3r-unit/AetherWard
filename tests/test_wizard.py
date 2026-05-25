"""
Comprehensive tests for the interactive config wizard.

Covers _wizard_quick and _wizard_custom for all three operating modes
with various hardware, GPS, IMU, sync, and output combinations.

All interactive I/O (_choose, _ask_str, _ask_int, _ask_float, _confirm)
is patched so tests run without a TTY or real hardware.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the repo root is on sys.path so 'cli' is importable.
_REPO = Path(__file__).parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import cli.aetherward as _aw
import cli._wizard as _wiz


# ── Mock helpers ──────────────────────────────────────────────────────────────

@contextmanager
def _mock_io(choices=(), strings=(), ints=(), floats=(), confirms=()):
    """
    Patch all wizard I/O functions in both cli.aetherward and cli._wizard.

    cli._wizard imports these names at module load time, so patching only
    cli.aetherward leaves cli._wizard's local references untouched.  Both
    namespaces must be patched with the same mock objects.
    """
    mock_choose  = MagicMock(side_effect=list(choices))
    mock_str     = MagicMock(side_effect=list(strings))
    mock_int     = MagicMock(side_effect=list(ints))
    mock_float   = MagicMock(side_effect=list(floats))
    mock_confirm = MagicMock(side_effect=list(confirms))
    mock_sep     = MagicMock()
    shared = dict(
        _choose=mock_choose, _ask_str=mock_str, _ask_int=mock_int,
        _ask_float=mock_float, _confirm=mock_confirm, _sep=mock_sep,
    )
    with patch.multiple('cli.aetherward', **shared), \
         patch.multiple('cli._wizard',    **shared):
        yield


def _hw(n_wifi=1, gpsd=False, serial=(), pps=()):
    """Build a synthetic hardware-scan result dict."""
    return {
        'wifi':   [{'name': f'wlan{i}', 'driver': 'ath9k', 'monitor': True}
                   for i in range(n_wifi)],
        'gpsd':   gpsd,
        'serial': list(serial),
        'pps':    list(pps),
    }


# ── Pure math: channel auto-split ─────────────────────────────────────────────

def _channel_split(n_ant: int) -> list[list[int]]:
    """Mirror of the auto-split logic in _step_channels."""
    all_ch = list(range(1, 14))
    n = max(n_ant, 1)
    return [
        all_ch[(i * len(all_ch)) // n : ((i + 1) * len(all_ch)) // n]
        for i in range(n)
    ]


class TestChannelSplit:
    def test_single_antenna_gets_all_channels(self):
        groups = _channel_split(1)
        assert groups == [list(range(1, 14))]

    def test_two_antennas_cover_all_channels(self):
        groups = _channel_split(2)
        flat = [ch for g in groups for ch in g]
        assert sorted(flat) == list(range(1, 14))
        assert len(groups) == 2
        assert len(groups[0]) == 6
        assert len(groups[1]) == 7

    def test_three_antennas_cover_all(self):
        groups = _channel_split(3)
        flat = [ch for g in groups for ch in g]
        assert sorted(flat) == list(range(1, 14))
        assert len(groups) == 3

    def test_four_antennas_cover_all(self):
        groups = _channel_split(4)
        flat = [ch for g in groups for ch in g]
        assert sorted(flat) == list(range(1, 14))
        assert len(groups) == 4

    def test_thirteen_antennas_one_each(self):
        groups = _channel_split(13)
        assert all(len(g) == 1 for g in groups)
        assert [g[0] for g in groups] == list(range(1, 14))

    def test_no_overlaps(self):
        for n in range(1, 8):
            groups = _channel_split(n)
            seen = []
            for g in groups:
                for ch in g:
                    assert ch not in seen, f"channel {ch} duplicated for n={n}"
                    seen.append(ch)

    def test_groups_are_contiguous(self):
        for n in range(1, 6):
            for g in _channel_split(n):
                if len(g) > 1:
                    assert g == list(range(g[0], g[-1] + 1))


# ── Frequency preset map ──────────────────────────────────────────────────────

class TestFreqPresets:
    def test_2_4ghz_preset(self):
        lo, hi = _aw._FREQ_PRESET_MAP['2.4ghz']
        assert lo == pytest.approx(2.4e9)
        assert hi == pytest.approx(2.5e9)

    def test_5ghz_preset(self):
        lo, hi = _aw._FREQ_PRESET_MAP['5ghz']
        assert lo == pytest.approx(5.0e9)
        assert hi == pytest.approx(5.9e9)

    def test_both_preset(self):
        lo, hi = _aw._FREQ_PRESET_MAP['both']
        assert lo == pytest.approx(2.4e9)
        assert hi == pytest.approx(5.9e9)

    def test_any_preset(self):
        lo, hi = _aw._FREQ_PRESET_MAP['any']
        assert lo == 0.0
        assert hi == float('inf')

    def test_all_presets_present(self):
        assert set(_aw._FREQ_PRESET_MAP) == {'2.4ghz', '5ghz', 'both', 'any'}


# ── Antenna pattern map ───────────────────────────────────────────────────────

class TestAntennaPatternMap:
    def test_dipole_stick(self):
        pat, gain = _aw._ANTENNA_PATTERN_MAP['dipole_stick']
        assert pat == 'dipole'
        assert gain == pytest.approx(2.15)

    def test_dipole_panel(self):
        pat, gain = _aw._ANTENNA_PATTERN_MAP['dipole_panel']
        assert pat == 'dipole'
        assert gain == pytest.approx(6.0)

    def test_yagi(self):
        pat, gain = _aw._ANTENNA_PATTERN_MAP['yagi']
        assert pat == 'dipole'
        assert gain == pytest.approx(10.0)

    def test_isotropic(self):
        pat, gain = _aw._ANTENNA_PATTERN_MAP['isotropic']
        assert pat == 'isotropic'
        assert gain == pytest.approx(0.0)

    def test_all_types_present(self):
        keys = set(_aw._ANTENNA_PATTERN_MAP)
        assert 'dipole_stick' in keys
        assert 'dipole_panel' in keys
        assert 'yagi' in keys
        assert 'isotropic' in keys


# ── Quick wizard ──────────────────────────────────────────────────────────────

class TestQuickWizardWardriver:
    """Quick wizard — wardriving mode with various hardware/GPS combos."""

    def test_single_antenna_no_gpsd(self):
        """1 wifi, no gpsd, no IMU → minimal wardriver config."""
        hw = _hw(n_wifi=1, gpsd=False)
        #   _choose calls in order:
        #   0: mode='wardriver'
        #   1: iface='wlan0'
        #   2: ant_type='dipole_stick'
        #   3: freq='2.4ghz'
        #   4: gps_alt='none'
        #   5: imu='none'
        with _mock_io(
            choices  = ['wardriver', 'wlan0', 'dipole_stick', '2.4ghz', 'none', 'none'],
            ints     = [1],
            strings  = ['/tmp/aw-quick.jsonl'],
        ):
            cfg = _wiz._wizard_quick(hw)

        assert cfg['mode'] == 'wardriver'
        assert len(cfg['antennas']) == 1
        ant = cfg['antennas'][0]
        assert ant['id'] == 'wlan0'
        assert ant['pattern'] == 'dipole'
        assert ant['gain_dbi'] == pytest.approx(2.15)
        assert ant['frequency_range'] == pytest.approx([2.4e9, 2.5e9])
        assert cfg['gps'] == {'backend': 'none'}
        assert cfg['imu'] == {'backend': 'null'}
        assert cfg['sync'] == {'source': 'software'}
        mc = cfg['mode_config']
        assert mc['channels'] == list(range(1, 14))
        assert mc['hop_interval'] == pytest.approx(0.1)

    def test_two_antennas_same_type_gpsd_accept_split(self):
        """2 wifi, gpsd, same antenna type, accept auto channel split."""
        hw = _hw(n_wifi=2, gpsd=True)
        #   _choose:  mode, wlan0, wlan1, ant_type (same), freq, imu
        #   _confirm: same_type=T, accept_split=T, use_gpsd=T
        with _mock_io(
            choices  = ['wardriver', 'wlan0', 'wlan1', 'dipole_stick', '2.4ghz', 'none'],
            ints     = [2],
            strings  = ['/tmp/aw-2ant.jsonl'],
            confirms = [True, True, True],
        ):
            cfg = _wiz._wizard_quick(hw)

        assert cfg['mode'] == 'wardriver'
        assert len(cfg['antennas']) == 2
        assert cfg['gps']['backend'] == 'gpsd'
        assert cfg['imu'] == {'backend': 'null'}
        cm = cfg['mode_config']['channels']
        assert sorted(cm) == list(range(1, 14))

    def test_two_antennas_diff_types_custom_channel_split(self):
        """2 wifi, gpsd, different antenna types, custom channel split."""
        hw = _hw(n_wifi=2, gpsd=True)
        #   _choose:  mode, wlan0, wlan1, ant_type_0, ant_type_1, freq, imu
        #   _confirm: same_type=F, accept_split=F, use_gpsd=T
        #   _ask_str: channels_wlan0, channels_wlan1, out_path
        with _mock_io(
            choices  = ['wardriver', 'wlan0', 'wlan1',
                        'dipole_stick', 'isotropic', '2.4ghz', 'none'],
            ints     = [2],
            strings  = ['1,6,11', '2,7,12', '/tmp/aw-split.jsonl'],
            confirms = [False, False, True],
        ):
            cfg = _wiz._wizard_quick(hw)

        assert cfg['antennas'][0]['pattern'] == 'dipole'
        assert cfg['antennas'][1]['pattern'] == 'isotropic'
        assert cfg['antennas'][1]['gain_dbi'] == pytest.approx(0.0)
        mc = cfg['mode_config']
        # channel_map is not directly in mode_config for quick wardriver;
        # quick wizard always sets channels=1..13 in mode_config.
        assert mc['channels'] == list(range(1, 14))

    def test_serial_imu(self):
        """1 wifi, no gpsd, serial IMU selected."""
        hw = _hw(n_wifi=1, gpsd=False, serial=['/dev/ttyUSB0'])
        #   _choose:  mode, wlan0, ant_type, freq, gps_alt, imu='serial'
        #   _ask_str: out_path, imu_device
        with _mock_io(
            choices  = ['wardriver', 'wlan0', 'dipole_stick', '2.4ghz', 'none', 'serial'],
            ints     = [1],
            strings  = ['/tmp/aw-imu.jsonl', '/dev/ttyUSB0'],
        ):
            cfg = _wiz._wizard_quick(hw)

        assert cfg['imu']['backend'] == 'serial'
        assert cfg['imu']['device'] == '/dev/ttyUSB0'
        assert cfg['imu']['baud'] == 115200

    def test_no_wifi_custom_interface(self):
        """No detected wifi → fallback to custom _ask_str for interface."""
        hw = _hw(n_wifi=0, gpsd=False)
        #   _choose:  mode, ant_type, freq, gps_alt, imu  (no iface _choose)
        #   _ask_str: custom_iface, out_path
        with _mock_io(
            choices  = ['wardriver', 'dipole_stick', '2.4ghz', 'none', 'none'],
            ints     = [1],
            strings  = ['wlan0', '/tmp/aw-nohw.jsonl'],
        ):
            cfg = _wiz._wizard_quick(hw)

        assert cfg['antennas'][0]['id'] == 'wlan0'

    def test_geoclue_gps(self):
        """Alternative GPS: GeoClue2."""
        hw = _hw(n_wifi=1, gpsd=False)
        with _mock_io(
            choices  = ['wardriver', 'wlan0', 'dipole_stick', '2.4ghz', 'geoclue', 'none'],
            ints     = [1],
            strings  = ['/tmp/aw-geo.jsonl'],
        ):
            cfg = _wiz._wizard_quick(hw)

        assert cfg['gps'] == {'backend': 'geoclue'}

    def test_mls_gps(self):
        """Alternative GPS: Mozilla LBS."""
        hw = _hw(n_wifi=1, gpsd=False)
        with _mock_io(
            choices  = ['wardriver', 'wlan0', 'dipole_stick', '2.4ghz', 'mls', 'none'],
            ints     = [1],
            strings  = ['/tmp/aw-mls.jsonl'],
        ):
            cfg = _wiz._wizard_quick(hw)

        assert cfg['gps'] == {'backend': 'mls'}

    def test_ip_gps(self):
        """Alternative GPS: IP geolocation."""
        hw = _hw(n_wifi=1, gpsd=False)
        with _mock_io(
            choices  = ['wardriver', 'wlan0', 'dipole_stick', '2.4ghz', 'ip', 'none'],
            ints     = [1],
            strings  = ['/tmp/aw-ip.jsonl'],
        ):
            cfg = _wiz._wizard_quick(hw)

        assert cfg['gps'] == {'backend': 'ip'}

    def test_gpsd_declined_falls_back(self):
        """gpsd detected but declined → alternative GPS prompt."""
        hw = _hw(n_wifi=1, gpsd=True)
        #   _confirm: use_gpsd=False
        #   _choose: mode, iface, ant_type, freq, gps_alt='none', imu
        with _mock_io(
            choices  = ['wardriver', 'wlan0', 'dipole_stick', '2.4ghz', 'none', 'none'],
            ints     = [1],
            strings  = ['/tmp/aw-decl.jsonl'],
            confirms = [True, False],  # accept channel split (n=1 → no confirm), then gpsd=False
        ):
            # n_ant=1, so no channel-split confirm; gpsd confirm is first
            # Re-trace: gpsd=True → _confirm first in _step_gps
            # But channel split confirm only fires when n_ant > 1.
            # So _confirm order: use_gpsd=False (only confirm needed)
            pass

        # Use correct confirms list (only gpsd confirm fires for n_ant=1)
        with _mock_io(
            choices  = ['wardriver', 'wlan0', 'dipole_stick', '2.4ghz', 'none', 'none'],
            ints     = [1],
            strings  = ['/tmp/aw-decl.jsonl'],
            confirms = [False],  # gpsd declined → fallback lbs
        ):
            cfg = _wiz._wizard_quick(hw)

        assert cfg['gps'] == {'backend': 'none'}

    def test_5ghz_freq_range(self):
        """Select 5 GHz frequency preset."""
        hw = _hw(n_wifi=1, gpsd=False)
        with _mock_io(
            choices  = ['wardriver', 'wlan0', 'dipole_stick', '5ghz', 'none', 'none'],
            ints     = [1],
            strings  = ['/tmp/aw-5g.jsonl'],
        ):
            cfg = _wiz._wizard_quick(hw)

        assert cfg['antennas'][0]['frequency_range'] == pytest.approx([5.0e9, 5.9e9])

    def test_output_structure(self):
        """Config always has all top-level keys."""
        hw = _hw(n_wifi=1, gpsd=False)
        with _mock_io(
            choices  = ['wardriver', 'wlan0', 'dipole_stick', '2.4ghz', 'none', 'none'],
            ints     = [1],
            strings  = ['/tmp/aw-struct.jsonl'],
        ):
            cfg = _wiz._wizard_quick(hw)

        for key in ('mode', 'array_id', 'antennas', 'gps', 'imu', 'sync',
                    'mode_config', 'output'):
            assert key in cfg, f"missing key: {key}"
        assert cfg['array_id'] == 'default'


class TestQuickWizardTrilateration:
    """Quick wizard — trilateration mode."""

    def test_four_antennas_gpsd(self):
        """4 antennas, gpsd accepted → trilateration config."""
        hw = _hw(n_wifi=4, gpsd=True)
        #   _choose: mode, wlan0, wlan1, wlan2, wlan3, ant_type, freq
        #   _confirm: same_type=T, use_gpsd=T
        with _mock_io(
            choices  = ['trilateration', 'wlan0', 'wlan1', 'wlan2', 'wlan3',
                        'dipole_stick', '2.4ghz'],
            ints     = [4],
            strings  = ['/tmp/aw-trilat.jsonl'],
            confirms = [True, True],
        ):
            cfg = _wiz._wizard_quick(hw)

        assert cfg['mode'] == 'trilateration'
        assert len(cfg['antennas']) == 4
        assert cfg['gps']['backend'] == 'gpsd'
        mc = cfg['mode_config']
        assert mc['channel'] == 6
        assert mc['reference_antenna'] == 'wlan0'
        assert mc['correlation_window'] == pytest.approx(0.001)
        assert mc['group_timeout'] == pytest.approx(0.05)

    def test_two_antennas_warning_geoclue(self):
        """2 antennas (below 4) → wizard continues with warning; geoclue GPS."""
        hw = _hw(n_wifi=2, gpsd=False)
        #   _choose: mode, wlan0, wlan1, ant_type, freq, gps_alt
        #   _confirm: same_type=T
        with _mock_io(
            choices  = ['trilateration', 'wlan0', 'wlan1', 'isotropic', '5ghz', 'geoclue'],
            ints     = [2],
            strings  = ['/tmp/aw-trilat2.jsonl'],
            confirms = [True],
        ):
            cfg = _wiz._wizard_quick(hw)

        assert cfg['mode'] == 'trilateration'
        assert len(cfg['antennas']) == 2
        assert cfg['gps']['backend'] == 'geoclue'
        assert cfg['imu'] == {'backend': 'null'}

    def test_imu_not_used_for_trilateration(self):
        """_step_imu returns null for trilateration regardless."""
        hw = _hw(n_wifi=4, gpsd=False)
        with _mock_io(
            choices  = ['trilateration', 'wlan0', 'wlan1', 'wlan2', 'wlan3',
                        'dipole_stick', '2.4ghz', 'none'],
            ints     = [4],
            strings  = ['/tmp/aw-trilat3.jsonl'],
            confirms = [True],
        ):
            cfg = _wiz._wizard_quick(hw)

        assert cfg['imu'] == {'backend': 'null'}

    def test_antenna_positions_are_evenly_spaced(self):
        """Quick wizard places antennas at 0, 0.5, 1.0, 1.5 m along x-axis."""
        hw = _hw(n_wifi=4, gpsd=False)
        with _mock_io(
            choices  = ['trilateration', 'wlan0', 'wlan1', 'wlan2', 'wlan3',
                        'dipole_stick', '2.4ghz', 'none'],
            ints     = [4],
            strings  = ['/tmp/aw-pos.jsonl'],
            confirms = [True],
        ):
            cfg = _wiz._wizard_quick(hw)

        for i, ant in enumerate(cfg['antennas']):
            assert ant['position'][0] == pytest.approx(i * 0.5)
            assert ant['position'][1] == pytest.approx(0.0)
            assert ant['position'][2] == pytest.approx(0.0)


class TestQuickWizardArraySensing:
    """Quick wizard — array sensing mode."""

    def test_two_antennas_no_gpsd(self):
        """2 antennas, no gpsd → array sensing config."""
        hw = _hw(n_wifi=2, gpsd=False)
        #   _choose: mode, wlan0, wlan1, ant_type, freq, gps_alt
        #   _confirm: same_type=T
        with _mock_io(
            choices  = ['array_sensing', 'wlan0', 'wlan1', 'dipole_stick', '2.4ghz', 'ip'],
            ints     = [2],
            strings  = ['/tmp/aw-array.jsonl'],
            confirms = [True],
        ):
            cfg = _wiz._wizard_quick(hw)

        assert cfg['mode'] == 'array_sensing'
        assert len(cfg['antennas']) == 2
        assert cfg['gps']['backend'] == 'ip'
        assert cfg['imu'] == {'backend': 'null'}
        mc = cfg['mode_config']
        assert mc['channel'] == 6
        assert mc['history_len'] == 100
        assert mc['calibration_frames'] == 50
        assert mc['sensitivity'] == pytest.approx(0.05)
        assert mc['hysteresis'] == pytest.approx(0.4)
        assert mc['ema_alpha'] == pytest.approx(0.3)

    def test_single_antenna_isotropic(self):
        """1 antenna with isotropic pattern."""
        hw = _hw(n_wifi=1, gpsd=False)
        with _mock_io(
            choices  = ['array_sensing', 'wlan0', 'isotropic', '2.4ghz', 'none'],
            ints     = [1],
            strings  = ['/tmp/aw-arr1.jsonl'],
        ):
            cfg = _wiz._wizard_quick(hw)

        assert cfg['antennas'][0]['pattern'] == 'isotropic'
        assert cfg['antennas'][0]['gain_dbi'] == pytest.approx(0.0)

    def test_channel_step_skipped(self):
        """Array sensing skips the channel-split step entirely."""
        hw = _hw(n_wifi=2, gpsd=False)
        # If _confirm were called for channel split it would raise StopIteration;
        # providing no confirms (except same_type) proves the step is skipped.
        with _mock_io(
            choices  = ['array_sensing', 'wlan0', 'wlan1', 'dipole_stick', '2.4ghz', 'none'],
            ints     = [2],
            strings  = ['/tmp/aw-arr2.jsonl'],
            confirms = [True],   # only same_type confirm
        ):
            cfg = _wiz._wizard_quick(hw)

        assert cfg['mode'] == 'array_sensing'


# ── Custom wizard ─────────────────────────────────────────────────────────────

class TestCustomWizardWardriver:
    """Custom wizard — wardriving mode."""

    def test_minimal_wardriver_gpsd_software_jsonl(self):
        """1 wifi, gpsd, software sync, jsonl output."""
        hw = _hw(n_wifi=1, gpsd=True)
        #   _choose: mode, iface, freq, ant_type, gps='gpsd', imu='none',
        #            sync='software', output_fmt='jsonl'
        #   _ask_int: n_ant=1, gps_port=2947
        #   _ask_str: gps_host, channels_raw, out_path
        #   _ask_float: hop_interval
        with _mock_io(
            choices  = ['wardriver', 'wlan0', '2.4ghz', 'dipole_stick',
                        'gpsd', 'none', 'software', 'jsonl'],
            ints     = [1, 2947],
            strings  = ['localhost', '1,2,3,4,5,6,7,8,9,10,11,12,13',
                        '/tmp/aw-cw.jsonl'],
            floats   = [0.1],
        ):
            cfg = _wiz._wizard_custom(hw)

        assert cfg['mode'] == 'wardriver'
        assert len(cfg['antennas']) == 1
        assert cfg['gps'] == {'backend': 'gpsd', 'host': 'localhost', 'port': 2947}
        assert cfg['imu'] == {'backend': 'null'}
        assert cfg['sync'] == {'source': 'software'}
        mc = cfg['mode_config']
        assert mc['channels'] == list(range(1, 14))
        assert mc['hop_interval'] == pytest.approx(0.1)
        assert cfg['output']['format'] == 'jsonl'

    def test_no_file_output(self):
        """Output format 'none' → no path asked."""
        hw = _hw(n_wifi=1, gpsd=False)
        with _mock_io(
            choices  = ['wardriver', 'wlan0', '2.4ghz', 'dipole_stick',
                        'none', 'none', 'software', 'none'],
            ints     = [1],
            strings  = ['1,2,3,4,5,6,7,8,9,10,11,12,13'],
            floats   = [0.1],
        ):
            cfg = _wiz._wizard_custom(hw)

        assert cfg['output']['format'] == 'none'
        assert 'path' not in cfg['output']

    def test_csv_output(self):
        """CSV output format."""
        hw = _hw(n_wifi=1, gpsd=False)
        with _mock_io(
            choices  = ['wardriver', 'wlan0', '2.4ghz', 'dipole_stick',
                        'none', 'none', 'software', 'csv'],
            ints     = [1],
            strings  = ['1,2,3,4,5,6,7,8,9,10,11,12,13', '/tmp/aw.csv'],
            floats   = [0.1],
        ):
            cfg = _wiz._wizard_custom(hw)

        assert cfg['output']['format'] == 'csv'
        assert cfg['output']['path'] == '/tmp/aw.csv'

    def test_serial_imu_wardriver(self):
        """Wardriver with serial IMU augmentation."""
        hw = _hw(n_wifi=1, gpsd=False, serial=['/dev/ttyUSB0'])
        #   _choose:  mode, iface, freq, ant_type, gps='none', imu='serial',
        #             sync='software', output='jsonl'
        #   _ask_str (in order): imu_device, channels, out_path
        #   _ask_int (in order): n_ant, imu_baud
        #   _ask_float: hop_interval
        with _mock_io(
            choices  = ['wardriver', 'wlan0', '2.4ghz', 'dipole_stick',
                        'none', 'serial', 'software', 'jsonl'],
            ints     = [1, 115200],
            strings  = ['/dev/ttyUSB0',
                        '1,2,3,4,5,6,7,8,9,10,11,12,13', '/tmp/aw-imu.jsonl'],
            floats   = [0.1],
        ):
            cfg = _wiz._wizard_custom(hw)

        assert cfg['imu']['backend'] == 'serial'
        assert cfg['imu']['device'] == '/dev/ttyUSB0'
        assert cfg['imu']['baud'] == 115200

    def test_ntp_sync(self):
        """NTP time sync selected."""
        hw = _hw(n_wifi=1, gpsd=False)
        with _mock_io(
            choices  = ['wardriver', 'wlan0', '2.4ghz', 'dipole_stick',
                        'none', 'none', 'ntp', 'jsonl'],
            ints     = [1],
            strings  = ['1,2,3,4,5,6,7,8,9,10,11,12,13', '/tmp/aw-ntp.jsonl'],
            floats   = [0.1],
        ):
            cfg = _wiz._wizard_custom(hw)

        assert cfg['sync']['source'] == 'ntp'

    def test_custom_channel_list(self):
        """Custom channel list string is parsed correctly."""
        hw = _hw(n_wifi=1, gpsd=False)
        with _mock_io(
            choices  = ['wardriver', 'wlan0', '2.4ghz', 'dipole_stick',
                        'none', 'none', 'software', 'jsonl'],
            ints     = [1],
            strings  = ['1,6,11', '/tmp/aw-ch.jsonl'],
            floats   = [0.2],
        ):
            cfg = _wiz._wizard_custom(hw)

        assert cfg['mode_config']['channels'] == [1, 6, 11]
        assert cfg['mode_config']['hop_interval'] == pytest.approx(0.2)


class TestCustomWizardTrilateration:
    """Custom wizard — trilateration mode with geometry."""

    def test_two_antennas_static_gps_pps_sync(self):
        """2 antennas, static GPS, PPS sync, null IMU, jsonl output."""
        hw = _hw(n_wifi=2, gpsd=False, pps=['/dev/pps0'])
        #   _choose: mode, hw0, freq0, type0, hw1, freq1, type1,
        #            gps='static', imu='null', sync='pps', ref_ant, output='jsonl'
        #   _ask_int: n_ant=2, channel=6
        #   _ask_str: pps_device, out_path
        #   _ask_float: 6 pos floats × 2 antennas + lat/lon/alt + corr/timeout
        floats = (
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]  # antenna 0 pos/euler
          + [0.5, 0.0, 0.0, 0.0, 0.0, 0.0]  # antenna 1 pos/euler
          + [48.8566, 2.3522, 35.0]           # static GPS
          + [0.001, 0.05]                     # corr_window, group_timeout
        )
        with _mock_io(
            choices  = ['trilateration',
                        'wlan0', '2.4ghz', 'dipole_stick',
                        'wlan1', '2.4ghz', 'dipole_stick',
                        'static', 'null', 'pps', 'wlan0', 'jsonl'],
            ints     = [2, 6],
            strings  = ['/dev/pps0', '/tmp/aw-trilat.jsonl'],
            floats   = floats,
        ):
            cfg = _wiz._wizard_custom(hw)

        assert cfg['mode'] == 'trilateration'
        assert len(cfg['antennas']) == 2
        assert cfg['gps'] == {
            'backend': 'static', 'lat': pytest.approx(48.8566),
            'lon': pytest.approx(2.3522), 'alt': pytest.approx(35.0),
        }
        assert cfg['imu'] == {'backend': 'null'}
        assert cfg['sync'] == {'source': 'pps', 'device': '/dev/pps0'}
        mc = cfg['mode_config']
        assert mc['channel'] == 6
        assert mc['reference_antenna'] == 'wlan0'
        assert mc['correlation_window'] == pytest.approx(0.001)
        assert mc['group_timeout'] == pytest.approx(0.05)
        assert cfg['antennas'][0]['position'] == pytest.approx([0.0, 0.0, 0.0])
        assert cfg['antennas'][1]['position'] == pytest.approx([0.5, 0.0, 0.0])

    def test_gpsd_gps_software_sync(self):
        """Trilateration with gpsd GPS and software sync."""
        hw = _hw(n_wifi=2, gpsd=True)
        floats = [0.0] * 6 + [0.5] + [0.0] * 5 + [0.001, 0.05]
        with _mock_io(
            choices  = ['trilateration',
                        'wlan0', '2.4ghz', 'dipole_stick',
                        'wlan1', '2.4ghz', 'dipole_stick',
                        'gpsd', 'null', 'software', 'wlan0', 'jsonl'],
            ints     = [2, 2947, 6],
            strings  = ['localhost', '/tmp/aw-ts.jsonl'],
            floats   = floats,
        ):
            cfg = _wiz._wizard_custom(hw)

        assert cfg['gps']['backend'] == 'gpsd'
        assert cfg['gps']['host'] == 'localhost'
        assert cfg['gps']['port'] == 2947
        assert cfg['sync']['source'] == 'software'

    def test_serial_imu_trilateration(self):
        """Trilateration with serial IMU for orientation tracking."""
        hw = _hw(n_wifi=2, gpsd=False, serial=['/dev/ttyUSB0'])
        floats = [0.0] * 6 + [0.5] + [0.0] * 5 + [0.001, 0.05]
        with _mock_io(
            choices  = ['trilateration',
                        'wlan0', '2.4ghz', 'dipole_stick',
                        'wlan1', '2.4ghz', 'dipole_stick',
                        'none', 'serial', 'software', 'wlan0', 'jsonl'],
            ints     = [2, 115200, 6],
            strings  = ['/dev/ttyUSB0', '/tmp/aw-imu.jsonl'],
            floats   = floats,
        ):
            cfg = _wiz._wizard_custom(hw)

        assert cfg['imu']['backend'] == 'serial'
        assert cfg['imu']['device'] == '/dev/ttyUSB0'
        assert cfg['imu']['baud'] == 115200

    def test_gpsdo_sync_with_device(self):
        """GPSDO sync asks for device path."""
        hw = _hw(n_wifi=2, gpsd=False, pps=['/dev/pps0'])
        floats = [0.0] * 6 + [0.5] + [0.0] * 5 + [0.001, 0.05]
        with _mock_io(
            choices  = ['trilateration',
                        'wlan0', '2.4ghz', 'dipole_stick',
                        'wlan1', '2.4ghz', 'dipole_stick',
                        'none', 'null', 'gpsdo', 'wlan0', 'jsonl'],
            ints     = [2, 6],
            strings  = ['/dev/pps0', '/tmp/aw-gpsdo.jsonl'],
            floats   = floats,
        ):
            cfg = _wiz._wizard_custom(hw)

        assert cfg['sync']['source'] == 'gpsdo'
        assert cfg['sync']['device'] == '/dev/pps0'


class TestCustomWizardArraySensing:
    """Custom wizard — array sensing mode with geometry."""

    def test_single_antenna_no_gps_software_sync(self):
        """1 antenna, no GPS, null IMU, software sync, jsonl output."""
        hw = _hw(n_wifi=1, gpsd=False)
        #   pos floats: x, y, z, roll, pitch, yaw  (6 total per antenna)
        #   mode_config floats: sensitivity, hysteresis, ema_alpha
        floats = [0.0] * 6 + [0.05, 0.4, 0.3]
        with _mock_io(
            choices  = ['array_sensing',
                        'wlan0', '2.4ghz', 'isotropic',
                        'none', 'null', 'software', 'jsonl'],
            ints     = [1, 6, 100, 50],
            strings  = ['/tmp/aw-array.jsonl'],
            floats   = floats,
        ):
            cfg = _wiz._wizard_custom(hw)

        assert cfg['mode'] == 'array_sensing'
        assert len(cfg['antennas']) == 1
        assert cfg['gps'] == {'backend': 'none'}
        assert cfg['imu'] == {'backend': 'null'}
        mc = cfg['mode_config']
        assert mc['channel'] == 6
        assert mc['history_len'] == 100
        assert mc['calibration_frames'] == 50
        assert mc['sensitivity'] == pytest.approx(0.05)
        assert mc['hysteresis'] == pytest.approx(0.4)
        assert mc['ema_alpha'] == pytest.approx(0.3)

    def test_two_antennas_with_geometry(self):
        """2 antennas each with custom position offsets."""
        hw = _hw(n_wifi=2, gpsd=False)
        floats = (
            [1.0, 0.0, 0.0, 0.0, 0.0, 0.0]   # antenna 0
          + [0.0, 1.0, 0.0, 0.0, 0.0, 0.0]   # antenna 1
          + [0.05, 0.4, 0.3]                  # sensitivity, hysteresis, ema_alpha
        )
        with _mock_io(
            choices  = ['array_sensing',
                        'wlan0', '2.4ghz', 'dipole_stick',
                        'wlan1', '2.4ghz', 'dipole_stick',
                        'none', 'null', 'software', 'jsonl'],
            ints     = [2, 6, 100, 50],
            strings  = ['/tmp/aw-arr2.jsonl'],
            floats   = floats,
        ):
            cfg = _wiz._wizard_custom(hw)

        assert cfg['antennas'][0]['position'] == pytest.approx([1.0, 0.0, 0.0])
        assert cfg['antennas'][1]['position'] == pytest.approx([0.0, 1.0, 0.0])

    def test_custom_sensitivity_values(self):
        """Non-default sensitivity and hysteresis values."""
        hw = _hw(n_wifi=1, gpsd=False)
        floats = [0.0] * 6 + [0.15, 0.6, 0.5]
        with _mock_io(
            choices  = ['array_sensing',
                        'wlan0', '2.4ghz', 'dipole_stick',
                        'none', 'null', 'software', 'jsonl'],
            ints     = [1, 6, 200, 80],
            strings  = ['/tmp/aw-sens.jsonl'],
            floats   = floats,
        ):
            cfg = _wiz._wizard_custom(hw)

        mc = cfg['mode_config']
        assert mc['sensitivity'] == pytest.approx(0.15)
        assert mc['hysteresis'] == pytest.approx(0.6)
        assert mc['ema_alpha'] == pytest.approx(0.5)
        assert mc['history_len'] == 200
        assert mc['calibration_frames'] == 80


class TestCustomWizardGPSPaths:
    """Custom wizard — GPS backend path coverage."""

    def _run_wardriver_with_gps(self, gps_choices, gps_ints, gps_strings, gps_floats):
        hw = _hw(n_wifi=1, gpsd=False)
        choices  = ['wardriver', 'wlan0', '2.4ghz', 'dipole_stick'] + gps_choices \
                 + ['none', 'software', 'jsonl']
        strings  = gps_strings + ['1,2,3,4,5,6,7,8,9,10,11,12,13', '/tmp/aw.jsonl']
        ints     = [1] + gps_ints
        floats   = gps_floats + [0.1]
        with _mock_io(choices=choices, ints=ints, strings=strings, floats=floats):
            return _wiz._wizard_custom(hw)

    def test_mls_gps_with_interface_and_url(self):
        """MLS GPS backend asks for wifi interface and API URL."""
        hw = _hw(n_wifi=1, gpsd=False)
        with _mock_io(
            choices  = ['wardriver', 'wlan0', '2.4ghz', 'dipole_stick',
                        'mls', 'none', 'software', 'jsonl'],
            ints     = [1],
            strings  = ['wlan0', 'https://location.services.mozilla.com/v1/geolocate?key=test',
                        '1,2,3,4,5,6,7,8,9,10,11,12,13', '/tmp/aw.jsonl'],
            floats   = [0.1],
        ):
            cfg = _wiz._wizard_custom(hw)

        assert cfg['gps']['backend'] == 'mls'
        assert cfg['gps']['interface'] == 'wlan0'
        assert 'api_url' in cfg['gps']

    def test_static_gps_lat_lon_alt(self):
        """Static GPS asks for lat, lon, alt."""
        hw = _hw(n_wifi=1, gpsd=False)
        with _mock_io(
            choices  = ['wardriver', 'wlan0', '2.4ghz', 'dipole_stick',
                        'static', 'none', 'software', 'jsonl'],
            ints     = [1],
            strings  = ['1,2,3,4,5,6,7,8,9,10,11,12,13', '/tmp/aw.jsonl'],
            floats   = [51.5074, -0.1278, 10.0, 0.1],
        ):
            cfg = _wiz._wizard_custom(hw)

        assert cfg['gps']['backend'] == 'static'
        assert cfg['gps']['lat'] == pytest.approx(51.5074)
        assert cfg['gps']['lon'] == pytest.approx(-0.1278)
        assert cfg['gps']['alt'] == pytest.approx(10.0)

    def test_geoclue_gps(self):
        """GeoClue2 GPS backend — no extra questions."""
        hw = _hw(n_wifi=1, gpsd=False)
        with _mock_io(
            choices  = ['wardriver', 'wlan0', '2.4ghz', 'dipole_stick',
                        'geoclue', 'none', 'software', 'jsonl'],
            ints     = [1],
            strings  = ['1,2,3,4,5,6,7,8,9,10,11,12,13', '/tmp/aw.jsonl'],
            floats   = [0.1],
        ):
            cfg = _wiz._wizard_custom(hw)

        assert cfg['gps'] == {'backend': 'geoclue'}

    def test_ip_gps(self):
        """IP geolocation GPS backend — no extra questions."""
        hw = _hw(n_wifi=1, gpsd=False)
        with _mock_io(
            choices  = ['wardriver', 'wlan0', '2.4ghz', 'dipole_stick',
                        'ip', 'none', 'software', 'jsonl'],
            ints     = [1],
            strings  = ['1,2,3,4,5,6,7,8,9,10,11,12,13', '/tmp/aw.jsonl'],
            floats   = [0.1],
        ):
            cfg = _wiz._wizard_custom(hw)

        assert cfg['gps'] == {'backend': 'ip'}

    def test_no_gps(self):
        """'none' GPS backend — no extra questions."""
        hw = _hw(n_wifi=1, gpsd=False)
        with _mock_io(
            choices  = ['wardriver', 'wlan0', '2.4ghz', 'dipole_stick',
                        'none', 'none', 'software', 'jsonl'],
            ints     = [1],
            strings  = ['1,2,3,4,5,6,7,8,9,10,11,12,13', '/tmp/aw.jsonl'],
            floats   = [0.1],
        ):
            cfg = _wiz._wizard_custom(hw)

        assert cfg['gps'] == {'backend': 'none'}


class TestCustomWizardFreqRange:
    """Custom wizard — frequency range selection via _configure_one_antenna."""

    def test_5ghz_freq(self):
        hw = _hw(n_wifi=1, gpsd=False)
        with _mock_io(
            choices  = ['wardriver', 'wlan0', '5ghz', 'dipole_stick',
                        'none', 'none', 'software', 'jsonl'],
            ints     = [1],
            strings  = ['1,2,3,4,5,6,7,8,9,10,11,12,13', '/tmp/aw.jsonl'],
            floats   = [0.1],
        ):
            cfg = _wiz._wizard_custom(hw)

        assert cfg['antennas'][0]['frequency_range'] == pytest.approx([5.0e9, 5.9e9])

    def test_dual_band_freq(self):
        hw = _hw(n_wifi=1, gpsd=False)
        with _mock_io(
            choices  = ['wardriver', 'wlan0', 'both', 'dipole_stick',
                        'none', 'none', 'software', 'jsonl'],
            ints     = [1],
            strings  = ['1,2,3,4,5,6,7,8,9,10,11,12,13', '/tmp/aw.jsonl'],
            floats   = [0.1],
        ):
            cfg = _wiz._wizard_custom(hw)

        assert cfg['antennas'][0]['frequency_range'] == pytest.approx([2.4e9, 5.9e9])

    def test_custom_freq_range(self):
        hw = _hw(n_wifi=1, gpsd=False)
        with _mock_io(
            choices  = ['wardriver', 'wlan0', 'custom', 'dipole_stick',
                        'none', 'none', 'software', 'jsonl'],
            ints     = [1],
            strings  = ['1,2,3,4,5,6,7,8,9,10,11,12,13', '/tmp/aw.jsonl'],
            floats   = [433e6, 434e6, 0.1],
        ):
            cfg = _wiz._wizard_custom(hw)

        fr = cfg['antennas'][0]['frequency_range']
        assert fr[0] == pytest.approx(433e6)
        assert fr[1] == pytest.approx(434e6)


class TestConfigOutputStructure:
    """Validate the output config dict structure for all modes."""

    REQUIRED_TOP = ('mode', 'array_id', 'antennas', 'gps', 'imu', 'sync',
                    'mode_config', 'output')

    def _quick_wardriver(self, n_wifi=1):
        hw = _hw(n_wifi=n_wifi, gpsd=False)
        with _mock_io(
            choices  = ['wardriver', 'wlan0', 'dipole_stick', '2.4ghz', 'none', 'none'],
            ints     = [1],
            strings  = ['/tmp/aw.jsonl'],
        ):
            return _wiz._wizard_quick(hw)

    def _quick_trilateration(self, n_wifi=4):
        hw = _hw(n_wifi=n_wifi, gpsd=False)
        choices = ['trilateration'] + [f'wlan{i}' for i in range(n_wifi)] \
                + ['dipole_stick', '2.4ghz', 'none']
        confirms = [True]  # same type
        with _mock_io(choices=choices, ints=[n_wifi], strings=['/tmp/aw.jsonl'],
                      confirms=confirms):
            return _wiz._wizard_quick(hw)

    def _quick_array_sensing(self, n_wifi=2):
        hw = _hw(n_wifi=n_wifi, gpsd=False)
        with _mock_io(
            choices  = ['array_sensing', 'wlan0', 'wlan1', 'dipole_stick', '2.4ghz', 'none'],
            ints     = [n_wifi],
            strings  = ['/tmp/aw.jsonl'],
            confirms = [True],
        ):
            return _wiz._wizard_quick(hw)

    def test_wardriver_has_all_keys(self):
        cfg = self._quick_wardriver()
        for k in self.REQUIRED_TOP:
            assert k in cfg

    def test_trilateration_has_all_keys(self):
        cfg = self._quick_trilateration()
        for k in self.REQUIRED_TOP:
            assert k in cfg

    def test_array_sensing_has_all_keys(self):
        cfg = self._quick_array_sensing()
        for k in self.REQUIRED_TOP:
            assert k in cfg

    def test_antenna_has_required_fields(self):
        cfg = self._quick_wardriver()
        for ant in cfg['antennas']:
            for field in ('id', 'backend', 'backend_config', 'position',
                          'orientation_euler', 'frequency_range',
                          'pattern', 'gain_dbi'):
                assert field in ant

    def test_wardriver_mode_config_keys(self):
        cfg = self._quick_wardriver()
        mc = cfg['mode_config']
        assert 'channels' in mc
        assert 'hop_interval' in mc
        assert isinstance(mc['channels'], list)
        assert all(isinstance(ch, int) for ch in mc['channels'])

    def test_trilateration_mode_config_keys(self):
        cfg = self._quick_trilateration()
        mc = cfg['mode_config']
        assert 'channel' in mc
        assert 'reference_antenna' in mc
        assert 'correlation_window' in mc
        assert 'group_timeout' in mc

    def test_array_sensing_mode_config_keys(self):
        cfg = self._quick_array_sensing()
        mc = cfg['mode_config']
        for k in ('channel', 'history_len', 'calibration_frames',
                  'sensitivity', 'hysteresis', 'ema_alpha'):
            assert k in mc

    def test_array_id_always_default(self):
        assert self._quick_wardriver()['array_id'] == 'default'
        assert self._quick_trilateration()['array_id'] == 'default'
        assert self._quick_array_sensing()['array_id'] == 'default'

    def test_sync_always_present(self):
        assert 'source' in self._quick_wardriver()['sync']
        assert 'source' in self._quick_trilateration()['sync']
        assert 'source' in self._quick_array_sensing()['sync']


# ── _confirm back-navigation ──────────────────────────────────────────────────

class TestConfirmWizardAbort:
    """
    _confirm() must raise _WizardAbort on 'q' — consistent with every other
    wizard input primitive.  Before the fix it silently returned False, making
    the user unable to go back from a yes/no prompt.
    """

    def test_q_raises_wizard_abort(self):
        with patch('cli.aetherward._raw', return_value='q'):
            with pytest.raises(_aw._WizardAbort):
                _aw._confirm('Continue?')

    def test_Q_uppercase_raises_wizard_abort(self):
        with patch('cli.aetherward._raw', return_value='Q'):
            with pytest.raises(_aw._WizardAbort):
                _aw._confirm('Continue?')

    def test_y_returns_true(self):
        with patch('cli.aetherward._raw', return_value='y'):
            assert _aw._confirm('Continue?') is True

    def test_yes_returns_true(self):
        with patch('cli.aetherward._raw', return_value='yes'):
            assert _aw._confirm('Continue?') is True

    def test_n_returns_false(self):
        with patch('cli.aetherward._raw', return_value='n'):
            assert _aw._confirm('Continue?') is False

    def test_enter_uses_default_true(self):
        with patch('cli.aetherward._raw', return_value=''):
            assert _aw._confirm('Continue?', default=True) is True

    def test_enter_uses_default_false(self):
        with patch('cli.aetherward._raw', return_value=''):
            assert _aw._confirm('Continue?', default=False) is False
