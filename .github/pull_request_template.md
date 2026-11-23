## What and why

<!-- One paragraph. Link the issue when there is one. -->

## How to check it

<!-- The literal ps line and the command a reviewer runs. -->

## Checklist

- [ ] The module is still one file with standard-library imports only.
- [ ] The change is exercised with a literal `LC_ALL=C TZ=UTC` `ps` line, not a live foreign process.
- [ ] Two missing values still do not compare equal.
- [ ] A line whose weekday contradicts its date is still rejected.
- [ ] A line is added under Unreleased in CHANGELOG.md when a caller will notice.
- [ ] Platforms not tested are named here.
