#!/usr/bin/env python3
"""aetherward — AetherWard CLI"""
from __future__ import annotations

# Bootstrap: importable from source without pip install
import sys, os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import argparse
import glob
import json
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

# Imported here so wizard can reference MozillaLBSBackend._DEFAULT_URL
# without requiring aetherward to be installed at import time.
try:
    from aetherward.hardware.gps import MozillaLBSBackend as _MozillaLBS
except ImportError:
    class _MozillaLBS:  # type: ignore[no-redef]
        _DEFAULT_URL = 'https://location.services.mozilla.com/v1/geolocate?key=test'

MozillaLBSBackend = _MozillaLBS

# ── Paths ────────────────────────────────────────────────────────────────────
AW_HOME     = Path.home() / '.aetherward'
AW_CONFIGS  = AW_HOME / 'configs'
AW_SESSIONS = AW_HOME / 'sessions'
AW_LAST     = AW_HOME / '.last_config'

def _ensure_home() -> None:
    AW_HOME.mkdir(exist_ok=True)
    AW_CONFIGS.mkdir(exist_ok=True)
    AW_SESSIONS.mkdir(exist_ok=True)

# ── Colour — sourced from shared palette ──────────────────────────────────────
import cli.palette as _pal

_TTY = _pal.TTY
_RST = _pal.RST;  _BLD = _pal.BLD;  _DIM = _pal.DIM
_RED = _pal.RED;  _DRD = _pal.DRD
_GRN = _pal.GRN;  _YLW = _pal.YLW;  _ORG = _pal.ORG
_PRP = _pal.PRP;  _WHT = _pal.CYN;  _CYN = _pal.SKY

def _c(text: str, *codes: str) -> str:
    return _pal.wc(text, *codes)

def _hi(t):   return _c(t, _RED, _BLD)
def _val(t):  return _c(t, _YLW)
def _path(t): return _c(t, _CYN)
def _ok(t):   return _c(t, _GRN, _BLD)
def _err(t):  return _c(t, _RED, _BLD)
def _dim(t):  return _c(t, _DIM)
def _lbl(t):  return _c(t, _WHT, _BLD)
def _num(t):  return _c(f'[{t}]', _DIM)
def _hlp(t):  return _c(t, _PRP, _BLD)

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mGKHF]')

def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub('', s)

class _WizardAbort(Exception):
    """Raised when the user types 'q' to go back one step."""

# ── Ctrl-C state (double-press = hard exit) ───────────────────────────────────
_cc_last: float = 0.0

def _handle_ctrl_c() -> None:
    global _cc_last
    now = time.time()
    if now - _cc_last < 1.5:
        print(f'\n\n  {_dim("Exiting.")}\n')
        sys.exit(0)
    _cc_last = now
    print(f'\n  {_dim("Ctrl-C  — q = go back  ·  Ctrl-C again = exit")}')
    raise _WizardAbort()

# ── Raw single-keypress reader (arrow keys, Enter, etc.) ─────────────────────
def _read_key() -> str:
    """Read one keypress in raw terminal mode.  Escape sequences returned intact."""
    try:
        import termios, tty, select as _sel
        import os as _os_mod
    except ImportError:
        return sys.stdin.readline().rstrip('\n') or '\r'
    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
    except termios.error:
        return sys.stdin.readline().rstrip('\n') or '\r'
    try:
        tty.setraw(fd)
        ch = _os_mod.read(fd, 1).decode('utf-8', errors='replace')
        if ch == '\x1b':
            seq = ch
            while _sel.select([fd], [], [], 0.1)[0]:
                c = _os_mod.read(fd, 1).decode('utf-8', errors='replace')
                seq += c
                if c.isalpha() or c == '~':
                    break
            return seq
        return ch
    except Exception:
        return '\r'
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

# ── Banner ────────────────────────────────────────────────────────────────────
def _banner() -> None:
    p = Path(__file__).parent.parent / 'banner.txt'
    try:
        raw = p.read_text()
        print(raw if _TTY else _strip_ansi(raw), end='', flush=True)
    except FileNotFoundError:
        pass

# ── Layout helpers ────────────────────────────────────────────────────────────
def _sep(label: str = '', width: int = 52) -> None:
    rule = _c('─', _DRD)
    if label:
        side = max(0, width - len(_strip_ansi(label)) - 4)
        print(f'\n  {rule} {_lbl(label)} {_c("─" * side, _DRD)}')
    else:
        print(f'  {_c("─" * width, _DRD)}')

def _print_kv(key: str, val: str) -> None:
    print(f'  {_dim(f"{key:<10}")}  {_val(val)}')

def _tick(label: str, detail: str = '') -> None:
    d = f'  {_dim(detail)}' if detail else ''
    print(f'  {_ok("✓")}  {label}{d}')

def _cross(label: str) -> None:
    print(f'  {_dim("✗")}  {_dim(label)}')

# ── Help display ──────────────────────────────────────────────────────────────
def _show_help(topic: str, body: str) -> None:
    """Render a ? help panel."""
    width = 56
    print(f'\n  {_hlp("?")} {_lbl(topic)}')
    print(f'  {_dim("─" * width)}')
    for line in body.rstrip().splitlines():
        print(f'  {line}')
    print(f'  {_dim("─" * width)}\n')

# ── Safe input primitives ─────────────────────────────────────────────────────
def _raw(prompt: str, default=None) -> str:
    dfmt = f'  {_dim(f"[{default}]")}' if default is not None else ''
    try:
        v = input(f'  {_c("›", _RED, _BLD)} {prompt}{dfmt}  ').strip()
    except KeyboardInterrupt:
        print()
        _handle_ctrl_c()
        return ''
    except EOFError:
        print()
        sys.exit(0)
    return v

def _ask_str(prompt: str, default: Optional[str] = None,
             required: bool = True, hint: str = '',
             help_text: Optional[tuple[str, str]] = None) -> str:
    h = f'  {_dim("? help  q back")}' if help_text else f'  {_dim("q back")}'
    full = f'{prompt}{("  " + _dim(hint)) if hint else ""}{h}'
    while True:
        v = _raw(full, default)
        if v == '?' and help_text:
            _show_help(*help_text)
            continue
        if v.lower() == 'q':
            raise _WizardAbort()
        result = v or (str(default) if default is not None else '')
        if result:
            return result
        if not required:
            return ''
        dflt = f'  {_dim("Enter = skip")}' if not required else ''
        print(f'    {_err("!")} This field cannot be empty.{dflt}')

def _ask_float(prompt: str, default: Optional[float] = None,
               required: bool = True, hint: str = '',
               help_text: Optional[tuple[str, str]] = None) -> Optional[float]:
    h = f'  {_dim("? help  q back")}' if help_text else f'  {_dim("q back")}'
    full = f'{prompt}{("  " + _dim(hint)) if hint else ""}{h}'
    while True:
        v = _raw(full, default)
        if v == '?' and help_text:
            _show_help(*help_text)
            continue
        if v.lower() == 'q':
            raise _WizardAbort()
        if not v and default is not None:
            return float(default)
        if not v and not required:
            return None
        try:
            return float(v)
        except ValueError:
            dflt = f'  {_dim(f"Enter = {default}")}' if default is not None else ''
            print(f'    {_err("!")} Enter a number  {_dim("e.g. 48.8566")}{dflt}')

def _ask_int(prompt: str, default: Optional[int] = None,
             required: bool = True,
             help_text: Optional[tuple[str, str]] = None) -> Optional[int]:
    h = f'  {_dim("? help  q back")}' if help_text else f'  {_dim("q back")}'
    full = f'{prompt}{h}'
    while True:
        v = _raw(full, default)
        if v == '?' and help_text:
            _show_help(*help_text)
            continue
        if v.lower() == 'q':
            raise _WizardAbort()
        if not v and default is not None:
            return int(default)
        if not v and not required:
            return None
        try:
            return int(v)
        except ValueError:
            dflt = f'  {_dim(f"Enter = {default}")}' if default is not None else ''
            print(f'    {_err("!")} Enter a whole number  {_dim("e.g. 6")}{dflt}')

def _choose(prompt: str,
            options: list[tuple[str, str, str]],
            default: int = 1,
            help_text: Optional[tuple[str, str]] = None) -> str:
    cur = (default - 1) if default is not None and 1 <= default <= len(options) else 0

    def _render(clear: bool = False) -> None:
        if clear and _TTY:
            # cursor is at end of status-bar line; move up to blank line and clear
            n_up = len(options) + 2  # blank + header + N options
            sys.stdout.write(f'\x1b[{n_up}A\r\x1b[J')
            sys.stdout.flush()
        print(f'\n  {_hi(prompt)}')
        for i, (_, label, desc) in enumerate(options):
            marker = _c('▶', _RED, _BLD) if i == cur else ' '
            lbl = _c(label, _BLD) if i == cur else _dim(label)
            print(f'  {marker} {_num(i + 1)} {lbl}  {_dim(desc)}')
        if _TTY:
            h = _dim('↑↓ move  1-9 jump  Enter select' +
                     ('  ? help' if help_text else '') + '  q back')
        else:
            hints = (['? help'] if help_text else []) + ['q back']
            h = _dim('  '.join(hints))
        print(f'  {h}', end='', flush=True)

    _render()

    if not _TTY:
        # Non-interactive fallback: line input
        print()
        while True:
            v = _raw(f'Choice [1-{len(options)}]', default)
            if v == '?' and help_text:
                _show_help(*help_text)
                _render()
                print()
                continue
            if v.lower() == 'q':
                raise _WizardAbort()
            if not v and default is not None and 1 <= default <= len(options):
                return options[default - 1][0]
            try:
                n = int(v)
                if 1 <= n <= len(options):
                    return options[n - 1][0]
            except (ValueError, TypeError):
                pass
            dflt_hint = f'  {_dim(f"Enter = {default}")}' if default is not None else ''
            print(f'    {_err("!")} Enter 1–{len(options)}{dflt_hint}')
            _render()
            print()

    # Arrow-key / raw-TTY mode
    while True:
        key = _read_key()
        if key in ('\r', '\n'):           # Enter → confirm selection
            print()
            return options[cur][0]
        elif key == '\x1b[A':             # up arrow
            if cur > 0:
                cur -= 1
                _render(clear=True)
        elif key == '\x1b[B':             # down arrow
            if cur < len(options) - 1:
                cur += 1
                _render(clear=True)
        elif key == '\x1b[5~':            # Page Up → first option
            cur = 0
            _render(clear=True)
        elif key == '\x1b[6~':            # Page Down → last option
            cur = len(options) - 1
            _render(clear=True)
        elif key == '?' and help_text:
            print()
            _show_help(*help_text)
            _render()
        elif key.lower() == 'q':
            print()
            raise _WizardAbort()
        elif key == '\x03':               # Ctrl-C in raw mode
            print()
            _handle_ctrl_c()
            _render(clear=True)
        elif key.isdigit():
            n = int(key)
            if 1 <= n <= len(options):
                cur = n - 1
                _render(clear=True)    # move cursor; Enter still needed to confirm

def _confirm(prompt: str, default: bool = True) -> bool:
    hint = _dim('Y/n') if default else _dim('y/N')
    v = _raw(f'{prompt} [{hint}]  {_dim("q back")}').lower()
    if v == 'q':
        raise _WizardAbort()
    return default if not v else v in ('y', 'yes')

# ── Hardware detection ────────────────────────────────────────────────────────
def _detect_wifi() -> list[dict]:
    found = []
    net = Path('/sys/class/net')
    if not net.exists():
        return found
    for iface in sorted(net.iterdir()):
        if not ((iface / 'wireless').exists() or (iface / 'phy80211').exists()):
            continue
        info = {'name': iface.name, 'driver': '', 'monitor': False}
        try:
            info['driver'] = (iface / 'device' / 'driver').resolve().name
        except Exception:
            pass
        try:
            r = subprocess.run(['iw', iface.name, 'info'],
                               capture_output=True, text=True, timeout=2)
            info['monitor'] = 'monitor' in r.stdout.lower() or 'type monitor' in r.stdout
        except Exception:
            pass
        found.append(info)
    return found

def _detect_gpsd() -> bool:
    try:
        s = socket.create_connection(('localhost', 2947), timeout=1)
        s.close()
        return True
    except Exception:
        return False

def _detect_serial() -> list[str]:
    return sorted(glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyACM*'))

def _detect_pps() -> list[str]:
    return sorted(glob.glob('/dev/pps*'))

def _scan_hardware() -> dict:
    print(f'\n  {_dim("Scanning hardware...")}')
    hw = {
        'wifi':   _detect_wifi(),
        'gpsd':   _detect_gpsd(),
        'serial': _detect_serial(),
        'pps':    _detect_pps(),
    }
    print()
    if hw['wifi']:
        for w in hw['wifi']:
            mon = _ok('monitor') if w['monitor'] else _dim('managed')
            drv = f'  {_dim(w["driver"])}' if w['driver'] else ''
            _tick(_lbl(w['name']) + drv, str(mon))
    else:
        _cross('no wireless adapters found')
    if hw['gpsd']:
        _tick(f'{_lbl("gpsd")}  {_dim("localhost:2947")}')
    else:
        _cross('gpsd not reachable')
    for d in hw['serial']:
        _tick(_lbl(d), 'serial')
    for d in hw['pps']:
        _tick(_lbl(d), 'PPS')
    print()
    return hw

# ── Antenna type picker ───────────────────────────────────────────────────────
_ANTENNA_TYPES = [
    ('dipole_stick',  'Standard WiFi stick',    '2.4/5 GHz USB adapter — most common'),
    ('dipole_panel',  'Panel / flat patch',      'flat directional, moderate gain'),
    ('yagi',          'Yagi / cantenna',          'high-gain directional'),
    ('isotropic',     'Unknown / generic',        'safe omnidirectional default'),
    ('expert',        'Expert — full control',    'pattern + gain manually'),
]

_ANTENNA_PATTERN_MAP = {
    'dipole_stick':  ('dipole',    2.15),
    'dipole_panel':  ('dipole',    6.0),
    'yagi':          ('dipole',    10.0),
    'isotropic':     ('isotropic', 0.0),
}

_HELP_ANTENNA_TYPE = (
    'Antenna radiation pattern',
    """\
The pattern tells the framework how your antenna radiates — i.e. how signal
strength varies by direction. This affects gain correction and, for
trilateration, source-position accuracy.

  Standard WiFi stick   — Vertical dipole, ~2.15 dBi. Omnidirectional in
                          the horizontal plane. Correct for most USB sticks
                          and built-in cards.

  Panel / flat patch    — Directional, ~6 dBi. Useful for scanning in one
                          direction (e.g. mounted on a vehicle or window).

  Yagi / cantenna       — Highly directional, 8-15 dBi. Use when pointing
                          at a specific target from a distance.

  Unknown / generic     — Treated as a perfect sphere (isotropic). Position
                          accuracy degrades slightly but nothing breaks.
                          Best choice when you genuinely don't know.

  Expert                — Full control: choose pattern model and enter the
                          exact gain, or load a custom .npy azimuth/elevation
                          grid measured in an anechoic chamber."""
)

def _ask_antenna_type() -> tuple[str, float]:
    choice = _choose('What kind of antenna is this?', _ANTENNA_TYPES,
                     help_text=_HELP_ANTENNA_TYPE)
    if choice == 'expert':
        pat = _choose('Radiation pattern model', [
            ('isotropic', 'Isotropic', '0 dBi  perfect sphere — radiates equally in all directions'),
            ('dipole',    'Dipole',    '2.15 dBi — standard half-wave dipole, null at tips'),
            ('custom',    'Custom',    'load a measured pattern from a .npy file'),
        ], help_text=(
            'Radiation pattern model',
            """\
Selects the mathematical model used to describe how your antenna
radiates and receives signal as a function of direction.

  Isotropic  — Perfect theoretical sphere.  No real antenna is truly
    isotropic, but it is the safest choice when you don't know your
    antenna's real pattern.  Gain = 0 dBi by definition.

  Dipole     — Half-wave dipole: omnidirectional in the horizontal
    plane, with nulls at the tips (pointing along the antenna axis).
    Gain ~2.15 dBi.  Correct for most vertical USB sticks, rubber
    duck antennas, and wire dipoles.  Also used as an approximation
    for panel antennas — enter the actual gain manually.

  Custom     — Load a measured or simulated pattern from a NumPy file.
    The array must have shape (N_az, N_el) with gain values in dBi.
    Azimuth axis: 0-360° (N points). Elevation axis: -90 to +90°.
    The framework bilinearly interpolates for arbitrary directions.
    Generate with antenna simulation software (e.g. 4NEC2, EZNEC,
    OpenEMS) or a calibrated anechoic chamber measurement rig."""
        ))
        if pat == 'custom':
            pat = _ask_str(
                'Path to .npy pattern file',
                help_text=(
                    'Custom pattern .npy file',
                    """\
NumPy array saved with np.save(), shape (N_az, N_el), dtype float64.
Values are gain in dBi at each (azimuth, elevation) pair.

  Azimuth  : 0° to 360°, N_az evenly-spaced points, starting at North.
  Elevation: -90° (nadir) to +90° (zenith), N_el evenly-spaced points.

The framework bilinearly interpolates for any direction at runtime.
Minimum useful resolution: 5° × 5° (72 × 37 points).

Load example in Python:
  import numpy as np
  pat = np.random.uniform(0, 3, (72, 37))   # synthetic
  np.save('my_antenna.npy', pat)"""
                ),
            )
        gain = _ask_float(
            'Peak gain (dBi)', 2.15,
            help_text=(
                'Peak antenna gain',
                """\
The maximum gain of the antenna, in dBi (decibels relative to isotropic).

  0.0 dBi  — isotropic reference (theoretical)
  2.15 dBi — half-wave dipole (most USB sticks)
  5-8 dBi  — panel / patch / small Yagi
  10-15 dBi— medium Yagi, cantenna, sector panel
  20+ dBi  — parabolic dish, long Yagi array

This value is used to calibrate RSSI readings: the framework subtracts
antenna gain to estimate the signal power at the air interface.

For the dipole and isotropic models, this directly scales the pattern.
For custom .npy patterns, this value overrides the file's peak."""
            ),
        )
        return pat, gain
    pat, gain = _ANTENNA_PATTERN_MAP[choice]
    return pat, gain

# ── Frequency range picker ────────────────────────────────────────────────────
_FREQ_PRESETS = [
    ('2.4ghz', '2.4 GHz WiFi',  '2400 – 2500 MHz  channels 1-13'),
    ('5ghz',   '5 GHz WiFi',    '5000 – 5900 MHz  channels 36-177'),
    ('both',   '2.4 + 5 GHz',   'dual-band adapter'),
    ('custom', 'Custom range',   'enter min/max in Hz manually'),
    ('any',    'Any / SDR',      'no restriction — use for RTL-SDR, HackRF'),
]

_FREQ_PRESET_MAP = {
    '2.4ghz': (2.4e9,  2.5e9),
    '5ghz':   (5.0e9,  5.9e9),
    'both':   (2.4e9,  5.9e9),
    'any':    (0.0,    float('inf')),
}

_HELP_FREQ = (
    'Frequency range',
    """\
Tells the backend which frequencies this antenna covers.  Used to:
  • Reject frames outside the expected band
  • Route channels to the right antenna in multi-adapter setups
  • Annotate every observation with coverage metadata

  2.4 GHz WiFi  — Channels 1-13 (2400-2500 MHz).  Most crowded band.
                  All basic WiFi adapters support this.

  5 GHz WiFi    — Channels 36-177 (5000-5900 MHz).  Less congested,
                  shorter range.  Adapter must be dual-band capable.

  2.4 + 5 GHz   — Use when your adapter supports both bands and you
                  want to scan across all channels.

  Custom        — For non-WiFi hardware (SDR, proprietary).  Enter
                  exact values in Hz (e.g. 433e6 for 433 MHz ISM).

  Any / SDR     — No restriction.  Typically RTL-SDR or HackRF where
                  the tunable range is set by the hardware itself."""
)

def _ask_freq_range() -> list[float]:
    choice = _choose('Frequency range', _FREQ_PRESETS, help_text=_HELP_FREQ)
    if choice == 'custom':
        fmin = _ask_float('  Min frequency (Hz)', 2.4e9, hint='e.g. 2.4e9')
        fmax = _ask_float('  Max frequency (Hz)', 2.5e9, hint='e.g. 2.5e9')
        return [fmin, fmax]
    fmin, fmax = _FREQ_PRESET_MAP.get(choice, (2.4e9, 2.5e9))
    return [fmin, fmax]

# ── Config I/O ────────────────────────────────────────────────────────────────
def _save_config(cfg: dict, name: str) -> Path:
    path = AW_CONFIGS / f'{name}.json'
    path.write_text(json.dumps(cfg, indent=2))
    AW_LAST.write_text(str(path))
    return path

def _load_config_file(path_or_name: str):
    from aetherward.config.schema import AWConfig
    p = Path(path_or_name)
    if not p.exists():
        for suffix in ('.json', '.toml', '.yaml', '.yml'):
            c = AW_CONFIGS / (path_or_name + suffix)
            if c.exists():
                p = c
                break
        else:
            raise FileNotFoundError(
                f'Config not found: {path_or_name!r}\n'
                f'  Saved configs live in: {AW_CONFIGS}'
            )
    if p.suffix == '.json':
        return AWConfig.from_dict(json.loads(p.read_text()))
    if p.suffix == '.toml':
        return AWConfig.from_toml(str(p))
    return AWConfig.from_yaml(str(p))

def _list_configs() -> list[Path]:
    exts = {'.json', '.toml', '.yaml', '.yml'}
    if not AW_CONFIGS.exists():
        return []
    return sorted(p for p in AW_CONFIGS.iterdir() if p.suffix in exts)

# ── Shared help texts ─────────────────────────────────────────────────────────
_HELP_MODE = (
    'Operating mode',
    """\
AetherWard has three modes.  Pick the one that matches your goal.

  Wardriving      — One or more adapters hop across WiFi channels while
                    GPS tags every captured frame with your real-world
                    position.  Good for mapping coverage, finding rogue
                    APs, or building RF fingerprint databases.
                    Minimum: 1 adapter + GPS (optional but recommended).

  Trilateration   — All antennas lock to the same channel and listen
                    simultaneously.  Tiny differences in the time a
                    signal reaches each antenna (TDOA) are solved into
                    a transmitter position in your local area.
                    Minimum: 4 antennas + PPS time sync.
                    Without PPS: output is produced but accuracy is poor
                    (1 ns timing error = 30 cm position error).

  Presence        — Passively monitors how the RF environment changes
                    over time.  Moving objects (people, vehicles)
                    disturb signals in measurable ways.  No transmitting
                    required and no active target needed.
                    Works with a single antenna; improves with more."""
)

_HELP_GPS = (
    'GPS / GNSS backend',
    """\
GPS anchors all relative solver results (metres from your array) to
real-world lat/lon coordinates.

  gpsd    — Reads from the system GPS daemon.  Recommended when you
             have a USB GNSS receiver.  Start gpsd before running:
               sudo gpsd /dev/ttyUSB0 -F /var/run/gpsd.sock
             Then verify: gpspipe -w -n 5

  Static  — You supply fixed coordinates.  Use for indoor bench tests
             or when the array is at a permanently known location.

  None    — No GPS.  Trilateration still works and produces positions
             in local ENU metres, but they will not be geolocated.
             Wardriving without GPS still captures frames — just no
             lat/lon tags in the output."""
)

_HELP_SYNC = (
    'Time synchronisation source',
    """\
TDOA trilateration works by comparing nanosecond-level differences in
signal arrival time across antennas.  Your clock quality is a hard
ceiling on position accuracy: 1 ns error → ~30 cm error.

  Software  — OS CLOCK_REALTIME.  Microsecond-class jitter (~1-100 µs).
               Fine for wardriving and testing.  Useless for real TDOA.

  NTP       — ~1 ms accuracy.  Still too coarse for useful trilateration
               but acceptable for logging timestamps.

  PPS       — GPS pulse-per-second signal on a serial port (/dev/ppsX).
               ~100 ns accuracy.  Minimum for meaningful trilateration.
               Requires kernel PPS support (CONFIG_PPS) and a GPS
               receiver with a PPS output.

  GPSDO     — GPS-disciplined oscillator.  ~1 ns accuracy.
               Best choice for high-precision TDOA work.

  Rule of thumb:
    Wardriving    → Software is fine.
    Trilateration → PPS minimum; GPSDO preferred."""
)

_HELP_IMU_WARDRIVE = (
    'Position augmentation for wardriving',
    """\
GPS gives one position fix per second at 1-10 m accuracy.
A relative position sensor fills the gaps between fixes.

  How it works:
    1. GPS fix arrives → stored as absolute anchor (lat/lon).
    2. Between fixes: IMU acceleration (double-integrated) or
       encoder/odometer displacement computes a RelativePosition
       delta from that anchor.
    3. Each captured frame is tagged with the projected absolute
       position — not just the last GPS fix, but where you actually
       were when the frame arrived.

  Result: smooth per-frame positioning at sensor rate (10-200 Hz)
  even when GPS updates only once per second.  Gaps in GPS coverage
  (tunnels, buildings, foliage) are bridged by dead reckoning and
  corrected automatically when the next GPS fix arrives.

  Sensor options:
    None      — GPS timestamps only.  Simple and always correct;
                 recommended unless you need intra-fix resolution.
    IMU       — UART stream from an IMU module (BNO055, MPU-9250,
                 VectorNav, etc.).  The framework reads acceleration
                 and integrates to position.  Drifts over tens of
                 seconds; re-anchors on every GPS fix.
    Encoder / odometer — wheel encoder or external odometry source.
                 Much lower drift than IMU for straight-line motion.
                 Expose as a JSON stream on a serial port.

  Note: the accuracy of dead reckoning is fundamentally limited by
  the sensor quality and integration drift.  A consumer-grade IMU
  gives ~0.5-2 m error per second without a GPS fix.  Encoder
  odometry is better but requires hardware integration."""
)

_HELP_IMU = (
    'IMU (Inertial Measurement Unit)',
    """\
An IMU provides the array's physical orientation as a quaternion
(heading, tilt, roll).  The framework uses it to rotate each antenna's
body-frame position into the ENU world frame before any geometry math.

  When is IMU useful?
    Trilateration / Presence — if the array can rotate or tilt, IMU
      keeps antenna positions accurate in world coordinates.
      A stationary, level, north-aligned array can safely use None.

    Wardriving — almost never needed.  GPS handles position tagging
      and wardriving does no geometry math.  Skip it unless you are
      physically rotating directional antennas and need to track heading.

  None    — Assumes the array is level and aligned to magnetic north.
             This is the correct choice for most setups.

  Serial  — Reads a JSON quaternion stream from a UART IMU module
             (e.g. BNO055, MPU-9250, VectorNav).
             Expected line format: {"w":1.0,"x":0.0,"y":0.0,"z":0.0}"""
)

_HELP_CHANNELS = (
    'WiFi channels to scan',
    """\
The list of channels the adapter will hop across during wardriving.

  2.4 GHz channel plan:
    Non-overlapping (most used): 1, 6, 11
    Full band (all regions):     1 – 13
    North America legal max:     1 – 11

  5 GHz channels: 36, 40, 44, 48, 52 … 177
    (Check your country's regulatory allowed list)

  Multi-adapter setups: channels are split automatically across
  adapters so each channel is covered by exactly one adapter —
  no gaps, no duplicates.

  Hop interval × number of channels = time for one full pass.
  At 0.1 s/channel across 13 channels: one pass every 1.3 s."""
)

_HELP_HOP = (
    'Channel dwell time',
    """\
How long the adapter stays on each channel before hopping to the next.

  Shorter dwell → faster full-band pass, but short packets on a channel
                  may be missed if the adapter hops away mid-frame.
  Longer dwell  → more frames captured per channel, slower full pass.

  Typical values:
    0.05 s — aggressive wardriving, fast vehicle speed
    0.10 s — standard (default), balanced
    0.30 s — slower scan, catches more traffic per channel
    1.00 s — stationary monitoring of specific channels

  Beacon frames repeat every 102.4 ms by default, so a dwell of 0.1 s
  or more is needed to reliably catch at least one per channel."""
)

_HELP_CORR_WINDOW = (
    'TDOA correlation window',
    """\
The maximum expected time difference between the earliest and latest
arrival of the same transmission across all antennas.

  Physical limit: aperture / speed_of_light.
    3 m array  →  10 ns window
    10 m array →  33 ns window

  In practice set it much wider to absorb processing latency and
  system clock granularity.

  Default 1 ms works for WiFi with software timestamps.
  If you see many "incomplete group" warnings in the log: increase it.
  If unrelated transmissions are being grouped together: decrease it.

  With PPS sync and hardware timestamps you can tighten this
  significantly — try 1 µs (0.000001) for a 3 m array."""
)

_HELP_SENSITIVITY = (
    'Detection sensitivity',
    """\
The minimum variance increase above the calibration baseline that
triggers a presence or motion event.

  Lower value → more sensitive: detects subtle movements and
                microwave-range disturbances.  More false positives.
  Higher value → less sensitive: only large movements trigger.
                Fewer false positives, may miss slow/distant motion.

  The framework automatically calibrates a quiet-environment baseline
  during the first N frames (calibration_frames, default 50), so you
  do not need to pre-configure the environment noise level — just set
  sensitivity relative to how much change you want to detect.

  Units: variance of CSI amplitude (complex squared magnitudes) for
  CSI-capable hardware, or dBm² for RSSI-only fallback.

  Start with the default (0.05) and tune:
    Events fire with no one present → increase.
    Presence not detected           → decrease."""
)

_HELP_ANT_POSITION = (
    'Antenna position in the array',
    """\
Where this antenna sits relative to the array's reference point
(origin = centre of the array), in metres.

  Coordinate system: ENU — East, North, Up.
    x = metres East  (positive = East, negative = West)
    y = metres North (positive = North, negative = South)
    z = metres Up    (positive = Up, e.g. raised mast)

  This is used in trilateration geometry to compute inter-antenna
  distances.  It does NOT matter for wardriving — channels are
  just hopped regardless of where the antennas sit.

  Example: two antennas 1 m apart on an East-West baseline:
    Antenna 0:  x=-0.5, y=0.0, z=0.0
    Antenna 1:  x=+0.5, y=0.0, z=0.0"""
)

_HELP_ANT_ORIENTATION = (
    'Antenna orientation (Euler angles)',
    """\
The physical pointing direction of this antenna, as ZYX Euler angles
in degrees.  Used to apply gain pattern corrections: the framework
rotates the radiation pattern to match how the antenna is mounted.

  Convention: ZYX (yaw → pitch → roll) applied in that order.
    roll  — rotation around the East axis (tilt left/right)
    pitch — rotation around the North axis (tilt forward/back)
    yaw   — rotation around the Up axis (compass heading)

  Most antennas are mounted upright and pointing North: 0, 0, 0.
  An antenna rotated 90° to point East: roll=0, pitch=0, yaw=90.
  A down-facing antenna: pitch=180.

  Leave all zeros if you are unsure — the effect is small for
  omnidirectional antennas."""
)

_HELP_REF_ANT = (
    'Reference antenna',
    """\
The antenna whose frame arrival time is treated as t=0 for all TDOA
measurements in this solve.  All other antennas' TDOAs are measured
relative to when the reference receives the frame.

  Mathematically, any antenna can be the reference — the solver
  produces the same result.

  Practical advice: pick the antenna physically closest to the
  centre of your array.  This minimises the absolute magnitude of
  TDOA values for the other antennas, which tends to improve
  numerical stability."""
)

_HELP_CALIB = (
    'Calibration frame count',
    """\
How many frames to collect before the sensing pipeline starts
detecting events.

  During calibration, the framework measures the quiet-environment
  variance (no people present).  This becomes the baseline against
  which all future variance is compared.

  Larger calibration window → more accurate baseline, longer startup.
  Smaller window            → faster to start, noisier baseline.

  Default 50 frames is fine for most setups.  If the environment
  is very noisy at startup (e.g. other devices connecting) use 100+."""
)

_HELP_HYSTERESIS = (
    'Absence hysteresis',
    """\
Controls how far below the detection threshold variance must drop
before an 'absence' event is fired and the system resets to idle.

  Formula: absence fires when variance_excess < sensitivity × hysteresis.

  Default 0.4 means: once triggered, the system stays 'active' until
  variance drops to 40% of the trigger threshold.  This prevents
  rapid on/off flickering at the boundary.

  Lower value → faster absence detection, more oscillation risk.
  Higher value → more stable state, slower to declare absence.

  Typical useful range: 0.2 – 0.6."""
)

# Sub-module imports are deferred into main() to avoid circular import when
# this file is executed as __main__ (python3 -m cli.aetherward).
# When imported as a module the entry-point path is: cli.aetherward → main()
# → lazy import, which works because cli.aetherward is already in sys.modules.

# ── Interactive menu ──────────────────────────────────────────────────────────
def _interactive_menu() -> None:
    cfgs = _list_configs()
    print(f'\n  {_hi("AetherWard")}  {_dim("RF observation framework")}\n')
    print(f'  {_dim("home")}  {_path(str(AW_HOME))}')
    print(f'  {_dim("configs")}  {_val(str(len(cfgs)))}\n')

    opts = [
        ('wizard', 'Setup wizard', 'configure a new session'),
        ('run',    'Run',          f'{len(cfgs)} saved config{"s" if len(cfgs) != 1 else ""}'),
        ('info',   'Info',         'version and status'),
        ('quit',   'Quit',         ''),
    ]
    try:
        choice = _choose('What do you want to do?', opts)
    except _WizardAbort:
        print()
        return  # q at the top menu = quit gracefully

    if choice == 'wizard':
        _wizard()
    elif choice == 'run':
        if not cfgs:
            print(_err('\n  No saved configs. Run the wizard first.\n'))
            return
        run_opts = [(str(p), p.stem, str(p)) for p in cfgs]
        chosen = _choose('Select config', run_opts)
        _run_session(chosen, None)
    elif choice == 'info':
        _cmd_info()

# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    # Lazy imports here avoid circular import when running as __main__
    global _wizard, _cmd_config, _cmd_info, _cmd_install, _cmd_process
    global _cmd_solve, _cmd_uninstall, _cmd_validate, _run_session
    from cli._wizard import _wizard
    from cli._commands import (
        _cmd_config, _cmd_info, _cmd_install, _cmd_process,
        _cmd_solve, _cmd_uninstall, _cmd_validate, _run_session,
    )
    _ensure_home()

    parser = argparse.ArgumentParser(
        prog='aetherward',
        description='AetherWard — RF observation framework',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'examples:\n'
            '  aetherward                              interactive menu\n'
            '  aetherward wizard                       configuration wizard\n'
            '  aetherward run mysetup\n'
            '  aetherward run config.toml\n'
            '  aetherward solve session.jsonl          live RSS trilateration\n'
            '  aetherward solve session.jsonl --follow continuously watch growing file\n'
            '  aetherward solve session.jsonl --follow --output pos.jsonl\n'
            '  aetherward solve session.jsonl --config myarray   TDOA + RSS\n'
            '  aetherward process session.jsonl        wardrive map (GeoJSON, batch)\n'
            '  aetherward process session.jsonl --format csv\n'
            '  aetherward process session.jsonl --mode tdoa-replay --config myarray\n'
            '  aetherward config list\n'
        ),
    )
    sub = parser.add_subparsers(dest='command')

    sub.add_parser('wizard',    help='Interactive configuration wizard')
    sub.add_parser('info',      help='Version and framework status')
    sub.add_parser('install',   help='Install aetherward command to PATH')
    sub.add_parser('uninstall', help=f'Remove {AW_HOME}')

    run_p = sub.add_parser('run', help='Start a scan session')
    run_p.add_argument('config', nargs='?')
    run_p.add_argument('--mode', choices=['wardriver', 'trilateration', 'array_sensing'])

    solve_p = sub.add_parser('solve', help='Live RSS/TDOA solver on a session JSONL')
    solve_p.add_argument('session',             help='Path to .jsonl session file')
    solve_p.add_argument('--output',            metavar='FILE',
                         help='Positions output file (default: stdout)')
    solve_p.add_argument('--follow', '-f',      action='store_true',
                         help='Keep watching a growing session file (live mode)')
    solve_p.add_argument('--config',            metavar='NAME',
                         help='Array config name — enables TDOA alongside RSS')
    solve_p.add_argument('--n-exp',             type=float, default=2.5,
                         metavar='N',
                         help='Path loss exponent for RSS (default: 2.5)')
    solve_p.add_argument('--interval',          type=float, default=2.0,
                         metavar='SEC',
                         help='Re-solve interval in seconds, live mode (default: 2)')
    solve_p.add_argument('--min-obs',           type=int,   default=3,
                         metavar='N',
                         help='Min GPS observations before solving (default: 3)')

    proc_p = sub.add_parser('process', help='Post-process a recorded session JSONL')
    proc_p.add_argument('session',              help='Path to .jsonl session file')
    proc_p.add_argument('--mode',   dest='proc_mode',
                        choices=['wardrive-map', 'tdoa-replay'],
                        default='wardrive-map',
                        help='Processing algorithm (default: wardrive-map)')
    proc_p.add_argument('--format', choices=['geojson', 'csv', 'kml', 'wigle'],
                        default='geojson',
                        help='Output format (default: geojson)')
    proc_p.add_argument('--output', metavar='FILE',
                        help='Output file path (default: next to session file)')
    proc_p.add_argument('--config', metavar='NAME',
                        help='Array config name — required for tdoa-replay')

    val_p = sub.add_parser('validate', help='Validate a config file')
    val_p.add_argument('config')

    web_p = sub.add_parser('web', help='Start the web interface')
    web_p.add_argument('--host',  default='127.0.0.1', metavar='HOST',
                       help='Bind address (default: 127.0.0.1)')
    web_p.add_argument('--port',  type=int, default=8080, metavar='PORT',
                       help='Port to listen on (default: 8080, auto-increments if busy)')
    web_p.add_argument('--open',  dest='open_browser', action='store_true',
                       help='Open browser automatically')

    cfg_p   = sub.add_parser('config', help='Manage saved configurations')
    cfg_sub = cfg_p.add_subparsers(dest='cfg_cmd')
    cfg_sub.add_parser('list', help='List saved configs')
    cfg_l = cfg_sub.add_parser('load', help='Show a saved config')
    cfg_l.add_argument('name')
    cfg_d = cfg_sub.add_parser('delete', help='Delete a saved config')
    cfg_d.add_argument('name')

    args = parser.parse_args()

    if args.command is None:
        _banner()
        _interactive_menu()
        return

    if args.command != 'validate':
        _banner()

    if   args.command == 'wizard':    _wizard()
    elif args.command == 'info':      _cmd_info()
    elif args.command == 'install':   _cmd_install()
    elif args.command == 'uninstall': _cmd_uninstall()
    elif args.command == 'solve':     _cmd_solve(args)
    elif args.command == 'process':   _cmd_process(args)
    elif args.command == 'validate':  _cmd_validate(args.config)
    elif args.command == 'config':    _cmd_config(args)
    elif args.command == 'web':
        from cli.web import _cmd_web
        _cmd_web(args)
    elif args.command == 'run':
        cfg_arg = getattr(args, 'config', None)
        if cfg_arg is None:
            if AW_LAST.exists():
                cfg_arg = AW_LAST.read_text().strip()
            else:
                cfgs = _list_configs()
                if cfgs:
                    cfg_arg = str(cfgs[0])
                else:
                    print(_err('\n  No config found. Run: aetherward wizard\n'))
                    sys.exit(1)
        _run_session(cfg_arg, getattr(args, 'mode', None))

if __name__ == '__main__':
    main()
