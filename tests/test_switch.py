import json
import os
import stat
from pathlib import Path

import pytest

from claude_provider_switch import core
from claude_provider_switch.cli import main
from claude_provider_switch.core import Paths, Provider, SwitchError, save_provider, switch

# Synthetic copy of a real-world layout: plugins, statusLine, nested settings, extra env keys.
SETTINGS = """{
  "env": {
    "ANTHROPIC_BASE_URL": "https://old.example.invalid/api",
    "ANTHROPIC_AUTH_TOKEN": "sk-old-0000000000000000",
    "IS_SANDBOX": "1",
    "TAVILY_API_KEY": "tvly-keep-me"
  },
  "statusLine": {
    "type": "command",
    "command": "bash ~/.claude/statusline-command.sh"
  },
  "enabledPlugins": {
    "dev-env@cc-plugins": true
  },
  "hooks": {
    "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "echo done"}]}]
  },
  "modelSettings": {
    "claude-opus-5-5": {
      "effortLevel": "high"
    }
  },
  "skipDangerousModePermissionPrompt": true,
  "theme": "dark",
  "permissions": {
    "deny": [
      "WebSearch",
      "WebFetch"
    ]
  },
  "note": "中文保持原样"
}
"""

NEW = Provider("new", "https://new.example.invalid/v1", "sk-new-1111111111111111")


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Paths:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    for key in (*core.MANAGED_KEYS, "CLAUDECODE"):
        monkeypatch.delenv(key, raising=False)
    p = Paths(tmp_path)
    p.settings.write_text(SETTINGS, "utf-8")
    os.chmod(p.settings, 0o644)
    save_provider(p, NEW)
    return p


def expected_text(original: str) -> str:
    return original.replace("https://old.example.invalid/api", NEW.base_url).replace(
        "sk-old-0000000000000000", NEW.token
    )


def test_switch_changes_only_url_and_token_bytes(paths: Paths) -> None:
    result = switch(paths, "new")
    assert result.changed
    # In-place replacement: even the hand-compacted "hooks" line is untouched.
    assert paths.settings.read_text("utf-8") == expected_text(SETTINGS)
    assert result.backup.read_text("utf-8") == SETTINGS


def test_reorders_nothing_when_a_key_must_be_added(paths: Paths) -> None:
    data = json.loads(SETTINGS)
    del data["env"][core.TOKEN_KEY]
    paths.settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")
    switch(paths, "new")
    after = json.loads(paths.settings.read_text("utf-8"))
    assert list(after) == list(data)
    assert list(after["env"]) == [core.URL_KEY, "IS_SANDBOX", "TAVILY_API_KEY", core.TOKEN_KEY]
    data["env"].update({core.URL_KEY: NEW.base_url, core.TOKEN_KEY: NEW.token})
    assert after == data


def test_ignores_lookalike_keys_outside_top_level_env(paths: Paths) -> None:
    original = (
        '{"other": {"env": {"ANTHROPIC_BASE_URL": "decoy"}},\n'
        ' "env": {"ANTHROPIC_AUTH_TOKEN": "sk-old-0000000000000000", "x": [1, {"ANTHROPIC_BASE_URL": "decoy"}],\n'
        '         "ANTHROPIC_BASE_URL": "https://old.example.invalid/api"}}'
    )
    paths.settings.write_text(original, "utf-8")
    switch(paths, "new")
    assert paths.settings.read_text("utf-8") == expected_text(original)


def test_switch_is_byte_exact_for_standard_formatting(paths: Paths) -> None:
    original = json.dumps(json.loads(SETTINGS), indent=2, ensure_ascii=False) + "\n"
    paths.settings.write_text(original, "utf-8")
    switch(paths, "new")
    assert paths.settings.read_text("utf-8") == expected_text(original)


def test_preserves_crlf_four_space_indent_and_missing_final_newline(paths: Paths) -> None:
    original = json.dumps(json.loads(SETTINGS), indent=4, ensure_ascii=False).replace("\n", "\r\n")
    paths.settings.write_bytes(original.encode("utf-8"))
    switch(paths, "new")
    assert paths.settings.read_bytes() == expected_text(original).encode("utf-8")


def test_preserves_bom(paths: Paths) -> None:
    original = json.dumps(json.loads(SETTINGS), indent=2, ensure_ascii=False) + "\n"
    paths.settings.write_bytes(core.BOM + original.encode("utf-8"))
    switch(paths, "new")
    assert paths.settings.read_bytes() == core.BOM + expected_text(original).encode("utf-8")


def test_preserves_mode(paths: Paths) -> None:
    os.chmod(paths.settings, 0o640)
    switch(paths, "new")
    assert stat.S_IMODE(paths.settings.stat().st_mode) == 0o640


def test_adds_env_when_missing(paths: Paths) -> None:
    paths.settings.write_text('{\n  "theme": "dark"\n}\n', "utf-8")
    switch(paths, "new")
    assert json.loads(paths.settings.read_text("utf-8")) == {"theme": "dark", **NEW.document()}


def test_noop_when_already_active(paths: Paths) -> None:
    switch(paths, "new")
    before = paths.settings.read_bytes()
    backups = list(paths.backups.iterdir())
    result = switch(paths, "new")
    assert not result.changed
    assert paths.settings.read_bytes() == before
    assert list(paths.backups.iterdir()) == backups


@pytest.mark.parametrize(
    "broken",
    [
        '{"env": {"ANTHROPIC_BASE_URL": "x",}}',  # trailing comma
        '{"env": {}, "env": {}}',  # duplicate key
        '{"a": NaN}',
        '["not", "an", "object"]',
        '{"env": "not an object"}',
    ],
)
def test_refuses_unparseable_settings(paths: Paths, broken: str) -> None:
    paths.settings.write_text(broken, "utf-8")
    with pytest.raises(SwitchError):
        switch(paths, "new")
    assert paths.settings.read_text("utf-8") == broken


def test_refuses_while_vim_swap_exists(paths: Paths) -> None:
    (paths.root / ".settings.json.swp").write_text("")
    with pytest.raises(SwitchError, match="编辑器锁文件"):
        switch(paths, "new")
    assert paths.settings.read_text("utf-8") == SETTINGS
    assert switch(paths, "new", force=True).changed


def test_aborts_if_file_changes_during_switch(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    real_backup = core._backup

    def racing_backup(p: Paths, s: core.Settings) -> Path:
        out = real_backup(p, s)
        p.settings.write_text(SETTINGS.replace("dark", "light"), "utf-8")
        return out

    monkeypatch.setattr(core, "_backup", racing_backup)
    with pytest.raises(SwitchError, match="被其他程序修改"):
        switch(paths, "new")
    assert paths.settings.read_text("utf-8") == SETTINGS.replace("dark", "light")
    assert not [f for f in paths.root.iterdir() if ".ccp-" in f.name]  # temp file cleaned


def test_follows_symlink(paths: Paths, tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    paths.settings.rename(real)
    paths.settings.symlink_to(real)
    switch(paths, "new")
    assert paths.settings.is_symlink()
    assert json.loads(real.read_text("utf-8"))["env"][core.URL_KEY] == NEW.base_url


def test_backups_are_private_and_pruned(paths: Paths) -> None:
    other = Provider("other", "https://other.example.invalid", "sk-other-222222222222")
    save_provider(paths, other)
    for i in range(core.BACKUP_KEEP + 5):
        switch(paths, "new" if i % 2 == 0 else "other")
    backups = list(paths.backups.glob("settings.*.json"))
    assert len(backups) == core.BACKUP_KEEP
    assert all(stat.S_IMODE(b.stat().st_mode) == 0o600 for b in backups)
    assert stat.S_IMODE(paths.backups.stat().st_mode) == 0o700


def test_provider_files_are_private_and_strict(paths: Paths) -> None:
    path = paths.providers / "new.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.providers.stat().st_mode) == 0o700
    data = json.loads(path.read_text("utf-8"))
    data["env"]["ANTHROPIC_MODEL"] = "something"
    path.write_text(json.dumps(data), "utf-8")
    with pytest.raises(SwitchError, match="格式不对"):
        switch(paths, "new")
    assert paths.settings.read_text("utf-8") == SETTINGS


def test_rejects_bad_names(paths: Paths) -> None:
    with pytest.raises(SwitchError, match="不合法"):
        switch(paths, "../settings")


def test_warns_about_api_key(paths: Paths) -> None:
    data = json.loads(SETTINGS)
    data["env"]["ANTHROPIC_API_KEY"] = "sk-api"
    paths.settings.write_text(json.dumps(data, indent=2), "utf-8")
    result = switch(paths, "new")
    assert any("ANTHROPIC_API_KEY" in w for w in result.warnings)
    assert json.loads(paths.settings.read_text("utf-8"))["env"]["ANTHROPIC_API_KEY"] == "sk-api"


def test_shell_env_warning_skipped_inside_claude_code(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(core.URL_KEY, "https://inherited.example.invalid")
    assert any(core.URL_KEY in w for w in switch(paths, "new").warnings)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert not any(core.URL_KEY in w for w in switch(paths, "new").warnings)


# ---------- CLI ----------


def test_cli_flow(paths: Paths, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["save", "old"]) == 0
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "* old" in out and "  new" in out
    assert "sk-old-0000000000000000" not in out  # tokens are masked

    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("sk-third-333333333333\n"))
    assert main(["add", "third", "--url", "https://third.example.invalid"]) == 0
    assert main(["use", "third"]) == 0
    assert json.loads(paths.settings.read_text("utf-8"))["env"][core.TOKEN_KEY] == "sk-third-333333333333"

    assert main(["use", "old"]) == 0
    assert paths.settings.read_text("utf-8") == SETTINGS

    assert main(["rm", "third"]) == 0
    assert main(["use", "missing"]) == 1
    assert "没有名为 missing" in capsys.readouterr().err
