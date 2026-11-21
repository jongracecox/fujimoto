# `vscode.py`

**`vscode.py`** — Open directories in VS Code:
- `open_vscode(directory)` — runs `code <directory>`. Raises `OSError` if the `code` CLI is not on PATH.
- `_has_vscode()` — checks for `code` on PATH via `shutil.which`
