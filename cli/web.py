"""
AetherWard web interface — self-contained, stdlib only + aetherward.
"""
from __future__ import annotations

import html as _html_mod
import json
import queue
import re as _re
import socketserver
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

# ── Shared state ──────────────────────────────────────────────────────────────

_positions:    dict = {}
_sse_clients:  list = []
_sse_lock             = threading.Lock()
_state_lock           = threading.Lock()

_solve_thread: Optional[threading.Thread] = None
_solve_stop           = threading.Event()
_solve_session: str  = ''
_total_updates: int  = 0

_run_thread:   Optional[threading.Thread] = None
_run_stop             = threading.Event()
_run_proc:     Optional[subprocess.Popen] = None
_run_state_lock       = threading.Lock()

AW_HOME     = Path.home() / '.aetherward'
AW_CONFIGS  = AW_HOME / 'configs'
AW_SESSIONS = AW_HOME / 'sessions'
_NAME_RE    = _re.compile(r'^[\w.\-]+$')


# ── SSE helpers ───────────────────────────────────────────────────────────────

def _push_sse(payload: str) -> None:
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)


def _broadcast(rec: dict) -> None:
    global _total_updates
    with _state_lock:
        _positions[rec['id']] = rec
        _total_updates += 1
    _push_sse('data: ' + json.dumps({'type': 'position', **rec}) + '\n\n')


def _broadcast_remove(src_id: str) -> None:
    with _state_lock:
        _positions.pop(src_id, None)
    _push_sse('data: ' + json.dumps({'type': 'source_removed', 'id': src_id}) + '\n\n')


def _broadcast_log(line: str, source: str = 'run') -> None:
    _push_sse('data: ' + json.dumps({'type': 'log', 'source': source, 'text': line}) + '\n\n')


# ── ANSI → HTML ───────────────────────────────────────────────────────────────

def _ansi_to_html(text: str) -> str:
    result = []
    depth  = 0
    for tok in _re.split(r'(\x1b\[[0-9;]*m)', text):
        m = _re.match(r'\x1b\[([0-9;]*)m', tok)
        if m:
            codes = m.group(1)
            parts = codes.split(';') if codes else ['0']
            if not parts[0] or parts[0] == '0':
                result.append('</span>' * depth)
                depth = 0
            elif parts[0] == '38' and len(parts) >= 5 and parts[1] == '2':
                r2, g2, b2 = int(parts[2]), int(parts[3]), int(parts[4])
                result.append(f'<span style="color:rgb({r2},{g2},{b2})">')
                depth += 1
            elif parts[0] == '30':
                result.append('<span style="color:transparent">')
                depth += 1
        else:
            result.append(_html_mod.escape(tok))
    result.append('</span>' * depth)
    return ''.join(result)


# ── Solver thread ─────────────────────────────────────────────────────────────

def _run_solver(session_path: str, config_name: Optional[str],
                n_exp: float, min_obs: int) -> None:
    from collections import defaultdict
    from aetherward.position.rss import rss_solve, rssi_centroid
    from aetherward.session import observer_point, receiver_id, signal_meta, source_id

    array = None; tdoa_solve = None; corr_win = 1e-3; ref_id = ''
    if config_name:
        try:
            import sys
            sys_path = str(Path(__file__).parent.parent)
            if sys_path not in sys.path:
                sys.path.insert(0, sys_path)
            from cli.aetherward import _load_config_file
            from cli._commands import _build_array_from_cfg
            from aetherward.core import tdoa_solve as _ts
            cfg = _load_config_file(config_name)
            array = _build_array_from_cfg(cfg); tdoa_solve = _ts
            corr_win = cfg.mode_config.get('correlation_window', 1e-3)
            ref_id   = cfg.antennas[0].id if cfg.antennas else ''
        except Exception:
            array = None

    rss_obs: dict  = defaultdict(list)
    rss_meta: dict = {}
    tdoa_buf: dict = defaultdict(list)
    solved: dict   = {}
    file_pos = 0
    _total_recs = 0
    _geo_recs   = 0
    _pass       = 0
    _idle_passes = 0

    while not _solve_stop.is_set():
        _pass += 1
        try:
            with open(session_path) as fh:
                fh.seek(file_pos); lines = fh.readlines(); file_pos = fh.tell()
        except FileNotFoundError:
            break
        new_recs = 0
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            new_recs += 1
            sid = source_id(rec)
            pt = observer_point(rec)
            if pt is not None:
                lat, lon, _alt, rssi, _acc = pt
                rss_obs[sid].append((lat, lon, rssi))
                _geo_recs += 1
            if sid not in rss_meta:
                rss_meta[sid] = signal_meta(rec)
            if tdoa_solve and receiver_id(rec):
                bucket = int(rec.get('t', 0.0) / corr_win)
                tdoa_buf[(sid, bucket)].append(rec)

        for sid, obs in rss_obs.items():
            if len(obs) < min_obs:
                continue
            rss = rss_solve(obs, n_exp=n_exp)
            if rss is not None:
                pos_rec = {**rss_meta.get(sid, {}), 'id': sid, 't': time.time(),
                           'pos_method': 'rss_trilateration', **rss}
            elif obs:
                lat, lon = rssi_centroid(obs)
                pos_rec = {**rss_meta.get(sid, {}), 'id': sid, 't': time.time(),
                           'pos_method': 'rssi_centroid', 'lat': lat, 'lon': lon,
                           'samples': len(obs)}
            else:
                continue
            if 'confidence_radius_m' not in pos_rec:
                res = pos_rec.get('residual_dBm') or pos_rec.get('rss_residual') or 8.0
                radius = max(25.0, float(res) * 18.0)
                if len(obs) < 8:
                    radius *= 1.5
                pos_rec['confidence_radius_m'] = round(radius, 1)
                pos_rec['confidence'] = 'medium' if len(obs) >= 8 and float(res) <= 10.0 else 'low'
            prev = solved.get(sid)
            if prev is None or _changed(prev, pos_rec):
                solved[sid] = pos_rec; _broadcast(pos_rec)
                label = rss_meta.get(sid, {}).get('ssid') or sid
                _broadcast_log(
                    f'[solved] {label} → {pos_rec["lat"]:.5f},{pos_rec["lon"]:.5f}'
                    f'  ({pos_rec["pos_method"]}, {len(obs)} obs)',
                    'solve')

        _total_recs += new_recs
        # After first complete read: warn and stop if file has records but none are geo-tagged
        if _pass == 1 and _total_recs > 0 and _geo_recs == 0:
            _broadcast_log(
                f'! No geo-tagged observations found in {_total_recs} record(s). '
                'RSS solver needs lat/lon fields. '
                'For ENU/TDOA sessions use the Array / ENU tab instead.',
                'solve')
            break
        # Warn (but keep looping) if geo-tagged data exists but no source meets min_obs
        if _pass == 1 and _geo_recs > 0:
            solvable = sum(1 for obs in rss_obs.values() if len(obs) >= min_obs)
            if solvable == 0:
                max_seen = max((len(o) for o in rss_obs.values()), default=0)
                _broadcast_log(
                    f'! {_geo_recs} geo-tagged record(s) across {len(rss_obs)} source(s); '
                    f'max {max_seen} obs/source — need ≥{min_obs}. '
                    'Reduce "Min observations" or collect more passes.',
                    'solve')

        if tdoa_solve:
            ready = [(k, g) for k, g in tdoa_buf.items()
                     if len({receiver_id(r) for r in g}) >= 3]
            for key, group in ready:
                sid = key[0]
                ant_ids = {receiver_id(r) for r in group}
                ref = next((r for r in group if receiver_id(r) == ref_id), group[0])
                meas = [{'antenna_id': receiver_id(r), 'tdoa': r['t']-ref['t'],
                         'rssi': r.get('rssi', -100.0), 'timestamp': r['t']}
                        for r in group]
                result = tdoa_solve(array, meas, receiver_id(ref))
                if result and result.get('valid'):
                    pos = result.get('position_absolute') or result.get('position_relative')
                    if pos and getattr(pos, 'lat', None):
                        pos_rec = {**rss_meta.get(sid, {}), 'id': sid, 't': time.time(),
                                   'pos_method': 'tdoa', 'lat': pos.lat, 'lon': pos.lon,
                                   'residual_m': result.get('residual'),
                                   'antennas': len(ant_ids)}
                        solved[sid] = pos_rec; _broadcast(pos_rec)
                tdoa_buf.pop(key, None)
        # Auto-stop when the file hasn't grown for 2 consecutive passes (static session)
        if new_recs == 0 and _total_recs > 0:
            _idle_passes += 1
            if _idle_passes >= 2:
                _broadcast_log(
                    f'Solver done — {len(solved)} source(s) positioned from {_total_recs} record(s).',
                    'solve')
                break
        else:
            _idle_passes = 0
        _solve_stop.wait(2.0)


def _changed(prev: dict, cur: dict, thr_m: float = 5.0) -> bool:
    import math
    dlat = (cur.get('lat', 0.0) - prev.get('lat', 0.0)) * 111_320.0
    dlon = ((cur.get('lon', 0.0) - prev.get('lon', 0.0))
            * 111_320.0 * math.cos(math.radians(cur.get('lat', 0.0) or 0.0)))
    return math.sqrt(dlat*dlat + dlon*dlon) > thr_m


# ── Capture run thread ────────────────────────────────────────────────────────

def _run_capture(config_name: str) -> None:
    global _run_proc
    import sys, shutil
    aw = shutil.which('aetherward')
    if aw:
        cmd = [aw, 'run', config_name]
    else:
        aw_py = str(Path(__file__).parent / 'aetherward.py')
        cmd   = [sys.executable, aw_py, 'run', config_name]
    _broadcast_log(f'[start] {" ".join(cmd)}')
    try:
        with _run_state_lock:
            _run_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, cwd=str(Path(__file__).parent.parent))
        for line in _run_proc.stdout:
            line = line.rstrip()
            if line:
                _broadcast_log(line)
            if _run_stop.is_set():
                break
        _run_proc.wait()
        _broadcast_log(f'[exit] code={_run_proc.returncode}')
    except Exception as e:
        _broadcast_log(f'[error] {e}')
    finally:
        with _run_state_lock:
            if _run_proc:
                try:
                    _run_proc.terminate()
                except Exception:
                    pass
            _run_proc = None


# ── Data helpers ──────────────────────────────────────────────────────────────

def _session_type(p: Path) -> str:
    """Peek at first valid record to classify session type."""
    try:
        for line in p.open():
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get('record_type') == 'observation' or r.get('observer'):
                return 'wardriver'
            if r.get('lat') is not None and r.get('lon') is not None:
                return 'wardriver'
            if r.get('x_enu') is not None or r.get('y_enu') is not None:
                return 'enu'
            if r.get('type') in ('presence', 'motion', 'absence'):
                return 'sensing'
            if r.get('ant') is not None:
                return 'tdoa_raw'
            return 'unknown'
    except Exception:
        pass
    return 'unknown'


def _list_sessions() -> list[dict]:
    if not AW_SESSIONS.exists():
        return []
    result = []
    for p in sorted(AW_SESSIONS.rglob('*.jsonl'),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            st = p.stat()
            n  = sum(1 for ln in p.open() if ln.strip())
        except OSError:
            continue
        rel    = p.relative_to(AW_SESSIONS)
        folder = str(rel.parent) if rel.parent != Path('.') else ''
        result.append({'name': p.name, 'path': str(p), 'size': st.st_size,
                       'records': n, 'stype': _session_type(p),
                       'folder': folder,
                       'mtime': time.strftime('%Y-%m-%d %H:%M',
                                              time.localtime(st.st_mtime))})
    return result


def _list_configs() -> list[dict]:
    if not AW_CONFIGS.exists():
        return []
    result = []
    for p in sorted(AW_CONFIGS.iterdir()):
        if p.suffix not in ('.json', '.toml', '.yaml', '.yml'):
            continue
        try:
            import sys
            sys_path = str(Path(__file__).parent.parent)
            if sys_path not in sys.path:
                sys.path.insert(0, sys_path)
            from cli.aetherward import _load_config_file
            cfg = _load_config_file(str(p))
            result.append({'name': p.stem, 'path': str(p),
                           'mode': cfg.mode, 'antennas': len(cfg.antennas)})
        except Exception:
            result.append({'name': p.stem, 'path': str(p), 'mode': '?', 'antennas': 0})
    return result


def _get_config_raw(name: str) -> dict | None:
    for ext in ('.toml', '.json', '.yaml', '.yml'):
        p = AW_CONFIGS / f'{name}{ext}'
        if p.exists():
            return {'name': name, 'content': p.read_text(), 'path': str(p)}
    return None


def _detect_hardware() -> dict:
    result: dict = {'wifi_ifaces': [], 'gpsd': False, 'rtlsdr': False,
                    'c_core': False, 'pps': False, 'serial': []}
    # WiFi interfaces
    try:
        r = subprocess.run(['iw', 'dev'], capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            result['wifi_ifaces'] = _re.findall(r'Interface (\S+)', r.stdout)
    except Exception:
        pass
    if not result['wifi_ifaces']:
        try:
            with open('/proc/net/wireless') as f:
                for line in f.readlines()[2:]:
                    iface = line.split(':')[0].strip()
                    if iface:
                        result['wifi_ifaces'].append(iface)
        except Exception:
            pass
    # gpsd
    try:
        import socket as _sock
        s = _sock.create_connection(('localhost', 2947), timeout=1)
        s.close(); result['gpsd'] = True
    except Exception:
        pass
    # Serial ports (GPS dongles appear here as /dev/ttyUSB* or /dev/ttyACM*)
    import glob as _glob
    result['serial'] = sorted(
        _glob.glob('/dev/ttyUSB*') + _glob.glob('/dev/ttyACM*')
    )
    # RTL-SDR
    try:
        import importlib as _il
        _il.import_module('rtlsdr'); result['rtlsdr'] = True
    except ImportError:
        pass
    # C core
    try:
        from aetherward.core import c_available
        result['c_core'] = c_available()
    except Exception:
        pass
    # PPS
    try:
        result['pps'] = any(Path('/dev').glob('pps*'))
    except Exception:
        pass
    return result


def _status() -> dict:
    from aetherward import __version__
    from aetherward.core import c_available
    with _state_lock:
        running     = _solve_thread is not None and _solve_thread.is_alive()
        run_running = _run_thread   is not None and _run_thread.is_alive()
        session = _solve_session; updates = _total_updates; sources = len(_positions)
    return {'version': __version__,
            'c_core': 'loaded' if c_available() else 'not available (Python fallback)',
            'home': str(AW_HOME), 'sessions_dir': str(AW_SESSIONS),
            'configs_dir': str(AW_CONFIGS),
            'solve_running': running, 'solve_session': session or '—',
            'run_running': run_running,
            'sources': sources, 'total_updates': updates}


# ── HTTP handler ──────────────────────────────────────────────────────────────

# ── Colour — sourced from shared palette ──────────────────────────────────────
import cli.palette as _pal

_TTY   = _pal.TTY
_C_ACC = _pal.ACC;  _C_RED = _pal.RED;  _C_DRD = _pal.DRD
_C_TXT = _pal.TXT;  _C_MU  = _pal.MU;   _C_VMU = _pal.VMU
_C_GRN = _pal.GRN;  _C_YLW = _pal.YLW;  _C_CYN = _pal.CYN
_C_ORG = _pal.ORG;  _C_RST = _pal.RST

def _wc(text: str, code: str) -> str:
    return _pal.wc(text, code)

def _web_log(method: str, path: str, code: int) -> None:
    if code >= 400:  col = _C_ACC
    elif code >= 300: col = _C_YLW
    else:             col = _C_GRN
    ts_s = _pal.wc(f'[{time.strftime("%H:%M:%S")}]', _pal.DIM)
    sep  = _wc('·', _C_DRD)
    meth = _wc(f'{method:<4}', _C_CYN)
    path_s = _pal.wc(path, _pal.SKY)
    sc   = _wc(str(code), col)
    print(f'  {ts_s} {sep} {meth} {path_s} {sep} {sc}', flush=True)

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        code = int(args[1]) if len(args) > 1 else 0
        path = str(args[0]).split()[1] if args else '?'
        _web_log(self.command, path, code)

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')

    def _json(self, data, code: int = 200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self._cors(); self.end_headers(); self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204); self._cors()
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        p = parsed.path; qs = parse_qs(parsed.query)

        if p in ('/', '/index.html'):
            body = _HTML.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
        elif p == '/api/status':
            self._json(_status())
        elif p == '/api/sessions':
            self._json(_list_sessions())
        elif p == '/api/configs':
            self._json(_list_configs())
        elif p == '/api/config/raw':
            name = (qs.get('name') or [''])[0].strip()
            if not name or not _NAME_RE.match(name):
                self._json({'error': 'invalid name'}, 400); return
            rec = _get_config_raw(name)
            self._json(rec) if rec else self._json({'error': 'not found'}, 404)
        elif p == '/api/positions/all':
            with _state_lock:
                self._json(list(_positions.values()))
        elif p == '/api/session/records':
            name = (qs.get('path') or [''])[0].strip()
            raw_all = (qs.get('raw') or [''])[0] == '1'
            p_obj = Path(name)
            if (p_obj.suffix != '.jsonl' or
                    not str(p_obj.resolve()).startswith(str(AW_SESSIONS.resolve()))):
                self._json({'error': 'not allowed'}, 403); return
            records = []
            if p_obj.exists():
                for raw in p_obj.read_text().strip().splitlines():
                    try:
                        r = json.loads(raw)
                        if raw_all:
                            records.append(r)
                        elif (r.get('lat') is not None and r.get('lon') is not None) or r.get('observer'):
                            from aetherward.session import oldstyle_observation
                            flat = oldstyle_observation(r)
                            if flat.get('lat') is not None and flat.get('lon') is not None:
                                records.append(flat)
                    except ValueError:
                        pass
            self._json(records)
        elif p == '/api/banner':
            bp = Path(__file__).parent.parent / 'banner.txt'
            if bp.exists():
                body = _ansi_to_html(bp.read_text()).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
            else:
                self._json({'error': 'banner not found'}, 404)
        elif p == '/api/detect':
            self._json(_detect_hardware())
        elif p == '/api/session/download':
            name = (qs.get('path') or [''])[0].strip()
            p_obj = Path(name)
            if (p_obj.suffix != '.jsonl' or
                    not str(p_obj.resolve()).startswith(str(AW_SESSIONS.resolve()))):
                self._json({'error': 'not allowed'}, 403); return
            if not p_obj.exists():
                self._json({'error': 'not found'}, 404); return
            body = p_obj.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-ndjson')
            self.send_header('Content-Disposition',
                             f'attachment; filename="{p_obj.name}"')
            self.send_header('Content-Length', str(len(body)))
            self._cors(); self.end_headers(); self.wfile.write(body)
        elif p == '/api/events':
            self._sse()
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        global _solve_thread, _solve_stop, _solve_session
        global _run_thread, _run_stop, _run_proc
        p    = urlparse(self.path).path
        body = b''
        if 'Content-Length' in self.headers:
            body = self.rfile.read(int(self.headers['Content-Length']))
        data = json.loads(body) if body else {}

        # ── Solve ──────────────────────────────────────────────────────────────
        if p == '/api/solve/start':
            session = data.get('session', '')
            if not session or not Path(session).exists():
                self._json({'error': 'session not found'}, 400); return
            _solve_stop.set()
            if _solve_thread and _solve_thread.is_alive():
                _solve_thread.join(timeout=3)
            _solve_stop.clear(); _solve_session = session
            _solve_thread = threading.Thread(
                target=_run_solver,
                args=(session, data.get('config'), data.get('n_exp', 2.5),
                      data.get('min_obs', 3)), daemon=True)
            _solve_thread.start()
            self._json({'ok': True, 'session': session})

        elif p == '/api/solve/stop':
            _solve_stop.set(); _solve_session = ''
            self._json({'ok': True})

        elif p == '/api/solve/batch':
            def _do_batch():
                from collections import defaultdict
                from aetherward.position.rss import rss_solve, rssi_centroid
                sessions = [s for s in _list_sessions()
                            if s['stype'] in ('wardriver', 'tdoa_raw', 'unknown')]
                total_solved = 0
                for sess in sessions:
                    rss_obs: dict = defaultdict(list)
                    rss_meta: dict = {}
                    try:
                        with open(sess['path']) as fh:
                            for raw in fh:
                                raw = raw.strip()
                                if not raw: continue
                                try: rec = json.loads(raw)
                                except ValueError: continue
                                sid = rec.get('id') or f"anon:{rec.get('freq',0):.0f}"
                                if rec.get('lat') is not None:
                                    rss_obs[sid].append(
                                        (rec['lat'], rec['lon'], rec.get('rssi', -100.0)))
                                if sid not in rss_meta:
                                    rss_meta[sid] = {
                                        'ssid': rec.get('ssid') or '',
                                        'freq_mhz': round((rec.get('freq') or 0) / 1e6, 3),
                                        'protocol': rec.get('protocol') or ''}
                    except Exception:
                        continue
                    for sid, obs in rss_obs.items():
                        if len(obs) < 3: continue
                        rss = rss_solve(obs, n_exp=2.5)
                        if rss is not None:
                            pos_rec = {**rss_meta.get(sid, {}), 'id': sid, 't': time.time(),
                                       'pos_method': 'rss_trilateration', **rss}
                        elif obs:
                            lat, lon = rssi_centroid(obs)
                            pos_rec = {**rss_meta.get(sid, {}), 'id': sid, 't': time.time(),
                                       'pos_method': 'rssi_centroid', 'lat': lat, 'lon': lon,
                                       'samples': len(obs)}
                        else:
                            continue
                        with _state_lock:
                            _positions[sid] = pos_rec
                        _broadcast(pos_rec)
                        total_solved += 1
                return total_solved, len(sessions)
            solved_n, sess_n = _do_batch()
            self._json({'ok': True, 'solved': solved_n, 'sessions': sess_n})

        # ── Capture run ────────────────────────────────────────────────────────
        elif p == '/api/run/start':
            cfg_name = data.get('config', '').strip()
            if not cfg_name:
                self._json({'error': 'config required'}, 400); return
            _run_stop.set()
            if _run_thread and _run_thread.is_alive():
                _run_thread.join(timeout=3)
            _run_stop.clear()
            _run_thread = threading.Thread(
                target=_run_capture, args=(cfg_name,), daemon=True)
            _run_thread.start()
            self._json({'ok': True, 'config': cfg_name})

        elif p == '/api/run/stop':
            _run_stop.set()
            with _run_state_lock:
                if _run_proc:
                    try: _run_proc.terminate()
                    except Exception: pass
            self._json({'ok': True})

        # ── Config CRUD ────────────────────────────────────────────────────────
        elif p == '/api/config/save':
            name = data.get('name', '').strip(); content = data.get('content', '')
            if not name or not _NAME_RE.match(name):
                self._json({'error': 'invalid name (letters, digits, _ - . only)'}, 400); return
            if not content.strip():
                self._json({'error': 'content is empty'}, 400); return
            AW_CONFIGS.mkdir(parents=True, exist_ok=True)
            cfg_path = AW_CONFIGS / f'{name}.toml'
            cfg_path.write_text(content)
            self._json({'ok': True, 'path': str(cfg_path)})

        elif p == '/api/config/delete':
            name = data.get('name', '').strip()
            if not name or not _NAME_RE.match(name):
                self._json({'error': 'invalid name'}, 400); return
            for ext in ('.toml', '.json', '.yaml', '.yml'):
                cfg_path = AW_CONFIGS / f'{name}{ext}'
                if cfg_path.exists():
                    cfg_path.unlink(); self._json({'ok': True}); return
            self._json({'error': 'not found'}, 404)

        # ── Session CRUD ───────────────────────────────────────────────────────
        elif p == '/api/session/delete':
            path = data.get('path', '').strip()
            if not path:
                self._json({'error': 'path required'}, 400); return
            p_obj = Path(path)
            if (p_obj.suffix != '.jsonl' or
                    not str(p_obj.resolve()).startswith(str(AW_SESSIONS.resolve()))):
                self._json({'error': 'not allowed'}, 403); return
            if p_obj.exists():
                p_obj.unlink(); self._json({'ok': True})
            else:
                self._json({'error': 'not found'}, 404)

        elif p == '/api/session/import':
            name = data.get('name', '').strip()
            content = data.get('content', '')
            if not name:
                self._json({'error': 'name required'}, 400); return
            stem = name.replace('.jsonl', '')
            if not _NAME_RE.match(stem):
                self._json({'error': 'invalid name'}, 400); return
            if not name.endswith('.jsonl'):
                name += '.jsonl'
            AW_SESSIONS.mkdir(parents=True, exist_ok=True)
            dest = AW_SESSIONS / name
            dest.write_text(content)
            self._json({'ok': True, 'path': str(dest)})

        elif p == '/api/session/rename':
            path = data.get('path', '').strip(); name = data.get('name', '').strip()
            if not path or not name:
                self._json({'error': 'path and name required'}, 400); return
            if not name.endswith('.jsonl'):
                name += '.jsonl'
            stem = name.replace('.jsonl', '')
            if not _NAME_RE.match(stem):
                self._json({'error': 'invalid name'}, 400); return
            p_obj = Path(path); new_path = p_obj.parent / name
            if p_obj.exists():
                p_obj.rename(new_path)
                self._json({'ok': True, 'path': str(new_path)})
            else:
                self._json({'error': 'not found'}, 404)

        # ── Source CRUD ────────────────────────────────────────────────────────
        elif p == '/api/source/delete':
            src_id = data.get('id', '')
            if not src_id:
                self._json({'error': 'id required'}, 400); return
            _broadcast_remove(src_id)
            self._json({'ok': True})

        elif p in ('/api/source/add', '/api/source/edit'):
            rec = dict(data)
            if 'id' not in rec:
                self._json({'error': 'id required'}, 400); return
            rec.setdefault('pos_method', 'manual')
            rec.setdefault('t', time.time())
            _broadcast(rec)
            self._json({'ok': True})

        else:
            self._json({'error': 'not found'}, 404)

    def _sse(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self._cors(); self.end_headers()

        q: queue.Queue = queue.Queue(maxsize=200)
        with _sse_lock:
            _sse_clients.append(q)

        with _state_lock:
            snapshot = list(_positions.values())
        for rec in snapshot:
            try:
                self.wfile.write(
                    ('data: ' + json.dumps({'type': 'position', **rec}) + '\n\n').encode())
            except OSError:
                break
        try:
            self.wfile.flush()
        except OSError:
            pass

        try:
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(msg.encode()); self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b': keepalive\n\n'); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _sse_lock:
                if q in _sse_clients:
                    _sse_clients.remove(q)


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ── HTML ──────────────────────────────────────────────────────────────────────

import os as _os
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cli._html_ui import _HTML_UI
from cli._html_js import _HTML_JS
_HTML = _HTML_UI + _HTML_JS



# ── Entry point ───────────────────────────────────────────────────────────────

def _cmd_web(args) -> None:
    host         = getattr(args, 'host', '127.0.0.1')
    port         = getattr(args, 'port', 8080)
    open_browser = getattr(args, 'open_browser', False)

    for attempt in range(10):
        try:
            server = _ThreadedHTTPServer((host, port + attempt), _Handler)
            port   = port + attempt
            break
        except OSError:
            continue
    else:
        print(f'  Could not bind to any port in {port}–{port+9}')
        return

    url = f'http://{host}:{port}'
    def _p(s): print(s, flush=True)
    _p(_wc('─'*52, _C_DRD))
    _p(f'  {_wc("AetherWard", _C_ACC)}  {_pal.wc("web interface", _pal.DIM)}')
    _p(f'  {_wc("─"*48, _C_DRD)}')
    _p(f'  {_pal.wc("url      ", _pal.DIM)} {_wc(url, _C_RED)}')
    _p(f'  {_pal.wc("sessions ", _pal.DIM)} {_pal.wc(str(AW_SESSIONS), _pal.SKY)}')
    _p(f'  {_pal.wc("configs  ", _pal.DIM)} {_pal.wc(str(AW_CONFIGS),  _pal.SKY)}')
    _p(_wc('─'*52, _C_DRD))
    _p(f'  {_pal.wc("Ctrl-C to stop  ·  requests logged below", _pal.DIM)}\n')

    if open_browser:
        import threading as _t
        _t.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _solve_stop.set()
        _run_stop.set()
        server.server_close()
        print('\n  Web server stopped.\n')
