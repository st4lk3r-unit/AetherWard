"""Wizard steps, step runner, and quick/custom paths for the aetherward CLI."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from cli.aetherward import (
    MozillaLBSBackend,
    _BLD, _RED, _YLW, _WizardAbort,
    _HELP_ANT_ORIENTATION, _HELP_ANT_POSITION, _HELP_CALIB, _HELP_CHANNELS,
    _HELP_CORR_WINDOW, _HELP_GPS, _HELP_HOP, _HELP_HYSTERESIS, _HELP_IMU,
    _HELP_IMU_WARDRIVE, _HELP_MODE, _HELP_REF_ANT, _HELP_SENSITIVITY, _HELP_SYNC,
    _ask_antenna_type, _ask_float, _ask_freq_range, _ask_int, _ask_str,
    _c, _choose, _confirm, _dim, _lbl, _ok, _path, _print_kv,
    _save_config, _scan_hardware, _sep, _val,
)
from cli._commands import _run_session
from aetherward.session import default_session_dir, default_session_path

# ── Step runner (q = go back one step) ───────────────────────────────────────
class _StepRunner:
    """
    Runs wizard sections sequentially with q-to-go-back navigation.

    Each step is a callable (ctx: dict) -> dict.  It receives the accumulated
    results of all completed steps and returns a dict to merge into ctx.

    Pressing q inside a step raises _WizardAbort.  The runner catches it and
    re-runs the previous step.  q at step 0 propagates to exit the wizard.
    """

    def __init__(self) -> None:
        self._steps: list[tuple[str, object]] = []

    def add(self, label: str, fn) -> None:
        self._steps.append((label, fn))

    def run(self) -> dict:
        n      = len(self._steps)
        saved: list[Optional[dict]] = [None] * n
        i = 0
        while i < n:
            _, fn = self._steps[i]
            ctx: dict = {}
            for j in range(i):
                if saved[j]:
                    ctx.update(saved[j])
            try:
                r     = fn(ctx)
                saved[i] = r
                i += 1
            except _WizardAbort:
                if i > 0:
                    print(f'\n  {_dim("← back")}')
                    i -= 1
                else:
                    raise
        out: dict = {}
        for s in saved:
            if s:
                out.update(s)
        return out


# ── Per-antenna configuration (inner step runner for question-level back) ─────
def _configure_one_antenna(hw: dict, idx: int, mode: str,
                           needs_geometry: bool) -> dict:
    """
    Configure a single antenna with per-question back navigation.
    Pressing q at any question goes back to the previous question;
    pressing q at the first question propagates _WizardAbort to the caller.
    """
    detected_opts = [
        (w['name'], w['name'],
         (w['driver'] or '') + ('  monitor' if w['monitor'] else ''))
        for w in hw['wifi']
    ] + [('__custom__', 'Other / enter manually', '')]

    inner = _StepRunner()

    def _sub_hw(ctx: dict) -> dict:
        if detected_opts[:-1]:
            default_idx = min(idx + 1, len(detected_opts))
            choice = _choose('  Hardware', detected_opts, default=default_idx)
        else:
            choice = '__custom__'
        backend = 'plugins.wifi_nl80211.NL80211Backend'
        bc: dict = {}
        if choice == '__custom__':
            bk = _choose('  Backend type', [
                ('wifi',   'WiFi nl80211', 'Linux 802.11 monitor mode'),
                ('rtlsdr', 'RTL-SDR',      'RTL2832U dongle'),
                ('hackrf', 'HackRF',       'HackRF One'),
                ('null',   'Null / test',  'no hardware — for development'),
            ], help_text=(
                'Hardware backend',
                'The backend drives the physical radio hardware.\n\n'
                '  WiFi nl80211 — Standard Linux wireless driver interface.\n'
                '    Adapter must support monitor mode.\n'
                '    Check: iw list | grep -A 10 "Supported interface modes"\n\n'
                '  RTL-SDR      — Cheap DVB-T dongle repurposed as SDR.\n'
                '    Frequency range: 24 MHz – 1.7 GHz (device-dependent).\n'
                '    Install pyrtlsdr: pip install "aetherward[sdr]"\n\n'
                '  HackRF       — Full-duplex SDR, 1 MHz – 6 GHz.\n\n'
                '  Null / test  — Generates no data.  Use for config testing.'
            ))
            if bk == 'wifi':
                iface = _ask_str('  Interface', f'wlan{idx}')
                bc, ant_id = {'interface': iface}, iface
            elif bk == 'rtlsdr':
                backend = 'plugins.rtlsdr.RTLSDRBackend'
                bc, ant_id = {'device_index': _ask_int('  Device index', idx)}, f'rtlsdr{idx}'
            elif bk == 'hackrf':
                backend = 'plugins.hackrf.HackRFBackend'
                bc = {'serial': _ask_str('  Serial  (blank = first found)', '', required=False)}
                ant_id = f'hackrf{idx}'
            else:
                backend, ant_id = 'null', f'ant{idx}'
        else:
            bc, ant_id = {'interface': choice}, choice
        return {'backend': backend, 'bc': bc, 'ant_id': ant_id}

    def _sub_pos(ctx: dict) -> dict:
        print(f'\n  {_lbl("Position in array")}  {_dim("ENU offset from array centre  (metres)")}')
        px = _ask_float('    x  East  m',  round(idx * 0.5, 2), help_text=_HELP_ANT_POSITION)
        py = _ask_float('    y  North m',  0.0,                 help_text=_HELP_ANT_POSITION)
        pz = _ask_float('    z  Up    m',  0.0,                 help_text=_HELP_ANT_POSITION)
        print(f'\n  {_lbl("Orientation")}  {_dim("ZYX Euler angles  (degrees)  — ? for explanation")}')
        roll  = _ask_float('    roll   °', 0.0, help_text=_HELP_ANT_ORIENTATION)
        pitch = _ask_float('    pitch  °', 0.0, help_text=_HELP_ANT_ORIENTATION)
        yaw   = _ask_float('    yaw    °', 0.0, help_text=_HELP_ANT_ORIENTATION)
        return {'px': px, 'py': py, 'pz': pz, 'roll': roll, 'pitch': pitch, 'yaw': yaw}

    def _sub_freq(ctx: dict) -> dict:
        return {'freq': _ask_freq_range()}

    def _sub_type(ctx: dict) -> dict:
        print(f'\n  {_lbl("Antenna type")}')
        pat, gain = _ask_antenna_type()
        return {'pat': pat, 'gain': gain}

    inner.add('hw',   _sub_hw)
    if needs_geometry:
        inner.add('pos', _sub_pos)
    else:
        print(f'\n  {_dim("Position and orientation not used in wardriving — using array defaults")}')
    inner.add('freq', _sub_freq)
    inner.add('type', _sub_type)

    ctx = inner.run()
    return {
        'id':                ctx['ant_id'],
        'backend':           ctx['backend'],
        'backend_config':    ctx['bc'],
        'position':          [ctx.get('px', round(idx * 0.5, 2)),
                              ctx.get('py', 0.0), ctx.get('pz', 0.0)],
        'orientation_euler': [ctx.get('roll', 0.0), ctx.get('pitch', 0.0), ctx.get('yaw', 0.0)],
        'frequency_range':   ctx['freq'],
        'pattern':           ctx['pat'],
        'gain_dbi':          ctx['gain'],
    }


def _default_session_desc() -> str:
    return f'{default_session_dir()}/<config>-YYYYmmdd-HHMMSS.jsonl'


def _choose_session_output_path(default_name: str = 'default', suffix: str = '.jsonl') -> tuple[bool, Optional[str]]:
    """Return (use_default_folder, custom_path)."""
    default_example = default_session_path(default_name, 'wardriver', suffix=suffix)
    choice = _ask_str(
        'Use default sessions path or custom path', 'default',
        hint='default/custom or direct file path',
        help_text=(
            'Session file location',
            'Type "default" to store each run as a new timestamped file in:\n'
            f'  {default_session_dir()}\n\n'
            'Type "custom" to choose an exact path.  You can also paste a\n'
            'file path directly here for faster setup.'
        ),
    ).strip()
    low = choice.lower()
    if low in ('default', 'd'):
        return True, None
    if low in ('custom', 'c'):
        path = _ask_str(
            'Save captures to', default_example,
            hint=f'{suffix} — one record per frame',
            help_text=(
                'Output file path',
                'Every captured frame is appended as a single JSON line.\n'
                'The file is created on first capture and never truncated,\n'
                'so you can safely resume interrupted sessions.\n\n'
                'Open in another terminal while running:\n'
                f'  tail -f {default_example} | jq .'
            ),
        )
        return False, path
    return False, choice


# ── Wizard — quick path ───────────────────────────────────────────────────────
def _wizard_quick(hw: dict) -> Optional[dict]:  # noqa: C901
    wifi = hw['wifi']
    runner = _StepRunner()

    def _step_mode(ctx: dict) -> dict:
        _sep('Mode')
        mode = _choose('What do you want to do?', [
            ('wardriver',     'Wardriving',    'scan channels, log signals, GPS-tag everything'),
            ('trilateration', 'Trilateration', 'pinpoint a transmitter with multiple antennas'),
            ('array_sensing', 'Presence',      'detect people or movement passively with RF'),
        ], help_text=_HELP_MODE)
        if mode == 'trilateration':
            if len(wifi) < 4:
                print(f'\n  {_c("!", _YLW, _BLD)} Trilateration needs ≥ 4 antennas for 3-D solve.')
                print(f'  {_dim("Detected")} {len(wifi)}.  {_dim("You can add hardware later.")}')
            print(f'  {_dim("Note: software clock used in quick setup.")}')
            print(f'  {_dim("For useful accuracy, use custom setup and choose PPS sync.")}')
        return {'mode': mode}

    def _step_antennas(ctx: dict) -> dict:
        _sep('Antennas')
        ant_opts = [
            (w['name'], w['name'],
             (w['driver'] or 'wireless') + ('  ' + _ok('monitor') if w['monitor'] else ''))
            for w in wifi
        ] + [('__custom__', 'Other / not listed', 'enter interface name manually')]
        if len(wifi) >= 2:
            print(f'  {_dim("Tip: more antennas = better channel coverage.")}')
        n_ant = _ask_int('How many antennas?', max(1, len(wifi)), help_text=(
            'Number of antennas',
            'How many wireless adapters to use in this session.\n'
            'More antennas means faster channel coverage — channels\n'
            'are split evenly so nothing is scanned twice.\n'
            'Trilateration needs at least 4.'
        ))
        if n_ant is None:
            n_ant = max(1, len(wifi))
        # Per-antenna interface selection with per-antenna back navigation.
        selected_ifaces: list[str] = []
        i = 0
        while i < n_ant:
            print(f'\n  {_lbl(f"Antenna {i + 1}")} / {n_ant}')
            try:
                if i < len(wifi):
                    iface = _choose('  Interface', ant_opts, default=i + 1)
                else:
                    iface = '__custom__'
                if iface == '__custom__':
                    iface = _ask_str('  Interface name', f'wlan{i}',
                                     hint='e.g. wlan0  wlan1  wlan2')
                if i < len(selected_ifaces):
                    selected_ifaces[i] = iface
                else:
                    selected_ifaces.append(iface)
                i += 1
            except _WizardAbort:
                if i > 0:
                    print(f'\n  {_dim(f"← back to Antenna {i}")}')
                    i -= 1
                    selected_ifaces = selected_ifaces[:i]
                else:
                    raise  # propagate → step runner goes back to mode
        # Antenna types
        ant_types: list[tuple[str, float]] = []
        if n_ant == 1:
            print(f'\n  {_lbl("Antenna type")}')
            ant_types = [_ask_antenna_type()]
        else:
            same = _confirm('Are all antennas the same type?', default=True)
            if same:
                print(f'\n  {_lbl("Antenna type  (applies to all)")}')
                t = _ask_antenna_type()
                ant_types = [t] * n_ant
            else:
                for j, iface in enumerate(selected_ifaces):
                    print(f'\n  {_lbl(f"Antenna {j + 1}")} ({iface})')
                    ant_types.append(_ask_antenna_type())
        return {'n_ant': n_ant, 'selected_ifaces': selected_ifaces, 'ant_types': ant_types}

    def _step_freq(ctx: dict) -> dict:
        _sep('Frequency')
        return {'freq': _ask_freq_range()}

    def _step_channels(ctx: dict) -> dict:
        mode = ctx['mode']
        n_ant = ctx['n_ant']
        sel   = ctx['selected_ifaces']
        if mode != 'wardriver':
            return {'channel_map': {}}
        _sep('Channels')
        all_ch = list(range(1, 14))
        n = max(n_ant, 1)
        auto_groups = [
            all_ch[(i * len(all_ch)) // n : ((i + 1) * len(all_ch)) // n]
            for i in range(n)
        ]
        print(f'\n  Auto channel split across {n_ant} antenna{"s" if n_ant > 1 else ""}:\n')
        for iface, grp in zip(sel, auto_groups):
            if grp:
                print(f'    {_val(iface):10s}  →  ch {grp[0]}–{grp[-1]}'
                      f'  {_dim(f"({len(grp)} channels)")}')
        print()
        channel_map: dict = {}
        if n_ant > 1 and not _confirm('Use this split?', default=True):
            for iface in sel:
                raw = _ask_str(f'  Channels for {iface}', '1,2,3,4,5,6',
                               hint='comma-separated')
                channel_map[iface] = [int(c.strip()) for c in raw.split(',') if c.strip()]
        else:
            channel_map = {iface: grp for iface, grp in zip(sel, auto_groups)}
        return {'channel_map': channel_map}

    def _step_gps(ctx: dict) -> dict:
        _sep('Position source')
        if hw['gpsd']:
            print(f'  {_ok("gpsd")} detected on localhost:2947\n')
            use_gps = _confirm('Use gpsd (GNSS)?', default=True)
            gps: Optional[dict] = {'backend': 'gpsd', 'host': 'localhost', 'port': 2947} \
                                  if use_gps else None
        else:
            print(f'  {_dim("gpsd not detected.")}\n')
            gps = None
        if gps is None:
            lbs = _choose('Alternative position source', [
                ('geoclue', 'GeoClue2',       'Linux location service — WiFi + cell  (~10-200 m)'),
                ('mls',     'Mozilla LBS',    'WiFi scan → cloud API  (~10-200 m)'),
                ('ip',      'IP geolocation', 'city-level via IP  (~1-50 km)  — no hardware'),
                ('none',    'None',           'no position — captures have no location tags'),
            ], help_text=_HELP_GPS)
            gps = {'backend': lbs}
        return {'gps': gps}

    def _step_output(ctx: dict) -> dict:
        _sep('Output')
        use_default, out_path = _choose_session_output_path('quick')
        return {'out_default': use_default, 'out_path': out_path}

    def _step_imu(ctx: dict) -> dict:
        mode = ctx['mode']
        if mode != 'wardriver':
            return {'imu': {'backend': 'null'}}
        _sep('Position augmentation  (optional)')
        print(f'  {_dim("GPS = absolute anchor.  IMU/encoder = fills gaps between fixes.")}')
        aug = _choose('Relative position sensor', [
            ('none',   'None',            'GPS timestamps only — simple and reliable'),
            ('serial', 'IMU / odometer',  'dead-reckon between GPS fixes via serial stream'),
        ], help_text=_HELP_IMU_WARDRIVE)
        imu: dict = {'backend': 'null'}
        if aug == 'serial':
            default_dev = hw['serial'][0] if hw['serial'] else '/dev/ttyUSB0'
            dev = _ask_str('  Device', default_dev, hint='e.g. /dev/ttyUSB0')
            imu = {'backend': 'serial', 'device': dev, 'baud': 115200}
        return {'imu': imu}

    runner.add('mode',     _step_mode)
    runner.add('antennas', _step_antennas)
    runner.add('freq',     _step_freq)
    runner.add('channels', _step_channels)
    runner.add('gps',      _step_gps)
    runner.add('output',   _step_output)
    runner.add('imu',      _step_imu)

    ctx = runner.run()

    mode = ctx['mode']
    sel  = ctx['selected_ifaces']
    freq = ctx['freq']
    out  = ctx.get('out_path')
    out_default = bool(ctx.get('out_default'))
    imu  = ctx['imu']
    gps  = ctx['gps']

    antennas = []
    for i, (iface, (pat, gain)) in enumerate(zip(sel, ctx['ant_types'])):
        antennas.append({
            'id':                iface,
            'backend':           'plugins.wifi_nl80211.NL80211Backend',
            'backend_config':    {'interface': iface},
            'position':          [round(i * 0.5, 2), 0.0, 0.0],
            'orientation_euler': [0.0, 0.0, 0.0],
            'frequency_range':   freq,
            'pattern':           pat,
            'gain_dbi':          gain,
        })

    mc: dict = {}
    if mode == 'wardriver':
        mc = {'channels': list(range(1, 14)), 'hop_interval': 0.1,
              'store_raw_frames': True}
        if not out_default and out:
            mc['output_path'] = out
    elif mode == 'trilateration':
        mc = {'channel': 6, 'reference_antenna': antennas[0]['id'],
              'correlation_window': 0.001, 'group_timeout': 0.05}
    elif mode == 'array_sensing':
        mc = {'channel': 6, 'history_len': 100, 'calibration_frames': 50,
              'sensitivity': 0.05, 'hysteresis': 0.4, 'ema_alpha': 0.3}

    return {
        'mode':        mode,
        'array_id':    'default',
        'antennas':    antennas,
        'gps':         gps,
        'imu':         imu,
        'sync':        {'source': 'software'},
        'mode_config': mc,
        'output':      ({'format': 'jsonl', 'path_policy': 'default'}
                        if out_default else {'format': 'jsonl', 'path': out}),
    }


# ── Wizard — full custom path ─────────────────────────────────────────────────
def _wizard_custom(hw: dict) -> Optional[dict]:  # noqa: C901
    runner = _StepRunner()

    def _step_mode(ctx: dict) -> dict:
        _sep('Mode')
        mode = _choose('Operating mode', [
            ('wardriver',     'Wardriver',     'channel scan + GPS correlation'),
            ('trilateration', 'Trilateration', 'TDOA-based source positioning'),
            ('array_sensing', 'Array Sensing', 'passive presence / motion detection'),
        ], help_text=_HELP_MODE)
        return {'mode': mode, 'needs_geometry': mode in ('trilateration', 'array_sensing')}

    def _step_count(ctx: dict) -> dict:
        _sep('Antennas')
        n_ant = _ask_int('Number of antennas', max(1, len(hw['wifi'])), help_text=(
            'Number of antennas',
            'How many physical antenna/adapter combinations to use.\n\n'
            '  Wardriving:     1+ (more = faster channel coverage)\n'
            '  Trilateration:  4 minimum for 3-D solve\n'
            '  Array sensing:  1+ (more = direction estimation)\n\n'
            'For trilateration with fewer than 4 antennas the solver\n'
            'will reject every group — you will get no output.'
        ))
        if n_ant is None:
            n_ant = max(1, len(hw['wifi']))
        return {'n_ant': n_ant}

    def _step_antennas(ctx: dict) -> dict:
        mode           = ctx['mode']
        needs_geometry = ctx['needs_geometry']
        n_ant          = ctx['n_ant']
        antennas: list[dict] = []
        i = 0
        while i < n_ant:
            print(f'\n  {_lbl(f"Antenna {i + 1}")} / {n_ant}')
            try:
                ant = _configure_one_antenna(hw, i, mode, needs_geometry)
                if i < len(antennas):
                    antennas[i] = ant
                else:
                    antennas.append(ant)
                i += 1
            except _WizardAbort:
                if i > 0:
                    print(f'\n  {_dim(f"← back to Antenna {i}")}')
                    i -= 1
                    antennas = antennas[:i]
                else:
                    raise  # propagate → step runner goes back to count

        return {'antennas': antennas}

    def _step_gps(ctx: dict) -> dict:
        _sep('Real position source')
        gps_opts = [
            ('gpsd',    'GNSS / gpsd',    f'{"detected  " if hw["gpsd"] else ""}hardware GPS via gpsd'),
            ('geoclue', 'GeoClue2',       'Linux location service — WiFi + cell tower + GPS'),
            ('mls',     'Mozilla LBS',    'WiFi scan → cloud API  (~10-200 m, no hardware)'),
            ('ip',      'IP geolocation', 'city-level fallback via IP  (~1-50 km)'),
            ('static',  'Static',         'fixed coordinates — bench / simulation'),
            ('none',    'None',           'no position — relative coordinates only'),
        ]
        gps_be = _choose('Position source', gps_opts,
                         default=1 if hw['gpsd'] else 2,
                         help_text=_HELP_GPS)
        gps: dict = {'backend': gps_be}
        if gps_be == 'gpsd':
            gps['host'] = _ask_str('  Host', 'localhost')
            gps['port'] = _ask_int('  Port', 2947)
        elif gps_be == 'mls':
            gps['interface'] = _ask_str(
                '  WiFi interface for scanning', '',
                required=False, hint='blank = auto-detect',
                help_text=(
                    'MLS WiFi scan interface',
                    'The wireless interface used to scan for nearby access\n'
                    'points.  Their BSSIDs and signal strengths are sent to\n'
                    'the MLS API to compute your position.\n\n'
                    'Leave blank to auto-detect the first wireless interface.\n'
                    'The interface does NOT need to be in monitor mode — the\n'
                    'regular managed-mode scan (iw dev <iface> scan) is used.'
                ),
            )
            gps['api_url'] = _ask_str(
                '  API endpoint', MozillaLBSBackend._DEFAULT_URL,
                required=False, hint='blank = default MLS endpoint',
            )
        elif gps_be == 'static':
            gps['lat'] = _ask_float('  Latitude  (decimal degrees)', hint='e.g.  48.8566')
            gps['lon'] = _ask_float('  Longitude (decimal degrees)', hint='e.g.   2.3522')
            gps['alt'] = _ask_float('  Altitude  (metres)', 0.0, required=False) or 0.0
        return {'gps': gps}

    def _step_imu(ctx: dict) -> dict:
        needs_geometry = ctx['needs_geometry']
        imu: dict = {'backend': 'null'}
        if needs_geometry:
            _sep('IMU  (optional)')
            serial_hint = f'  detected: {", ".join(hw["serial"])}' if hw['serial'] else ''
            imu_be = _choose('IMU backend  (orientation tracking)', [
                ('null',   'None',   'assume level and north-aligned'),
                ('serial', 'Serial', f'UART JSON quaternion stream{serial_hint}'),
            ], help_text=_HELP_IMU)
            imu = {'backend': imu_be}
            if imu_be == 'serial':
                default_dev = hw['serial'][0] if hw['serial'] else '/dev/ttyUSB0'
                imu['device'] = _ask_str('  Device', default_dev)
                imu['baud']   = _ask_int('  Baud rate', 115200)
        else:
            _sep('Position augmentation  (optional)')
            print(f'  {_dim("GPS = absolute anchor.  IMU/encoder fills gaps between GPS fixes.")}')
            aug = _choose('Relative position sensor', [
                ('none',   'None',           'GPS only — one fix per second, 1-10 m accuracy'),
                ('serial', 'IMU / odometer', 'dead-reckon between fixes via UART serial stream'),
            ], help_text=_HELP_IMU_WARDRIVE)
            if aug == 'serial':
                default_dev = hw['serial'][0] if hw['serial'] else '/dev/ttyUSB0'
                imu = {
                    'backend': 'serial',
                    'device':  _ask_str('  Device', default_dev, hint='e.g. /dev/ttyUSB0  /dev/ttyACM0'),
                    'baud':    _ask_int('  Baud rate', 115200),
                }
        return {'imu': imu}

    def _step_sync(ctx: dict) -> dict:
        mode = ctx['mode']
        _sep('Time sync')
        pps_hint = f'  detected: {", ".join(hw["pps"])}' if hw['pps'] else ''
        sync_opts = [
            ('software', 'Software', 'OS clock  µs-class — OK for wardriving'),
            ('ntp',      'NTP',      'NTP-disciplined  ~1 ms'),
            ('pps',      'PPS',      f'GPS PPS pulse  ~100 ns — minimum for TDOA{pps_hint}'),
            ('gpsdo',    'GPSDO',    'GPS-disciplined oscillator  ~1 ns'),
        ]
        default_sync = 1 if mode == 'wardriver' else (3 if hw['pps'] else 1)
        sync_src = _choose('Source', sync_opts, default=default_sync, help_text=_HELP_SYNC)
        sync: dict = {'source': sync_src}
        if sync_src in ('pps', 'gpsdo'):
            default_pps = hw['pps'][0] if hw['pps'] else '/dev/pps0'
            sync['device'] = _ask_str('  PPS device', default_pps)
        return {'sync': sync}

    def _step_mode_config(ctx: dict) -> dict:
        mode     = ctx['mode']
        antennas = ctx['antennas']
        _sep(f'Mode config  ({mode})')
        mc: dict = {}
        if mode == 'wardriver':
            raw_ch = _ask_str('Channels to scan', '1,2,3,4,5,6,7,8,9,10,11,12,13',
                              hint='comma-separated integers', help_text=_HELP_CHANNELS)
            mc['channels']     = [int(c.strip()) for c in raw_ch.split(',') if c.strip()]
            mc['hop_interval'] = _ask_float('Dwell per channel (s)', 0.1, help_text=_HELP_HOP)
        elif mode == 'trilateration':
            mc['channel'] = _ask_int('Channel to lock on', 6, help_text=(
                'Channel for trilateration',
                'All antennas lock to this single channel and listen\n'
                'simultaneously.  Choose a channel where the target\n'
                'transmitter is active.  For WiFi: 1, 6, or 11 are\n'
                'the most commonly used non-overlapping channels.'
            ))
            ref_opts = [(a['id'], a['id'], '') for a in antennas]
            mc['reference_antenna']  = _choose('Reference antenna', ref_opts,
                                               help_text=_HELP_REF_ANT)
            mc['correlation_window'] = _ask_float('Correlation window (s)', 0.001,
                                                  help_text=_HELP_CORR_WINDOW)
            mc['group_timeout']      = _ask_float('Incomplete group timeout (s)', 0.05, help_text=(
                'Group timeout',
                'If a frame group never receives arrivals from all antennas\n'
                '(e.g. one antenna missed the packet due to capture loss),\n'
                'it is discarded after this many seconds.\n\n'
                'Set to 5-10× the correlation window.  The default 0.05 s\n'
                'works for WiFi.  For very slow data rates, increase it.'
            ))
            print(f'\n  {_dim("Note: useful TDOA accuracy requires PPS or GPSDO sync.")}')
        elif mode == 'array_sensing':
            mc['channel']            = _ask_int('Channel to monitor', 6)
            mc['history_len']        = _ask_int('History window (frames)', 100, help_text=(
                'History window depth',
                'How many CSI/RSSI snapshots to keep per antenna.\n'
                'Variance is computed across this rolling window.\n\n'
                'Larger window → smoother variance, slower to react.\n'
                'Smaller window → noisier but more responsive.\n\n'
                'Default 100 is a good starting point.'
            ))
            mc['calibration_frames'] = _ask_int('Calibration frames', 50, help_text=_HELP_CALIB)
            mc['sensitivity']        = _ask_float('Detection sensitivity', 0.05, help_text=_HELP_SENSITIVITY)
            mc['hysteresis']         = _ask_float('Absence hysteresis', 0.4, help_text=_HELP_HYSTERESIS)
            mc['ema_alpha']          = _ask_float('EMA smoothing alpha (0-1)', 0.3, help_text=(
                'Exponential moving average weight',
                'Controls how quickly the smoothed variance responds to\n'
                'changes.  Higher alpha = more responsive, noisier.\n'
                'Lower alpha = smoother, slower to react.\n\n'
                '  0.1  — very smooth, slow response\n'
                '  0.3  — balanced (default)\n'
                '  0.7  — fast response, noisy'
            ))
        return {'mc': mc}

    def _step_output(ctx: dict) -> dict:
        mode = ctx['mode']
        mc   = ctx['mc']
        _sep('Output')
        out_fmt = _choose('Format', [
            ('jsonl', 'JSON Lines', 'one JSON object per frame  (.jsonl)'),
            ('csv',   'CSV',        'comma-separated  (.csv)'),
            ('none',  'None',       'no file output'),
        ], help_text=(
            'Output format',
            'The format in which captured observations are written to disk.\n\n'
            '  JSON Lines  — One JSON object per line, newline-delimited.\n'
            '    Easy to stream, grep, and pipe to jq.\n'
            '    tail -f session.jsonl | jq .rssi\n\n'
            '  CSV         — Comma-separated, Excel-compatible.\n'
            '    Good for post-processing in pandas or spreadsheets.\n\n'
            '  None        — No file.  Use when only the callback API\n'
            '    is needed or during quick interactive tests.'
        ))
        output: dict = {'format': out_fmt}
        if out_fmt != 'none':
            use_default, out_path = _choose_session_output_path('custom', suffix=f'.{out_fmt}')
            if use_default:
                output['path_policy'] = 'default'
            else:
                output['path'] = out_path
            if mode == 'wardriver':
                if not use_default and out_path:
                    mc['output_path'] = out_path
                mc.setdefault('store_raw_frames', True)
        return {'output': output}

    runner.add('mode',        _step_mode)
    runner.add('count',       _step_count)
    runner.add('antennas',    _step_antennas)
    runner.add('gps',         _step_gps)
    runner.add('imu',         _step_imu)
    runner.add('sync',        _step_sync)
    runner.add('mode_config', _step_mode_config)
    runner.add('output',      _step_output)

    ctx = runner.run()

    return {
        'mode':        ctx['mode'],
        'array_id':    'default',
        'antennas':    ctx['antennas'],
        'gps':         ctx['gps'],
        'imu':         ctx['imu'],
        'sync':        ctx['sync'],
        'mode_config': ctx['mc'],
        'output':      ctx['output'],
    }

# ── Wizard dispatcher ─────────────────────────────────────────────────────────
def _wizard() -> None:
    print(_c('\n  AetherWard Setup\n', _BLD, _RED))
    print(f'  {_dim("Enter = accept default  ·  ? = help  ·  q = go back  ·  Ctrl-C = quit")}')

    hw = _scan_hardware()
    _sep()

    n_wifi = len(hw['wifi'])
    if n_wifi > 0:
        quick_desc = f'{n_wifi} adapter{"s" if n_wifi > 1 else ""} detected'
        if hw['gpsd']:
            quick_desc += '  +  gpsd'
        path = _choose('How do you want to proceed?', [
            ('quick',  'Quick setup',  quick_desc + '  —  ~2 min'),
            ('custom', 'Custom setup', 'configure every option manually'),
        ])
    else:
        print(f'  {_dim("No adapters detected.  Entering custom setup.")}\n')
        path = 'custom'

    try:
        cfg = _wizard_quick(hw) if path == 'quick' else _wizard_custom(hw)
    except _WizardAbort:
        print(f'\n  {_dim("Wizard aborted — nothing saved.")}\n')
        return
    if cfg is None:
        return

    # Summary
    _sep('Review')
    print()
    _print_kv('mode',     cfg['mode'])
    _print_kv('antennas', str(len(cfg['antennas'])) + '  ' +
              '  '.join(a['id'] for a in cfg['antennas']))
    _print_kv('gps',      cfg.get('gps', {}).get('backend', 'none'))
    _print_kv('imu',      cfg.get('imu', {}).get('backend', 'null'))
    _print_kv('sync',     cfg.get('sync', {}).get('source', 'software'))
    _print_kv('output',   cfg.get('output', {}).get('format', 'none'))
    print()

    if not _confirm('Save this configuration?'):
        print(_dim('\n  Discarded.\n'))
        return

    try:
        name = _ask_str('Configuration name', 'default')
    except _WizardAbort:
        print(_dim('\n  Discarded.\n'))
        return
    cfg['array_id'] = name
    saved = _save_config(cfg, name)

    print(f'\n  {_ok("✓")} Saved  {_path(str(saved))}')
    print(f'  {_dim("Run with:")}  {_val(f"aetherward run {name}")}')
    print()

    if _confirm('Start session now?', default=False):
        _run_session(str(saved), None)

