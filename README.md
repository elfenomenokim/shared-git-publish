<div align="center">

# shared-git-publish

Publish explicit Git work and advance shared checkouts while preserving local edits.

[한국어](README.ko.md) · Python 3.9+

[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

[Install](#install) · [Quick start](#quick-start) · [Reference](#reference) · [Limits](#limits)

</div>

## Install

From the source directory, run `pipx install .` or `uv tool install .`.
In an activated virtual environment, run `python -m pip install .`.

Use Git 2.38 or newer and Python 3.9 or newer. The package uses POSIX file locks.
Install `lsof` to let it verify that an old index lock has no holder.

## Quick start

In a temporary checkout called `work`, commit `note.txt`, push it to
`origin/main`, and then edit it. These commands show the status fields from that
example. The full JSON also carries commit IDs and recovery details.

```console
$ shared-git-publish publish --repo work --path note.txt --message 'Update note' | jq -c '[.publication.status, .adoption.status]'
["published","adopted"]
$ shared-git-publish adopt --repo work --target origin/main | jq -c '.adoption.status'
"up_to_date"
$ shared-git-publish publish-commit --repo work --tip HEAD --path note.txt --no-adopt | jq -c '[.publication.status, .adoption.status]'
["up_to_date","skipped"]
```

Use separate working trees when each writer needs an independent index. Git
worktrees provide that isolation in one repository. Jujutsu snapshots its
working copy as a commit and supports separate workspaces. GitButler organizes
parallel and stacked branches. This tool handles explicit publication from a
shared checkout with the existing Git index and remote. [Git worktree](https://git-scm.com/docs/git-worktree) · [Jujutsu working copy](https://docs.jj-vcs.dev/latest/working-copy/) · [GitButler](https://gitbutler.com/)

## Reference

| Command | Effect |
| --- | --- |
| `publish --repo R --path P --message M` | Build a commit on the remote tip with a private index. Repeat `--path` for each working file. |
| `publish-commit --repo R --tip OID --path P` | Send the already-committed history leading to the tip. Repeat `--path` to name the unit's paths. Review the entire outgoing history before running this command. |
| `adopt --repo R --target OID` | Advance the current branch to a locally resolvable commit. It can align patch-equivalent history and skips genuinely unpublished local commits. |

Both publish commands default to `--remote origin --branch main` and support
`--no-adopt`. For working files, `--base` records where you began editing and
`--source-root` supplies a reviewed tree of owned bytes. Use `--delete P` to
remove a file whose base version is still on the remote. Use `--allow-empty`
when an empty file is the intended replacement for a nonempty one.

Read `publication.status` and `adoption.status` separately in schema version
1. A publication can land even when the local checkout refuses to advance.
The publish commands exit 0 on success or no change. A standalone `adopt`
exits 0 when adopted or current, 2 when refused, and 3 when skipped. An
unconfirmed push needs remote reconciliation before another attempt.

Adoption rehearses against a copy of the index. When it must retain staged
intermediates, it writes backup refs under `refs/shared-git-publish/backup/`.
Recognized automatic pairs expire after seven days. Pending recovery refs keep
their payload until a later adoption can safely reconcile it.

## Limits

A working-file publication cannot identify another writer's unstaged edits in
the same file. Use a reviewed `--source-root` when edits overlap. Working-file
publication accepts regular files and preserves their executable bit.
Adoption can refuse overlapping edits, an active Git operation, or an index
lock. The checkout holds those edits for you to resolve. The package has local
macOS coverage and CI jobs for macOS and Linux.

Run the local Git fixtures when contributing. [Contributing](CONTRIBUTING.md) · [MIT license](LICENSE)
