#!/usr/bin/env python3
"""Stale staged entries must not block adoption, and incoming additions must keep local bytes.

Two shared-checkout regressions:
- 187 index entries held an older *published* version of the same path. They carried no work, but
  adoption refused with "published staged base overlaps unproved working bytes" and the checkout fell
  259 commits behind while other sessions kept real edits in the working tree.
- Untracked files whose newer local bytes differed from what origin added refused with
  "incoming addition differs from untracked local path".

Synthetic local repositories only.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared_git_publish import adopt as adopt_checkout  # noqa: E402


def run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
                               'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}).stdout.strip()


class AdoptStaleIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / 'checkout'
        run('git', 'init', '-q', '-b', 'main', str(self.repo))
        self.git('config', 'user.name', 't')
        self.git('config', 'user.email', 't@t')
        self.versions = [b'VALUE = %d\n' % n for n in range(4)]
        commits = []
        for data in self.versions:
            (self.repo / 'runner.py').write_bytes(data)
            if not commits:
                (self.repo / 'gone.txt').write_bytes(b'still here\n')
            self.git('add', '-A')
            self.git('commit', '-qm', 'version %d' % self.versions.index(data))
            commits.append(self.git('rev-parse', 'HEAD'))
        self.head, self.target = commits[2], commits[3]
        self.blobs = [self.git('rev-parse', c + ':runner.py') for c in commits]
        # Publish the target as the incoming tip, then return the checkout to the older commit.
        self.git('update-ref', 'refs/remotes/origin/main', self.target)
        self.git('update-ref', 'refs/heads/main', self.head, self.target)
        self.git('read-tree', '-u', '--reset', self.head)

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return run('git', '-C', str(self.repo), *args)

    def publish(self, files, message):
        """Commit `files` (path -> bytes) on top of the incoming tip and move origin/main."""
        for path, data in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            self.git('add', '--', path)
        tree = self.git('write-tree')
        tip = self.git('commit-tree', tree, '-p', self.target, '-m', message)
        self.git('update-ref', 'refs/remotes/origin/main', tip)
        self.git('read-tree', '-u', '--reset', self.head)
        return tip

    def test_stale_published_stage_with_conflicting_work_refuses(self):
        self.git('update-index', '--cacheinfo', '100644,' + self.blobs[1] + ',runner.py')
        (self.repo / 'runner.py').write_bytes(b'VALUE = 99\n')
        result = adopt_checkout.adopt_checkout(str(self.repo), 'main', 'refs/remotes/origin/main')
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['blocking'], ['runner.py'])
        self.assertEqual((self.repo / 'runner.py').read_bytes(), b'VALUE = 99\n')
        self.assertEqual(self.git('rev-parse', ':runner.py'), self.blobs[1])
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.head)

    def test_published_base_merge_keeps_disjoint_working_edit(self):
        self.check_published_base_merge('false')

    def test_published_base_merge_keeps_disjoint_working_edit_with_autocrlf_input(self):
        self.check_published_base_merge('input')

    def check_published_base_merge(self, autocrlf):
        self.git('config', 'core.autocrlf', autocrlf)
        staged = b'top published\n\nunchanged middle\n\nbottom staged\n'
        self.target = self.publish({'runner.py': staged}, 'publish staged base')
        base_blob = self.git('rev-parse', self.target + ':runner.py')
        target = self.publish({'runner.py': staged.replace(b'top published', b'top incoming')}, 'incoming edit')
        self.git('update-index', '--cacheinfo', '100644,' + base_blob + ',runner.py')
        work = self.repo / 'runner.py'
        ours = staged.replace(b'bottom staged', b'bottom local')
        work.write_bytes(ours)
        expected = ours.replace(b'top published', b'top incoming')

        class Interrupted(BaseException):
            pass

        git = adopt_checkout.git

        def stop_before_head_moves(repo, *args, **kwargs):
            if args[:2] == ('update-ref', 'refs/heads/main'):
                raise Interrupted
            return git(repo, *args, **kwargs)

        with patch.object(adopt_checkout, 'git', side_effect=stop_before_head_moves):
            with self.assertRaises(Interrupted):
                adopt_checkout.adopt_checkout(str(self.repo), 'main', target)
        self.assertEqual(work.read_bytes(), expected)
        with adopt_checkout.publication_lock(self.repo):
            self.assertEqual(adopt_checkout.recover_pending(self.repo, 'main'), [])
        self.assertEqual(work.read_bytes(), ours)
        self.assertEqual(self.git('rev-parse', ':runner.py'), base_blob)

        result = adopt_checkout.adopt_checkout(str(self.repo), 'main', target)

        self.assertEqual(result['status'], 'adopted', result)
        self.assertEqual(result['merged_published'], 1)
        self.assertEqual(work.read_bytes(), expected)
        self.assertEqual(self.git('rev-parse', ':runner.py'), self.git('rev-parse', target + ':runner.py'))
        self.assertEqual(self.git('rev-parse', result['index_backup_ref'] + ':runner.py'), base_blob)
        self.assertEqual(self.git('rev-parse', 'HEAD'), target)

    def test_publish_local_false_skips_unpublished_commit(self):
        (self.repo / 'local.txt').write_bytes(b'local work\n')
        self.git('add', 'local.txt')
        self.git('commit', '-qm', 'local work')
        head = self.git('rev-parse', 'HEAD')

        result = adopt_checkout.adopt_checkout(str(self.repo), 'main', self.target, publish_local=False)

        self.assertEqual(result['status'], 'skipped', result)
        self.assertEqual([commit['commit'] for commit in result['unpublished_commits']], [head])
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertEqual(self.git('rev-parse', 'origin/main'), self.target)

    def test_stale_stage_over_unchanged_disk_bytes_advances(self):
        """A stale stage over working bytes equal to HEAD's version ends at the target version."""
        self.git('update-index', '--cacheinfo', '100644,' + self.blobs[0] + ',runner.py')
        result = adopt_checkout.adopt_checkout(str(self.repo), 'main', 'refs/remotes/origin/main')
        self.assertEqual(result['status'], 'adopted', result)
        self.assertEqual((self.repo / 'runner.py').read_bytes(), self.versions[3])
        self.assertEqual(result['reset_stale_paths'], ['runner.py'])

    def test_stale_staged_deletion_of_present_file_is_not_work(self):
        """A staged deletion whose file is still on disk at HEAD's bytes is lag."""
        self.publish({'gone.txt': b'peer updated this\n'}, 'peer edits gone.txt')
        (self.repo / 'runner.py').write_bytes(b'VALUE = 99\n')
        self.git('rm', '--cached', '-q', '--', 'gone.txt')
        result = adopt_checkout.adopt_checkout(str(self.repo), 'main', 'refs/remotes/origin/main')
        self.assertEqual(result['status'], 'adopted', result)
        self.assertEqual((self.repo / 'gone.txt').read_bytes(), b'peer updated this\n')
        self.assertEqual((self.repo / 'runner.py').read_bytes(), b'VALUE = 99\n')
        self.assertEqual(result['reset_stale_paths'], ['gone.txt'])
        self.assertEqual(self.git('rev-parse', ':gone.txt'),
                         self.git('rev-parse', 'refs/remotes/origin/main:gone.txt'))

    def test_unproved_staged_work_still_refuses(self):
        """A staged hunk absent from history remains another session's work and blocks adoption."""
        local = b'my unrelated bytes\n'
        self.publish({'stage.txt': b'their published bytes\n'}, 'peer publishes stage.txt')
        (self.repo / 'stage.txt').write_bytes(local)
        work_blob = self.git('hash-object', '-w', '--', 'stage.txt')
        self.git('update-index', '--add', '--cacheinfo', '100644,' + work_blob + ',stage.txt')
        result = adopt_checkout.adopt_checkout(str(self.repo), 'main', 'refs/remotes/origin/main')
        self.assertEqual(result['status'], 'refused', result)
        self.assertIn('overlaps staged local work', result['reason'])
        self.assertEqual((self.repo / 'stage.txt').read_bytes(), local)
        self.assertEqual(self.git('rev-parse', ':stage.txt'), work_blob)

    def test_untracked_collision_keeps_local_bytes_as_modification(self):
        """Local bytes win on disk; the incoming version becomes the index entry."""
        local = b'local untracked bytes\n'
        self.publish({'added.txt': b'published\n'}, 'peer adds added.txt')
        (self.repo / 'added.txt').write_bytes(local)
        result = adopt_checkout.adopt_checkout(str(self.repo), 'main', 'refs/remotes/origin/main')
        self.assertEqual(result['status'], 'adopted', result)
        self.assertEqual((self.repo / 'added.txt').read_bytes(), local)
        self.assertEqual(result['local_untracked_overlay'], ['added.txt'])
        self.assertEqual(self.git('rev-parse', ':added.txt'),
                         self.git('rev-parse', 'refs/remotes/origin/main:added.txt'))
        self.assertEqual(self.git('diff-files', '--name-only', '--', 'added.txt'), 'added.txt')

    def test_identical_untracked_addition_keeps_local_file(self):
        """An untracked file already equal to the incoming bytes stays byte-identical."""
        self.publish({'added.txt': b'published\n'}, 'peer adds added.txt')
        (self.repo / 'added.txt').write_bytes(b'published\n')
        result = adopt_checkout.adopt_checkout(str(self.repo), 'main', 'refs/remotes/origin/main')
        self.assertEqual(result['status'], 'adopted', result)
        self.assertEqual((self.repo / 'added.txt').read_bytes(), b'published\n')
        self.assertNotIn('local_untracked_overlay', result)
        self.assertEqual(self.git('rev-parse', ':added.txt'),
                         self.git('rev-parse', 'refs/remotes/origin/main:added.txt'))


if __name__ == '__main__':
    unittest.main()
