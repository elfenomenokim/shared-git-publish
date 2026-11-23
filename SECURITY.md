# Security policy

## Supported versions

Security fixes go into the latest release. Include the version and platform in
your report so the maintainers can reproduce the behavior.

## Reporting

Use the repository's Security tab and choose **Report a vulnerability**. Include
the package and Git versions, a temporary-repository reproducer, and the effect
on the working tree, index, or remote. Remove credentials and private paths from
the reproducer. [Private advisory form](https://github.com/elfenomenokim/shared-git-publish/security/advisories/new)

## Scope

Report an operation that publishes unintended working bytes, overwrites
unrelated staged work, or escapes the named repository or source directory.
Git runs with the calling account's existing permissions, configuration and
hooks. Run the tool on repositories and remotes you trust.

The maintainers coordinate disclosure after a fix is available.
