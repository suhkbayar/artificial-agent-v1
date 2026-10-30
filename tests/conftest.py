import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "toy-repo"
REAL_MEMORY_DIR = Path("~/Documents/my-memory").expanduser()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def toy_repo(tmp_path) -> Path:
    """A throwaway git repo (with one commit of its own) built from tests/fixtures/toy-repo."""
    repo = tmp_path / "toy"
    shutil.copytree(FIXTURE, repo)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    return repo


@pytest.fixture
def mem_dir(tmp_path) -> Path:
    """A tmp COPY of the memory graph (S3: tests never touch the real memory.jsonl)."""
    d = tmp_path / "my-memory"
    d.mkdir()
    real = REAL_MEMORY_DIR / "memory.jsonl"
    if real.is_file():
        shutil.copy2(real, d / "memory.jsonl")
        shutil.copy2(REAL_MEMORY_DIR / "confidence.jsonl", d / "confidence.jsonl")
    else:  # CI without the user's graph
        (d / "memory.jsonl").write_text(
            '{"type":"entity","name":"Agent Orchestrator","entityType":"Project",'
            '"observations":["root"]}\n'
        )
        (d / "confidence.jsonl").write_text('{"entity": "Agent Orchestrator", "conf": "V"}\n')
    return d


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch, mem_dir):
    """No test may read the real .env.local, memory graph, state.db or orchestrator home."""
    root = tmp_path / "project-root"
    root.mkdir()
    monkeypatch.setenv("ORCHESTRATOR_PROJECT_ROOT", str(root))
    monkeypatch.setenv("ORCHESTRATOR_MEMORY_DIR", str(mem_dir))
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ORCHESTRATOR_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.delenv("ORCHESTRATOR_CONFIG", raising=False)
