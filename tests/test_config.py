from pathlib import Path

from orchestrator.config import CONFIG_FILE_ENV, HOME_DIR_ENV, STATE_DB_ENV, load_config


def test_defaults_match_decision_o12(tmp_path, monkeypatch):
    monkeypatch.setenv(HOME_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(CONFIG_FILE_ENV, raising=False)
    monkeypatch.delenv(STATE_DB_ENV, raising=False)
    cfg = load_config()
    assert (cfg.budget.per_call, cfg.budget.per_session, cfg.budget.total) == (2.0, 10.0, 100.0)
    assert cfg.checkpoint_dir == tmp_path / "checkpoints"  # outside any repo
    assert cfg.state_db == Path("state.db")


def test_toml_and_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv(HOME_DIR_ENV, str(tmp_path))
    (tmp_path / "config.toml").write_text("call_timeout_s = 99\n[budget_usd]\ntotal = 5\n")
    monkeypatch.setenv(STATE_DB_ENV, str(tmp_path / "x.db"))
    cfg = load_config()
    assert cfg.call_timeout_s == 99
    assert cfg.budget.total == 5.0 and cfg.budget.per_call == 2.0
    assert cfg.state_db == tmp_path / "x.db"
