"""Publish owned working files without the shared index. Unstaged foreign edits cannot be distinguished."""
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time

from .adopt import (
    _object_preparation_deadline, adopt_checkout, fetch_missing_objects,
    object_preparation_budget, publication_lock,
)
from .profile import commit_message, profile
from .transport import push as push_once, deterministic_race

def git(repo, *args, data=None, env=None, check=True):
    env = dict(os.environ if env is None else env, GIT_NO_LAZY_FETCH='1', GIT_LITERAL_PATHSPECS='1')
    deadline = _object_preparation_deadline.get()
    timeout = 120 if deadline is None else min(120, deadline - time.monotonic())
    if timeout <= 0:
        raise RuntimeError('object preparation exceeded 300-second total budget')
    try:
        p = subprocess.run(['git', '-C', str(repo), *args], input=data, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError('timed out waiting for git ' + ' '.join(args[:2]) +
                           ('; push outcome unknown, do not resend' if args[0] == 'push' else ''))
    if check and p.returncode:
        raise RuntimeError(p.stderr.decode(errors='replace').strip() or 'git ' + ' '.join(args) + ' failed')
    return p


def merge(repo, td, ours, base, theirs, path):
    paths = [Path(td) / name for name in ('ours', 'base', 'theirs')]
    for p, content in zip(paths, (ours, base, theirs)):
        p.write_bytes(content)
    result = git(repo, 'merge-file', '-p', *(str(p) for p in paths), check=False)
    if result.returncode:
        raise RuntimeError('merge conflict; use existing conflict route: ' + path)
    return result.stdout


def parser(ap):
    ap.add_argument('--repo', required=True)
    ap.add_argument('--path', action='append', default=[], help='literal repository-relative working file')
    ap.add_argument('--delete', action='append', default=[], help='literal repository-relative path to delete from remote')
    ap.add_argument('--message', '-m', required=True)
    ap.add_argument('--base', help='commit the working edits started from (defaults to checkout HEAD)')
    ap.add_argument('--source-root', help='reviewed file tree containing only owned bytes, at repository-relative paths')
    ap.add_argument('--allow-empty', action='store_true',
                    help='allow --path to replace a nonempty base file with empty content')
    ap.add_argument('--no-adopt', action='store_true',
                    help='Publish only; do not fast-forward this checkout afterwards')
    ap.add_argument('--remote', default='origin')
    ap.add_argument('--branch', default='main')
    return ap


def publish(a):
    if not a.path and not a.delete:
        raise RuntimeError('at least one --path or --delete is required')
    if a.remote.startswith('-'):
        raise RuntimeError('remote must be a name or URL, not an option')
    settings = profile.get()
    repo = Path(git(a.repo, 'rev-parse', '--show-toplevel').stdout.decode().strip())
    source_root = Path(a.source_root).resolve() if a.source_root else repo
    common = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.decode().strip())
    head_commit = git(repo, 'rev-parse', 'HEAD').stdout.decode().strip()
    base = git(repo, 'rev-parse', '--verify', (a.base or head_commit) + '^{commit}').stdout.decode().strip()
    if settings.check_repo:
        settings.check_repo(str(repo))
    files = {}
    for path in a.path + a.delete:
        if not path or path.startswith(('/', '-', ':')) or '..' in Path(path).parts or path in files:
            raise RuntimeError('paths must be unique literal repository-relative files')
        f = repo / path
        if not f.resolve().is_relative_to(repo) or f.is_symlink():
            raise RuntimeError('path must be inside repository and not a symlink: ' + path)
        parent = f.parent
        while not parent.exists():
            parent = parent.parent
        if Path(git(parent, 'rev-parse', '--show-toplevel').stdout.decode().strip()) != repo or git(repo, 'ls-files', '-u', '--', path).stdout:
            raise RuntimeError('nested repository or unresolved index conflict: ' + path)
        content = None
        if path not in a.delete:
            source_file = source_root / path
            if not source_file.resolve().is_relative_to(source_root) or source_file.is_symlink():
                raise RuntimeError('owned source must be inside source-root and not a symlink: ' + path)
            if not source_file.is_file():
                raise RuntimeError('working file is missing or not regular: ' + path)
            content = source_file.read_bytes()
        if settings.check_path:
            settings.check_path(path, lambda: content, committed=False)
        files[path] = None if content is None else (
            content, '100755' if source_file.stat().st_mode & stat.S_IXUSR else '100644')
    git(repo, 'check-ref-format', 'refs/heads/' + a.branch)
    with publication_lock(repo) as lock:
        preparation_deadline = time.monotonic() + 300
        with tempfile.TemporaryDirectory(prefix=settings.scratch_prefix + 'publish-', dir=common) as td:
            excluded = []
            for path, item in ([] if a.source_root else list(files.items())):
                head = git(repo, 'ls-tree', head_commit, '--', path).stdout.split()
                index = git(repo, 'ls-files', '-s', '--', path).stdout.split()
                if (head and not index) or (index and not head):
                    raise RuntimeError('staged addition/deletion cannot be separated safely: ' + path)
                if head and index and (index[0] != head[0] or index[1] != head[2]):
                    if item is None or index[0] != head[0]:
                        raise RuntimeError('staged change overlaps deletion or mode change: ' + path)
                    staged = git(repo, 'cat-file', 'blob', index[1].decode()).stdout
                    head_bytes = git(repo, 'cat-file', 'blob', head[2].decode()).stdout
                    files[path] = (merge(repo, td, item[0], staged, head_bytes, path), item[1])
                    excluded.append(path)
            env = os.environ.copy()
            env['GIT_INDEX_FILE'] = str(Path(td) / 'index')
            for attempt in range(4):
                # Resolve before fetching; a later advance produces a deterministic NFF retry.
                tip = git(repo, 'ls-remote', '--exit-code', a.remote, 'refs/heads/' + a.branch).stdout.split()[0].decode()
                git(repo, 'fetch', a.remote, 'refs/heads/' + a.branch)
                git(repo, 'cat-file', '-e', tip + '^{commit}')
                if git(repo, 'merge-base', '--is-ancestor', base, tip, check=False).returncode:
                    raise RuntimeError('working base is not an ancestor of the remote tip')
                git(repo, 'read-tree', tip, env=env)
                entries = {}
                with object_preparation_budget(preparation_deadline):
                    required = set()
                    for path in files:
                        old_entry = git(repo, 'ls-tree', base, '--', path).stdout.split()
                        upstream_entry = git(repo, 'ls-tree', tip, '--', path).stdout.split()
                        entries[path] = old_entry, upstream_entry
                        required.update(entry[2].decode() for entry in (old_entry, upstream_entry)
                                        if entry and entry[0] in (b'100644', b'100755', b'120000'))
                    fetch_missing_objects(repo, required, remote=a.remote)
                for path, item in files.items():
                    old_entry, upstream_entry = entries[path]
                    if old_entry and upstream_entry and old_entry[0] != upstream_entry[0]:
                        raise RuntimeError('upstream changed file mode; resolve conflict: ' + path)
                    if item is not None and not item[0] and not a.allow_empty and old_entry:
                        size = git(repo, 'cat-file', '-s', f'{base}:{path}')
                        if int(size.stdout) > 0:
                            raise RuntimeError('refusing empty --path with nonempty base: ' + path +
                                               '; use --allow-empty for intentional empty content, --delete for deletion')
                    old = git(repo, 'show', f'{base}:{path}', '--', path, check=False)
                    upstream = git(repo, 'show', f'{tip}:{path}', '--', path, check=False)
                    if old.returncode and upstream.returncode == 0:
                        raise RuntimeError('upstream added path; resolve conflict: ' + path)
                    if old.returncode == 0 and upstream.returncode:
                        raise RuntimeError('upstream deleted path; resolve conflict: ' + path)
                    if item is None:
                        if old.returncode or upstream.returncode or old.stdout != upstream.stdout:
                            raise RuntimeError('deletion requires the unchanged last version on remote: ' + path)
                        git(repo, 'update-index', '--force-remove', '--', path, env=env)
                        continue
                    content, mode = item
                    if old.returncode == 0 and old.stdout != upstream.stdout:
                        content = merge(repo, td, content, old.stdout, upstream.stdout, path)
                    blob = git(repo, 'hash-object', '-w', '--stdin', data=content).stdout.decode().strip()
                    git(repo, 'update-index', '--add', '--cacheinfo', f'{mode},{blob},{path}', env=env)
                # Every entry outside `files` comes unchanged from the remote tip, which holds its blobs;
                # a blobless clone need not, and write-tree refused the tip's unfetched blobs.
                tree = git(repo, 'write-tree', '--missing-ok', env=env).stdout.decode().strip()
                if tree == git(repo, 'rev-parse', tip + '^{tree}').stdout.decode().strip():
                    # Nothing differs from the remote tip: report it instead of pushing an empty commit.
                    return {'status': 'unchanged', 'base': tip, 'paths': list(files)}
                commit = git(repo, 'commit-tree', tree, '-p', tip, '-m', commit_message(a.message)).stdout.decode().strip()
                push = push_once(repo, a.remote, commit, a.branch)
                if push.returncode == 0:
                    break
                nff = deterministic_race(push.stdout.decode(errors='replace'), push.stderr.decode(errors='replace'))
                if not nff:
                    raise RuntimeError('push outcome uncertain; do not resend: ' + push.stderr.decode(errors='replace'))
                if attempt == 3:
                    raise RuntimeError('push rejected non-fast-forward after 3 rebuilds')
        adoption = None if a.no_adopt else adopt_checkout(repo, a.branch, commit, remote=a.remote,
                                                publish_local=settings.publish_local_on_adopt, _publish_lock=lock)
        result = {'status': 'published', 'commit': commit, 'base': tip,
                  'push_reconciled': push.reconciled, 'slow_push_diagnostic': push.diagnostic,
                  'staged_deltas_excluded': excluded, 'nff_rebuilds': attempt}
        if not a.no_adopt:
            result['adoption'] = adoption
        if settings.remote_checkout:
            result['remote_checkout'] = settings.remote_checkout(repo, a.remote, commit)
        drift = settings.flow_maps(repo, list(files)) if settings.flow_maps else None
        if drift is not None:
            result['flowMaps'] = drift
        return result
