import pytest
from fastapi.testclient import TestClient

from xwing import upload as upload_module
from xwing.app import create_app
from xwing.config import Settings


@pytest.fixture
def root(tmp_path):
    return tmp_path


@pytest.fixture
def tmp_dir(tmp_path):
    d = tmp_path / "tmp"
    d.mkdir()
    return d


@pytest.fixture
def settings(root, tmp_dir):
    return Settings(root_dir=root, tmp_dir=tmp_dir)


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture(autouse=True)
def clear_sessions():
    """Ensure the module-level sessions dict is clean for every test."""
    upload_module._sessions.clear()
    yield
    upload_module._sessions.clear()
