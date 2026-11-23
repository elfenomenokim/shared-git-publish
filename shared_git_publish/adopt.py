"""Fast-forward a shared checkout while retaining local work."""
import os
import fcntl
import re
import signal
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from .profile import profile


_object_preparation_deadline = ContextVar('object_preparation_deadline', default=None)


@contextmanager
def object_preparation_budget(deadline=None):
    deadline = time.monotonic() + 300 if deadline is None else deadline
    token = _object_preparation_deadline.set(deadline)
    try:
        yield deadline
    finally:
        _object_preparation_deadline.reset(token)


def git(repo, *args, env=None, data=None, check=True):
    env = dict(os.environ if env is None else env, GIT_NO_LAZY_FETCH='1')
    deadline = _object_preparation_deadline.get()
    timeout = 120 if deadline is None else min(120, deadline - time.monotonic())
    if timeout <= 0:
        raise RuntimeError('object preparation exceeded 300-second total budget')
    result = subprocess.run(['git', '-C', str(repo), *args], env=env, input=data,
                            capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace').strip())
    return result


def acquire_index_lock(index):
    """Only reclaim an old, unchanged, unheld lock; fresh locks get a brief retry."""
    lock = Path(str(index) + '.lock')
    for attempt in range(3):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
            return fd, lock
        except FileExistsError:
            try:
                before = lock.stat()
                if time.time() - before.st_mtime > 30:
                    time.sleep(0.1)
                    after = lock.stat()
                    signature = lambda s: (s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
                    lsof = shutil.which('lsof') or '/usr/sbin/lsof'
                    held = subprocess.run([lsof, '-t', '--', str(lock)], capture_output=True, timeout=10)
                    if (signature(before) == signature(after) and held.returncode == 1
                            and not held.stdout and not held.stderr
                            and signature(after) == signature(lock.stat())):
                        lock.unlink()
                        print('cleared stale index.lock: ' + str(lock), file=sys.stderr)
                        continue
            except FileNotFoundError:
                continue
            if attempt < 2:
                time.sleep(0.1)
    raise RuntimeError('index.lock is fresh, changing, held, or its holder could not be checked')


@contextmanager
def held_index_lock(index):
    """Clean up on termination, and retain a visible descriptor until replacement."""
    signals = (signal.SIGTERM, signal.SIGHUP)
    previous = {sig: signal.getsignal(sig) for sig in signals}
    def terminate(signum, _frame):
        raise SystemExit(128 + signum)
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
    fd = None
    lock = None
    held = True
    handlers_active = True
    try:
        for sig in signals:
            signal.signal(sig, terminate)
        fd, lock = acquire_index_lock(index)
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        def replace():
            nonlocal fd, held, handlers_active
            mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
            try:
                os.close(fd)
                fd = None
                os.replace(lock, index)
                held = False
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
                handlers_active = False
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        yield lock, replace
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, signals)
        if fd is not None:
            os.close(fd)
        if lock is not None and held:
            lock.unlink(missing_ok=True)
        if handlers_active:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)


def work_entry(repo, path, shadowed=(), allow_directory=False):
    # Never inspect through a verified incoming symlink as if its target were
    # a deleted child of the old directory.
    if any(path.startswith(parent + '/') for parent in shadowed):
        return None
    parent = repo
    for component in Path(path).parts[:-1]:
        parent = parent / component
        if parent.is_symlink():
            raise BlockedPath('working parent is an unexpected symlink', path)
    file = repo / path
    try:
        mode = file.lstat().st_mode
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(mode):
        blob = git(repo, 'hash-object', '-w', '--stdin', data=os.fsencode(os.readlink(file))).stdout.strip()
        return b'120000', blob
    if stat.S_ISDIR(mode) and allow_directory:
        return b'040000', b''
    if not stat.S_ISREG(mode):
        raise BlockedPath('directory or unsupported working type', path)
    # -w: this blob can enter the rehearsal index; with core.autocrlf=input the filtered
    # hash of a CRLF file is a new object, and an unwritten one made write-tree fail
    # ('invalid object') on 2026-11-17 for Windows benchmark outputs.
    blob = git(repo, 'hash-object', '-w', '--path=' + path, '--stdin', data=file.read_bytes()).stdout.strip()
    executable = git(repo, 'config', '--bool', 'core.filemode', check=False).stdout.strip() != b'false'
    return (b'100755' if executable and mode & stat.S_IXUSR else b'100644'), blob


def committed_ancestor_entry(repo, target, path, entry):
    """Whether exact mode and bytes occur in the incoming path history."""
    if not entry or entry[0] not in (b'100644', b'100755', b'120000'):
        return False
    # Runtime data may be rewritten by a live job to bytes that coincidentally
    # match history. Preserve those bytes rather than treating them as lag.
    data_suffixes = {'.json', '.jsonl', '.sqlite', '.sqlite3', '.db', '.csv', '.ndjson', '.parquet', '.pkl'}
    if (Path(path).name.rstrip().lower().endswith(('-wal', '-shm', '-journal'))
            or any(re.sub(r'-(?:wal|shm|journal)$', '', suffix.strip().lower()) in data_suffixes
                   for suffix in Path(path).suffixes)):
        return False
    history = git(repo, 'log', '--format=', '--raw', '--no-abbrev', '--no-renames',
                  target, '--', ':(literal)' + path).stdout
    for line in history.splitlines():
        meta = line.split(b'\t', 1)[0]
        if not meta.startswith(b':'):
            continue
        fields = meta[1:].split()
        if len(fields) >= 5 and (entry == (fields[0], fields[2]) or entry == (fields[1], fields[3])):
            return True
    return False


class BlockedPath(RuntimeError):
    def __init__(self, reason, path):
        super().__init__(reason + ': ' + path)
        self.path = path


def merge_published_work(repo, path, staged, target, working, scratch):
    """Merge the user's raw text delta against its already published index base."""
    ours, base, theirs = (git(repo, 'cat-file', 'blob', oid).stdout
                          for oid in (working['oid'], staged[1], target[1]))
    if any(b'\0' in content for content in (ours, base, theirs)):
        raise BlockedPath('published-base merge requires text', path)
    files = [Path(scratch) / name for name in ('ours', 'base', 'theirs')]
    for file, content in zip(files, (ours, base, theirs)):
        file.write_bytes(content)
    merged = git(repo, 'merge-file', '-p', *(str(file) for file in files), check=False)
    if merged.returncode:
        raise BlockedPath('published-base merge conflict', path)
    oid = git(repo, 'hash-object', '-w', '--stdin', data=merged.stdout).stdout.decode().strip()
    return working, dict(working, oid=oid)


def pending_prefix():
    return profile.get().retained_prefix + 'adoption/'


BACKUP_PAIR_LIMIT = 8192


@contextmanager
def publication_lock(repo, held=None):
    """A caller holding the publisher lock passes its open descriptor, never re-locks."""
    common = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.decode().strip())
    path = common / profile.get().lock_name
    if held is not None:
        descriptor, expected = os.fstat(held.fileno()), path.stat()
        if (descriptor.st_dev, descriptor.st_ino) != (expected.st_dev, expected.st_ino):
            raise RuntimeError('publisher lock descriptor belongs to a different repository')
        yield held
    else:
        with path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield lock


def index_entries(repo, env=None, paths=None):
    selected = None if paths is None else set(paths)
    batches = [()]
    if selected is not None:
        # ls-files accepts path arguments only. Leave room for the command and
        # environment on checkouts with large incoming changes or long paths.
        batches = []
        batch, size = [], 0
        for path in sorted(selected):
            cost = len(os.fsencode(path)) + 16
            if batch and size + cost > 96 * 1024:
                batches.append(batch)
                batch, size = [], 0
            batch.append(path)
            size += cost
        if batch:
            batches.append(batch)
    entries = {}
    for batch in batches:
        rows = git(repo, '--literal-pathspecs', 'ls-files', '-s', '-z', '--', *batch, env=env).stdout
        for item in rows.split(b'\0'):
            if not item:
                continue
            meta, raw_path = item.split(b'\t', 1)
            path = os.fsdecode(raw_path)
            # Git's literal pathspec also matches children of a named directory.
            if selected is not None and path not in selected:
                continue
            mode, oid, stage = meta.decode().split()
            if stage != '0':
                raise RuntimeError('unresolved index stages')
            entries[path] = [mode, oid]
    return entries


def require_resolved_index(repo, env=None):
    if git(repo, 'ls-files', '-u', '-z', env=env).stdout:
        raise RuntimeError('unresolved index stages')


def refresh_index_paths(repo, paths, env, *, check=True):
    git(repo, '--literal-pathspecs', 'add', '--refresh', '--pathspec-from-file=-',
        '--pathspec-file-nul', env=env, check=check,
        data=b''.join(os.fsencode(path) + b'\0' for path in paths))


def raw_work(repo, path, save=False):
    """Raw-byte fingerprint; never follow a symlink in a parent component."""
    parent = Path(repo)
    for component in Path(path).parts[:-1]:
        parent /= component
        if parent.is_symlink():
            return {'kind': 'shadowed'}
    file = Path(repo) / path
    try:
        mode = file.lstat().st_mode
    except FileNotFoundError:
        return {'kind': 'absent'}
    if stat.S_ISDIR(mode):
        return {'kind': 'directory'}
    args = ['hash-object', '--no-filters'] + (['-w'] if save else [])
    if stat.S_ISLNK(mode):
        kind = 'symlink'
        oid = git(repo, *args, '--stdin', data=os.fsencode(os.readlink(file))).stdout.decode().strip()
    elif stat.S_ISREG(mode):
        kind = 'file'
        oid = git(repo, *args, '--', path).stdout.decode().strip()  # Stream in Git, not Python.
    else:
        raise BlockedPath('unsupported working type', path)
    return {'kind': kind, 'mode': '100755' if mode & stat.S_IXUSR else '100644', 'oid': oid}


def target_fingerprint(repo, target, path, entry, directory=False):
    """Hash the expected checkout stream; never write/store target payload again."""
    if directory or entry is None:
        return {'kind': 'directory' if directory else 'absent'}
    mode, oid = (v.decode() for v in entry)
    if mode == '120000':
        return {'kind': 'symlink', 'mode': '100755', 'oid': oid}
    env = dict(os.environ, GIT_NO_LAZY_FETCH='1')
    with tempfile.TemporaryFile() as errors:
        source = subprocess.Popen(['git', '-C', str(repo), 'cat-file', '--filters',
            '--path=' + path, target + ':' + path], stdout=subprocess.PIPE, stderr=errors, env=env)
        try:
            result = subprocess.run(['git', '-C', str(repo), 'hash-object', '--stdin'],
                stdin=source.stdout, capture_output=True, timeout=120, env=env)
            source.stdout.close()
            if source.wait(timeout=120) or result.returncode:
                raise RuntimeError('cannot fingerprint target checkout: ' + path)
            return {'kind': 'file', 'mode': mode, 'oid': result.stdout.decode().strip()}
        finally:
            source.stdout.close()
            if source.poll() is None:
                source.kill()
                source.wait()


def begin_transaction(repo, branch, head, target, index, original, prepared, changes, directories,
                      published_merges=None):
    """Pin the complete recovery payload before replacing the real index."""
    repo, index = Path(repo).resolve(), Path(index).resolve()
    enforce_backup_cap(repo, target)
    transaction = uuid.uuid4().hex
    record = {'schema': 1, 'id': transaction, 'created_at': int(time.time()),
              'head': head, 'target': target, 'branch': branch, 'index_path': str(index),
              'clear_owner': profile.get().clear_owner, 'paths': {}}
    paths = [path for path, _, _ in changes]
    original_entries = index_entries(repo, paths=paths)
    with tempfile.TemporaryDirectory(prefix=profile.get().scratch_prefix + 'prepared-', dir=index.parent) as td:
        prepared_file = Path(td) / 'index'
        prepared_file.write_bytes(prepared)
        prepared_entries = index_entries(repo, dict(os.environ, GIT_INDEX_FILE=str(prepared_file)), paths)
    payload = {}  # OIDs only; the original index tree already preserves staged entries.
    published_merges = published_merges or {}
    for n, (path, before, after) in enumerate(changes):
        previous = raw_work(repo, path)
        if 'oid' in previous and (before is None or previous['oid'] != before[1].decode()):
            previous = raw_work(repo, path, save=True)
            payload[f'before-{n}'] = previous['oid']
        if path in published_merges:
            expected, future = published_merges[path]
            if previous != expected:
                raise BlockedPath('working bytes changed during published-base merge rehearsal', path)
        else:
            future = target_fingerprint(repo, target, path, after, path in directories)
        record['paths'][path] = {'index': original_entries.get(path),
                                 'prepared_index': prepared_entries.get(path),
                                 'target_index': [x.decode() for x in after] if after else None,
                                 'before': previous, 'after': future,
                                 'permissions': stat.S_IMODE((repo / path).lstat().st_mode)
                                 if previous['kind'] == 'file' else None}
    payload['manifest.json'] = git(repo, 'hash-object', '-w', '--stdin',
        data=json.dumps(record, ensure_ascii=True, sort_keys=True).encode()).stdout.decode().strip()
    entries = [b'100644 blob ' + oid.encode() + b'\t' + name.encode() + b'\0'
               for name, oid in payload.items()]
    tree = git(repo, 'mktree', '-z', data=b''.join(entries)).stdout.decode().strip()
    storage = git(repo, 'commit-tree', tree, '-p', head, '-p', target,
                  '-m', 'Pending checkout adoption ' + transaction).stdout.decode().strip()
    with tempfile.TemporaryDirectory(prefix=profile.get().scratch_prefix + 'backup-', dir=index.parent) as td:
        copy = Path(td) / 'index'
        copy.write_bytes(original)
        # --missing-ok: a blobless sparse checkout never fetched the blobs of
        # skip-worktree paths, and lazy fetching is off here; their OIDs are still exact.
        saved_tree = git(repo, 'write-tree', '--missing-ok',
                         env=dict(os.environ, GIT_INDEX_FILE=str(copy))).stdout.decode().strip()
    message = ('Preserve index before publisher fast-forward\n\n'
               f'{profile.get().creation_trailer}: {record["created_at"]}\n{profile.get().transaction_trailer}: {transaction}')
    backup = git(repo, 'commit-tree', saved_tree, '-p', head, '-p', storage,
                 '-m', message).stdout.decode().strip()
    record['ref'] = pending_prefix() + transaction
    record['oid'] = storage
    zero = '0' * len(head)
    record['index_ref'] = profile.get().backup_prefix + 'index-' + transaction
    record['head_ref'] = profile.get().backup_prefix + 'head-' + transaction
    git(repo, 'update-ref', record['index_ref'], backup, zero)
    git(repo, 'update-ref', record['head_ref'], head, zero)
    git(repo, 'update-ref', record['ref'], storage, zero)
    return record


def finish_transaction(repo, record):
    git(repo, 'update-ref', '-d', record['ref'], record['oid'])


def restore_raw(repo, path, desired, permissions=None, *, expected=None):
    file = Path(repo) / path
    current = raw_work(repo, path)
    if expected is not None and current != expected:
        raise BlockedPath('working bytes changed before published-base merge', path)
    if current == desired:
        return
    if current['kind'] == 'shadowed':
        if desired['kind'] == 'shadowed':
            return
        raise RuntimeError('working parent is still a symlink: ' + path)
    # Read retained bytes before touching the destination. Regular replacements
    # stay atomic so a killed recovery never first unlinks the original file.
    data = git(repo, 'cat-file', 'blob', desired['oid']).stdout if 'oid' in desired else None
    if desired['kind'] in ('absent', 'shadowed'):
        if current['kind'] == 'directory':
            file.rmdir()  # Never remove unknown children.
        elif current['kind'] != 'absent':
            file.unlink()
        return
    if desired['kind'] == 'directory':
        if current['kind'] != 'absent':
            file.unlink()
        file.mkdir(parents=True, exist_ok=True)
        return
    file.parent.mkdir(parents=True, exist_ok=True)
    if desired['kind'] == 'file':
        mode = permissions if permissions is not None else (0o755 if desired['mode'] == '100755' else 0o644)
        from atomic_write import atomic_path

        with atomic_path(
            file,
            binary=True,
            mode=mode,
            durability='file',
            follow_symlinks=False,
        ) as staged:
            staged.write(data)
            staged.sync()
            if raw_work(repo, path) != current:
                raise BlockedPath('working path changed during replacement', path)
            if current['kind'] == 'directory':
                file.rmdir()
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=file.parent, prefix='.' + profile.get().scratch_prefix + 'recover-', delete=False) as out:
            temporary = Path(out.name)
        if desired['kind'] == 'symlink':
            temporary.unlink()
            temporary.symlink_to(os.fsdecode(data))
        if raw_work(repo, path) != current:
            raise RuntimeError('working path changed during recovery: ' + path)
        if current['kind'] == 'directory':
            file.rmdir()
        os.replace(temporary, file)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def quarantine_pending(repo, ref, oid):
    """CAS-move one leaked/bad marker; retain its complete payload indefinitely."""
    if not ref.startswith(pending_prefix()):
        raise RuntimeError('not an adoption pending ref')
    destination = profile.get().retained_prefix + 'quarantined-adoption/' + ref[len(pending_prefix()):]
    existing = git(repo, 'rev-parse', '--verify', destination, check=False)
    if existing.returncode == 0 and existing.stdout.decode().strip() != oid:
        raise RuntimeError('different quarantine payload already exists: ' + destination)
    operation = ('update ' + destination + ' ' + oid + ' ' + oid if existing.returncode == 0
                 else 'create ' + destination + ' ' + oid)
    commands = f'start\n{operation}\ndelete {ref} {oid}\nprepare\ncommit\n'
    git(repo, 'update-ref', '--stdin', data=commands.encode())
    return destination


def recover_pending(repo, branch, events=None, *, pending_ref=None):
    """Restore only recorded writes. Unresolved markers constrain their path sets."""
    if git(repo, 'symbolic-ref', '--short', '-q', 'HEAD', check=False).stdout.decode().strip() != branch:
        return []
    listed = git(repo, 'for-each-ref', '--format=%(refname) %(objectname)', pending_ref or pending_prefix()).stdout
    index = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'index').stdout.decode().strip())
    blocked = []
    events = events if events is not None else []
    for line in listed.decode().splitlines():
        ref, oid = line.split()
        try:
            record = json.loads(git(repo, 'show', oid + ':manifest.json').stdout)
            for key in ('id', 'head', 'target', 'index_path', 'branch', 'clear_owner', 'paths'):
                record[key]
            paths = record['paths']
            if not isinstance(paths, dict) or not paths or any(
                    not p or Path(p).is_absolute() or '..' in Path(p).parts for p in paths):
                raise ValueError('invalid pending path set')
            for row in paths.values():
                for key in ('index', 'prepared_index', 'target_index', 'before', 'after'):
                    row[key]
            same_index = Path(record['index_path']).resolve() == index.resolve()
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as error:
            destination = quarantine_pending(repo, ref, oid)
            events.append({'marker': ref, 'status': 'quarantined', 'retained_ref': destination,
                           'reason': str(error), 'clear_owner': profile.get().clear_owner})
            continue
        if not same_index or record['branch'] != branch:
            continue  # A different registered worktree is not this checkout.
        record.update(ref=ref, oid=oid)
        head = git(repo, 'rev-parse', 'HEAD').stdout.decode().strip()
        require_resolved_index(repo)
        entries = index_entries(repo, paths=paths)
        actual = {p: raw_work(repo, p) for p in paths}
        original = all(entries.get(p) == row['index'] and actual[p] == row['before']
                       for p, row in paths.items())
        completed = (head == record['target'] and all(
            entries.get(p) == row['target_index'] and actual[p] in (row['before'], row['after'])
            for p, row in paths.items()))
        stale = (head == record['head'] and all(entries.get(p) == row['index']
                 and (actual[p] != row['after'] or row['before'] == row['after'])
                 for p, row in paths.items()))
        if (head == record['head'] and original) or completed or stale:
            finish_transaction(repo, record)
            events.append({'id': record['id'], 'status': 'completed' if completed else 'cleared_unapplied'})
            continue
        safe = head == record['head'] and all(
            entries.get(p) in (row['index'], row['prepared_index'], row['target_index'])
            and actual[p] in (row['before'], row['after']) for p, row in paths.items())
        if safe:
            try:
                with held_index_lock(index) as (lock, replace):
                    previous = index.read_bytes()
                    require_resolved_index(repo)
                    locked_entries = index_entries(repo, paths=paths)
                    if (git(repo, 'rev-parse', 'HEAD').stdout.decode().strip() != head
                            or any(locked_entries.get(p) not in (row['index'], row['prepared_index'], row['target_index'])
                                   for p, row in paths.items())):
                        raise RuntimeError('HEAD/index changed before recovery')
                    with tempfile.TemporaryDirectory(prefix=profile.get().scratch_prefix + 'recover-', dir=index.parent) as td:
                        copy = Path(td) / 'index'
                        copy.write_bytes(previous)
                        env = dict(os.environ, GIT_INDEX_FILE=str(copy))
                        updates = []
                        for path, row in paths.items():
                            entry = row['index'] or ['0', '0' * len(head)]
                            updates.append((' '.join(entry).encode() + b'\t' + os.fsencode(path) + b'\0'))
                        git(repo, 'update-index', '--replace', '-z', '--index-info', env=env,
                            data=b''.join(updates))
                        # Remove known transition children before restoring a link parent.
                        for path in sorted(paths, key=lambda p: p.count('/'), reverse=True):
                            if paths[path]['before']['kind'] in ('absent', 'shadowed'):
                                if raw_work(repo, path) not in (paths[path]['before'], paths[path]['after']):
                                    raise RuntimeError('working path changed during recovery: ' + path)
                                restore_raw(repo, path, paths[path]['before'], paths[path].get('permissions'))
                        for path in sorted(paths, key=lambda p: p.count('/')):
                            if paths[path]['before']['kind'] not in ('absent', 'shadowed'):
                                if raw_work(repo, path) not in (paths[path]['before'], paths[path]['after']):
                                    raise RuntimeError('working path changed during recovery: ' + path)
                                restore_raw(repo, path, paths[path]['before'], paths[path].get('permissions'))
                        tracked = [p for p, row in paths.items() if row['index'] is not None]
                        if tracked:
                            refresh_index_paths(repo, tracked, env, check=False)
                        if (git(repo, 'rev-parse', 'HEAD').stdout.decode().strip() != head
                                or index.read_bytes() != previous):
                            raise RuntimeError('HEAD/index changed during recovery')
                        lock.write_bytes(copy.read_bytes())
                        replace()
                finish_transaction(repo, record)
                events.append({'id': record['id'], 'status': 'restored'})
                continue
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass  # Payload and marker stay pinned; only overlaps are refused.
        blocked.append({'id': record['id'], 'paths': sorted(paths), 'clear_owner': record['clear_owner']})
    return blocked


def refuse_pending_overlap(repo, head, target, pending):
    incoming = [os.fsdecode(p) for p in git(repo, 'diff', '--no-renames', '--name-only', '-z', head, target, '--', '.').stdout.split(b'\0') if p]
    for record in pending:
        if any(a == b or a.startswith(b + '/') or b.startswith(a + '/')
               for a in incoming for b in record['paths']):
            raise RuntimeError('pending_adoption ' + json.dumps(record, sort_keys=True))


def missing_objects(repo, objects):
    objects = sorted(set(objects))
    if not objects:
        return []
    if any(not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', oid) for oid in objects):
        raise RuntimeError('object preparation requires complete object IDs')
    checked = git(repo, 'cat-file', '--batch-check=%(objectname) %(objecttype)',
                  data=('\n'.join(objects) + '\n').encode()).stdout.splitlines()
    if len(checked) != len(objects):
        raise RuntimeError('incomplete object availability response')
    return [line.split()[0].decode() for line in checked if line.endswith(b' missing')]


def fetch_missing_objects(repo, objects, remote='origin'):
    missing = missing_objects(repo, objects)
    for start in range(0, len(missing), 64):
        batch = missing[start:start + 64]
        # Same negotiation Git's own promisor fetch uses (noop, no `have` lines). With ordinary negotiation the
        # server treats every blob reachable from our blobless/shallow commits as already held and sends an
        # empty pack: "bad revision ... did not send all necessary objects".
        git(repo, '-c', 'maintenance.auto=false', '-c', 'fetch.negotiationAlgorithm=noop', 'fetch', '--no-tags',
            '--no-write-fetch-head', '--filter=blob:none', remote, *batch)
    remaining = missing_objects(repo, missing)
    if remaining:
        raise RuntimeError('explicit fetch did not supply required objects: ' + ', '.join(remaining))
    return len(missing)


def prepare_adoption_objects(repo, head, target, remote='origin', deadline=None):
    """Explicit OID fetches before index.lock; never deepen/refetch all history."""
    with object_preparation_budget(deadline):
        fetched = fetch_missing_objects(repo, (head, target), remote)
        for _ in range(16):
            listed = git(repo, 'rev-list', '--objects', '--no-object-names', '--missing=print', head + '..' + target).stdout
            missing = [line[1:].decode() for line in listed.splitlines() if line.startswith(b'?')]
            if not missing:
                break
            fetched += fetch_missing_objects(repo, missing, remote)
        else:
            raise RuntimeError('object preparation exceeded 16 metadata discovery waves')
        # The range excludes H: include old entries and older published staged/raw
        # working bases needed by the rehearsal and backup, without fetching novel W.
        raw = git(repo, 'diff', '--raw', '--no-abbrev', '--no-renames', '-z', head, target, '--', '.').stdout.split(b'\0')
        paths = [os.fsdecode(path) for path in raw[1::2]]
        require_resolved_index(repo)
        index = index_entries(repo, paths=paths)
        known, candidates = set(), []
        for header, path in zip(raw[::2], paths):
            om, nm, old, new, _ = header[1:].split()
            known.update(oid.decode() for mode, oid in ((om, old), (nm, new))
                         if mode in (b'100644', b'100755', b'120000') and oid.strip(b'0'))
            staged = index.get(path)
            if staged:
                candidates.append((path, tuple(v.encode() for v in staged)))
            working = raw_work(repo, path)
            if 'oid' in working:
                mode = b'120000' if working['kind'] == 'symlink' else working['mode'].encode()
                candidates.append((path, (mode, working['oid'].encode())))
        absent = set(missing_objects(repo, known | {entry[1].decode() for path, entry in candidates}))
        approved = absent & known
        for path, entry in candidates:
            if entry[1].decode() in absent and committed_ancestor_entry(repo, target, path, entry):
                approved.add(entry[1].decode())
        return fetched + fetch_missing_objects(repo, approved, remote)


def _adopt(repo, branch, target, pending=(), *, publish_local=True, remote='origin'):
    repo = Path(git(repo, 'rev-parse', '--show-toplevel').stdout.decode().strip()).resolve()
    current = git(repo, 'symbolic-ref', '--short', '-q', 'HEAD', check=False)
    if current.returncode or current.stdout.decode().strip() != branch:
        return {'status': 'skipped', 'reason': 'the checkout is not on ' + branch}
    head = git(repo, 'rev-parse', 'HEAD').stdout.decode().strip()
    if head == target:
        return {'status': 'adopted', 'head_before': head, 'head': head,
                'kept_identical': 0, 'merged_published': 0}
    for marker in ('MERGE_HEAD', 'REBASE_HEAD', 'CHERRY_PICK_HEAD', 'rebase-merge', 'rebase-apply'):
        path = git(repo, 'rev-parse', '--git-path', marker).stdout.decode().strip()
        if (repo / path).exists():
            raise RuntimeError('operation in progress: ' + marker)
    patch_equivalent = False
    published_local = None
    require_resolved_index(repo)
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', target):
        target = git(repo, 'rev-parse', '--verify', target + '^{commit}').stdout.decode().strip()
        if head == target:
            return {'status': 'adopted', 'head_before': head, 'head': head,
                    'kept_identical': 0, 'merged_published': 0}
    preparation_deadline = time.monotonic() + 300
    fetched_objects = prepare_adoption_objects(repo, head, target, remote=remote, deadline=preparation_deadline)
    if git(repo, 'merge-base', '--is-ancestor', head, target, check=False).returncode:
        cherries = git(repo, 'cherry', target, head).stdout.decode().splitlines()
        if not publish_local and (not cherries or any(not line.startswith('-') for line in cherries)):
            return {'status': 'skipped', 'head': head,
                    'reason': 'not a fast-forward: the checkout has unpublished local commits',
                    'unpublished_commits': unpublished_commits(repo, target)}
        if any(line.startswith('+') for line in cherries):
            # Committed local work is never discarded. Publish it through the same bounded route.
            from .unit import publish_unit
            base = git(repo, 'merge-base', head, target).stdout.decode().strip()
            paths = [os.fsdecode(p) for p in git(repo, 'diff', '--name-only', '-z', base, head, '--', '.').stdout.split(b'\0') if p]
            result = publish_unit(str(repo), head, remote, branch, paths=paths)
            if result.get('status') not in ('published', 'up_to_date'):
                raise RuntimeError('unpublished local work could not be integrated: ' + json.dumps(result))
            target = result['published_tip']
            published_local = target
            fetched_objects += prepare_adoption_objects(repo, head, target, remote=remote, deadline=preparation_deadline)
        else:
            if not cherries or any(not line.startswith('-') for line in cherries):
                raise RuntimeError('local history is not proven patch-equivalent to upstream')
            patch_equivalent = True
    refuse_pending_overlap(repo, head, target, pending)
    index = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'index').stdout.decode().strip())
    with held_index_lock(index) as (lock, replace_index):
        original = index.read_bytes()
        with tempfile.TemporaryDirectory(prefix=profile.get().scratch_prefix + 'adopt-', dir=index.parent) as td:
            copy = Path(td) / 'index'
            copy.write_bytes(original)
            env = dict(os.environ, GIT_INDEX_FILE=str(copy))
            require_resolved_index(repo, env)
            raw = git(repo, 'diff', '--raw', '--no-abbrev', '--no-renames', '-z', head, target, '--', '.').stdout.split(b'\0')
            plan = []
            observed = {}
            refresh_paths = []
            identical = 0
            identical_untracked = {}
            local_additions = set()
            historical_work = {}
            historical_index = set()
            zero = b'0' * len(head)
            changes = []
            for n in range(0, len(raw) - 1, 2):
                om, nm, old, new, status = raw[n][1:].split()
                path = os.fsdecode(raw[n + 1])
                changes.append((path, None if old == zero else (om, old),
                                None if new == zero else (nm, new)))
            entries = {path: (mode.encode(), oid.encode()) for path, (mode, oid) in
                       index_entries(repo, env, paths=[path for path, _, _ in changes]).items()}
            # If the working tree already contains the incoming link, its old
            # tracked children are shadowed by that link, not real deletions.
            shadowed = set()
            for path, before, after in sorted(changes, key=lambda item: item[0].count('/')):
                if after and after[0] == b'120000' and not any(path.startswith(p + '/') for p in shadowed):
                    if work_entry(repo, path) == after:
                        shadowed.add(path)
            incoming = {path for path, before, after in changes if after is not None}
            incoming_directories = {path for path, before, after in changes
                                    if before and before[0] == b'120000' and after is None
                                    and any(child.startswith(path + '/') for child in incoming)}
            # A historical staged entry usually needs only an index reset. If both
            # layers differ and the working bytes are new, use the staged base to
            # carry that delta across the incoming change.
            stale_reset = {}
            published_merges = {}
            for path, before, after in changes:
                staged = entries.get(path)
                if staged in (before, after):
                    continue
                if staged is not None and committed_ancestor_entry(repo, target, path, staged):
                    if before and after and staged[0] in (b'100644', b'100755'):
                        work = work_entry(repo, path, shadowed, path in incoming_directories)
                        if work and work not in (before, staged, after):
                            working = raw_work(repo, path, save=True)
                            raw_entry = (work[0], working['oid'].encode()) if 'oid' in working else None
                            if not committed_ancestor_entry(repo, target, path, raw_entry):
                                if working['kind'] != 'file' or not before[0] == staged[0] == after[0] == work[0]:
                                    raise BlockedPath('published-base merge requires unchanged regular-file mode', path)
                                published_merges[path] = merge_published_work(repo, path, staged, after, working, td)
                                continue
                    stale_reset[path] = before
                elif (staged is None and before is not None
                      and work_entry(repo, path, shadowed, path in incoming_directories) == before):
                    # A staged deletion that the disk still contradicts (bytes equal HEAD)
                    # is the same lag; real deletions no longer have the file on disk.
                    stale_reset[path] = before
                else:
                    continue
                historical_index.add(path)
                entries[path] = before
            if stale_reset:
                git(repo, 'update-index', '--replace', '-z', '--index-info', env=env,
                    data=b''.join((before[0] + b' ' + before[1] if before else b'0 ' + zero)
                                  + b'\t' + os.fsencode(path) + b'\0'
                                  for path, before in stale_reset.items()))
                # Cacheinfo entries carry no stat data; a plain merge then refuses them as
                # not-uptodate. Refresh only where the disk still matches the reset entry.
                git(repo, 'update-index', '--refresh', '--', *stale_reset, env=env, check=False)
            for path, before, after in changes:
                staged = entries.get(path)
                actual = work_entry(repo, path, shadowed, path in incoming_directories)
                observed[path] = actual
                if path in published_merges:
                    mode, blob = after
                    plan.append((True, mode + b' ' + blob + b'\t' + os.fsencode(path) + b'\0'))
                    continue
                work = None if actual and actual[0] == b'040000' and path in incoming_directories else actual
                if any(path.startswith(parent + '/') for parent in shadowed):
                    # Removing a staged local modification would lose work even
                    # though the working path is now hidden behind the link.
                    if staged not in (before, after):
                        raise BlockedPath('incoming link shadows staged local work', path)
                if any(e and e[0] not in (b'100644', b'100755', b'120000') for e in (before, after, staged)):
                    raise BlockedPath('unsupported incoming type', path)
                if staged not in (before, after):
                    # Every provable historical staged entry was reset before this
                    # loop, so a differing entry here is another session's real work.
                    raise BlockedPath('incoming change overlaps staged local work', path)
                incoming_bytes_old_mode = (before and after and work
                                           and before[0] in (b'100644', b'100755')
                                           and after[0] in (b'100644', b'100755')
                                           and work == (before[0], after[1]))
                # Classify the working layer independently, even when the index
                # already holds T. History proof uses raw bytes, not line sets or
                # clean-filter equality that could hide a distinct working file.
                candidate = work is not None and work != after and (staged == after or work not in (before, after))
                raw = raw_work(repo, path) if candidate else {}
                raw_entry = (work[0], raw['oid'].encode()) if candidate and 'oid' in raw else None
                proven_work = raw_entry and (committed_ancestor_entry(repo, target, path, raw_entry)
                    or (incoming_bytes_old_mode and raw_entry[1] == after[1]))
                if proven_work and work != after and (work != before or staged == after):
                    historical_work[path] = work
                    mode, blob = work
                    plan.append((True, mode + b' ' + blob + b'\t' + os.fsencode(path) + b'\0'))
                    continue
                if before is None and staged is None:
                    if work is None:
                        if raw_work(repo, path)['kind'] == 'directory':
                            raise BlockedPath('incoming addition collides with a local directory', path)
                    else:
                        if work != after and work[0] == after[0] == b'100644':
                            # core.autocrlf=input normalizes CRLF in hash-object --path,
                            # while publish_working stores the raw bytes; the same file
                            # then looks different. Compare the unfiltered bytes too.
                            raw = git(repo, 'hash-object', '--no-filters', '--stdin',
                                      data=(repo / path).read_bytes()).stdout.strip()
                            if raw == after[1]:
                                work = after
                        if work != after:
                            # The incoming entry takes the index; the local bytes stay on
                            # disk as an ordinary modification of it, never overwritten.
                            if work[0] != after[0]:
                                raise BlockedPath('incoming addition differs in type from untracked local path', path)
                            local_additions.add(path)
                        identical_untracked[path] = after
                if staged == after or (staged == before and work == before):
                    if staged == before and staged is not None:
                        refresh_paths.append(path)
                    continue
                if after is None and work is not None:
                    raise BlockedPath('incoming deletion overlaps local work', path)
                if before and after and before[0] != after[0] and work not in (before, after):
                    raise BlockedPath('incoming type/mode change overlaps local work', path)
                identical += work == after
                mode, blob = after if after else (b'0', zero)
                plan.append((after is not None, mode + b' ' + blob + b'\t' + os.fsencode(path) + b'\0'))
            if plan:
                # File/directory transitions require removing old children or
                # parent entries before installing the incoming counterpart.
                git(repo, 'update-index', '--replace', '-z', '--index-info', env=env,
                    data=b''.join(item for _, item in sorted(plan, key=lambda item: item[0])))
            if local_additions:
                git(repo, 'update-index', '--add', '-z', '--index-info', env=env,
                    data=b''.join(identical_untracked[path][0] + b' ' + identical_untracked[path][1]
                                  + b'\t' + os.fsencode(path) + b'\0' for path in sorted(local_additions)))
            if historical_work:
                # Cacheinfo entries have no stat data; refresh only the exact
                # historical paths against the matching bytes already on disk.
                git(repo, 'update-index', '--refresh', '--', *historical_work, env=env, check=False)
                paths = [':(literal)' + path for path in historical_work]
                if git(repo, 'diff-files', '--quiet', '--', *paths, env=env, check=False).returncode:
                    raise RuntimeError('historical working bytes changed during index refresh')
            if refresh_paths:
                # read-tree checks cache stat data even for a byte-identical file
                # recreated since it was last seen in the original index.
                refresh_index_paths(repo, refresh_paths, env)
            before_tree = head
            if identical_untracked or historical_work:
                # Build the transition baseline from bytes already on disk.
                # Matching additions stay in place; committed stale copies are
                # updated from their real historical content to the target.
                baseline_env = dict(os.environ, GIT_INDEX_FILE=str(Path(td) / 'baseline'))
                git(repo, 'read-tree', head, env=baseline_env)
                for path, (mode, blob) in {**identical_untracked, **historical_work}.items():
                    git(repo, 'update-index', '--add', '--replace', '--cacheinfo',
                        mode.decode() + ',' + blob.decode() + ',' + path, env=baseline_env)
                # HEAD's entries are exact even where a blobless sparse clone lacks the blob.
                before_tree = git(repo, 'write-tree', '--missing-ok', env=baseline_env).stdout.decode().strip()
            # A plain modification-only transition whose index and working bytes both equal HEAD
            # cannot differ from the real fast-forward below (which refuses any overwrite), so the
            # 0.45 s dry-run two-tree read-tree is skipped there. Any other shape keeps it.
            redundant = (not (plan or patch_equivalent or stale_reset or identical_untracked or historical_work
                              or local_additions or published_merges or shadowed or incoming_directories)
                         and all(before is not None and after is not None and before[0] == after[0]
                                 and before[0] in (b'100644', b'100755', b'120000')
                                 and entries.get(path) == before and observed[path] == before
                                 for path, before, after in changes))
            if not redundant:
                git(repo, 'read-tree', '-m', '-u', '-n', before_tree, target, env=env)
            if git(repo, 'rev-parse', 'HEAD').stdout.decode().strip() != head or index.read_bytes() != original:
                raise RuntimeError('HEAD or index changed during adoption rehearsal')
            if any(work_entry(repo, p, shadowed, p in incoming_directories) != entry
                   for p, entry in observed.items()):
                raise RuntimeError('working files changed during adoption rehearsal')
            prepared = copy.read_bytes()
            transaction = None
            backup_ref = head_backup_ref = None
            if plan or patch_equivalent or stale_reset:
                transaction = begin_transaction(repo, branch, head, target, index, original,
                                                prepared, changes, incoming_directories, published_merges)
                backup_ref, head_backup_ref = transaction['index_ref'], transaction['head_ref']
                if (git(repo, 'rev-parse', 'HEAD').stdout.decode().strip() != head or index.read_bytes() != original
                        or any(raw_work(repo, p) != row['before'] for p, row in transaction['paths'].items())):
                    raise RuntimeError('HEAD/index/working bytes changed while preserving pending ' + transaction['id'])
            lock.write_bytes(prepared)
            replace_index()
            if patch_equivalent or identical_untracked or historical_work or published_merges:
                # Use the rehearsed two-tree transition directly: merge would reject an
                # identical untracked compatibility link over an old tracked directory.
                merged = git(repo, 'read-tree', '-m', '-u', before_tree, target, check=False)
                if merged.returncode == 0:
                    try:
                        for path, (expected, desired) in published_merges.items():
                            restore_raw(repo, path, desired, transaction['paths'][path]['permissions'], expected=expected)
                        moved = git(repo, 'update-ref', 'refs/heads/' + branch, target, head, check=False)
                        if moved.returncode:
                            raise RuntimeError('HEAD changed during adoption; original HEAD/index retained at ' +
                                               str(head_backup_ref) + ' and ' + str(backup_ref))
                    except Exception:
                        if published_merges:
                            recover_pending(repo, branch, pending_ref=transaction['ref'])
                        raise
            else:
                merged = git(repo, 'merge', '--ff-only', '--no-overwrite-ignore', target, check=False)
            if merged.returncode:
                # A refusal must not leave the preparatory index edits behind.
                with held_index_lock(index) as (lock2, replace_index2):
                    if (git(repo, 'rev-parse', 'HEAD').stdout.decode().strip() == head
                            and git(repo, 'ls-files', '-s', '-z').stdout == git(repo, 'ls-files', '-s', '-z', env=env).stdout):
                        lock2.write_bytes(original)
                        replace_index2()
                    elif plan:
                        raise RuntimeError('merge refused and concurrent index/HEAD change prevents rollback; index backup: ' + str(backup_ref))
                raise RuntimeError(merged.stderr.decode(errors='replace').strip())
            actual = git(repo, 'rev-parse', 'HEAD').stdout.decode().strip()
            if actual != target:
                raise RuntimeError('HEAD changed after adoption' + ('; pending ' + transaction['id'] if transaction else ''))
            if transaction:
                finish_transaction(repo, transaction)
            result = {'status': 'adopted', 'head_before': head, 'head': actual, 'kept_identical': identical,
                      'merged_published': len(published_merges),
                      'advanced_historical': len(historical_work),
                      'advanced_historical_index': len(historical_index),
                      'preserved_local': sum(work != entries.get(p) for p, work in observed.items()
                                             if p not in historical_work)}
            if stale_reset:
                # Index-only reset of lagging historical entries; working bytes unchanged.
                result['reset_stale_index'] = len(stale_reset)
                result['reset_stale_paths'] = sorted(stale_reset)
            if local_additions:
                # Local bytes kept on disk as an ordinary modification of the incoming file.
                result['local_untracked_overlay'] = sorted(local_additions)
            history_paths = sorted(set(historical_work) | historical_index)
            if history_paths:
                result['historical_paths'] = history_paths
            if history_paths:
                code_suffixes = {'.py', '.sh', '.js', '.mjs', '.ts', '.tsx', '.rb', '.pl'}
                marked = [p + (' [code, ' if Path(p).suffix.lower() in code_suffixes else ' [')
                          + ('disk, ' if p in historical_work else 'index, ')
                          + 'mode=' + (historical_work.get(p) or entries[p])[0].decode() + ']'
                          for p in history_paths]
                result['working_code_notice'] = 'Historical entries advanced: ' + '; '.join(marked)
                if any(Path(p).suffix.lower() in code_suffixes for p in history_paths):
                    result['working_code_notice'] += "; loaded code stays until the owning session's normal reload."
            if fetched_objects:
                result['fetched_objects'] = fetched_objects
            if backup_ref:
                result['index_backup_ref'] = backup_ref
            if head_backup_ref:
                result['head_backup_ref'] = head_backup_ref
            if patch_equivalent:
                result['patch_equivalent'] = True
            if published_local:
                result['published_local_tip'] = published_local
            return result


BACKUP_REF_RETENTION_DAYS = 7


def backup_inventory(repo, target=None):
    """One listing/ancestry query; one malformed line disables the entire pass."""
    settings = profile.get()
    prefix = settings.backup_prefix
    listed = git(repo, 'for-each-ref',
                 f'--format=%(refname)%00%(objectname)%00%(objecttype)%00%(committerdate:unix)%00%(subject)%00%(parent)%00%(contents:trailers:key={settings.creation_trailer},valueonly,separator=)%00%(contents:trailers:key={settings.transaction_trailer},valueonly,separator=)',
                 prefix, check=False)
    if listed.returncode:
        raise RuntimeError('backup inventory unavailable')
    automatic = re.compile(re.escape(prefix) + r'(index|head)-[0-9a-f]{32}')
    indices, heads, excluded = [], [], 0
    # Trailer atoms can end in a newline; normalize them in Git's formatter.
    for line in listed.stdout.decode(errors='replace').split('\n'):
        if not line:
            continue
        fields = line.split('\0')
        if len(fields) != 8:
            raise RuntimeError('malformed backup inventory')
        name, oid, kind, stamp, subject, parents, created, transaction = fields
        match = automatic.fullmatch(name)
        if not match or kind != 'commit':
            excluded += 1
            continue
        if match[1] == 'head':
            heads.append((name, oid))
        else:
            parents = parents.split()
            legacy = subject == 'Preserve index before publisher fast-forward' and len(parents) == 1
            modern = (subject == 'Preserve index before publisher fast-forward' and len(parents) == 2
                      and created.isdigit() and re.fullmatch('[0-9a-f]{32}', transaction or '')
                      and name.endswith(transaction))
            indices.append({'name': name, 'oid': oid, 'parents': parents,
                            'created_at': int(created) if modern else int(stamp) if legacy and stamp.isdigit() else None,
                            'id': transaction if modern else None})
            if not (legacy or modern):
                excluded += 1
    frozen = git(repo, 'rev-parse', '--verify',
                 (target or 'refs/remotes/origin/main') + '^{commit}', check=False)
    published = set()
    if frozen.returncode == 0:
        merged = git(repo, 'for-each-ref', '--merged', frozen.stdout.decode().strip(),
                     '--format=%(refname) %(objectname)', prefix + 'head-*', check=False)
        if merged.returncode:
            raise RuntimeError('backup ancestry inventory unavailable')
        published = set(tuple(line.split()) for line in merged.stdout.decode(errors='replace').splitlines())
    excluded += sum(head not in published for head in heads)
    return indices, heads, published, excluded


def delete_backup_batch(repo, refs):
    """Bound each CAS transaction to 200 refs; a rejected batch falls back per ref."""
    removed = []
    for offset in range(0, len(refs), 200):
        batch = refs[offset:offset + 200]
        commands = ''.join(f'delete {name} {oid}\n' for name, oid in batch).encode()
        if git(repo, 'update-ref', '--stdin', data=commands, check=False).returncode == 0:
            removed.extend(name for name, oid in batch)
        else:
            for name, oid in batch:
                if git(repo, 'update-ref', '-d', name, oid, check=False).returncode == 0:
                    removed.append(name)
    return removed


def prune_backup_refs(repo, now=None, days=BACKUP_REF_RETENTION_DAYS, target=None, *, inventory=None):
    """Prune recognized automatic pairs only, never pending or manual payload."""
    try:
        if inventory is None:
            inventory = backup_inventory(repo, target)
        indices, heads, published, _ = inventory
        pending = {line.rsplit('/', 1)[-1] for line in git(
            repo, 'for-each-ref', '--format=%(refname)', pending_prefix()).stdout.decode().splitlines()}
    except Exception as error:
        print('backup prune skipped: ' + (str(error).partition('\n')[0] or type(error).__name__), file=sys.stderr)
        return []  # Maintenance never changes a successful adoption/publication result.
    cutoff = (now if now is not None else time.time()) - days * 86400
    eligible = [(row['name'], row['oid']) for row in indices
                if row['created_at'] is not None and row['created_at'] < cutoff and row['id'] not in pending]
    removed = delete_backup_batch(repo, eligible)
    surviving = {row['parents'][0] for row in indices if row['parents'] and row['name'] not in removed}
    # Manual index refs do not protect redundant published HEAD pointers.
    # Concurrent new pairs pin their own parent; CAS never removes their fresh refs.
    removable_heads = [(name, oid) for name, oid in heads if oid not in surviving and (name, oid) in published]
    return removed + delete_backup_batch(repo, removable_heads)


def enforce_backup_cap(repo, target):
    # Same retention predicate, legacy expiry before capacity. A pair is one unit.
    inventory = backup_inventory(repo, target)
    removed = set(prune_backup_refs(repo, inventory=inventory))
    indices, heads, published, excluded = inventory
    if removed:
        indices = [row for row in indices if row['name'] not in removed]
        heads = [head for head in heads if head[0] not in removed]
    counted = [row for row in indices if row['created_at'] is not None]
    parents = {row['parents'][0] for row in counted}
    orphan_heads = sum(head in published and head[1] not in parents for head in heads)
    used = len(counted) + orphan_heads
    basis = {'counted_pairs': used, 'counted_index': len(counted), 'orphan_head_units': orphan_heads,
             'excluded_retained_unknown': excluded, 'limit_pairs': BACKUP_PAIR_LIMIT,
             'remaining_pairs': max(0, BACKUP_PAIR_LIMIT-used), 'required_pairs': 1}
    if used + 1 > BACKUP_PAIR_LIMIT:
        raise RuntimeError('backup_ref_cap ' + json.dumps(basis, sort_keys=True))
    return basis


def unpublished_commits(repo, target):
    details = git(repo, 'log', f'--format=%H%x1f%s%x1f%cI%x1f%(trailers:key={profile.get().writer_trailer},valueonly)%x1e',
                  str(target) + '..HEAD', '--', '.').stdout.decode(errors='replace')
    commits = []
    for record in details.split('\x1e'):
        fields = record.strip('\n\r ').split('\x1f')
        if len(fields) == 4:
            commits.append(dict(zip(('commit', 'subject', 'time', 'writer'), fields)))
    return commits


def adopt_checkout(repo, branch, target, *, publish_local=True, remote='origin', _publish_lock=None):
    """Publication remains successful when checkout adoption is refused."""
    events = []
    try:
        if not target:
            return {'status': 'skipped', 'reason': 'no published tip'}
        repo = Path(git(repo, 'rev-parse', '--show-toplevel').stdout.decode().strip()).resolve()
        with publication_lock(repo, _publish_lock):
            pending = recover_pending(repo, branch, events)
            result = _adopt(repo, branch, target, pending=pending, publish_local=publish_local, remote=remote)
            if events:
                result['pending_events'] = events
            if pending:
                result['pending_adoptions'] = pending
    except Exception as error:
        # Adoption errors must remain separate from an already landed publication.
        result = {'status': 'refused', 'reason': str(error)}
        if isinstance(error, BlockedPath):
            result['blocking'] = [error.path]
        if events:
            result['pending_events'] = events
        try:
            summaries = []
            for oid in git(repo, 'for-each-ref', '--format=%(objectname)', pending_prefix()).stdout.decode().splitlines():
                marker = json.loads(git(repo, 'show', oid + ':manifest.json').stdout)
                summaries.append({key: marker[key] for key in ('id', 'clear_owner') } | {'paths': sorted(marker['paths'])})
            if summaries:
                result['pending_adoptions'] = summaries
        except Exception:
            pass  # Diagnostic metadata cannot fail a landed publication either.
        try:
            commits = unpublished_commits(repo, target)
            if commits:
                result['unpublished_commits'] = commits
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pass
        return result
    # A transaction already pruned against the frozen target during its cap check.
    if 'index_backup_ref' in result:
        return result
    try:
        pruned = prune_backup_refs(repo, target=result.get('head') or target)
    except Exception as error:
        # Maintenance must never turn a landed publication into a reported failure.
        print('backup prune skipped: ' + (str(error).partition('\n')[0] or type(error).__name__), file=sys.stderr)
        pruned = []
    if pruned:
        result['pruned_backup_refs'] = len(pruned)
    return result
