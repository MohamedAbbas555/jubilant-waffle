from coop_apply.cli import main
from coop_apply.config import load_config


def test_init_writes_loadable_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    cfg = load_config("config.toml")
    assert cfg.claude.model == "claude-opus-5-5"
    assert cfg.waterlooworks.selectors.row == "table tbody tr"
    assert cfg.waterlooworks.selectors.apply_button  # defaults kept for keys not in the file
    assert main(["init"]) == 1  # refuses to overwrite


def test_unknown_config_key_is_rejected(tmp_path):
    import pytest

    p = tmp_path / "c.toml"
    p.write_text("[search]\nmin_scroe = 3\n")
    with pytest.raises(ValueError, match="min_scroe"):
        load_config(p)


def test_list_on_empty_db(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["list"]) == 0
    assert "No ranked jobs" in capsys.readouterr().out


def test_missing_resume_reports_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    assert main(["resume"]) == 1
    assert "Resume not found" in capsys.readouterr().err


def test_app_saved_key_beats_environment(tmp_path, monkeypatch):
    import os

    from coop_apply.config import load_api_key

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stale-from-setx")
    assert load_api_key(tmp_path) == "environment"
    (tmp_path / "api_key").write_text("sk-ant-from-app\n")
    assert load_api_key(tmp_path) == "app"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-from-app"
