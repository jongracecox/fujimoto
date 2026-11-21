# `yaml_edit.py`

**`yaml_edit.py`** — `update_key(path, key, value, model, remove=)` changes one
top-level key of a YAML file **on the text**: `yaml.compose` gives each
top-level key's start line, the key's block runs to the next key minus any
trailing column-0 comments/blank lines, and only those lines are replaced,
removed or appended (value rendered by `yaml.safe_dump`, so `on` is quoted for
the PyYAML reader). The result is validated against `model` before an atomic
write (tmp + `os.replace`); an invalid value leaves the file untouched; an
unchanged value doesn't rewrite. ruamel.yaml round-tripping was tried and
rejected: a comment-only file (the `--create-config` template) loads as
`None`, and deleting the last key drops the comment after it.
