import logging
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must not see JOB_SCOUT_* vars from the developer's shell."""
    for var in list(os.environ):
        if var.startswith("JOB_SCOUT_"):
            monkeypatch.delenv(var)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A writable copy of the real config/ directory."""
    target = tmp_path / "config"
    shutil.copytree(REPO_CONFIG, target, copy_function=shutil.copyfile)  # drop read-only modes
    return target


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{old!r} not in {path.name}"
    path.write_text(text.replace(old, new, 1))


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """CLI tests call setup_logging, which binds handlers to CliRunner's temporary stderr."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
