"""
Bounded Git-publication helper for explicit committed work units.

Replaces foreground's repeated fetch/merge-tree/commit-tree/push turns with
one deterministic CLI command. Operates only on an already-committed tip:
publication itself never touches worktrees, staging, indices, stashes, or refs.

After a successful publication the CLI fast-forwards the publishing checkout
through the shared-checkout adopter (skip with --no-adopt). It rehearses on a copied index,
keeps local edits, and saves staged intermediates before adopting incoming paths.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .profile import commit_message, profile
from .transport import push as push_once, deterministic_race


def run_git(
    cmd: List[str],
    cwd: str,
    runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    text: bool = True,
    input_data: Optional[Union[str, bytes]] = None,
) -> subprocess.CompletedProcess:
    if runner is not None:
        return runner(cmd, cwd=cwd, text=text, input_data=input_data)
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=text,
        input=input_data,
        timeout=120,
    )


def is_deterministic_nff(stdout: str, stderr: str) -> bool:
    # Only machine-format rejection proves no effect; hook/transport prose does not.
    return deterministic_race(stdout, stderr)


def resolve_commit_sha(repo: str, rev: str, runner=None) -> Optional[str]:
    res = run_git(["git", "-C", repo, "rev-parse", "--verify", f"{rev}^{{commit}}"], cwd=repo, runner=runner)
    if res.returncode == 0 and res.stdout.strip():
        sha = res.stdout.strip()
        if re.match(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", sha):
            return sha
    return None


def get_remote_branch_tip(repo: str, remote: str, branch: str, runner=None) -> Optional[str]:
    """Query current remote tip using ls-remote."""
    ref_name = f"refs/heads/{branch}"
    res = run_git(["git", "-C", repo, "ls-remote", remote, ref_name], cwd=repo, runner=runner)
    if res.returncode != 0:
        raise RuntimeError('Remote ref lookup unavailable; not evidence of an absent branch.')
    if res.stdout.strip():
        for line in res.stdout.strip().splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == ref_name:
                sha = parts[0]
                if re.match(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", sha):
                    return sha
    return None


def fetch_remote_branch(repo: str, remote: str, branch: str, runner=None) -> Tuple[bool, Optional[str]]:
    """Fetch remote branch into local object DB and return (exists, tip_sha)."""
    remote_tip = get_remote_branch_tip(repo, remote, branch, runner=runner)
    if not remote_tip:
        return False, None

    res = run_git(["git", "-C", repo, "fetch", remote, f"refs/heads/{branch}"], cwd=repo, runner=runner)
    if res.returncode != 0:
        raise RuntimeError('Fetch failed; no publication attempted from stale objects.')
    # FETCH_HEAD is mutable shared state. Keep the exact observed remote commit.
    if run_git(["git", "-C", repo, "cat-file", "-t", remote_tip], cwd=repo, runner=runner).stdout.strip() != 'commit':
        raise RuntimeError('Observed remote commit is unavailable after fetch.')
    return True, remote_tip


def check_is_ancestor(repo: str, ancestor: str, descendant: str, runner=None) -> bool:
    res = run_git(["git", "-C", repo, "merge-base", "--is-ancestor", ancestor, descendant], cwd=repo, runner=runner)
    return res.returncode == 0


def create_merge_commit_tree(
    repo: str,
    local_tip: str,
    remote_tip: str,
    message: str,
    runner=None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Run merge-tree --write-tree. Require exit 0 AND exactly one object ID line.
    Refuse conflicts with exact tips, never publish markers.
    Returns (success, merge_commit_sha, error_details).
    """
    res = run_git(["git", "-C", repo, "merge-tree", "--write-tree", local_tip, remote_tip], cwd=repo, runner=runner)
    lines = [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]
    if res.returncode != 0 or len(lines) != 1 or not re.match(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", lines[0]):
        details = (res.stdout.strip() + "\n" + res.stderr.strip()).strip()
        return False, None, details or "merge-tree returned non-zero or conflict marker lines"

    tree_sha = lines[0]
    # Two-parent commit: local_tip first (-p local_tip), remote_tip second (-p remote_tip)
    res_ct = run_git(
        ["git", "-C", repo, "commit-tree", tree_sha, "-p", local_tip, "-p", remote_tip, "-m", commit_message(message)],
        cwd=repo,
        runner=runner,
    )
    if res_ct.returncode != 0 or not res_ct.stdout.strip():
        return False, None, res_ct.stderr.strip() or "commit-tree failed"
    commit_sha = res_ct.stdout.strip()
    if not re.match(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", commit_sha):
        return False, None, f"Invalid commit SHA returned: {commit_sha}"
    return True, commit_sha, None


def execute_push(
    repo: str,
    remote: str,
    commit_sha: str,
    branch: str,
    runner=None,
) -> Tuple[bool, bool, str, str]:
    """
    Execute push without --force.
    Returns (success, is_nff, stdout, stderr).
    """
    refspec = f"{commit_sha}:refs/heads/{branch}"
    try:
        if runner is None:
            res = push_once(repo, remote, commit_sha, branch)
            stdout, stderr = res.stdout.decode(errors='replace'), res.stderr.decode(errors='replace')
        else:
            res = run_git(["git", "-C", repo, "push", "--porcelain", remote, refspec], cwd=repo, runner=runner)
            stdout, stderr = res.stdout, res.stderr
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        return False, False, '', str(error)  # Send disposition unknown: reconcile, never resend.
    if res.returncode == 0:
        return True, False, stdout, stderr
    is_nff = is_deterministic_nff(stdout, stderr)
    return False, is_nff, stdout, stderr


def read_git_blob(repo: str, commit_sha: str, path: str, runner=None) -> bytes:
    """Read exact raw bytes of a file blob from a commit object without decoding."""
    res = run_git(
        ["git", "-C", repo, "cat-file", "-p", f"{commit_sha}:{path}"],
        cwd=repo,
        runner=runner,
        text=False,
    )
    if res.returncode == 0:
        return res.stdout
    res_show = run_git(
        ["git", "-C", repo, "show", f"{commit_sha}:{path}", "--", path],
        cwd=repo,
        runner=runner,
        text=False,
    )
    if res_show.returncode == 0:
        return res_show.stdout
    raise RuntimeError(f"Failed to read object {commit_sha}:{path}")


def _publish_unit(
    repo: str,
    tip: str,
    remote: str,
    branch: str,
    paths: Optional[List[str]] = None,
    message: Optional[str] = None,
    runner=None,
) -> Dict[str, Any]:
    """
    Publish an already-committed work unit to remote.
    Returns concise JSON terminal receipt.
    """
    repo_path = os.path.abspath(repo)
    settings = profile.get()
    if settings.check_repo:
        settings.check_repo(repo_path)
    clean_paths = list(paths) if paths else []
    if not clean_paths or any(not p or p.startswith(('/', ':', '-')) or '..' in p.split('/') for p in clean_paths):
        raise RuntimeError('Publication requires explicit repository-relative literal unit paths.')
    if remote.startswith('-') or branch.startswith('-'):
        raise RuntimeError('Remote/branch must be explicit values, not options.')
    if run_git(['git', '-C', repo_path, 'check-ref-format', 'refs/heads/' + branch], cwd=repo_path, runner=runner).returncode:
        raise RuntimeError('Invalid target branch.')

    # Verify repo
    res_repo = run_git(["git", "-C", repo_path, "rev-parse", "--git-dir"], cwd=repo_path, runner=runner)
    if res_repo.returncode != 0:
        return {
            "status": "error",
            "repo": repo_path,
            "remote": remote,
            "branch": branch,
            "local_tip": None,
            "remote_tip": None,
            "published_tip": None,
            "method": "none",
            "paths": clean_paths,
            "recovery_attempts": 0,
            "error": f"Invalid git repository: {repo_path}",
        }

    # Verify already-committed tip
    local_tip = resolve_commit_sha(repo_path, tip, runner=runner)
    if not local_tip:
        return {
            "status": "error",
            "repo": repo_path,
            "remote": remote,
            "branch": branch,
            "local_tip": None,
            "remote_tip": None,
            "published_tip": None,
            "method": "none",
            "paths": clean_paths,
            "recovery_attempts": 0,
            "error": f"Tip rev '{tip}' does not resolve to an existing commit object",
        }

    if settings.check_path:
        def read_committed(path):
            blob = run_git(['git', '-C', repo_path, 'show', local_tip + ':' + path, '--', path],
                           cwd=repo_path, runner=runner, text=False)
            return blob.stdout if blob.returncode == 0 else None

        for path in clean_paths:
            settings.check_path(path, lambda: read_committed(path), committed=True)

    # Fetch current target
    branch_exists, remote_tip = fetch_remote_branch(repo_path, remote, branch, runner=runner)

    if not branch_exists:
        # Branch does not exist on remote yet; initial push
        target_to_push = local_tip
        method = "fast_forward"
    else:
        # Remote branch exists
        if remote_tip == local_tip:
            return {
                "status": "up_to_date",
                "repo": repo_path,
                "remote": remote,
                "branch": branch,
                "local_tip": local_tip,
                "remote_tip": remote_tip,
                "published_tip": remote_tip,
                "method": "none",
                "paths": clean_paths,
                "recovery_attempts": 0,
                "message": "Remote branch is already up to date with local tip",
            }

        # Check if local_tip is already fast-forward of remote_tip
        if check_is_ancestor(repo_path, remote_tip, local_tip, runner=runner):
            target_to_push = local_tip
            method = "fast_forward"
        elif check_is_ancestor(repo_path, local_tip, remote_tip, runner=runner):
            # Local tip has already been integrated into remote
            return {
                "status": "up_to_date",
                "repo": repo_path,
                "remote": remote,
                "branch": branch,
                "local_tip": local_tip,
                "remote_tip": remote_tip,
                "published_tip": remote_tip,
                "method": "none",
                "paths": clean_paths,
                "recovery_attempts": 0,
                "message": "Local tip is already merged into remote branch",
            }
        else:
            # Diverged! Require merge-tree exit 0 and single object ID line
            merge_msg = message or f"Publish merge: {local_tip[:8]} with {remote_tip[:8]}"
            ok, merge_sha, err = create_merge_commit_tree(repo_path, local_tip, remote_tip, merge_msg, runner=runner)
            if not ok:
                return {
                    "status": "conflict",
                    "repo": repo_path,
                    "remote": remote,
                    "branch": branch,
                    "local_tip": local_tip,
                    "remote_tip": remote_tip,
                    "published_tip": None,
                    "method": "merge_tree",
                    "paths": clean_paths,
                    "recovery_attempts": 0,
                    "conflict_details": err,
                    "error": "Conflict detected during merge-tree: refusing to publish marker tree",
                }
            target_to_push = merge_sha
            method = "merge_tree"

    # Execute push
    success, is_nff, push_out, push_err = execute_push(repo_path, remote, target_to_push, branch, runner=runner)
    if success:
        return {
            "status": "published",
            "repo": repo_path,
            "remote": remote,
            "branch": branch,
            "local_tip": local_tip,
            "remote_tip": remote_tip,
            "published_tip": target_to_push,
            "method": method,
            "paths": clean_paths,
            "recovery_attempts": 0,
        }

    # Push failed. Check if deterministic NFF vs ambiguous send failure
    if is_nff:
        # At most ONE recovery attempt
        recovery_attempts = 1
        exists, new_remote_tip = fetch_remote_branch(repo_path, remote, branch, runner=runner)
        if not exists or not new_remote_tip or new_remote_tip == remote_tip:
            return {
                "status": "error",
                "repo": repo_path,
                "remote": remote,
                "branch": branch,
                "local_tip": local_tip,
                "remote_tip": remote_tip,
                "published_tip": None,
                "method": method,
                "paths": clean_paths,
                "recovery_attempts": recovery_attempts,
                "error": "Deterministic non-fast-forward rejection, but remote tip did not advance after re-fetch",
                "push_stderr": push_err.strip(),
            }

        # Check if local_tip is already merged into new remote tip
        if new_remote_tip == local_tip or check_is_ancestor(repo_path, local_tip, new_remote_tip, runner=runner):
            return {
                "status": "up_to_date",
                "repo": repo_path,
                "remote": remote,
                "branch": branch,
                "local_tip": local_tip,
                "remote_tip": new_remote_tip,
                "published_tip": new_remote_tip,
                "method": "none",
                "paths": clean_paths,
                "recovery_attempts": recovery_attempts,
                "message": "Local tip is already merged into remote branch",
            }

        if check_is_ancestor(repo_path, new_remote_tip, local_tip, runner=runner):
            new_target = local_tip
            new_method = "fast_forward"
        else:
            merge_msg = message or f"Publish merge: {local_tip[:8]} with {new_remote_tip[:8]}"
            ok, merge_sha, err = create_merge_commit_tree(repo_path, local_tip, new_remote_tip, merge_msg, runner=runner)
            if not ok:
                return {
                    "status": "conflict",
                    "repo": repo_path,
                    "remote": remote,
                    "branch": branch,
                    "local_tip": local_tip,
                    "remote_tip": new_remote_tip,
                    "published_tip": None,
                    "method": "merge_tree",
                    "paths": clean_paths,
                    "recovery_attempts": recovery_attempts,
                    "conflict_details": err,
                    "error": "Conflict detected during merge-tree on recovery: refusing to publish marker tree",
                }
            new_target = merge_sha
            new_method = "merge_tree"

        # Final push attempt (at most one recovery)
        success2, is_nff2, push_out2, push_err2 = execute_push(repo_path, remote, new_target, branch, runner=runner)
        if success2:
            return {
                "status": "published",
                "repo": repo_path,
                "remote": remote,
                "branch": branch,
                "local_tip": local_tip,
                "remote_tip": new_remote_tip,
                "published_tip": new_target,
                "method": new_method,
                "paths": clean_paths,
                "recovery_attempts": recovery_attempts,
            }

        if is_nff2:
            return {
                "status": "failed_nff_retry_exhausted",
                "repo": repo_path,
                "remote": remote,
                "branch": branch,
                "local_tip": local_tip,
                "remote_tip": new_remote_tip,
                "published_tip": None,
                "method": new_method,
                "paths": clean_paths,
                "recovery_attempts": recovery_attempts,
                "error": "Push rejected with non-fast-forward after maximum (1) recovery attempt",
                "push_stderr": push_err2.strip(),
            }

        # Ambiguous send failure after recovery
        reconciled_tip = reconcile_remote_tip(repo_path, remote, branch, runner=runner)
        if reconciled_tip == new_target:
            return {
                "status": "published",
                "repo": repo_path,
                "remote": remote,
                "branch": branch,
                "local_tip": local_tip,
                "remote_tip": new_remote_tip,
                "published_tip": new_target,
                "method": new_method,
                "paths": clean_paths,
                "recovery_attempts": recovery_attempts,
                "reconciled": True,
                "message": "Push completed remotely despite ambiguous transport error",
            }
        return {
            "status": "ambiguous_failure",
            "effectUnknown": True,
            "repo": repo_path,
            "remote": remote,
            "branch": branch,
            "local_tip": local_tip,
            "remote_tip": reconciled_tip,
            "target_tip": new_target,
            "published_tip": None,
            "method": new_method,
            "paths": clean_paths,
            "recovery_attempts": recovery_attempts,
            "reconciled": False,
            "error": f"Push failed with ambiguous send failure; remote ref reconciled to {reconciled_tip} != target {new_target}; blind resend refused",
            "push_stderr": push_err2.strip(),
        }

    # Ambiguous send failure on initial push: reconcile remote ref, never blind resend
    reconciled_tip = reconcile_remote_tip(repo_path, remote, branch, runner=runner)
    if reconciled_tip == target_to_push:
        return {
            "status": "published",
            "repo": repo_path,
            "remote": remote,
            "branch": branch,
            "local_tip": local_tip,
            "remote_tip": remote_tip,
            "published_tip": target_to_push,
            "method": method,
            "paths": clean_paths,
            "recovery_attempts": 0,
            "reconciled": True,
            "message": "Push completed remotely despite ambiguous transport error",
        }
    return {
        "status": "ambiguous_failure",
        "effectUnknown": True,
        "repo": repo_path,
        "remote": remote,
        "branch": branch,
        "local_tip": local_tip,
        "remote_tip": reconciled_tip,
        "target_tip": target_to_push,
        "published_tip": None,
        "method": method,
        "paths": clean_paths,
        "recovery_attempts": 0,
        "reconciled": False,
        "error": f"Push failed with ambiguous send failure; remote ref reconciled to {reconciled_tip} != target {target_to_push}; blind resend refused",
        "push_stderr": push_err.strip(),
    }


def reconcile_remote_tip(repo, remote, branch, runner=None):
    try:
        return get_remote_branch_tip(repo, remote, branch, runner=runner)
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return None  # Unavailable reconciliation is unknown, never permission to resend.


def publish_unit(repo, tip, remote, branch, paths=None, message=None, runner=None):
    try:
        return _publish_unit(repo, tip, remote, branch, paths, message, runner)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        return {'status': 'error', 'local_tip': tip, 'remote_tip': None,
                'published_tip': None, 'paths': paths or [], 'error': str(error)}
