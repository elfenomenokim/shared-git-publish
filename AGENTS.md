# AGENTS.md

## Set up for a user

1. Run `uname -s`, `python3 --version`, and `git --version`. Use macOS or Linux,
   Python 3.9 or newer, and Git 2.38 or newer. Check `command -v lsof` before
   testing recovery of stale index locks.
2. In the source directory, run `python3 -m venv .venv` and
   `.venv/bin/python -m pip install .`. Use a virtual environment.
3. For a real publication, the person needs to select the repository, remote,
   branch, and paths. When those are unspecified, ask “Which checkout, branch,
   and files should I publish?” Use their existing Git credentials.
4. Run `.venv/bin/shared-git-publish --help`. The usage should list
   `publish`, `publish-commit`, and `adopt`.
5. If that fails:
   - Verify that the venv's Python ran the install and that both dependencies
     resolved. `.venv/bin/python -m shared_git_publish --help` uses the same CLI.
   - If the Git executable is missing or too old, install a supported Git in
     the user's environment before running a command.
   - If a real command confirms publication but refuses adoption, read the
     adoption reason. Keep the checkout intact and resolve the named conflict
     before adopting again.
   - If a send is unconfirmed, resolve remote state before another publication.

## Working in the repo

- `shared_git_publish/publish.py` builds a working-file publication in a private
  index.
- `unit.py` handles already-committed tips.
- `adopt.py` rehearses checkout advancement and recovers interrupted writes.
- `transport.py` bounds each push and checks the remote after an ambiguous send.
- `profile.py` holds integration settings. Keep host policy in the caller.
- `cli.py` renders versioned results and supplies compatibility entry points.

Run from this directory.

```sh
.venv/bin/python -m pip install . 'pytest<9'
.venv/bin/python -m pytest -q tests
.venv/bin/shared-git-publish --help
```

Use temporary repositories and local remotes in tests. Create no remote refs in
a user's repository as fixtures. Leave staging and working bytes intact on
adoption refusal. Keep publication and adoption outcomes independent, and
reconcile ambiguous pushes without replaying them. Document any change to the
schema or recovery behavior for callers.

Update both READMEs when the command contract changes. A source build and test
run authorize no upload.
