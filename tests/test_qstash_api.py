"""Test QStash API compatibility."""
from qstash import QStash

def test_qstash_import():
    """Test QStash import from correct module."""
    try:
        from qstash import QStash
        print("✅ QStash imported from qstash module")
        return True
    except ImportError as e:
        print(f"❌ QStash import failed: {e}")
        return False

def test_qstash_client():
    """Test QStash client creation."""
    try:
        q = QStash(token="test_token")
        print("✅ QStash client created")
        return True
    except Exception as e:
        print(f"❌ QStash client creation failed: {e}")
        return False

def test_qstash_message_api():
    """Test QStash message API exists."""
    try:
        q = QStash(token="test_token")
        assert hasattr(q, 'message'), "QStash should have 'message' attribute"
        assert hasattr(q.message, 'publish_json'), "QStash.message should have 'publish_json'"
        print("✅ QStash message.publish_json API available")
        return True
    except Exception as e:
        print(f"❌ QStash message API test failed: {e}")
        return False

if __name__ == "__main__":
    import sys
    results = []
    results.append(test_qstash_import())
    results.append(test_qstash_client())
    results.append(test_qstash_message_api())
    
    if all(results):
        print("\n✅ All QStash tests passed")
        sys.exit(0)
    else:
        print("\n❌ Some QStash tests failed")
        sys.exit(1)
