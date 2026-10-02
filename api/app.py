"""Vercel entrypoint for the multi-user Telegram broadcast bot.

The mature broadcast implementation stays in api.index. Import it once,
install the safe per-user Telegram QR-login layer, then re-export its FastAPI
application.
"""

import importlib
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

legacy = importlib.import_module("api.index")

from services.multiuser_qr_extension import install_multiuser  # noqa: E402

install_multiuser(legacy)

app = legacy.app
