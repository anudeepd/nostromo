import pytest
from fastapi.testclient import TestClient

from xwing.app import create_app
from xwing.config import Settings

_ALL_PERMS_YAML = """\
users:
  "*":
    read: true
    write: true
    delete: true
"""


@pytest.fixture
def root(tmp_path):
    return tmp_path


@pytest.fixture
def tmp_dir(tmp_path):
    d = tmp_path / "tmp"
    d.mkdir()
    return d


@pytest.fixture
def users_yaml(tmp_path):
    f = tmp_path / "users.yaml"
    f.write_text(_ALL_PERMS_YAML)
    return f


@pytest.fixture
def settings(root, tmp_dir, users_yaml):
    return Settings(root_dir=root, tmp_dir=tmp_dir, users_config=users_yaml)


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
