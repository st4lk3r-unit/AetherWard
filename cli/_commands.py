"""CLI commands: info, validate, config, process, run, solve, install."""
from __future__ import annotations

import shutil
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from cli.aetherward import (
    AW_CONFIGS, AW_HOME, AW_SESSIONS,
    _TTY, _banner, _confirm, _dim, _ensure_home, _err, _hi, _lbl,
    _list_configs, _load_config_file, _ok, _path, _print_kv, _sep, _tick,
    _val,
)

# ── Commands ──────────────────────────────────────────────────────────────────
def _cmd_info() -> None:
    try:
        import aetherward
        from aetherward.core import c_available
        cfgs = _list_configs()
        print(f'\n  {_hi("AetherWard")}  v{_val(aetherward.__version__)}')
        print(f'  {_dim("C core :")}  {_val("loaded" if c_available() else "not found — Python fallbacks active")}')
        print(f'  {_dim("Modes  :")}  {_val(", ".join(aetherward.MODES))}')
        print(f'  {_dim("Home   :")}  {_path(str(AW_HOME))}')
        print(f'  {_dim("Configs:")}  {_val(str(len(cfgs)))}')
        print()
    except ImportError as e:
        print(_err(f'\n  Import error: {e}\n'))
        print(f'  Install with:  {_val("pip install .")}\n')
        sys.exit(1)

def _cmd_validate(config: str) -> None:
    try:
        cfg = _load_config_file(config)
        print(f'  {_ok("OK")}  mode={_val(cfg.mode)}  antennas={_val(str(len(cfg.antennas)))}')
    except Exception as e:
        print(f'  {_err("ERROR:")} {e}', file=sys.stderr)
        sys.exit(1)

def _cmd_config(args) -> None:
    if args.cfg_cmd == 'list':
        cfgs = _list_configs()
        if not cfgs:
            print(_dim('  No saved configurations.'))
            return
        print(f'\n  {_lbl("Saved configurations")}  {_path(str(AW_CONFIGS))}\n')
        for p in cfgs:
            print(f'  {_hi("▸")} {_val(p.stem)}  {_dim(p.suffix[1:])}')
        print()

    elif args.cfg_cmd == 'load':
        try:
            cfg = _load_config_file(args.name)
            print(f'  {_ok("OK")} {_val(args.name)}  mode={_val(cfg.mode)}')
        except Exception as e:
            print(f'  {_err("ERROR:")} {e}', file=sys.stderr)
            sys.exit(1)

    elif args.cfg_cmd == 'delete':
        for suffix in ('.json', '.toml', '.yaml', '.yml'):
            p = AW_CONFIGS / (args.name + suffix)
            if p.exists():
                if _confirm(f'Delete {p.name}?', default=False):
                    p.unlink()
                    print(f'  {_ok("✓")} Deleted.')
                return
        print(f'  {_err("Not found:")} {args.name}', file=sys.stderr)
        sys.exit(1)

def _cmd_process(args) -> None:
    """
    Post-process a recorded session JSONL.

    Wardriving sessions write one JSON record per observed frame.
    This command replays those records and produces a processed output:

      wardrive-map  — group by source (BSSID/ID), emit GeoJSON or CSV with
                      per-source mean RSSI, sample count, and bounding fix.
      tdoa-replay   — replay multi-antenna captures through the TDOA solver
                      using the antenna array from a saved config.  Requires
                      the session to have been recorded with synchronised clocks.
    """
    import json as _json

    session_path = args.session
    out_format   = (args.format or 'geojson').lower()
    out_path     = args.output
    proc_mode    = (args.proc_mode or 'wardrive-map').lower()

    # ── Load records ──────────────────────────────────────────────────────
    try:
        with open(session_path) as fh:
            records = [_json.loads(line) for line in fh if line.strip()]
    except FileNotFoundError:
        print(f'  {_err("Not found:")} {session_path}', file=sys.stderr)
        sys.exit(1)
    except _json.JSONDecodeError as e:
        print(f'  {_err("Parse error:")} {e}', file=sys.stderr)
        sys.exit(1)

    print(f'  {_lbl("Session")}  {_val(session_path)}  '
          f'{_dim(str(len(records)) + " records")}')

    if proc_mode == 'wardrive-map':
        _proc_wardrive_map(records, out_format, out_path)
    elif proc_mode == 'tdoa-replay':
        cfg_name = getattr(args, 'config', None)
        if not cfg_name:
            print(f'  {_err("--config required for tdoa-replay")}', file=sys.stderr)
            sys.exit(1)
        _proc_tdoa_replay(records, cfg_name, out_format, out_path)
    else:
        print(f'  {_err("Unknown processing mode:")} {proc_mode}', file=sys.stderr)
        sys.exit(1)


def _proc_wardrive_map(records: list, fmt: str, out_path: str | None) -> None:
    """
    Group wardrive observations by source ID and estimate transmitter position.

    Uses RSS trilateration (Gauss-Newton) when ≥3 GPS-tagged observations are
    available.  Falls back to an RSSI-weighted centroid for 1–2 observations.
    The output lat/lon is the *estimated transmitter position*, not the mean of
    the observer's GPS track.
    """
    from collections import defaultdict
    from aetherward.position.rss import rss_solve, rssi_centroid

    # Accumulate per-source stats
    sources: dict = defaultdict(lambda: {
        'rssi': [], 'obs': [], 'alt': [],
        'freq': None, 'protocol': None, 'ssid': None, 'count': 0,
    })

    for rec in records:
        sid = rec.get('id') or f"anon:{rec.get('freq', 0):.0f}"
        s   = sources[sid]
        s['count'] += 1
        rssi = rec.get('rssi', -100.0)
        s['rssi'].append(rssi)
        if rec.get('lat') is not None:
            s['obs'].append((rec['lat'], rec['lon'], rssi))
            s['alt'].append(rec.get('alt', 0.0))
        if s['freq']     is None: s['freq']     = rec.get('freq')
        if s['protocol'] is None: s['protocol'] = rec.get('protocol')
        if s['ssid']     is None: s['ssid']     = rec.get('ssid')

    # Estimate transmitter position per source
    results = []
    n_trilat = n_centroid = n_no_fix = 0

    for sid, s in sources.items():
        obs = s['obs']
        if not obs:
            n_no_fix += 1
            continue

        mean_rssi = sum(s['rssi']) / len(s['rssi'])
        mean_alt  = sum(s['alt'])  / len(s['alt']) if s['alt'] else 0.0

        # Try RSS trilateration first (needs ≥3 GPS observations)
        rss = rss_solve(obs) if len(obs) >= 3 else None

        if rss is not None:
            est_lat, est_lon = rss['lat'], rss['lon']
            pos_method  = 'rss_trilateration'
            rssi_at_1m  = rss['rssi_at_1m']
            rss_residual = rss['residual_dBm']
            n_trilat += 1
        else:
            # Fall back: RSSI-weighted centroid
            est_lat, est_lon = rssi_centroid(obs)
            pos_method  = 'rssi_centroid'
            rssi_at_1m  = None
            rss_residual = None
            n_centroid += 1

        results.append({
            'id':           sid,
            'ssid':         s['ssid'] or '',
            'protocol':     s['protocol'] or '',
            'freq_mhz':     round((s['freq'] or 0) / 1e6, 3),
            'rssi_mean':    round(mean_rssi, 1),
            'rssi_min':     round(min(s['rssi']), 1),
            'rssi_max':     round(max(s['rssi']), 1),
            'samples':      s['count'],
            'gps_obs':      len(obs),
            'lat':          est_lat,
            'lon':          est_lon,
            'alt':          mean_alt,
            'pos_method':   pos_method,
            'rssi_at_1m':   rssi_at_1m,
            'rss_residual': rss_residual,
        })

    results.sort(key=lambda r: r['rssi_mean'], reverse=True)

    print(f'  {_lbl("Sources")}  {_val(str(len(results)))} positioned  '
          f'{_dim(f"({n_trilat} RSS-trilat  {n_centroid} centroid  {n_no_fix} no-fix)")}')

    import json as _json

    if fmt == 'geojson':
        features = []
        for r in results:
            props = {k: v for k, v in r.items()
                     if k not in ('lat', 'lon', 'alt') and v is not None}
            features.append({
                'type': 'Feature',
                'geometry': {'type': 'Point', 'coordinates': [r['lon'], r['lat'], r['alt']]},
                'properties': props,
            })
        doc = {'type': 'FeatureCollection', 'features': features}
        out = _json.dumps(doc, indent=2)
        suffix = '.geojson'

    elif fmt == 'csv':
        import io
        import csv as _csv
        buf = io.StringIO()
        if results:
            w = _csv.DictWriter(buf, fieldnames=list(results[0].keys()))
            w.writeheader()
            w.writerows(results)
        out = buf.getvalue()
        suffix = '.csv'

    elif fmt == 'kml':
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>']
        for r in results:
            residual_str = (f'  Residual: {r["rss_residual"]} dB'
                            if r.get('rss_residual') is not None else '')
            lines.append(
                f'<Placemark><name>{r["id"]}</name>'
                f'<description>SSID: {r["ssid"]}  RSSI: {r["rssi_mean"]} dBm  '
                f'Samples: {r["samples"]}  Method: {r["pos_method"]}'
                f'{residual_str}</description>'
                f'<Point><coordinates>{r["lon"]},{r["lat"]},{r["alt"]}'
                f'</coordinates></Point></Placemark>'
            )
        lines += ['</Document></kml>']
        out = '\n'.join(lines)
        suffix = '.kml'

    elif fmt == 'wigle':
        def _freq_to_channel(mhz: float) -> int:
            if 2412 <= mhz <= 2484: return round((mhz - 2412) / 5) + 1
            if mhz >= 5180:         return round((mhz - 5000) / 5)
            return 0
        hdr = ('WigleWifi-1.4,appRelease=AetherWard,model=,release=,'
               'device=,display=,board=,brand=,star=,body=\n'
               'MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,'
               'CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n')
        rows = []
        for r in results:
            freq_khz = round(r['freq_mhz'] * 1000)
            ch       = _freq_to_channel(r['freq_mhz'])
            acc      = round(r['rss_residual'], 1) if r.get('rss_residual') else 0
            ssid_esc = '"' + r['ssid'].replace('"', '""') + '"' if ',' in r['ssid'] or '"' in r['ssid'] else r['ssid']
            rows.append(','.join([
                r['id'], ssid_esc, '', '',
                str(ch), str(freq_khz), str(round(r['rssi_mean'])),
                f"{r['lat']:.7f}", f"{r['lon']:.7f}",
                f"{r['alt']:.1f}", str(acc), 'WIFI',
            ]))
        out    = hdr + '\n'.join(rows)
        suffix = '.wigle.csv'

    else:
        print(f'  {_err("Unknown format:")} {fmt}  (geojson | csv | kml | wigle)', file=sys.stderr)
        sys.exit(1)

    if out_path:
        with open(out_path, 'w') as fh:
            fh.write(out)
        print(f'  {_ok("✓")} Written to {_path(out_path)}')
    else:
        # Default: write next to session file
        import pathlib
        default = pathlib.Path(
            getattr(sys, '_proc_session', '.')
        ).with_suffix(suffix)
        print(f'  {_ok("✓")} Written to {_path(str(default))}')
        with open(default, 'w') as fh:
            fh.write(out)


def _proc_tdoa_replay(records: list, config: str, fmt: str, out_path: str | None) -> None:
    """Replay captured frames through the TDOA solver using saved array config."""
    try:
        from aetherward import AntennaArray, Antenna
        from aetherward.core import tdoa_solve
        from aetherward.position.relative import RelativePosition
        from aetherward.orientation.quaternion import Orientation
    except ImportError as e:
        print(f'  {_err("Import error:")} {e}', file=sys.stderr)
        sys.exit(1)

    try:
        cfg = _load_config_file(config)
    except Exception as e:
        print(f'  {_err("Config error:")} {e}', file=sys.stderr)
        sys.exit(1)

    array = AntennaArray(id=cfg.array_id)
    for ac in cfg.antennas:
        x, y, z    = ac.position
        r_, p_, y_ = ac.orientation_euler
        array.add(Antenna(
            id=ac.id,
            position=RelativePosition(x=x, y=y, z=z),
            orientation=Orientation.from_euler(r_, p_, y_),
            frequency_range=tuple(ac.frequency_range),
        ))

    correlation_window = cfg.mode_config.get('correlation_window', 1e-3)

    # Group records into TDOA groups: same freq bucket + timestamp window
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for rec in records:
        freq   = rec.get('freq', 0)
        t      = rec.get('t', 0.0)
        bucket = int(t / correlation_window)
        groups[f"{freq:.0f}:{bucket}"].append(rec)

    print(f'  {_lbl("Groups")}  {_val(str(len(groups)))} candidate TDOA groups')

    ref_id = cfg.antennas[0].id if cfg.antennas else ''
    results = []
    solved = 0
    for key, group in groups.items():
        ant_ids = {r['ant'] for r in group}
        if len(ant_ids) < 2:
            continue  # need at least 2 antennas
        ref = next((r for r in group if r['ant'] == ref_id), group[0])
        measurements = [
            {'antenna_id': r['ant'], 'tdoa': r['t'] - ref['t'],
             'rssi': r.get('rssi', -100.0), 'timestamp': r['t']}
            for r in group
        ]
        result = tdoa_solve(array, measurements, ref['ant'])
        if not result or not result.get('valid'):
            continue
        solved += 1
        pos = result.get('position_absolute') or result.get('position_relative')
        if pos:
            results.append({
                'group': key,
                'lat':   getattr(pos, 'lat', None),
                'lon':   getattr(pos, 'lon', None),
                'alt':   getattr(pos, 'alt', None),
                'x':     getattr(pos, 'x', None),
                'y':     getattr(pos, 'y', None),
                'z':     getattr(pos, 'z', None),
                'antennas': len(ant_ids),
            })

    print(f'  {_ok("✓")} Solved {_val(str(solved))} / {len(groups)} groups')

    import json as _json
    out = _json.dumps(results, indent=2)
    dest = out_path or 'tdoa_results.json'
    with open(dest, 'w') as fh:
        fh.write(out)
    print(f'  {_ok("✓")} Written to {_path(dest)}')


def _cmd_install() -> None:
    """Install the aetherward command to ~/.local/bin (or /usr/local/bin)."""
    import stat
    _banner()
    _sep('install')
    _ensure_home()

    src = Path(__file__)
    # Prefer user-local bin, fall back to system
    for bin_dir in [Path.home()/'.local'/'bin', Path('/usr/local/bin')]:
        try:
            bin_dir.mkdir(parents=True, exist_ok=True)
            dest = bin_dir / 'aetherward'
            dest.write_text(f'#!/usr/bin/env python3\nimport sys, os\nsys.path.insert(0,{str(src.parent.parent)!r})\nfrom cli.aetherward import main\nmain()\n')
            dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            _print_kv('installed', str(dest))
            _print_kv('home',      str(AW_HOME))
            _print_kv('sessions',  str(AW_SESSIONS))
            _print_kv('configs',   str(AW_CONFIGS))
            print(f'\n  {_ok("✓")}  {_lbl("aetherward")} installed.')
            print(f'  {_dim("Add")} {_path(str(bin_dir))} {_dim("to PATH if not already there.")}')
            print(f'  {_dim("Then run:")} {_val("aetherward wizard")}\n')
            return
        except PermissionError:
            continue
    print(f'  {_err("!")} Could not write to ~/.local/bin or /usr/local/bin.')
    print(f'  {_dim("Try:")} {_val("sudo python3 " + str(src) + " install")}\n')


def _cmd_uninstall() -> None:
    if not AW_HOME.exists():
        print(_dim(f'\n  {AW_HOME} does not exist.\n'))
        return
    print(f'\n  This will permanently remove {_path(str(AW_HOME))}\n')
    if not _confirm('Are you sure?', default=False):
        print(_dim('  Aborted.\n'))
        return
    shutil.rmtree(AW_HOME)
    print(f'  {_ok("✓")} Removed.\n')

def _load_backend(backend_str: str, backend_config: dict):
    """Dynamically import, instantiate, and initialise a hardware backend."""
    import importlib
    if not backend_str or backend_str.lower() == 'null':
        return None
    try:
        mod_path, cls_name = backend_str.rsplit('.', 1)
    except ValueError:
        raise RuntimeError(f"Backend path must be 'module.ClassName', got: {backend_str!r}")
    try:
        mod = importlib.import_module(mod_path)
    except ModuleNotFoundError:
        raise RuntimeError(f"Backend module not found: {mod_path!r}")
    cls = getattr(mod, cls_name, None)
    if cls is None:
        raise RuntimeError(f"Class {cls_name!r} not in module {mod_path!r}")
    # Construct with keyword args that match the constructor signature; ignore unknowns
    import inspect
    sig  = inspect.signature(cls.__init__)
    known = {k: v for k, v in backend_config.items()
             if k in sig.parameters and k != 'self'}
    be = cls(**known)
    be.configure(backend_config)
    be.initialize()
    return be


def _run_session(config: str, mode_override: Optional[str]) -> None:
    try:
        import aetherward
        from aetherward import AntennaArray, Antenna, MODES
        from aetherward.position.relative import RelativePosition
        from aetherward.orientation.quaternion import Orientation
    except ImportError as e:
        print(_err(f'\n  Import error: {e}'))
        sys.exit(1)

    try:
        cfg = _load_config_file(config)
    except Exception as e:
        print(f'  {_err("ERROR:")} {e}', file=sys.stderr)
        sys.exit(1)

    mode_name = mode_override or cfg.mode
    mode_cls  = MODES.get(mode_name)
    if mode_cls is None:
        print(f'  {_err("Unknown mode:")} {mode_name}', file=sys.stderr)
        sys.exit(1)

    array = AntennaArray(id=cfg.array_id)
    for ac in cfg.antennas:
        x, y, z   = ac.position
        r_, p_, y_ = ac.orientation_euler
        ant = Antenna(
            id=ac.id,
            position=RelativePosition(x=x, y=y, z=z),
            orientation=Orientation.from_euler(r_, p_, y_),
            frequency_range=tuple(ac.frequency_range),
        )
        # Wire up hardware backend if specified
        if ac.backend and ac.backend.lower() != 'null':
            try:
                be = _load_backend(ac.backend, ac.backend_config)
                if be is not None:
                    ant.backend = be
                    short = ac.backend.rsplit('.', 1)[-1]
                    _tick(f'{_lbl(ac.id)}', f'backend={short}')
            except Exception as exc:
                print(f'  {_err("!")} {ac.id}: backend failed — {exc}', file=sys.stderr)
        array.add(ant)

    gps_be = _init_gps(cfg, array)

    # Inject a frame counter so the status loop can report activity
    _frame_count = [0]
    mc = dict(cfg.mode_config)
    _orig_on_obs = mc.get('on_observation')
    def _count_frame(obs):
        _frame_count[0] += 1
        if _orig_on_obs:
            _orig_on_obs(obs)
    mc['on_observation'] = _count_frame

    mode = mode_cls(array, mc)

    print(f'\n  {_hi("▸")} mode={_val(mode_name)}  '
          f'array={_val(cfg.array_id)}  '
          f'antennas={_val(str(array.n))}')
    print(f'  {_dim("Ctrl-C to stop.")}\n')

    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())

    t_start     = time.time()
    last_status = t_start

    try:
        mode.start()
        while not stopped.is_set():
            time.sleep(0.25)
            now = time.time()
            if now - last_status >= 5.0:
                elapsed = int(now - t_start)
                n_src   = len(mode.sources) if hasattr(mode, 'sources') else 0
                pos     = array.absolute_position
                gps_str = (f'{pos.lat:.5f},{pos.lon:.5f}'
                           if pos and pos.is_valid() else 'no fix')
                print(
                    f'  t={elapsed}s  frames={_frame_count[0]}'
                    f'  sources={n_src}  gps={gps_str}',
                    flush=True,
                )
                last_status = now
    finally:
        mode.stop()
        if gps_be:
            gps_be.close()
        elapsed = int(time.time() - t_start)
        print(f'\n  {_ok("✓")} Session stopped.'
              f'  frames={_frame_count[0]}  sources='
              f'{len(mode.sources) if hasattr(mode, "sources") else 0}'
              f'  runtime={elapsed}s\n')

def _autostart_gpsd(host: str, port: int) -> None:
    """Start gpsd if not reachable, using the first detected serial GPS device."""
    import socket, glob, subprocess
    try:
        s = socket.create_connection((host, port), timeout=1)
        s.close()
        return  # already running
    except OSError:
        pass
    devices = sorted(glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyACM*'))
    if not devices:
        return
    dev = devices[0]
    print(f'  {_dim("gpsd not running — auto-starting with")} {_val(dev)}')
    try:
        subprocess.Popen(
            ['gpsd', dev, '-F', '/var/run/gpsd.sock'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        print(f'  {_err("gpsd binary not found")} — sudo apt install gpsd', file=sys.stderr)
        return
    # Wait up to 4 s for gpsd to become reachable
    for _ in range(8):
        time.sleep(0.5)
        try:
            s = socket.create_connection((host, port), timeout=1)
            s.close()
            print(f'  {_ok("✓")} gpsd started')
            return
        except OSError:
            pass
    print(f'  {_err("gpsd started but not yet reachable")} — connection will retry', file=sys.stderr)


def _init_gps(cfg, array):
    from aetherward.hardware.gps import (
        GPSDBackend, StaticGPSBackend,
        GeoclueBackend, MozillaLBSBackend, IPGeolocationBackend,
    )
    gc = cfg.gps
    # Poll interval: LBS sources are slow (WiFi scan ~5 s), GNSS can be 1 s
    poll_interval = 1.0
    try:
        if gc.backend == 'gpsd':
            _autostart_gpsd(gc.host, gc.port)
            be = GPSDBackend(host=gc.host, port=gc.port)
        elif gc.backend == 'static' and gc.lat is not None:
            be = StaticGPSBackend(lat=gc.lat, lon=gc.lon, alt=gc.alt)
        elif gc.backend == 'geoclue':
            be = GeoclueBackend()
            poll_interval = 2.0
        elif gc.backend == 'mls':
            be = MozillaLBSBackend(
                interface=getattr(gc, 'interface', ''),
                api_url=getattr(gc, 'api_url', ''),
            )
            poll_interval = 10.0   # WiFi scan takes ~5 s, API call ~1 s
        elif gc.backend == 'ip':
            be = IPGeolocationBackend()
            poll_interval = 60.0   # city-level, no point polling fast
        else:
            return None
        be.initialize()
    except Exception as e:
        print(f'  {_err("Position source init failed:")} {e}', file=sys.stderr)
        return None

    label = gc.backend
    print(f'  {_ok("✓")} Position source: {_val(label)}')

    def _loop():
        while True:
            try:
                pos = be.get_position()
                if pos and pos.is_valid():
                    array.update_position(pos)
            except Exception:
                pass
            time.sleep(poll_interval)

    threading.Thread(target=_loop, daemon=True).start()
    return be

# ── Live solver ───────────────────────────────────────────────────────────────

def _pos_changed(prev: dict, cur: dict, threshold_m: float = 5.0) -> bool:
    """True when the position shifted by more than threshold_m metres."""
    import math
    dlat = (cur.get('lat', 0.0) - prev.get('lat', 0.0)) * 111_320.0
    dlon = ((cur.get('lon', 0.0) - prev.get('lon', 0.0))
            * 111_320.0 * math.cos(math.radians(cur.get('lat', 0.0) or 0.0)))
    return math.sqrt(dlat*dlat + dlon*dlon) > threshold_m


def _build_array_from_cfg(cfg):
    from aetherward import AntennaArray, Antenna
    from aetherward.position.relative import RelativePosition
    from aetherward.orientation.quaternion import Orientation
    array = AntennaArray(id=cfg.array_id)
    for ac in cfg.antennas:
        x, y, z    = ac.position
        r_, p_, y_ = ac.orientation_euler
        array.add(Antenna(
            id=ac.id,
            position=RelativePosition(x=x, y=y, z=z),
            orientation=Orientation.from_euler(r_, p_, y_),
            frequency_range=tuple(ac.frequency_range),
        ))
    return array


def _cmd_solve(args) -> None:
    """
    Live solver — reads a session JSONL (optionally a growing one) and streams
    a positions JSONL with one record per source per update.

    RSS trilateration is always attempted when GPS-tagged observations exist.
    TDOA is added on top when --config points to a multi-antenna array config.

    Output format  (one JSON line per update):
        {"t": …, "id": …, "ssid": …, "lat": …, "lon": …,
         "pos_method": "rss_trilateration"|"rssi_centroid"|"tdoa",
         "rssi_at_1m": …, "residual_dBm": …, "samples": …}
    """
    import json as _json

    session_path = args.session
    out_path     = getattr(args, 'output',   None)
    follow       = getattr(args, 'follow',   False)
    n_exp        = getattr(args, 'n_exp',    2.5)
    interval     = getattr(args, 'interval', 2.0)
    min_obs      = getattr(args, 'min_obs',  3)
    cfg_name     = getattr(args, 'config',   None)

    # ── Optional: load array config for TDOA ──────────────────────────────
    array      = None
    tdoa_solve = None
    corr_win   = 1e-3
    ref_id     = ''

    if cfg_name:
        try:
            cfg   = _load_config_file(cfg_name)
            array = _build_array_from_cfg(cfg)
            from aetherward.core import tdoa_solve as _ts
            tdoa_solve   = _ts
            corr_win     = cfg.mode_config.get('correlation_window', 1e-3)
            ref_id       = cfg.antennas[0].id if cfg.antennas else ''
            _tick(f'Array config loaded  ({array.n} antennas, TDOA enabled)')
        except Exception as e:
            print(f'  {_err("⚠")} Could not load array config ({e}) — TDOA disabled')

    from collections import defaultdict
    from aetherward.position.rss import rss_solve, rssi_centroid

    # Per-source state
    rss_obs  : dict = defaultdict(list)   # sid → [(lat, lon, rssi)]
    rss_meta : dict = {}                  # sid → {ssid, freq_mhz, protocol}
    tdoa_buf : dict = defaultdict(list)   # (sid, bucket) → [raw rec]
    solved   : dict = {}                  # sid → last emitted record

    file_pos = 0
    out_f    = open(out_path, 'w', buffering=1) if out_path else None

    _sep()
    print(f'  {_lbl("Input")}   {_val(session_path)}')
    if out_path:
        print(f'  {_lbl("Output")}  {_val(out_path)}')
    else:
        print(f'  {_lbl("Output")}  {_dim("stdout")}')
    print(f'  {_dim(f"follow={follow}  n_exp={n_exp}  min_obs={min_obs}  interval={interval}s")}')
    _sep()
    print(f'  {_dim("Ctrl-C to stop.")}')
    print()

    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())

    def _emit(rec: dict) -> None:
        line = _json.dumps(rec)
        if out_f:
            out_f.write(line + '\n')
        else:
            print(line, flush=True)

    try:
        while not stopped.is_set():
            # ── Read new records from session file ─────────────────────────
            try:
                with open(session_path) as fh:
                    fh.seek(file_pos)
                    lines    = fh.readlines()
                    file_pos = fh.tell()
            except FileNotFoundError:
                print(f'  {_err("Not found:")} {session_path}', file=sys.stderr)
                break

            new_n = 0
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = _json.loads(raw)
                except ValueError:
                    continue
                new_n += 1

                sid = rec.get('id') or f"anon:{rec.get('freq', 0):.0f}"

                # RSS accumulation
                if rec.get('lat') is not None:
                    rss_obs[sid].append((rec['lat'], rec['lon'],
                                         rec.get('rssi', -100.0)))
                if sid not in rss_meta:
                    rss_meta[sid] = {
                        'ssid':     rec.get('ssid') or '',
                        'freq_mhz': round((rec.get('freq') or 0) / 1e6, 3),
                        'protocol': rec.get('protocol') or '',
                    }

                # TDOA accumulation
                if tdoa_solve and rec.get('ant'):
                    bucket = int(rec.get('t', 0.0) / corr_win)
                    tdoa_buf[(sid, bucket)].append(rec)

            # ── RSS solve ─────────────────────────────────────────────────
            updates = 0
            for sid, obs in rss_obs.items():
                if len(obs) < min_obs:
                    continue

                rss = rss_solve(obs, n_exp=n_exp)
                if rss is not None:
                    pos_rec = {**rss_meta.get(sid, {}),
                               'id': sid, 't': time.time(),
                               'pos_method': 'rss_trilateration',
                               **rss}
                elif len(obs) >= 1:
                    lat, lon = rssi_centroid(obs)
                    pos_rec = {**rss_meta.get(sid, {}),
                               'id': sid, 't': time.time(),
                               'pos_method': 'rssi_centroid',
                               'lat': lat, 'lon': lon, 'samples': len(obs)}
                else:
                    continue

                prev = solved.get(sid)
                if prev is None or _pos_changed(prev, pos_rec):
                    _emit(pos_rec)
                    solved[sid] = pos_rec
                    updates += 1

            # ── TDOA solve ────────────────────────────────────────────────
            if tdoa_solve:
                ready = [(k, g) for k, g in tdoa_buf.items()
                         if len({r['ant'] for r in g}) >= 3]
                for key, group in ready:
                    sid = key[0]
                    ant_ids = {r['ant'] for r in group}
                    ref = next((r for r in group if r['ant'] == ref_id),
                               group[0])
                    meas = [{'antenna_id': r['ant'],
                             'tdoa':      r['t'] - ref['t'],
                             'rssi':      r.get('rssi', -100.0),
                             'timestamp': r['t']}
                            for r in group]
                    result = tdoa_solve(array, meas, ref['ant'])
                    if result and result.get('valid'):
                        pos = (result.get('position_absolute')
                               or result.get('position_relative'))
                        if pos:
                            pos_rec = {
                                **rss_meta.get(sid, {}),
                                'id':         sid,
                                't':          time.time(),
                                'pos_method': 'tdoa',
                                'lat':        getattr(pos, 'lat', None),
                                'lon':        getattr(pos, 'lon', None),
                                'residual_m': result.get('residual'),
                                'antennas':   len(ant_ids),
                            }
                            prev = solved.get(sid)
                            if prev is None or _pos_changed(prev, pos_rec):
                                _emit(pos_rec)
                                solved[sid] = pos_rec
                                updates += 1
                    # Remove solved group regardless
                    tdoa_buf.pop(key, None)

            # ── Status line ───────────────────────────────────────────────
            if _TTY:
                sys.stdout.write(
                    f'\r  {_dim("+" + str(new_n) + " records")}'
                    f'  {_lbl(str(len(solved)))} sources'
                    f'  {_ok(str(updates)) if updates else _dim("0")} updates'
                    + '    '
                )
                sys.stdout.flush()

            if not follow:
                break

            stopped.wait(interval)

    finally:
        if out_f:
            out_f.close()
        n_rss  = sum(1 for r in solved.values()
                     if r.get('pos_method') in ('rss_trilateration', 'rssi_centroid'))
        n_tdoa = sum(1 for r in solved.values() if r.get('pos_method') == 'tdoa')
        _sep()
        _tick(f'Solve finished — {len(solved)} sources positioned'
              f'  ({n_rss} RSS  {n_tdoa} TDOA)')
        print()


