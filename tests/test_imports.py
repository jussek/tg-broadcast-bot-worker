"""Test basic imports without environment variables."""
import sys
import os

# Mock environment variables for import testing
os.environ["BOT_TOKEN"] = "test"
os.environ["API_ID"] = "123"
os.environ["API_HASH"] = "test"
os.environ["TELEGRAM_SESSION_STRING"] = "test"
os.environ["UPSTASH_REDIS_REST_URL"] = "http://test"
os.environ["UPSTASH_REDIS_REST_TOKEN"] = "test"
os.environ["QSTASH_TOKEN"] = "test"

def test_fastapi_import():
    """Test FastAPI app import."""
    try:
        from api.index import app
        assert app is not None
        print("✅ FastAPI app imported successfully")
        return True
    except Exception as e:
        print(f"❌ FastAPI import failed: {e}")
        return False

def test_storage_imports():
    """Test storage module imports."""
    try:
        from storage import redis_client, models
        print("✅ Storage modules imported successfully")
        return True
    except Exception as e:
        print(f"❌ Storage import failed: {e}")
        return False

def test_services_imports():
    """Test services module imports."""
    try:
        from services import telegram_service, scheduler_service
        print("✅ Services modules imported successfully")
        return True
    except Exception as e:
        print(f"❌ Services import failed: {e}")
        return False

def test_bot_imports():
    """Test bot module imports."""
    try:
        from bot import dispatcher
        print("✅ Bot modules imported successfully")
        return True
    except Exception as e:
        print(f"❌ Bot import failed: {e}")
        return False

if __name__ == "__main__":
    results = []
    results.append(test_fastapi_import())
    results.append(test_storage_imports())
    results.append(test_services_imports())
    results.append(test_bot_imports())
    
    if all(results):
        print("\n✅ All imports successful")
        sys.exit(0)
    else:
        print("\n❌ Some imports failed")
        sys.exit(1)
