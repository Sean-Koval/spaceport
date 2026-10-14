"""Host privacy/readability preparation, separate from required real UID999 CI."""
import json
import os
from pathlib import Path
import re
import runpy
import stat

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "scripts/cube_runtime_secrets.py"


def prepare(directory):
    assert MODULE.exists(), "Missing container-readable synthetic secret preparation"
    return runpy.run_path(str(MODULE))["prepare"](directory)


def test_disposable_file_secrets_are_container_readable_inside_private_host_directories(tmp_path):
    directory = tmp_path / "secrets"
    directory.mkdir(mode=0o700)
    prepare(directory)
    expected = set(json.loads((ROOT / "infra/compose.yaml").read_text())["secrets"])
    assert {file.name for file in directory.iterdir()} == expected
    assert stat.S_IMODE(tmp_path.stat().st_mode) == stat.S_IMODE(directory.stat().st_mode) == 0o700
    contents = set()
    for file in directory.iterdir():
        assert stat.S_IMODE(file.stat().st_mode) == 0o444
        assert file.stat().st_uid == os.getuid()
        value = file.read_text()
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}\n", value):
            pytest.fail("Invalid disposable synthetic secret format", pytrace=False)
        contents.add(value)
    assert len(contents) == len(expected)


@pytest.mark.parametrize("parent_mode,directory_mode", [(0o755, 0o700), (0o700, 0o755), (0o777, 0o777)])
def test_preparation_rejects_host_directory_exposure_before_creating_files(tmp_path, parent_mode, directory_mode):
    tmp_path.chmod(parent_mode)
    directory = tmp_path / "secrets"
    directory.mkdir(mode=directory_mode)
    directory.chmod(directory_mode)
    try:
        with pytest.raises(ValueError, match="private synthetic secret directory"):
            prepare(directory)
        assert list(directory.iterdir()) == []
    finally:
        tmp_path.chmod(0o700)
        directory.chmod(0o700)


def test_preparation_refuses_existing_files_and_symlinks(tmp_path):
    directory = tmp_path / "secrets"
    directory.mkdir(mode=0o700)
    existing = directory / "cube_signing_key"
    existing.write_text("existing-synthetic-marker")
    with pytest.raises(ValueError, match="private synthetic secret directory"):
        prepare(directory)
    assert existing.read_text() == "existing-synthetic-marker"
    link = tmp_path / "secret-link"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError, match="private synthetic secret directory"):
        prepare(link)
