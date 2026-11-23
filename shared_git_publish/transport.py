"""Bounded Git push and read-only reconciliation of ambiguous sends."""
import os
import re
import subprocess
import time

from .profile import profile

TIMEOUT = 120
SLOW_AFTER = 20
SETTLE_SECONDS = 60
SETTLE_POLL_SECONDS = 1.0


def _git(repo, *args, timeout=30):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, timeout=timeout)


def remote_tip(repo, remote, branch):
    result = _git(repo, 'ls-remote', '--exit-code', remote, 'refs/heads/' + branch)
    if result.returncode == 2:
        return None
    if result.returncode:
        raise RuntimeError('remote ref reconciliation unavailable; do not resend')
    return result.stdout.split()[0].decode()


def landed(repo, remote, branch, commit, observed):
    if not observed:
        return False
    if observed == commit:
        return True
    if _git(repo, 'cat-file', '-e', observed + '^{commit}').returncode:
        if _git(repo, 'fetch', remote, 'refs/heads/' + branch).returncode:
            return False
    return _git(repo, 'merge-base', '--is-ancestor', commit, observed).returncode == 0


def deterministic_race(stdout, stderr):
    if any(re.fullmatch(r'!\t[^\t]+\t\[rejected\] \((?:fetch first|non-fast-forward)\)', line.strip())
           for line in stdout.splitlines()):
        return True
    # Another publisher held the remote ref lock: receive-pack refused the update outright
    # ("cannot lock ref ... File exists"), so nothing landed and a bounded rebuild is safe.
    if re.search(r"cannot lock ref 'refs/heads/[^']+'", stderr) and 'File exists' in stderr:
        return True
    # The exact receive-pack old-value refusal is deterministic even when the porcelain
    # stdout line was lost or never emitted; no update was applied, so a rebuild is safe.
    if re.search(r'is at [0-9a-f]{40,64} but expected [0-9a-f]{40,64}', stderr):
        return True
    # receive-pack rejected the single ref transaction after another writer advanced it.
    return (any(re.fullmatch(r'!\t[^\t]+\t\[remote rejected\] \(failed to update ref\)', line.strip())
                for line in stdout.splitlines())
            and re.search(r'is at [0-9a-f]{40,64} but expected [0-9a-f]{40,64}', stderr) is not None)


def push(repo, remote, commit, branch, *, timeout=TIMEOUT, slow_after=SLOW_AFTER,
         settle_seconds=SETTLE_SECONDS):
    args = ['git', '-C', str(repo), '-c', 'maintenance.auto=false', 'push', '--porcelain']
    settings = profile.get()
    if settings.receive_pack:
        receive_pack = settings.receive_pack(repo, remote)
        if receive_pack:
            args.append('--receive-pack=' + receive_pack)
    args += [remote, commit + ':refs/heads/' + branch]
    started = time.monotonic()
    diagnostic = None
    # The push child inherited the full environment under Popen; bounded_run's
    # default is a small curated set, which would drop SSH_AUTH_SOCK and friends.
    import bounded_run

    handle = bounded_run.start(args, timeout=timeout, idle=0, max_output=64 * 1024 * 1024,
                               env=dict(os.environ))
    try:
        try:
            # The slow boundary is a caller-side sample, not a kill: the run
            # keeps going while the diagnostic is collected.
            handle.wait(slow_after)
        except TimeoutError:
            if settings.slow_snapshot:
                try:
                    diagnostic = settings.slow_snapshot(repo, remote, commit, time.monotonic() - started)
                except Exception:
                    pass  # A diagnostic failure cannot change the disposition of a push.
        run_result = handle.wait()
    except BaseException:
        # Never orphan a push in flight when the publisher is interrupted.
        handle.cancel()
        try:
            handle.wait(5)
        except Exception:
            pass
        raise
    expired = run_result.classification == "timeout"
    truncated = run_result.stdout_truncated or run_result.stderr_truncated
    out, err = run_result.stdout, run_result.stderr
    result = subprocess.CompletedProcess(args, run_result.returncode or (124 if expired else 0), out, err)
    result.reconciled = False
    result.diagnostic = diagnostic
    if result.returncode == 0:
        return result
    text_out, text_err = out.decode(errors='replace'), err.decode(errors='replace')
    if deterministic_race(text_out, text_err):
        return result
    elapsed = time.monotonic() - started
    # A killed/time-limited process or an answer with no Git status is not proof of failure.
    # Read the exact remote ref for a bounded settle window; never resend during that read.
    uncertain = (expired or truncated or elapsed >= slow_after
                 or not (text_out.strip() or text_err.strip()))
    observed = None
    settle_error = None
    if uncertain:
        settled, observed, settle_error = settle_remote(repo, remote, branch, commit, settle_seconds)
        if settled:
            result.returncode = 0
            result.reconciled = True
            return result
    else:
        try:
            observed = remote_tip(repo, remote, branch)
            if landed(repo, remote, branch, commit, observed):
                result.returncode = 0
                result.reconciled = True
                return result
        except RuntimeError as exc:
            settle_error = str(exc)
    # Preserve a hook or transport's own diagnostic alongside the settlement result.
    git_said = ' git_stderr=' + text_err.strip()[-800:] if text_err.strip() else ''
    settled = ' settle_error=' + settle_error if settle_error else ''
    raise RuntimeError('push outcome unconfirmed; do not resend; target=' + commit +
                       ' remote=' + str(observed) + (' diagnostic=' + diagnostic if diagnostic else '') + git_said + settled)


def settle_remote(repo, remote, branch, commit, seconds=SETTLE_SECONDS):
    """Read the remote ref for a bounded window; only commit/descendant is success."""
    deadline = time.monotonic() + max(0.0, seconds)
    observed = None
    last_error = None
    while True:
        try:
            observed = remote_tip(repo, remote, branch)
            if landed(repo, remote, branch, commit, observed):
                return True, observed, None
        except Exception as exc:  # network/fetch failure is retried only inside this window
            last_error = str(exc)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, observed, last_error
        time.sleep(min(SETTLE_POLL_SECONDS, remaining))
