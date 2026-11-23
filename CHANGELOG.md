# Changelog

## Unreleased

### Added

- Publish explicit working paths from a private index, with bounded recovery from
  remote ref races.
- Publish an already-committed tip with `publish-commit`.
- Advance a checkout with `adopt`, preserving local staging and working edits or
  reporting the paths that prevent adoption.
- Return schema version 1 JSON with separate publication and adoption results.
