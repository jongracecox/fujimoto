from __future__ import annotations

import json
import sys
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from fujimoto import version_check
from fujimoto.version_check import (
    CheckState,
    InstallInfo,
    InstallMethod,
    agent_dismiss,
    agent_update_notice,
    check_for_update,
    detect_install,
    dismiss,
    fetch_latest_from_pypi,
    is_newer,
    load_state,
    pending_agent_update,
    refresh_latest,
    save_state,
    should_check,
)


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        version_check,
        "_cache_path",
        lambda: tmp_path / ".cache" / "fujimoto" / "version_check.json",
    )
    return tmp_path


def test_should_check_when_no_prior_check() -> None:
    assert should_check(CheckState(), datetime(2026, 5, 1, 12, 0, 0))


def test_should_check_within_24h_returns_false() -> None:
    state = CheckState(last_check=datetime(2026, 5, 1, 0, 0, 0))
    assert not should_check(state, datetime(2026, 5, 1, 12, 0, 0))


def test_should_check_after_24h_returns_true() -> None:
    state = CheckState(last_check=datetime(2026, 5, 1, 0, 0, 0))
    assert should_check(state, datetime(2026, 5, 2, 1, 0, 0))


def test_load_state_missing_file(tmp_home: Path) -> None:
    state = load_state()
    assert state == CheckState()


def test_load_state_corrupt_json(tmp_home: Path) -> None:
    cache = tmp_home / ".cache" / "fujimoto" / "version_check.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("{not json")
    assert load_state() == CheckState()


def test_save_and_load_roundtrip(tmp_home: Path) -> None:
    now = datetime(2026, 5, 1, 10, 0, 0)
    save_state(
        CheckState(last_check=now, latest_version="1.2.3", dismissed_version="1.2.2")
    )
    loaded = load_state()
    assert loaded.last_check == now
    assert loaded.latest_version == "1.2.3"
    assert loaded.dismissed_version == "1.2.2"


def test_is_newer() -> None:
    assert is_newer("1.2.3", "1.2.2")
    assert is_newer("2.0.0", "1.99.99")
    assert not is_newer("1.2.3", "1.2.3")
    assert not is_newer("1.2.2", "1.2.3")


def test_fetch_latest_from_pypi_url_error() -> None:
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
        assert fetch_latest_from_pypi() is None


def test_fetch_latest_from_pypi_success() -> None:
    class Resp:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *a: object) -> None:  # noqa: D401
            return None

        def read(self) -> bytes:
            return json.dumps({"info": {"version": "9.9.9"}}).encode()

    def fake_urlopen(url, timeout):  # type: ignore[no-untyped-def]
        return Resp()

    with patch("urllib.request.urlopen", fake_urlopen):
        with patch("json.load", lambda r: {"info": {"version": "9.9.9"}}):
            assert fetch_latest_from_pypi() == "9.9.9"


def test_check_for_update_notifies_when_newer(tmp_home: Path) -> None:
    with patch.object(version_check, "fetch_latest_from_pypi", return_value="9.9.9"):
        latest, notify = check_for_update("1.0.0")
    assert latest == "9.9.9"
    assert notify is True


def test_check_for_update_silent_when_dismissed(tmp_home: Path) -> None:
    save_state(
        CheckState(
            last_check=datetime.now(),
            latest_version="9.9.9",
            dismissed_version="9.9.9",
        )
    )
    latest, notify = check_for_update("1.0.0")
    assert latest == "9.9.9"
    assert notify is False


def test_check_for_update_silent_when_current_is_latest(tmp_home: Path) -> None:
    with patch.object(version_check, "fetch_latest_from_pypi", return_value="1.0.0"):
        latest, notify = check_for_update("1.0.0")
    assert latest == "1.0.0"
    assert notify is False


def test_check_for_update_uses_cache_within_interval(tmp_home: Path) -> None:
    save_state(CheckState(last_check=datetime.now(), latest_version="9.9.9"))
    with patch.object(
        version_check, "fetch_latest_from_pypi", side_effect=AssertionError
    ):
        latest, notify = check_for_update("1.0.0")
    assert latest == "9.9.9"
    assert notify is True


def test_check_for_update_refetches_after_interval(tmp_home: Path) -> None:
    save_state(
        CheckState(
            last_check=datetime.now() - timedelta(days=2),
            latest_version="0.5.0",
        )
    )
    with patch.object(version_check, "fetch_latest_from_pypi", return_value="9.9.9"):
        latest, notify = check_for_update("1.0.0")
    assert latest == "9.9.9"
    assert notify is True


def test_check_for_update_offline_keeps_old_state(tmp_home: Path) -> None:
    with patch.object(version_check, "fetch_latest_from_pypi", return_value=None):
        latest, notify = check_for_update("1.0.0")
    assert latest is None
    assert notify is False


def test_dismiss_writes_dismissed_version(tmp_home: Path) -> None:
    dismiss("9.9.9")
    assert load_state().dismissed_version == "9.9.9"


def test_agent_dismiss_is_separate_from_banner_dismiss(tmp_home: Path) -> None:
    dismiss("9.9.9")
    agent_dismiss("9.9.8")
    state = load_state()
    assert state.dismissed_version == "9.9.9"
    assert state.agent_dismissed_version == "9.9.8"


def test_save_and_load_keep_agent_dismissal(tmp_home: Path) -> None:
    save_state(CheckState(agent_dismissed_version="1.2.3"))
    assert load_state().agent_dismissed_version == "1.2.3"


def test_check_for_update_keeps_a_dismissal_written_meanwhile(tmp_home: Path) -> None:
    def fetch() -> str:
        # Another process dismisses while this one waits on PyPI.
        agent_dismiss("9.9.9")
        return "9.9.9"

    with patch.object(version_check, "fetch_latest_from_pypi", side_effect=fetch):
        check_for_update("1.0.0")
    assert load_state().agent_dismissed_version == "9.9.9"


class TestPendingAgentUpdate:
    def test_none_without_cache(self, tmp_home: Path) -> None:
        assert pending_agent_update("1.0.0") is None

    def test_newer_version_is_offered(self, tmp_home: Path) -> None:
        save_state(CheckState(latest_version="1.1.0"))
        assert pending_agent_update("1.0.0") == "1.1.0"

    def test_not_offered_when_current(self, tmp_home: Path) -> None:
        save_state(CheckState(latest_version="1.0.0"))
        assert pending_agent_update("1.0.0") is None

    def test_dev_build_ahead_of_release_is_not_nagged(self, tmp_home: Path) -> None:
        save_state(CheckState(latest_version="1.0.0"))
        assert pending_agent_update("1.0.1.dev3+gabc") is None

    def test_agent_dismissal_hides_that_version_only(self, tmp_home: Path) -> None:
        save_state(CheckState(latest_version="1.1.0", agent_dismissed_version="1.1.0"))
        assert pending_agent_update("1.0.0") is None
        save_state(CheckState(latest_version="1.2.0", agent_dismissed_version="1.1.0"))
        assert pending_agent_update("1.0.0") == "1.2.0"

    def test_banner_dismissal_does_not_hide_it(self, tmp_home: Path) -> None:
        save_state(CheckState(latest_version="1.1.0", dismissed_version="1.1.0"))
        assert pending_agent_update("1.0.0") == "1.1.0"

    def test_never_fetches(self, tmp_home: Path) -> None:
        with patch.object(
            version_check, "fetch_latest_from_pypi", side_effect=AssertionError
        ):
            assert pending_agent_update("1.0.0") is None


class TestRefreshLatest:
    def test_fetches_and_caches(self, tmp_home: Path) -> None:
        now = datetime(2026, 5, 1, 9, 0, 0)
        save_state(CheckState(last_check=now, latest_version="1.0.0"))
        with patch.object(
            version_check, "fetch_latest_from_pypi", return_value="2.0.0"
        ):
            assert refresh_latest(now) == "2.0.0"
        assert load_state().latest_version == "2.0.0"

    def test_offline_falls_back_to_cache(self, tmp_home: Path) -> None:
        save_state(CheckState(latest_version="1.5.0"))
        with patch.object(version_check, "fetch_latest_from_pypi", return_value=None):
            assert refresh_latest() == "1.5.0"


class TestDetectInstall:
    @pytest.fixture
    def env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        prefix = tmp_path / "env"
        prefix.mkdir()
        monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "base"))
        monkeypatch.setattr(version_check, "_direct_url", lambda: None)
        return prefix

    def test_uv_tool_from_pypi(self, env: Path) -> None:
        (env / "uv-receipt.toml").write_text("")
        info = detect_install(env)
        assert info == InstallInfo(InstallMethod.UV_TOOL, "uv tool upgrade fujimoto")

    def test_uv_tool_from_local_path_reinstalls_from_pypi(
        self, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (env / "uv-receipt.toml").write_text("")
        monkeypatch.setattr(
            version_check,
            "_direct_url",
            lambda: {"url": "file:///src/fujimoto", "dir_info": {}},
        )
        info = detect_install(env)
        assert info.method is InstallMethod.UV_TOOL
        assert info.upgrade_command == "uv tool install --force fujimoto"
        assert info.source == "/src/fujimoto"

    def test_uv_tool_from_git_upgrades_in_place(
        self, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (env / "uv-receipt.toml").write_text("")
        monkeypatch.setattr(
            version_check,
            "_direct_url",
            lambda: {"url": "https://example.com/f.git", "vcs_info": {}},
        )
        info = detect_install(env)
        assert info.upgrade_command == "uv tool upgrade fujimoto"
        assert info.source is None

    def test_pipx(self, env: Path) -> None:
        (env / "pipx_metadata.json").write_text("{}")
        assert detect_install(env).upgrade_command == "pipx upgrade fujimoto"

    def test_editable_checkout(
        self, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            version_check,
            "_direct_url",
            lambda: {"url": "file:///src/fujimoto", "dir_info": {"editable": True}},
        )
        info = detect_install(env)
        assert info == InstallInfo(InstallMethod.EDITABLE, None, "/src/fujimoto")

    def test_plain_virtualenv_is_pip(self, env: Path) -> None:
        info = detect_install(env)
        assert info.method is InstallMethod.PIP
        assert info.upgrade_command is not None
        assert info.upgrade_command.endswith("-m pip install --upgrade fujimoto")

    def test_system_python_is_unknown(
        self, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "base_prefix", str(env))
        assert detect_install(env) == InstallInfo(InstallMethod.UNKNOWN, None)


class TestDirectUrl:
    def test_reads_installed_metadata(self) -> None:
        class Dist:
            def read_text(self, name: str) -> str:
                assert name == "direct_url.json"
                return '{"url": "file:///x"}'

        with patch.object(version_check, "distribution", return_value=Dist()):
            assert version_check._direct_url() == {"url": "file:///x"}

    @pytest.mark.parametrize("text", [None, "", "{bad", "[1]"])
    def test_missing_or_bad_metadata(self, text: str | None) -> None:
        class Dist:
            def read_text(self, name: str) -> str | None:
                return text

        with patch.object(version_check, "distribution", return_value=Dist()):
            assert version_check._direct_url() is None

    def test_not_installed(self) -> None:
        with patch.object(
            version_check,
            "distribution",
            side_effect=version_check.PackageNotFoundError("fujimoto"),
        ):
            assert version_check._direct_url() is None


class TestAgentUpdateNotice:
    def test_names_the_upgrade_and_dismiss_commands(self) -> None:
        info = InstallInfo(InstallMethod.PIPX, "pipx upgrade fujimoto")
        text = agent_update_notice("1.0.0", "1.1.0", info)
        assert "fujimoto 1.1.0 is available" in text
        assert "ask the user once" in text
        assert "`pipx upgrade fujimoto`" in text
        assert "yes, not now, or don't ask again" in text
        assert "Not now: do nothing" in text
        assert "Don't ask again: run `fujimoto update dismiss 1.1.0`" in text

    def test_editable_points_at_the_checkout(self) -> None:
        info = InstallInfo(InstallMethod.EDITABLE, None, "/src/fujimoto")
        text = agent_update_notice("1.0.0", "1.1.0", info)
        assert "editable checkout (/src/fujimoto)" in text
        assert "update that checkout" in text

    def test_unknown_asks_the_agent_to_find_out(self) -> None:
        text = agent_update_notice(
            "1.0.0", "1.1.0", InstallInfo(InstallMethod.UNKNOWN, None)
        )
        assert "find out how it was installed" in text
