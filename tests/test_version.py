from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

from fujimoto.version import get_version


def test_get_version_returns_string() -> None:
    assert isinstance(get_version(), str)
    assert get_version()


def test_get_version_fallback_on_missing_metadata() -> None:
    with patch(
        "fujimoto.version._pkg_version",
        side_effect=PackageNotFoundError("fujimoto"),
    ):
        assert get_version() == "0.0.0+unknown"
