"""
AetherWard unified ANSI colour palette.
Single source of truth — imported by cli.aetherward and cli.web.
Red is the primary accent colour.
"""
import os as _os
import sys as _sys

def _is_tty() -> bool:
    return bool(_sys.stdout.isatty() and not _os.environ.get('NO_COLOR'))

TTY: bool = _is_tty()  # snapshot — use _is_tty() for live checks

# ── Reset / modifiers ─────────────────────────────────────────────────────────
RST = '\033[0m'
BLD = '\033[1m'
DIM = '\033[2m'

# ── Primary ───────────────────────────────────────────────────────────────────
RED = '\033[38;2;255;60;60m'         # vivid red — values, URLs
ACC = '\033[38;2;255;60;60m\033[1m'  # bold red — titles, accent

# ── Structural ────────────────────────────────────────────────────────────────
DRD = '\033[38;2;100;40;60m'         # dim red-maroon — rules, separators

# ── Semantic ──────────────────────────────────────────────────────────────────
GRN = '\033[38;2;34;210;140m'        # emerald — success, 2xx
YLW = '\033[38;2;240;180;40m'        # amber — warnings, 3xx
ORG = '\033[38;2;255;140;60m'        # orange — notices
PRP = '\033[38;2;168;100;255m'       # purple — secondary info
CYN = '\033[38;2;0;212;200m'         # teal — labels, headings, HTTP methods
SKY = '\033[38;2;100;180;255m'       # sky blue — paths, hints

# ── Neutrals ──────────────────────────────────────────────────────────────────
TXT = '\033[38;2;160;145;180m'       # warm lavender — body text
MU  = '\033[38;2;110;95;130m'        # muted purple-gray — secondary labels
VMU = '\033[38;2;68;55;82m'          # very muted — timestamps, dim decoration


def wc(text: str, *codes: str) -> str:
    """Wrap *text* in ANSI *codes* and reset. No-op when stdout is not a TTY."""
    return (''.join(codes) + str(text) + RST) if _is_tty() else str(text)
