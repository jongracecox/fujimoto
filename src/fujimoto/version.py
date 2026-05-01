from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version


def get_version() -> str:
    try:
        return _pkg_version("fujimoto")
    except PackageNotFoundError:
        return "0.0.0+unknown"
