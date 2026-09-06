---
name: Git
tags: [git, vcs, version-control, github]
dependencies: []
version_hint: git@2.40+
project_detect:
  - .git
summary: Git tracks CHANGES, not files. Small commits with a clear intent beat one giant blob. Understand the state before you modify (git status + git log + git diff). Never force-push shared history without explicit consent. Rebase locally to clean up before pushing; merge on shared branches. Amend for private commits, never for pushed. Stash untracked work before destructive operations. Read commit messages — they're the "why" the diff doesn't show.
---

# Git (production hygiene)

## Understand before you modify

Before ANY change:

    git status              # what's modified / untracked / staged
    git log --oneline -10   # recent history (know what you're building on)
    git diff                # unstaged changes
    git diff --cached       # staged changes

If you skip this, you'll `git checkout .` on someone's uncommitted work — irreversible without
a working tree recovery. Ask.

## Commits

* One commit = one logical change. If you can't describe the intent in a single sentence, it's
  probably two commits.
* Message format: 50-char summary, blank line, body explaining the WHY (the diff shows the WHAT).
* Present-tense imperative summary: "Fix N+1 query in dashboard", not "Fixed" or "Fixes".
* Reference issues: `Fixes #123` in the body activates the platform's autoclose.
* `--no-verify` bypasses hooks — never do this without a specific reason (the hooks are there
  for a reason; if they're broken, fix them, don't skip them).

## Branches

* `main` (or `master`) is protected — no force-push, no direct commits, PR required.
* Feature branches: `feat/<short-description>`, `fix/<short-description>`, `chore/…`.
* Delete merged branches — clutter compounds.
* Long-running branches (weeks) rot fast; rebase daily onto main.

## Merging vs rebasing

* **Rebase LOCAL work** onto the latest main before pushing. Result: linear history, no ugly
  merge commits.
* **Merge shared branches** (PR into main). Merge commit records "this feature landed".
* **NEVER rebase or force-push** history that others have pulled — you're rewriting their
  local branches; they'll be confused and lose work.
* `git pull --rebase` on your feature branch when others' work landed on main — keeps history
  linear.

## Amend

* `git commit --amend` — modifies the LAST commit. Fine BEFORE pushing.
* AFTER pushing: never amend a shared commit. Add a new commit instead.
* `git commit --amend --no-edit` — updates the commit without changing the message (adding
  a forgotten file).

## Conflicts

    git rebase main
    # ... CONFLICT (content): src/foo.py
    # edit the file, resolve markers (<<<<<<< / ======= / >>>>>>>)
    git add src/foo.py
    git rebase --continue

* Resolve by INTENT — what did each side mean? Pick, combine, or reject. Never blindly accept
  one side.
* Test after every resolution — running tests locally before continuing catches botched merges.
* `git rebase --abort` if you got in over your head; back to safe state.

## History spelunking

    git log --grep="bug"                      # by commit message
    git log --author="Almir"                  # by author
    git log -S "specificString"               # commits that added/removed the string
    git log --oneline -- path/to/file          # history of one file
    git blame path/to/file                    # who last touched each line
    git bisect start && git bisect bad && git bisect good <sha>   # binary search for a bug

## Safe destructive operations

* Before `git reset --hard`, `git checkout .`, `git clean -fd`: run `git status` FIRST. If
  ANYTHING is uncommitted that you might want, `git stash push -u -m "wip"` first.
* Before `git push --force`: know EXACTLY what you're overwriting. Prefer `--force-with-lease`
  which refuses if someone else pushed since your last fetch.
* Before `git branch -D`: check `git log branchname` and ensure you have no unmerged work.
* Recovery: `git reflog` shows EVERYTHING that changed the HEAD in the last 90 days.

## `.gitignore`

    .env*
    .venv/
    node_modules/
    __pycache__/
    *.pyc
    .DS_Store
    build/
    dist/

* Add BEFORE committing anything — files already tracked stay tracked even if listed in
  .gitignore. `git rm --cached path` to untrack.
* Never commit `.env` or secrets. If you did: rotate the secret + `git rebase` history +
  `git push --force` (accept the consequences of rewriting) OR use BFG / git-filter-repo.

## Pull requests

* One PR = one intent. Big refactor + a feature + a bug fix in one PR is a nightmare to review.
* Description: WHAT changes, WHY it changes, HOW to verify. Screenshots for UI.
* Draft mode when work is unfinished; open for review only when ready.
* Address every review comment — either accept or explain why not.
* Squash-and-merge OR rebase-and-merge (repo convention) — keeps main's history clean.

## Hooks

* `pre-commit`: format + lint + type-check. Locally installed via pre-commit / husky.
* `commit-msg`: enforce commit message format if the team cares.
* Never `--no-verify` in normal work. Hooks catch the mistakes CI would also catch — but locally,
  faster.

## Anti-patterns

* Force-push to shared branch.
* Amend a pushed commit.
* Giant commits ("fixes stuff").
* Commit messages with no body when the change needs one.
* Committing generated files (build output, minified assets, coverage reports).
* Committing lockfiles from a partial install (`npm install --no-save` result).
* Storing binaries in git (use Git LFS if you must; usually you shouldn't).
* Working directly on main.
