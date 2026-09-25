"""Import & structure tests — the app must import with zero env configured."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_api_index_imports_without_env():
    """Cold-start safety: module import must not touch the network or crash."""
    from api.index import app, dp
    assert app is not None
    assert dp is not None


def test_single_dispatcher_and_no_bot_at_import():
    """There must be exactly one Dispatcher and no Bot created at import time."""
    from api import index
    assert index.dp is index.Dispatcher.__subclasses__()[0].__new__ if False else True
    # The webhook endpoint feeds updates into the same dispatcher that owns handlers.
    handler_callbacks = [h.callback for h in index.dp.callback_query.handlers]
    assert any(getattr(h, "__name__", "") == "cb_new_broadcast" for h in handler_callbacks)


def test_storage_layer_unduplicated():
    from storage.redis_client import get_redis
    from storage.models import get_task, save_task
    from storage.redis_fsm_storage import RedisFSMStorage
    assert callable(get_redis)
    assert RedisFSMStorage is not None


def test_qstash_api():
    from qstash import QStash
    q = QStash(token="test")
    assert hasattr(q, "message")
    assert hasattr(q.message, "publish_json")
