#!/usr/bin/env python3
"""fetch_missing_objects must fetch blobs that a shallow blobless clone's own commits already reference.

Synthetic local repositories only. A plain `git fetch origin <blob>` sends `have` lines; the server then
assumes the client holds every blob reachable from them and returns an empty pack, so adoption was refused
with "bad revision ... did not send all necessary objects" (2026-11-13).
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from shared_git_publish import adopt as adopt_checkout


def run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, 'GIT_NO_LAZY_FETCH': '1', 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
                               'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}).stdout.strip()


class FetchMissingObjects(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        server, work, self.client = root / 'server.git', root / 'work', root / 'client'
        run('git', 'init', '-q', '--bare', '-b', 'main', str(server))
        run('git', '-C', str(server), 'config', 'uploadpack.allowFilter', 'true')
        run('git', '-C', str(server), 'config', 'uploadpack.allowAnySHA1InWant', 'true')
        run('git', 'init', '-q', '-b', 'main', str(work))
        for n in (1, 2, 3):
            (work / 'a.txt').write_text('v%d\n' % n)
            run('git', 'add', 'a.txt', cwd=work)
            run('git', 'commit', '-qm', 'c%d' % n, cwd=work)
        run('git', 'push', '-q', str(server), 'main', cwd=work)
        self.tip_blob = run('git', 'rev-parse', 'HEAD:a.txt', cwd=work)
        self.old_blob = run('git', 'rev-parse', 'HEAD~2:a.txt', cwd=work)
        run('git', 'clone', '-q', '--depth', '1', '--filter=blob:none', '--no-checkout', 'file://' + str(server), str(self.client))

    def tearDown(self):
        self.tmp.cleanup()

    def present(self, oid):
        return subprocess.run(['git', '-C', str(self.client), 'cat-file', '-e', oid],
                              env={**os.environ, 'GIT_NO_LAZY_FETCH': '1'}).returncode == 0

    def test_blob_reachable_from_our_shallow_commit_is_fetched(self):
        self.assertFalse(self.present(self.tip_blob))
        # The exact failure this guards: ordinary negotiation returns an empty pack for this blob.
        plain = subprocess.run(['git', '-C', str(self.client), 'fetch', '--no-tags', '--no-write-fetch-head', 'origin', self.tip_blob],
                               capture_output=True, text=True, env={**os.environ, 'GIT_NO_LAZY_FETCH': '1'})
        self.assertFalse(self.present(self.tip_blob), plain.stderr)
        self.assertEqual(adopt_checkout.fetch_missing_objects(self.client, [self.tip_blob, self.old_blob]), 2)
        self.assertTrue(self.present(self.tip_blob) and self.present(self.old_blob))

    def test_nothing_missing_means_no_fetch(self):
        adopt_checkout.fetch_missing_objects(self.client, [self.tip_blob])
        self.assertEqual(adopt_checkout.fetch_missing_objects(self.client, [self.tip_blob]), 0)


if __name__ == '__main__':
    unittest.main()
