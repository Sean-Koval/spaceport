from pathlib import Path
import importlib.util
import pytest

pytestmark = pytest.mark.unit


ROOT = Path(__file__).resolve().parents[2]


def test_source_migration_has_legacy_parent_and_no_source_role_creation():
    path = ROOT / "migrations/versions/0005_source_lifecycle.py"
    spec = importlib.util.spec_from_file_location("source_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "0004_publishing_fixtures"
    assert "CREATE ROLE" not in path.read_text()
    assert "source_bq_admit" in module.FUNCTION_NAMES
    assert "source_bq_lookup" in module.FUNCTION_NAMES
    assert "source_domo_arm_send" in module.FUNCTION_NAMES
    assert "source_current_authority" not in module.PROVIDER_FUNCTIONS
