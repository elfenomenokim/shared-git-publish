#!/usr/bin/env python3
"""A clean modification-only adoption skips the dry-run rehearsal; any other shape keeps it.

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


class AdoptRehearsalSkip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / 'checkout'
        run('git', 'init', '-q', '-b', 'main', str(self.repo))
        self.git('config', 'user.name', 't')
        self.git('config', 'user.email', 't@t')
        commits = []
        for n in range(3):
            (self.repo / 'runner.py').write_bytes(b'VALUE = %d\n' % n)
            self.git('add', '-A')
            self.git('commit', '-qm', 'version %d' % n)
            commits.append(self.git('rev-parse', 'HEAD'))
        self.head, self.target = commits[1], commits[2]
        self.git('update-ref', 'refs/remotes/origin/main', self.target)
        self.git('update-ref', 'refs/heads/main', self.head, self.target)
        self.git('read-tree', '-u', '--reset', self.head)

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return run('git', '-C', str(self.repo), *args)

    def adopt(self):
        """Run adoption and record every git argument list it issues through the module."""
        calls = []
        git = adopt_checkout.git

        def spy(repo, *args, **kwargs):
            calls.append(args)
            return git(repo, *args, **kwargs)

        with patch.object(adopt_checkout, 'git', side_effect=spy):
            result = adopt_checkout.adopt_checkout(str(self.repo), 'main', 'refs/remotes/origin/main')
        return result, calls

    @staticmethod
    def rehearsed(calls):
        return any(args[:4] == ('read-tree', '-m', '-u', '-n') for args in calls)

    def test_clean_modification_skips_rehearsal(self):
        result, calls = self.adopt()
        self.assertEqual(result['status'], 'adopted', result)
        self.assertFalse(self.rehearsed(calls), calls)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.target)
        self.assertEqual((self.repo / 'runner.py').read_bytes(), b'VALUE = 2\n')
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_local_working_edit_keeps_rehearsal(self):
        (self.repo / 'runner.py').write_bytes(b'VALUE = 99\n')
        result, calls = self.adopt()
        self.assertTrue(self.rehearsed(calls), calls)
        self.assertEqual(result['status'], 'adopted', result)
        self.assertEqual(result['preserved_local'], 1)
        self.assertEqual((self.repo / 'runner.py').read_bytes(), b'VALUE = 99\n')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.target)


if __name__ == '__main__':
    unittest.main()
