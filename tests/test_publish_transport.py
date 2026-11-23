"""Regressions for landed-but-timed-out sends and divergent shared-checkout adoption."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from shared_git_publish.adopt import adopt_checkout
from shared_git_publish import transport
from shared_git_publish.profile import Profile, use_profile


class PublicationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='publication-recovery-')
        self.root = Path(self.tmp.name)
        self.bare, self.repo = self.root / 'remote.git', self.root / 'local'
        self.run_git(self.root, 'init', '--bare', str(self.bare))
        self.run_git(self.root, 'init', '-b', 'main', str(self.repo))
        self.run_git(self.repo, 'config', 'user.name', 'Local')
        self.run_git(self.repo, 'config', 'user.email', 'local@example.invalid')
        self.run_git(self.repo, 'remote', 'add', 'origin', str(self.bare))
        (self.repo / 'README.md').write_text('base\n')
        self.commit(self.repo, 'README.md', 'base')
        self.run_git(self.repo, 'push', 'origin', 'HEAD:main')
        self.run_git(self.root, '--git-dir=' + str(self.bare), 'symbolic-ref', 'HEAD', 'refs/heads/main')
        self.peer = self.root / 'peer'
        self.run_git(self.root, 'clone', str(self.bare), str(self.peer))
        self.run_git(self.peer, 'config', 'user.name', 'Peer')
        self.run_git(self.peer, 'config', 'user.email', 'peer@example.invalid')

    def tearDown(self):
        self.tmp.cleanup()

    def run_git(self, repo, *args):
        return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE).decode().strip()

    def commit(self, repo, path, message):
        self.run_git(repo, 'add', '--', path)
        self.run_git(repo, 'commit', '-m', message, '--', path)
        return self.run_git(repo, 'rev-parse', 'HEAD')

    def peer_push(self, path, content):
        (self.peer / path).write_text(content)
        sha = self.commit(self.peer, path, 'peer update')
        self.run_git(self.peer, 'push', 'origin', 'HEAD:main')
        return sha

    def keep_peer_edits(self):
        (self.repo / 'README.md').write_text('peer unstaged draft\n')
        (self.repo / 'staged.txt').write_text('peer staged content\n')
        self.run_git(self.repo, 'add', '--', 'staged.txt')
        return self.run_git(self.repo, 'ls-files', '-s', '--', 'staged.txt')

    def assert_peer_edits(self, index):
        self.assertEqual((self.repo / 'README.md').read_text(), 'peer unstaged draft\n')
        self.assertEqual(self.run_git(self.repo, 'ls-files', '-s', '--', 'staged.txt'), index)

    def test_patch_equivalent_commit_adopts_without_losing_peer_work(self):
        (self.repo / 'task.txt').write_text('same patch\n')
        local = self.commit(self.repo, 'task.txt', 'local version')
        self.peer_push('task.txt', 'same patch\n')
        target = self.peer_push('remote.txt', 'new upstream work\n')
        self.run_git(self.repo, 'fetch', 'origin', 'main')
        index = self.keep_peer_edits()
        result = adopt_checkout(self.repo, 'main', target)
        self.assertEqual(result['status'], 'adopted', result)
        self.assertTrue(result['patch_equivalent'])
        self.assertEqual(self.run_git(self.repo, 'rev-parse', 'HEAD'), target)
        self.assertEqual(self.run_git(self.repo, 'rev-parse', result['head_backup_ref']), local)
        self.assert_peer_edits(index)

    def test_unpublished_local_commit_is_published_before_adoption(self):
        (self.repo / 'local.txt').write_text('unpublished work\n')
        local = self.commit(self.repo, 'local.txt', 'local work')
        target = self.peer_push('remote.txt', 'upstream work\n')
        self.run_git(self.repo, 'fetch', 'origin', 'main')
        index = self.keep_peer_edits()
        result = adopt_checkout(self.repo, 'main', target)
        self.assertEqual(result['status'], 'adopted', result)
        head = self.run_git(self.repo, 'rev-parse', 'HEAD')
        self.assertEqual(self.run_git(self.root, '--git-dir=' + str(self.bare), 'rev-parse', 'main'), head)
        self.run_git(self.repo, 'merge-base', '--is-ancestor', local, head)
        self.assertEqual((self.repo / 'local.txt').read_text(), 'unpublished work\n')
        self.assert_peer_edits(index)

    def test_conflicting_local_commit_is_kept_and_not_pushed(self):
        (self.repo / 'README.md').write_text('local committed work\n')
        local = self.commit(self.repo, 'README.md', 'local conflict\n\nWriter: original-session')
        target = self.peer_push('README.md', 'remote committed work\n')
        self.run_git(self.repo, 'fetch', 'origin', 'main')
        result = adopt_checkout(self.repo, 'main', target)
        self.assertEqual(result['status'], 'refused')
        detail = result['unpublished_commits'][0]
        self.assertEqual(detail['commit'], local)
        self.assertEqual(detail['subject'], 'local conflict')
        self.assertEqual(detail['writer'].strip(), 'original-session')
        self.assertTrue(detail['time'])
        self.assertEqual(self.run_git(self.repo, 'rev-parse', 'HEAD'), local)
        self.assertEqual(self.run_git(self.root, '--git-dir=' + str(self.bare), 'rev-parse', 'main'), target)

    def test_landed_push_timeout_reconciles_once_without_resending(self):
        (self.repo / 'task.txt').write_text('landed\n')
        target = self.commit(self.repo, 'task.txt', 'landed task')
        real_git = shutil.which('git')
        binaries = self.root / 'bin'
        binaries.mkdir()
        script = binaries / 'git'
        calls = self.root / 'push-calls'
        script.write_text('#!' + sys.executable + '\n' +
            'import os,subprocess,sys,time\n' +
            'args=sys.argv[1:]\n' +
            'if "push" in args:\n' +
            ' open(' + repr(str(calls)) + ',"a").write("push\\n")\n' +
            ' result=subprocess.run([' + repr(real_git) + ',*args])\n' +
            ' time.sleep(2)\n sys.exit(result.returncode)\n' +
            'os.execv(' + repr(real_git) + ',[' + repr(real_git) + ',*args])\n')
        script.chmod(0o755)
        from unittest.mock import Mock
        snapshot = Mock(return_value='one-sample.json')
        with patch.dict(os.environ, {'PATH': str(binaries) + os.pathsep + os.environ['PATH']}), \
             use_profile(Profile(slow_snapshot=snapshot)):
            result = transport.push(self.repo, 'origin', target, 'main', timeout=.5, slow_after=.1)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.reconciled)
        self.assertEqual(calls.read_text(), 'push\n')
        snapshot.assert_called_once()

    def test_identical_untracked_file_and_symlink_adopt_with_peer_work(self):
        self.peer_push('incoming.txt', 'already here\n')
        os.symlink('incoming.txt', self.peer / 'alias')
        target = self.commit(self.peer, 'alias', 'add alias')
        self.run_git(self.peer, 'push', 'origin', 'HEAD:main')
        self.run_git(self.repo, 'fetch', 'origin', 'main')
        (self.repo / 'incoming.txt').write_text('already here\n')
        os.symlink('incoming.txt', self.repo / 'alias')
        index = self.keep_peer_edits()
        result = adopt_checkout(self.repo, 'main', target)
        self.assertEqual(result['status'], 'adopted', result)
        self.assertEqual(self.run_git(self.repo, 'rev-parse', 'HEAD'), target)
        self.assertEqual(os.readlink(self.repo / 'alias'), 'incoming.txt')
        self.assert_peer_edits(index)

    def test_differing_untracked_file_or_symlink_keeps_local_bytes(self):
        """An incoming addition never overwrites a differing untracked path."""
        for symlink in (False, True):
            with self.subTest(symlink=symlink):
                name = 'different-alias' if symlink else 'different.txt'
                if symlink:
                    os.symlink('remote-target', self.peer / name)
                    os.symlink('local-target', self.repo / name)
                else:
                    (self.peer / name).write_text('remote\n')
                    (self.repo / name).write_text('local\n')
                target = self.commit(self.peer, name, 'incoming difference')
                self.run_git(self.peer, 'push', 'origin', 'HEAD:main')
                self.run_git(self.repo, 'fetch', 'origin', 'main')
                index = self.keep_peer_edits()
                result = adopt_checkout(self.repo, 'main', target)
                self.assertEqual(result['status'], 'adopted', result)
                self.assertEqual(result['local_untracked_overlay'], [name])
                self.assertEqual(self.run_git(self.repo, 'rev-parse', 'HEAD'), target)
                self.assertEqual(self.run_git(self.repo, 'rev-parse', ':' + name),
                                 self.run_git(self.repo, 'rev-parse', 'HEAD:' + name))
                self.assertEqual(os.readlink(self.repo / name) if symlink else (self.repo / name).read_text(),
                                 'local-target' if symlink else 'local\n')
                self.assert_peer_edits(index)

    def test_untracked_compatibility_symlink_after_staged_directory_rename(self):
        (self.repo / 'old').mkdir()
        (self.repo / 'old' / 'guide.md').write_text('guide\n')
        self.commit(self.repo, 'old/guide.md', 'old directory')
        self.run_git(self.repo, 'push', 'origin', 'HEAD:main')
        self.run_git(self.peer, 'pull', '--ff-only')
        self.run_git(self.peer, 'mv', 'old', 'new')
        os.symlink('new', self.peer / 'old')
        self.run_git(self.peer, 'add', '--', 'old', 'new')
        self.run_git(self.peer, 'commit', '-m', 'directory rename and compatibility link')
        target = self.run_git(self.peer, 'rev-parse', 'HEAD')
        self.run_git(self.peer, 'push', 'origin', 'HEAD:main')
        self.run_git(self.repo, 'fetch', 'origin', 'main')
        self.run_git(self.repo, 'mv', 'old', 'new')
        os.symlink('new', self.repo / 'old')
        index = self.keep_peer_edits()
        result = adopt_checkout(self.repo, 'main', target)
        self.assertEqual(result['status'], 'adopted', result)
        self.assertEqual(os.readlink(self.repo / 'old'), 'new')
        self.assertEqual((self.repo / 'new' / 'guide.md').read_text(), 'guide\n')
        self.assert_peer_edits(index)

    def test_directory_to_already_matching_symlink_does_not_follow_shadowed_children(self):
        (self.repo / 'old').mkdir()
        (self.repo / 'old' / 'guide.md').write_text('old guide\n')
        self.commit(self.repo, 'old/guide.md', 'old directory')
        self.run_git(self.repo, 'push', 'origin', 'HEAD:main')
        self.run_git(self.peer, 'pull', '--ff-only')
        self.run_git(self.peer, 'rm', '-r', '--', 'old')
        (self.peer / 'new').mkdir()
        (self.peer / 'new' / 'guide.md').write_text('new guide\n')
        os.symlink('new', self.peer / 'old')
        self.run_git(self.peer, 'add', '--', 'old', 'new')
        self.run_git(self.peer, 'commit', '-m', 'replace directory with link')
        target = self.run_git(self.peer, 'rev-parse', 'HEAD')
        self.run_git(self.peer, 'push', 'origin', 'HEAD:main')
        self.run_git(self.repo, 'fetch', 'origin', 'main')
        shutil.rmtree(self.repo / 'old')
        (self.repo / 'new').mkdir()
        (self.repo / 'new' / 'guide.md').write_text('new guide\n')
        os.symlink('new', self.repo / 'old')
        index = self.keep_peer_edits()
        result = adopt_checkout(self.repo, 'main', target)
        self.assertEqual(result['status'], 'adopted', result)
        self.assertEqual(os.readlink(self.repo / 'old'), 'new')
        self.assertEqual((self.repo / 'new' / 'guide.md').read_text(), 'new guide\n')
        self.assert_peer_edits(index)

    def test_link_transition_does_not_erase_staged_child_work(self):
        (self.repo / 'old').mkdir()
        (self.repo / 'old' / 'guide.md').write_text('base\n')
        self.commit(self.repo, 'old/guide.md', 'directory baseline')
        self.run_git(self.repo, 'push', 'origin', 'HEAD:main')
        self.run_git(self.peer, 'pull', '--ff-only')
        self.run_git(self.peer, 'rm', '-r', '--', 'old')
        (self.peer / 'new').mkdir()
        (self.peer / 'new' / 'guide.md').write_text('new target\n')
        os.symlink('new', self.peer / 'old')
        self.run_git(self.peer, 'add', '--', 'old', 'new')
        self.run_git(self.peer, 'commit', '-m', 'replace directory with link')
        target = self.run_git(self.peer, 'rev-parse', 'HEAD')
        self.run_git(self.peer, 'push', 'origin', 'HEAD:main')
        self.run_git(self.repo, 'fetch', 'origin', 'main')
        (self.repo / 'old' / 'guide.md').write_text('staged local change\n')
        self.run_git(self.repo, 'add', '--', 'old/guide.md')
        index = self.run_git(self.repo, 'ls-files', '-s', '--', 'old/guide.md')
        shutil.rmtree(self.repo / 'old')
        (self.repo / 'new').mkdir()
        (self.repo / 'new' / 'guide.md').write_text('new target\n')
        os.symlink('new', self.repo / 'old')
        result = adopt_checkout(self.repo, 'main', target)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.run_git(self.repo, 'ls-files', '-s', '--', 'old/guide.md'), index)
        self.assertEqual((self.repo / 'new' / 'guide.md').read_text(), 'new target\n')

    def test_symlink_to_already_matching_directory_keeps_extra_files(self):
        (self.repo / 'target').mkdir()
        (self.repo / 'target' / 'guide.md').write_text('target guide\n')
        os.symlink('target', self.repo / 'old')
        self.run_git(self.repo, 'add', '--', 'target', 'old')
        self.run_git(self.repo, 'commit', '-m', 'link baseline')
        self.run_git(self.repo, 'push', 'origin', 'HEAD:main')
        self.run_git(self.peer, 'pull', '--ff-only')
        self.run_git(self.peer, 'rm', '--', 'old')
        (self.peer / 'old').mkdir()
        (self.peer / 'old' / 'guide.md').write_text('incoming guide\n')
        self.run_git(self.peer, 'add', '--', 'old')
        self.run_git(self.peer, 'commit', '-m', 'replace link with directory')
        target = self.run_git(self.peer, 'rev-parse', 'HEAD')
        self.run_git(self.peer, 'push', 'origin', 'HEAD:main')
        self.run_git(self.repo, 'fetch', 'origin', 'main')
        (self.repo / 'old').unlink()
        (self.repo / 'old').mkdir()
        (self.repo / 'old' / 'guide.md').write_text('incoming guide\n')
        (self.repo / 'old' / 'local.txt').write_text('retain me\n')
        index = self.keep_peer_edits()
        result = adopt_checkout(self.repo, 'main', target)
        self.assertEqual(result['status'], 'adopted', result)
        self.assertEqual((self.repo / 'old' / 'guide.md').read_text(), 'incoming guide\n')
        self.assertEqual((self.repo / 'old' / 'local.txt').read_text(), 'retain me\n')
        self.assert_peer_edits(index)

    def test_tracked_older_committed_bytes_advance_without_touching_peer_edits(self):
        (self.repo / 'versioned.txt').write_text('old committed\n')
        self.commit(self.repo, 'versioned.txt', 'old version')
        (self.repo / 'versioned.txt').write_text('head committed\n')
        self.commit(self.repo, 'versioned.txt', 'head version')
        self.run_git(self.repo, 'push', 'origin', 'HEAD:main')
        self.run_git(self.peer, 'pull', '--ff-only')
        target = self.peer_push('versioned.txt', 'incoming committed\n')
        self.run_git(self.repo, 'fetch', 'origin', 'main')
        (self.repo / 'versioned.txt').write_text('old committed\n')
        index = self.keep_peer_edits()
        result = adopt_checkout(self.repo, 'main', target)
        self.assertEqual(result['status'], 'adopted', result)
        self.assertEqual((self.repo / 'versioned.txt').read_text(), 'incoming committed\n')
        self.assert_peer_edits(index)

    def test_incoming_bytes_with_old_executable_mode_advance(self):
        path = self.repo / 'mode.sh'
        path.write_text('old\n')
        path.chmod(0o755)
        self.commit(self.repo, 'mode.sh', 'executable old version')
        self.run_git(self.repo, 'push', 'origin', 'HEAD:main')
        self.run_git(self.peer, 'pull', '--ff-only')
        peer_path = self.peer / 'mode.sh'
        peer_path.write_text('incoming\n')
        peer_path.chmod(0o644)
        self.commit(self.peer, 'mode.sh', 'incoming content and mode')
        target = self.run_git(self.peer, 'rev-parse', 'HEAD')
        self.run_git(self.peer, 'push', 'origin', 'HEAD:main')
        self.run_git(self.repo, 'fetch', 'origin', 'main')
        path.write_text('incoming\n')
        result = adopt_checkout(self.repo, 'main', target)
        self.assertEqual(result['status'], 'adopted', result)
        self.assertEqual(path.read_text(), 'incoming\n')
        self.assertFalse(path.stat().st_mode & 0o111)

    def test_unexpected_parent_symlink_refuses_without_touching_its_target(self):
        (self.peer / 'folder').mkdir()
        (self.peer / 'folder' / 'file.txt').write_text('incoming\n')
        self.run_git(self.peer, 'add', '--', 'folder/file.txt')
        self.run_git(self.peer, 'commit', '-m', 'add folder')
        target = self.run_git(self.peer, 'rev-parse', 'HEAD')
        self.run_git(self.peer, 'push', 'origin', 'HEAD:main')
        self.run_git(self.repo, 'fetch', 'origin', 'main')
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'file.txt').write_text('keep this\n')
        os.symlink(str(outside), self.repo / 'folder')
        result = adopt_checkout(self.repo, 'main', target)
        self.assertEqual(result['status'], 'refused', result)
        self.assertIn('unexpected symlink', result['reason'])
        self.assertEqual((outside / 'file.txt').read_text(), 'keep this\n')

    def test_runtime_json_matching_history_remains_local(self):
        (self.repo / 'state.json').write_text('{"cursor":1}\n')
        self.commit(self.repo, 'state.json', 'old state')
        (self.repo / 'state.json').write_text('{"cursor":2}\n')
        self.commit(self.repo, 'state.json', 'head state')
        self.run_git(self.repo, 'push', 'origin', 'HEAD:main')
        self.run_git(self.peer, 'pull', '--ff-only')
        target = self.peer_push('state.json', '{"cursor":3}\n')
        self.run_git(self.repo, 'fetch', 'origin', 'main')
        (self.repo / 'state.json').write_text('{"cursor":1}\n')
        result = adopt_checkout(self.repo, 'main', target)
        self.assertEqual(result['status'], 'adopted', result)
        self.assertEqual((self.repo / 'state.json').read_text(), '{"cursor":1}\n')
        self.assertTrue(self.run_git(self.repo, 'diff', '--name-only', '--', 'state.json'))

    def test_untracked_older_committed_bytes_advance(self):
        (self.repo / 'returning.txt').write_text('old committed\n')
        self.commit(self.repo, 'returning.txt', 'old version')
        self.run_git(self.repo, 'rm', '--', 'returning.txt')
        self.run_git(self.repo, 'commit', '-m', 'remove old version')
        self.run_git(self.repo, 'push', 'origin', 'HEAD:main')
        self.run_git(self.peer, 'pull', '--ff-only')
        target = self.peer_push('returning.txt', 'incoming committed\n')
        self.run_git(self.repo, 'fetch', 'origin', 'main')
        (self.repo / 'returning.txt').write_text('old committed\n')
        index = self.keep_peer_edits()
        result = adopt_checkout(self.repo, 'main', target)
        self.assertEqual(result['status'], 'adopted', result)
        self.assertEqual((self.repo / 'returning.txt').read_text(), 'incoming committed\n')
        self.assert_peer_edits(index)

    def test_changed_staged_overlap_is_refused_and_preserved(self):
        target = self.peer_push('README.md', 'incoming committed\n')
        self.run_git(self.repo, 'fetch', 'origin', 'main')
        head = self.run_git(self.repo, 'rev-parse', 'HEAD')
        (self.repo / 'README.md').write_text('staged local change\n')
        self.run_git(self.repo, 'add', '--', 'README.md')
        index = self.run_git(self.repo, 'ls-files', '-s', '--', 'README.md')
        result = adopt_checkout(self.repo, 'main', target)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.run_git(self.repo, 'rev-parse', 'HEAD'), head)
        self.assertEqual(self.run_git(self.repo, 'ls-files', '-s', '--', 'README.md'), index)
        self.assertEqual((self.repo / 'README.md').read_text(), 'staged local change\n')

    def test_exact_ref_transaction_race_is_retryable_but_unknown_lock_is_not(self):
        rejected = '!\tabc:refs/heads/main\t[remote rejected] (failed to update ref)'
        message = "cannot lock ref 'refs/heads/main': is at " + '1' * 40 + ' but expected ' + '2' * 40
        self.assertTrue(transport.deterministic_race(rejected, message))
        self.assertFalse(transport.deterministic_race(rejected, 'main.lock exists'))


if __name__ == '__main__':
    unittest.main()
