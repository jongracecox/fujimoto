from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fujimoto import debug
from fujimoto.config import ConfigError
from fujimoto.global_config import (
    GlobalConfig,
    global_config_path,
    load_global_config,
    update_global_key,
)
from fujimoto.project_config import ProjectConfig, template_text
from fujimoto.yaml_edit import update_key


class TestPath:
    def test_honours_xdg_config_home(self, tmp_path: Path) -> None:
        assert global_config_path() == (
            tmp_path / "xdg-config" / "fujimoto" / "config.yaml"
        )

    def test_defaults_to_dot_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XDG_CONFIG_HOME")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert global_config_path() == tmp_path / ".config" / "fujimoto" / "config.yaml"


class TestLoad:
    def _write(self, text: str) -> None:
        path = global_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_missing_is_empty(self) -> None:
        assert load_global_config() == GlobalConfig()

    def test_empty_file_is_empty(self) -> None:
        self._write("")
        assert load_global_config() == GlobalConfig()

    def test_values(self) -> None:
        self._write("model: opus\nquick_terminal: true\n")
        assert load_global_config() == GlobalConfig(model="opus", quick_terminal=True)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("page_step: 25%\n", "25%"), ("page_step: 8\n", "8")],
    )
    def test_page_step(self, text: str, expected: str) -> None:
        self._write(text)
        assert load_global_config().page_step == expected

    def test_invalid_page_step_raises(self) -> None:
        self._write("page_step: 0%\n")
        with pytest.raises(ConfigError, match="page_step"):
            load_global_config()

    def test_unknown_key_raises(self) -> None:
        self._write("modle: opus\n")
        with pytest.raises(ConfigError, match="Invalid"):
            load_global_config()

    def test_bad_yaml_raises(self) -> None:
        self._write("model: [unclosed\n")
        with pytest.raises(ConfigError, match="Could not read"):
            load_global_config()

    def test_logs_when_debugging(self, tmp_path: Path) -> None:
        debug.enable(redact=False, log_dir=tmp_path / "logs")
        try:
            self._write("model: opus\n")
            load_global_config()
            log = debug.log_path()
        finally:
            debug.disable()
        assert log is not None
        assert "global_config.load" in log.read_text()


class TestUpdateKey:
    def test_creates_file_and_directory(self) -> None:
        assert update_global_key("model", "opus") is True
        assert yaml.safe_load(global_config_path().read_text()) == {"model": "opus"}

    def test_unchanged_value_is_not_rewritten(self) -> None:
        update_global_key("model", "opus")
        assert update_global_key("model", "opus") is False

    def test_remove(self) -> None:
        update_global_key("model", "opus")
        assert update_global_key("model", None, remove=True) is True
        assert global_config_path().read_text() == ""
        assert update_global_key("model", None, remove=True) is False

    def test_invalid_value_leaves_file_untouched(self) -> None:
        update_global_key("model", "opus")
        before = global_config_path().read_text()
        with pytest.raises(ConfigError, match="quick_terminal"):
            update_global_key("quick_terminal", "maybe")
        assert global_config_path().read_text() == before

    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(ConfigError):
            update_global_key("bogus", 1)
        assert not global_config_path().exists()

    def test_keeps_template_comments(self, tmp_path: Path) -> None:
        path = tmp_path / ".fujimoto.yaml"
        path.write_text(template_text())
        update_key(path, "model", "sonnet", ProjectConfig)
        update_key(path, "claude_args", ["--plugin-dir", "x"], ProjectConfig)
        text = path.read_text()
        assert text.startswith(template_text().splitlines()[0])
        assert "# copy: files to copy into the worktree" in text
        data = yaml.safe_load(text)
        assert data == {"model": "sonnet", "claude_args": ["--plugin-dir", "x"]}

    def test_non_mapping_file_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "c.yaml"
        path.write_text("- a\n- b\n")
        with pytest.raises(ConfigError, match="mapping"):
            update_key(path, "model", "x", GlobalConfig)

    def test_unreadable_yaml_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "c.yaml"
        path.write_text("a: [\n")
        with pytest.raises(ConfigError, match="Could not read"):
            update_key(path, "model", "x", GlobalConfig)

    def test_failed_write_leaves_no_temp_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*a: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("fujimoto.yaml_edit.os.replace", _boom)
        path = tmp_path / "c.yaml"
        with pytest.raises(OSError):
            update_key(path, "model", "x", GlobalConfig)
        assert not [p for p in tmp_path.iterdir() if p.name.startswith(".c.yaml")]

    def test_unreadable_file_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "c.yaml"
        path.mkdir()  # read_text() on a directory raises
        with pytest.raises(ConfigError, match="Could not read"):
            update_key(path, "model", "x", GlobalConfig)


class TestEditText:
    """Only the edited key's lines change; every other byte survives."""

    P = Path("c.yaml")

    def edit(self, text: str, key: str, value: object = None, **kw: bool) -> str:
        from fujimoto.yaml_edit import edit_text

        return edit_text(text, key, value, path=self.P, **kw)

    def test_comment_only_file_keeps_comments(self) -> None:
        text = "# notes\n# model: x\n"
        assert self.edit(text, "model", "opus") == text + "model: opus\n"

    def test_missing_final_newline(self) -> None:
        assert self.edit("a: 1", "b", 2) == "a: 1\nb: 2\n"

    def test_replace_keeps_surrounding_comments(self) -> None:
        text = "# head\nmodel: opus  # mine\n\n# about b\nb:\n  - x\n# tail\n"
        assert self.edit(text, "model", "haiku") == (
            "# head\nmodel: haiku\n\n# about b\nb:\n  - x\n# tail\n"
        )

    def test_replace_list_value(self) -> None:
        text = "claude_args:\n  - a\n  - b\n\n# next\nmodel: x\n"
        assert self.edit(text, "claude_args", ["c"]) == (
            "claude_args:\n- c\n\n# next\nmodel: x\n"
        )

    def test_remove_last_key_keeps_trailing_comment(self) -> None:
        text = "# head\nquick_terminal: true\nmodel: opus  # inline\n# tail\n"
        assert self.edit(text, "model", remove=True) == (
            "# head\nquick_terminal: true\n# tail\n"
        )

    def test_remove_only_key_keeps_comments(self) -> None:
        text = "# head\n\nmodel: opus\n\n# tail\n"
        assert self.edit(text, "model", remove=True) == "# head\n\n\n# tail\n"

    def test_remove_absent_key_is_a_no_op(self) -> None:
        assert self.edit("a: 1\n", "b", remove=True) == "a: 1\n"

    def test_strings_that_look_like_booleans_are_quoted(self) -> None:
        out = self.edit("", "model", "on")
        assert yaml.safe_load(out) == {"model": "on"}

    def test_flow_mapping_value(self) -> None:
        text = "a: {x: 1,\n  y: 2}\nb: 3\n"
        assert self.edit(text, "a", 5) == "a: 5\nb: 3\n"
