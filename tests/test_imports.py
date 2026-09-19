"""Test all imports and basic structure."""
import sys
sys.path.insert(0, '/workspace')

def test_api_index():
    """Test FastAPI app import."""
    from api.index import app, dp
    assert app is not None
    assert dp is not None
    print("✅ api/index.py: OK")

def test_services():
    """Test services imports."""
    from services.scheduler_service import SchedulerService, get_scheduler
    from services.telegram_service import TelegramService
    print("✅ services/: OK")

def test_storage():
    """Test storage imports."""
    from storage.redis_client import get_redis
    from storage.models import Task, Template, get_task, save_task
    print("✅ storage/: OK")

def test_bot():
    """Test bot imports."""
    from bot.dispatcher import dp, bot
    print("✅ bot/: OK")

def test_qstash():
    """Test QStash API."""
    from qstash import QStash
    q = QStash(token='test')
    assert hasattr(q, 'message')
    assert hasattr(q.message, 'publish_json')
    print("✅ QStash API: OK")

if __name__ == "__main__":
    test_api_index()
    test_services()
    test_storage()
    test_bot()
    test_qstash()
    print("\n🎉 Все тесты пройдены!")
