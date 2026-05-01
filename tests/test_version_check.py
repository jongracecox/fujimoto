from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from fujimoto import version_check
from fujimoto.version_check import (
    CheckState,
    check_for_update,
    dismiss,
    fetch_latest_from_pypi,
    is_newer,
    load_state,
    save_state,
    should_check,
)


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
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
