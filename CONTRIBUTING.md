# Contributing

## Before you start

For an adoption bug, include the starting HEAD, index and working-file states in
a small reproducer. For a publication bug, include the expected remote branch and
the JSON result. Use temporary repositories with example data.

Open an issue before changing the JSON schema, ref layout, retry policy, or
checkout recovery protocol. Report security issues privately. [Security policy](SECURITY.md)

## Setup

From the source directory, install into a virtual environment and run the suite.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install '.[test]'
.venv/bin/python -m pytest -q
```

The tests use temporary repositories and local remotes. Install `lsof` so the
suite can exercise stale-index-lock recovery.

## What a patch needs

- A temporary Git repository fixture that reproduces a behavior change.
- The observed publication and adoption outcomes when both are affected.
- A changelog entry for behavior callers can observe, including changes to JSON.
- The platforms and Git versions you exercised.

Preserve independent index and working changes, use bounded ref reconciliation,
and keep installation-specific policy outside the package.

Contributions are licensed under the MIT license. [License](LICENSE)
