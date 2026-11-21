# `git.py`

**`git.py`** — Thin wrappers around `git` subprocess calls:
- `_run(args, cwd)` — subprocess runner, raises `GitError` on non-zero exit
- `get_repo_root()` — `git rev-parse --show-toplevel`
- `get_project_root(cwd)` — the main clone's root (via `get_main_worktree_root`), even from inside a linked worktree. This is what the TUI's `_project_root` and `fujimoto new` use, so running fujimoto from a worktree session sees the same project, and creates worktrees in the same place, as running it from the main clone. (It used to be `get_repo_root`, which named the project after the *worktree* directory and nested new worktrees under it.) Git commands still run in `_project_cwd`, and "current branch" is still the cwd's.
- `get_project_name()` — basename of `get_project_root`
- `get_main_worktree_root(cwd)` — parent of `--git-common-dir`; the main worktree root even when `cwd` is a linked worktree. Falls back to `get_repo_root` when the common dir isn't named `.git` (a submodule's is `<super>/.git/modules/<name>`, a bare repo's is `repo.git`), where the parent is not a working copy
- `get_current_branch()` — `git branch --show-current`
- `get_default_branch()` — tries `symbolic-ref`, falls back to checking main/master
- `fetch_and_rebase_branch(branch)` — `git fetch origin` + `git rebase origin/{branch}`
- `list_branches()` — sorted list of local branch names
- `create_worktree(path, base_branch, new_branch)` — `git worktree add -b`. In a repository with no commits yet (fresh `git init`, where `main` is unborn and git says `invalid reference: main`) it falls back to `git worktree add --orphan -b` (git 2.42+); a bad base in a repo that *has* commits still raises
- `has_commits()` — `git rev-list -n 1 --all` is non-empty
- `remove_worktree(path)` — `git worktree remove --force`
- `get_unpushed_commits(branch)` — commits not yet on remote
- `get_merge_base(branch)` — fork point from default branch
- `is_branch_merged(branch, into)` — `git merge-base --is-ancestor`
- `has_remote_branch(branch)` — `git ls-remote --heads`
- `push_branch(branch)` — `git push -u origin`
- `delete_branch(branch, remote)` — `git branch -D`, optionally remote
- `cherry_pick_branch(branch, onto)` — cherry-picks commit range onto target
