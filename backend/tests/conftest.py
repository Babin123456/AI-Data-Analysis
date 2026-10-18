"""Global pytest fixtures for backend tests."""
import pytest
from app.main import app
from app.utils.auth import get_current_user


async def _global_mock_get_current_user():
    return {"username": "testuser", "email": "test@example.com", "role": "user"}


@pytest.fixture(autouse=True)
def setup_auth_override():
    """Ensure authentication is always mocked for test endpoints unless explicitly altered."""
    app.dependency_overrides[get_current_user] = _global_mock_get_current_user
    yield
    app.dependency_overrides[get_current_user] = _global_mock_get_current_user
