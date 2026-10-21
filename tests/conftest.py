import os
import shutil
import tempfile
import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Ensure an isolated database and private uploads directory for every test run."""
    temp_dir = tempfile.mkdtemp(prefix="hs_test_")
    test_db = os.path.join(temp_dir, "test_homeserve.db")
    test_storage = os.path.join(temp_dir, "private_uploads")

    monkeypatch.setattr(main, "DB_PATH", test_db)
    monkeypatch.setattr(main, "PRIVATE_STORAGE", main.Path(test_storage).resolve())
    monkeypatch.setattr(main, "ADMIN_KEY", "test_admin_key_998877")
    monkeypatch.setattr(main, "COMMISSION_PCT", 10.0)
    monkeypatch.setattr(main, "ASSIGN_TIMEOUT_MIN", 10)
    # Ensure tests trigger instant keyword triage fallback without network timeout delay
    monkeypatch.setattr(main, "ai_analyze", lambda text: main.keyword_guess(text))

    # Initialize isolated DB and seed demo data
    main.init_db()

    yield {
        "temp_dir": temp_dir,
        "db_path": test_db,
        "admin_key": "test_admin_key_998877",
    }

    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def client():
    return TestClient(main.app)
