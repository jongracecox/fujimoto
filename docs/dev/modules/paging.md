# `paging.py`

**`paging.py`** — what PageUp/PageDown move by in the TUI's lists
(`PagingListView`, see `cli.md`). `PageStep(kind, amount)`: `kind="percent"`
moves by `amount`% of the viewport's rows, `kind="rows"` by `amount` rows.
`parse_page_step()` accepts `"50%"`, `"8"` or the int `8` (how YAML reads a bare
number) and raises `ValueError` for anything else, including `0`, `0%`, over
`100%`, fractions and bools. `str(step)` gives back the canonical text, which
is what the config stores. `DEFAULT` is half a page: a whole page made it easy
to jump past the row you wanted.
