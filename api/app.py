"""Vercel entrypoint for the multi-user Telegram broadcast bot."""

import importlib
import sys
from pathlib import Path

from fastapi import FastAPI

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

legacy = importlib.import_module("api.index")

from services.multiuser_qr_extension import install_multiuser  # noqa: E402
from services.account_selector_extension import install_account_selector  # noqa: E402

install_multiuser(legacy)
install_account_selector(legacy)

app = FastAPI(title="Telegram Broadcast Bot")
app.mount("/", legacy.app)
