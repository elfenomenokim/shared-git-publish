#!/usr/bin/env python3
"""
Unit and integration tests for publish_unit.py.

Uses local synthetic bare repositories only. No remote credentials or external calls.
Covers:
- Fast-Forward push
- Conflict-free divergence preserving peer bytes
- Conflict no push (exact tips reported, no marker trees published)
- >50KB full blob preserved without loss or truncation
- Deterministic NFF with finite recovery (at most one attempt)
- Ambiguous send failure reconciled vs not reconciled (never blind resend)
- CLI invocation and terminal JSON receipt format
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import time
from pathlib import Path
from unittest.mock import patch

CLI = [sys.executable, '-m', 'shared_git_publish']

from shared_git_publish.unit import (
    create_merge_commit_tree,
    get_remote_branch_tip,
    is_deterministic_nff,
    publish_unit,
    read_git_blob,
    resolve_commit_sha,
    run_git,
)


class TestPublishUnitBase(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="git_pub_test_")
        self.bare_dir = os.path.join(self.test_dir, "remote.git")
        self.local_dir = os.path.join(self.test_dir, "local")

        # Initialize bare remote
        subprocess.run(["git", "init", "--bare", "-b", "main", self.bare_dir], check=True, capture_output=True)

        # Initialize local repo
        subprocess.run(["git", "init", "-b", "main", self.local_dir], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.local_dir, "config", "user.name", "Tester"], check=True)
        subprocess.run(["git", "-C", self.local_dir, "config", "user.email", "tester@example.invalid"], check=True)
        subprocess.run(["git", "-C", self.local_dir, "remote", "add", "origin", self.bare_dir], check=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_initial_commit(self) -> str:
        init_file = os.path.join(self.local_dir, "README.md")
        with open(init_file, "w") as f:
            f.write("# Init Project\n")
        subprocess.run(["git", "-C", self.local_dir, "add", "README.md"], check=True)
        subprocess.run(["git", "-C", self.local_dir, "commit", "-m", "Initial commit"], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.local_dir, "push", "origin", "HEAD:refs/heads/main"], check=True, capture_output=True)
        return resolve_commit_sha(self.local_dir, "HEAD")

    def _create_peer_commit_on_remote(self, file_path: str, content: bytes, msg: str = "Peer commit") -> str:
        """Create a commit on remote using a secondary clone to simulate a peer session."""
        peer_dir = os.path.join(self.test_dir, "peer_clone")
        subprocess.run(["git", "clone", self.bare_dir, peer_dir], check=True, capture_output=True)
        subprocess.run(["git", "-C", peer_dir, "config", "user.name", "Peer"], check=True)
        subprocess.run(["git", "-C", peer_dir, "config", "user.email", "peer@example.invalid"], check=True)

        full_path = os.path.join(peer_dir, file_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(content)
        subprocess.run(["git", "-C", peer_dir, "add", file_path], check=True)
        subprocess.run(["git", "-C", peer_dir, "commit", "-m", msg], check=True, capture_output=True)
        subprocess.run(["git", "-C", peer_dir, "push", "origin", "HEAD:refs/heads/main"], check=True, capture_output=True)
        peer_tip = resolve_commit_sha(peer_dir, "HEAD")
        shutil.rmtree(peer_dir, ignore_errors=True)
        return peer_tip


class TestPublishUnitCore(TestPublishUnitBase):
    def test_fast_forward_push(self):
        """Test 1: Fast-forward publication when remote is direct ancestor."""
        base_sha = self._create_initial_commit()

        # Local makes a new commit
        work_file = os.path.join(self.local_dir, "task1.txt")
        with open(work_file, "w") as f:
            f.write("Task 1 content\n")
        subprocess.run(["git", "-C", self.local_dir, "add", "task1.txt"], check=True)
        subprocess.run(["git", "-C", self.local_dir, "commit", "-m", "Task 1 complete"], check=True, capture_output=True)
        local_tip = resolve_commit_sha(self.local_dir, "HEAD")

        staged = os.path.join(self.local_dir, 'staged.txt')
        with open(staged, 'wb') as f: f.write(b'peer staging')
        subprocess.run(['git', '-C', self.local_dir, 'add', '--', 'staged.txt'], check=True)
        index_before = subprocess.check_output(['git', '-C', self.local_dir, 'ls-files', '--stage', '--', 'staged.txt'])
        receipt = publish_unit(
            repo=self.local_dir,
            tip=local_tip,
            remote="origin",
            branch="main",
            paths=["task1.txt"],
        )
        self.assertEqual(subprocess.check_output(['git', '-C', self.local_dir, 'ls-files', '--stage', '--', 'staged.txt']), index_before)
        with open(staged, 'rb') as f: self.assertEqual(f.read(), b'peer staging')

        self.assertEqual(receipt["status"], "published")
        self.assertEqual(receipt["method"], "fast_forward")
        self.assertEqual(receipt["local_tip"], local_tip)
        self.assertEqual(receipt["remote_tip"], base_sha)
        self.assertEqual(receipt["published_tip"], local_tip)
        self.assertEqual(receipt["recovery_attempts"], 0)

        # Remote ref must equal local_tip
        remote_tip = get_remote_branch_tip(self.local_dir, "origin", "main")
        self.assertEqual(remote_tip, local_tip)

    def test_conflict_free_divergence_preserving_peer_bytes(self):
        """Test 2: Conflict-free divergence creates 2-parent merge and preserves peer bytes exactly."""
        base_sha = self._create_initial_commit()

        # Local commits its unit on local_unit.txt
        local_file = os.path.join(self.local_dir, "local_unit.txt")
        with open(local_file, "wb") as f:
            f.write(b"local unit data line 1\nline 2\n")
        subprocess.run(["git", "-C", self.local_dir, "add", "local_unit.txt"], check=True)
        subprocess.run(["git", "-C", self.local_dir, "commit", "-m", "Local unit commit"], check=True, capture_output=True)
        local_tip = resolve_commit_sha(self.local_dir, "HEAD")

        # Peer pushes a separate file with specific arbitrary bytes
        peer_bytes = b"PEER_SIGNATURE_BINARY_\x00\x01\x02_DATA\xff\xfe\r\nEnd"
        peer_tip = self._create_peer_commit_on_remote("peer_data.dat", peer_bytes, msg="Peer work")

        # Now local and remote have diverged
        receipt = publish_unit(
            repo=self.local_dir,
            tip=local_tip,
            remote="origin",
            branch="main",
            paths=["local_unit.txt"],
        )

        self.assertEqual(receipt["status"], "published")
        self.assertEqual(receipt["method"], "merge_tree")
        self.assertEqual(receipt["local_tip"], local_tip)
        self.assertEqual(receipt["remote_tip"], peer_tip)
        published_tip = receipt["published_tip"]
        self.assertIsNotNone(published_tip)

        # Inspect parents: two parents (-p local_tip -p peer_tip)
        res_parents = subprocess.run(
            ["git", "-C", self.local_dir, "rev-list", "--parents", "-n", "1", published_tip],
            capture_output=True,
            text=True,
            check=True,
        )
        parents = res_parents.stdout.strip().split()
        self.assertEqual(parents[0], published_tip)
        self.assertEqual(parents[1], local_tip)
        self.assertEqual(parents[2], peer_tip)

        # Read peer blob from the published commit using raw bytes; must match byte-for-byte
        preserved_peer_bytes = read_git_blob(self.local_dir, published_tip, "peer_data.dat")
        self.assertEqual(preserved_peer_bytes, peer_bytes)

        # Local unit data must also be preserved
        preserved_local_bytes = read_git_blob(self.local_dir, published_tip, "local_unit.txt")
        self.assertEqual(preserved_local_bytes, b"local unit data line 1\nline 2\n")

    def test_conflict_no_push(self):
        """Test 3: Conflicting divergence refuses push, reports exact tips, never publishes markers."""
        base_sha = self._create_initial_commit()

        # Shared file
        shared_rel = "config/shared.env"

        # Peer pushes a change to shared.env
        peer_tip = self._create_peer_commit_on_remote(shared_rel, b"MODE=peer_mode\nPORT=8000\n", msg="Peer env")

        # Local commits conflicting change to shared.env
        local_shared = os.path.join(self.local_dir, shared_rel)
        os.makedirs(os.path.dirname(local_shared), exist_ok=True)
        with open(local_shared, "wb") as f:
            f.write(b"MODE=local_mode\nPORT=9000\n")
        subprocess.run(["git", "-C", self.local_dir, "add", shared_rel], check=True)
        subprocess.run(["git", "-C", self.local_dir, "commit", "-m", "Local conflict"], check=True, capture_output=True)
        local_tip = resolve_commit_sha(self.local_dir, "HEAD")

        receipt = publish_unit(
            repo=self.local_dir,
            tip=local_tip,
            remote="origin",
            branch="main",
            paths=[shared_rel],
        )

        self.assertEqual(receipt["status"], "conflict")
        self.assertEqual(receipt["local_tip"], local_tip)
        self.assertEqual(receipt["remote_tip"], peer_tip)
        self.assertIsNone(receipt["published_tip"])
        self.assertIn("Conflict detected during merge-tree", receipt["error"])

        # Remote ref must still be untouched at peer_tip
        remote_tip = get_remote_branch_tip(self.local_dir, "origin", "main")
        self.assertEqual(remote_tip, peer_tip)

    def test_over_50kb_full_blob_preserved(self):
        """Test 4: Blob larger than 50KB is published and verified via object full bytes without loss."""
        self._create_initial_commit()

        # Generate 64KB synthetic blob with non-trivial pattern
        large_blob = b"".join(f"ROW_{i:05d}_PAYLOAD_{'x' * 50}\n".encode("utf-8") for i in range(1000))
        self.assertGreater(len(large_blob), 50 * 1024)

        large_rel = "data/payload_64k.bin"
        local_target = os.path.join(self.local_dir, large_rel)
        os.makedirs(os.path.dirname(local_target), exist_ok=True)
        with open(local_target, "wb") as f:
            f.write(large_blob)

        subprocess.run(["git", "-C", self.local_dir, "add", large_rel], check=True)
        subprocess.run(["git", "-C", self.local_dir, "commit", "-m", "Commit 64k payload"], check=True, capture_output=True)
        local_tip = resolve_commit_sha(self.local_dir, "HEAD")

        receipt = publish_unit(
            repo=self.local_dir,
            tip=local_tip,
            remote="origin",
            branch="main",
            paths=[large_rel],
        )

        self.assertEqual(receipt["status"], "published")
        published_tip = receipt["published_tip"]

        # Read back raw blob bytes directly from published commit in bare remote repo
        retrieved_bytes = read_git_blob(self.bare_dir, published_tip, large_rel)
        self.assertEqual(len(retrieved_bytes), len(large_blob))
        self.assertEqual(retrieved_bytes, large_blob)


class TestPublishUnitRecoveryAndAmbiguity(TestPublishUnitBase):
    def test_unavailable_lookup_or_fetch_never_pushes(self):
        tip = self._create_initial_commit()
        for failed_command in ['ls-remote', 'fetch']:
            sent = []
            def runner(cmd, cwd=None, text=True, input_data=None):
                if 'push' in cmd: sent.append(cmd)
                if failed_command in cmd:
                    return subprocess.CompletedProcess(cmd, 128, '', 'synthetic network failure')
                return subprocess.run(cmd, cwd=cwd, capture_output=True, text=text, input=input_data)
            result = publish_unit(self.local_dir, tip, 'origin', 'main', ['base.txt'], runner=runner)
            self.assertEqual(result['status'], 'error'); self.assertEqual(sent, [])

    def test_only_machine_rejection_authorizes_recovery_and_scope_is_explicit(self):
        self.assertFalse(is_deterministic_nff('', 'remote hook says [rejected] (fetch first); connection lost'))
        self.assertTrue(is_deterministic_nff('!\tabc:refs/heads/main\t[rejected] (fetch first)\n', ''))
        def no_command(*args, **kwargs): self.fail('invalid scope ran git')
        for paths in [[], ['../outside'], ['/' ], [':(glob)**']]:
            self.assertEqual(publish_unit(self.local_dir, 'HEAD', 'origin', 'main', paths, runner=no_command)['status'], 'error')

    def test_deterministic_nff_single_recovery_success(self):
        """Test 5A: Deterministic NFF rejection triggers at most ONE recovery which succeeds."""
        base_sha = self._create_initial_commit()

        # Local commits
        f = os.path.join(self.local_dir, "local_recovery.txt")
        with open(f, "w") as fp:
            fp.write("recovery 1\n")
        subprocess.run(["git", "-C", self.local_dir, "add", "local_recovery.txt"], check=True)
        subprocess.run(["git", "-C", self.local_dir, "commit", "-m", "Local recovery"], check=True, capture_output=True)
        local_tip = resolve_commit_sha(self.local_dir, "HEAD")

        # Mock runner that simulates remote race on first push
        push_call_count = [0]
        real_run = subprocess.run

        def mock_runner(cmd, cwd=None, text=True, input_data=None):
            if "push" in cmd and push_call_count[0] == 0:
                push_call_count[0] += 1
                # In the background before first push, a peer pushed to main!
                self._create_peer_commit_on_remote("peer_mid_race.txt", b"race data\n", "Peer mid race")
                # Return standard git NFF rejection
                return subprocess.CompletedProcess(
                    cmd,
                    returncode=1,
                    stdout="To remote.git\n!\tlocal:refs/heads/main\t[rejected] (fetch first)\n",
                    stderr="error: failed to push some refs\nhint: Updates were rejected because the remote contains work that you do not have locally.\n",
                )
            return real_run(cmd, cwd=cwd, capture_output=True, text=text, input=input_data)

        receipt = publish_unit(
            repo=self.local_dir,
            tip=local_tip,
            remote="origin",
            branch="main",
            paths=["local_recovery.txt"],
            runner=mock_runner,
        )

        self.assertEqual(receipt["status"], "published")
        self.assertEqual(receipt["recovery_attempts"], 1)
        self.assertEqual(receipt["method"], "merge_tree")
        self.assertIsNotNone(receipt["published_tip"])

    def test_deterministic_nff_retry_exhausted(self):
        """Test 5B: Deterministic NFF rejection stops after 1 recovery attempt (finite known NFF)."""
        self._create_initial_commit()

        f = os.path.join(self.local_dir, "local_exhaust.txt")
        with open(f, "w") as fp:
            fp.write("exhaust\n")
        subprocess.run(["git", "-C", self.local_dir, "add", "local_exhaust.txt"], check=True)
        subprocess.run(["git", "-C", self.local_dir, "commit", "-m", "Local exhaust"], check=True, capture_output=True)
        local_tip = resolve_commit_sha(self.local_dir, "HEAD")

        # Mock runner where every push fails with NFF
        def mock_runner(cmd, cwd=None, text=True, input_data=None):
            if "push" in cmd:
                return subprocess.CompletedProcess(
                    cmd,
                    returncode=1,
                    stdout="To remote.git\n!\tlocal:refs/heads/main\t[rejected] (fetch first)\n",
                    stderr="error: failed to push some refs\nhint: (non-fast-forward)\n",
                )
            if "fetch" in cmd:
                # advance peer commit on remote during fetch to simulate persistent racing peer
                self._create_peer_commit_on_remote(f"racing_{tempfile.mktemp()}.txt", b"fast peer\n")
            return subprocess.run(cmd, cwd=cwd, capture_output=True, text=text, input=input_data)

        receipt = publish_unit(
            repo=self.local_dir,
            tip=local_tip,
            remote="origin",
            branch="main",
            paths=["local_exhaust.txt"],
            runner=mock_runner,
        )

        self.assertEqual(receipt["status"], "failed_nff_retry_exhausted")
        self.assertEqual(receipt["recovery_attempts"], 1)
        self.assertIsNone(receipt["published_tip"])
        self.assertIn("maximum (1) recovery attempt", receipt["error"])

    def test_ambiguous_send_failure_reconciled(self):
        """Test 5C: Ambiguous send failure reconciles remote ref without blind resend."""
        self._create_initial_commit()

        f = os.path.join(self.local_dir, "ambiguous_ok.txt")
        with open(f, "w") as fp:
            fp.write("ambiguous ok\n")
        subprocess.run(["git", "-C", self.local_dir, "add", "ambiguous_ok.txt"], check=True)
        subprocess.run(["git", "-C", self.local_dir, "commit", "-m", "Ambiguous commit"], check=True, capture_output=True)
        local_tip = resolve_commit_sha(self.local_dir, "HEAD")

        real_run = subprocess.run

        def mock_runner(cmd, cwd=None, text=True, input_data=None):
            if "push" in cmd:
                # Real push happens on remote server...
                real_run(cmd, cwd=cwd, capture_output=True, text=text, input=input_data)
                # ...but transport drops connection at EOF with broken pipe / connection reset
                return subprocess.CompletedProcess(
                    cmd,
                    returncode=128,
                    stdout="",
                    stderr="fatal: the remote end hung up unexpectedly\nfatal: write error: Broken pipe\n",
                )
            return real_run(cmd, cwd=cwd, capture_output=True, text=text, input=input_data)

        receipt = publish_unit(
            repo=self.local_dir,
            tip=local_tip,
            remote="origin",
            branch="main",
            paths=["ambiguous_ok.txt"],
            runner=mock_runner,
        )

        self.assertEqual(receipt["status"], "published")
        self.assertTrue(receipt["reconciled"])
        self.assertEqual(receipt["published_tip"], local_tip)
        self.assertEqual(receipt["recovery_attempts"], 0)

    def test_push_oserror_is_unknown_not_a_pre_execution_error(self):
        tip = self._create_initial_commit()
        with open(os.path.join(self.local_dir, 'unknown.txt'), 'w') as f: f.write('candidate')
        subprocess.run(['git', '-C', self.local_dir, 'add', '--', 'unknown.txt'], check=True)
        subprocess.run(['git', '-C', self.local_dir, 'commit', '-m', 'unknown'], check=True, capture_output=True)
        tip = resolve_commit_sha(self.local_dir, 'HEAD'); sent = []
        def runner(cmd, cwd=None, text=True, input_data=None):
            if 'push' in cmd: sent.append(cmd); raise OSError('synthetic uncertain send')
            return subprocess.run(cmd, cwd=cwd, capture_output=True, text=text, input=input_data)
        result = publish_unit(self.local_dir, tip, 'origin', 'main', ['unknown.txt'], runner=runner)
        self.assertEqual(result['status'], 'ambiguous_failure'); self.assertTrue(result['effectUnknown']); self.assertEqual(len(sent), 1)

    def test_ambiguous_send_failure_not_reconciled_no_blind_resend(self):
        """Test 5D: Ambiguous send failure when remote was not updated refuses blind resend."""
        base_sha = self._create_initial_commit()

        f = os.path.join(self.local_dir, "ambiguous_fail.txt")
        with open(f, "w") as fp:
            fp.write("ambiguous fail\n")
        subprocess.run(["git", "-C", self.local_dir, "add", "ambiguous_fail.txt"], check=True)
        subprocess.run(["git", "-C", self.local_dir, "commit", "-m", "Ambiguous fail commit"], check=True, capture_output=True)
        local_tip = resolve_commit_sha(self.local_dir, "HEAD")

        push_attempts = [0]
        real_run = subprocess.run

        def mock_runner(cmd, cwd=None, text=True, input_data=None):
            if "push" in cmd:
                push_attempts[0] += 1
                # Network failed completely before touching remote
                return subprocess.CompletedProcess(
                    cmd,
                    returncode=128,
                    stdout="",
                    stderr="fatal: unable to access 'remote.git': Could not resolve host\n",
                )
            return real_run(cmd, cwd=cwd, capture_output=True, text=text, input=input_data)

        receipt = publish_unit(
            repo=self.local_dir,
            tip=local_tip,
            remote="origin",
            branch="main",
            paths=["ambiguous_fail.txt"],
            runner=mock_runner,
        )

        self.assertEqual(receipt["status"], "ambiguous_failure")
        self.assertFalse(receipt["reconciled"])
        self.assertIsNone(receipt["published_tip"])
        self.assertIn("blind resend refused", receipt["error"])
        # Exactly one push attempted; NO blind resend!
        self.assertEqual(push_attempts[0], 1)


class TestPublishUnitCLI(TestPublishUnitBase):
    def test_cli_execution_and_receipt(self):
        """Test 6: Verify CLI entry point returns JSON receipt to stdout and exit code 0."""
        self._create_initial_commit()

        cli_file = os.path.join(self.local_dir, "cli_test.txt")
        with open(cli_file, "w") as f:
            f.write("CLI test content\n")
        subprocess.run(["git", "-C", self.local_dir, "add", "cli_test.txt"], check=True)
        subprocess.run(["git", "-C", self.local_dir, "commit", "-m", "CLI commit"], check=True, capture_output=True)
        local_tip = resolve_commit_sha(self.local_dir, "HEAD")

        cmd = [
            *CLI, 'publish-commit',
            "--repo",
            self.local_dir,
            "--tip",
            local_tip,
            "--remote",
            "origin",
            "--branch",
            "main",
            "--path",
            "cli_test.txt",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"CLI stderr: {res.stderr}")

        payload = json.loads(res.stdout)
        self.assertEqual(payload['schema_version'], 1)
        receipt = payload['publication']
        self.assertEqual(receipt["status"], "published")
        self.assertEqual(receipt["published_tip"], local_tip)
        self.assertEqual(receipt["paths"], ["cli_test.txt"])


class TestPublishUnitAdoption(TestPublishUnitBase):
    """After a successful publication the CLI fast-forwards the publishing checkout."""

    def _diverged_unit(self):
        self._create_initial_commit()
        subprocess.run(["git", "-C", self.local_dir, "branch", "-M", "main"], check=True)
        self._create_peer_commit_on_remote("peer.txt", b"peer bytes\n")
        # The peer's bytes already sit here uncommitted: the case plain `merge --ff-only` refuses.
        with open(os.path.join(self.local_dir, "peer.txt"), "wb") as f:
            f.write(b"peer bytes\n")
        with open(os.path.join(self.local_dir, "unit.txt"), "w") as f:
            f.write("unit\n")
        subprocess.run(["git", "-C", self.local_dir, "add", "unit.txt"], check=True)
        subprocess.run(["git", "-C", self.local_dir, "commit", "-m", "unit"], check=True, capture_output=True)
        return resolve_commit_sha(self.local_dir, "HEAD")

    def _publish(self, tip, *extra):
        cmd = [*CLI, 'publish-commit', "--repo", self.local_dir, "--tip", tip, "--remote", "origin",
               "--branch", "main", "--path", "unit.txt", *extra]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"CLI stderr: {res.stderr}")
        return json.loads(res.stdout)

    def test_checkout_follows_the_published_merge_past_an_identical_copy(self):
        tip = self._diverged_unit()
        receipt = self._publish(tip)
        self.assertEqual(receipt["publication"]["status"], "published")
        self.assertEqual((receipt["adoption"]["status"], receipt["adoption"]["kept_identical"]), ("adopted", 1))
        self.assertEqual(resolve_commit_sha(self.local_dir, "HEAD"), receipt["publication"]["published_tip"])
        status = subprocess.run(["git", "-C", self.local_dir, "status", "--porcelain"], capture_output=True, text=True)
        self.assertEqual(status.stdout, "")
        with open(os.path.join(self.local_dir, "peer.txt"), "rb") as f:
            self.assertEqual(f.read(), b"peer bytes\n")

    def test_no_adopt_publishes_only(self):
        tip = self._diverged_unit()
        receipt = self._publish(tip, "--no-adopt")
        self.assertEqual(receipt["publication"]["status"], "published")
        self.assertEqual(receipt["adoption"]["status"], "skipped")
        self.assertEqual(resolve_commit_sha(self.local_dir, "HEAD"), tip)

    def test_a_refused_adoption_never_fails_the_publication(self):
        tip = self._diverged_unit()
        os.unlink(os.path.join(self.local_dir, "peer.txt"))
        os.mkdir(os.path.join(self.local_dir, "peer.txt"))
        receipt = self._publish(tip)
        self.assertEqual(receipt["publication"]["status"], "published")
        self.assertEqual(receipt["adoption"]["status"], "refused")
        self.assertEqual(resolve_commit_sha(self.local_dir, "HEAD"), tip)
        self.assertTrue(os.path.isdir(os.path.join(self.local_dir, "peer.txt")))

    def test_working_publisher_adopts_and_preserves_peer_edits_and_staging(self):
        self._create_initial_commit()
        subprocess.run(['git', '-C', self.local_dir, 'branch', '-M', 'main'], check=True)
        self._create_peer_commit_on_remote('README.md', b'# Upstream README\n')
        readme = Path(self.local_dir) / 'README.md'
        readme.write_bytes(b'peer unstaged README\n')
        staged = Path(self.local_dir) / 'staged.txt'
        staged.write_bytes(b'peer staged bytes\n')
        subprocess.run(['git', '-C', self.local_dir, 'add', '--', 'staged.txt'], check=True)
        before = subprocess.check_output(['git', '-C', self.local_dir, 'ls-files', '-s', '--', 'staged.txt'])
        (Path(self.local_dir) / 'unit.txt').write_bytes(b'owned unit\n')
        lock = Path(self.local_dir) / '.git/index.lock'
        lock.touch()
        os.utime(lock, (time.time() - 400, time.time() - 400))
        result = subprocess.run([*CLI, 'publish',
                                 '--repo', self.local_dir, '--path', 'unit.txt', '--message', 'working unit'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertIn('cleared stale index.lock:', result.stderr)
        self.assertEqual(receipt['adoption']['status'], 'adopted', receipt)
        self.assertEqual(resolve_commit_sha(self.local_dir, 'HEAD'), receipt['publication']['commit'])
        self.assertEqual(readme.read_bytes(), b'peer unstaged README\n')
        self.assertEqual(staged.read_bytes(), b'peer staged bytes\n')
        self.assertEqual(subprocess.check_output(['git', '-C', self.local_dir, 'ls-files', '-s', '--', 'staged.txt']), before)
        self.assertFalse(lock.exists())

    def test_fresh_lock_is_left_and_publication_is_reported_separately(self):
        tip = self._diverged_unit()
        lock = Path(self.local_dir) / '.git/index.lock'
        lock.write_bytes(b'fresh lock')
        before = (Path(self.local_dir) / '.git/index').read_bytes()
        receipt = self._publish(tip)
        self.assertEqual(receipt['publication']['status'], 'published')
        self.assertEqual(receipt['adoption']['status'], 'refused')
        self.assertIn('index.lock', receipt['adoption']['reason'])
        self.assertEqual(lock.read_bytes(), b'fresh lock')
        self.assertEqual((Path(self.local_dir) / '.git/index').read_bytes(), before)
        self.assertEqual(resolve_commit_sha(self.local_dir, 'HEAD'), tip)

    def test_overlapping_staged_intermediate_is_preserved_on_refusal(self):
        self._create_initial_commit()
        subprocess.run(['git', '-C', self.local_dir, 'branch', '-M', 'main'], check=True)
        target = self._create_peer_commit_on_remote('README.md', b'upstream\n')
        readme = Path(self.local_dir) / 'README.md'
        readme.write_bytes(b'staged intermediate\n')
        subprocess.run(['git', '-C', self.local_dir, 'add', '--', 'README.md'], check=True)
        tree = subprocess.check_output(['git', '-C', self.local_dir, 'write-tree'], text=True).strip()
        backup = subprocess.check_output(['git', '-C', self.local_dir, 'commit-tree', tree, '-m', 'existing backup'], text=True).strip()
        backup_ref = 'refs/shared-git-publish/backup/index-existing'
        subprocess.run(['git', '-C', self.local_dir, 'update-ref', backup_ref, backup], check=True)
        readme.write_bytes(b'working after staged\n')
        subprocess.run(['git', '-C', self.local_dir, 'fetch', 'origin'], check=True, capture_output=True)
        index_before = (Path(self.local_dir) / '.git/index').read_bytes()
        from shared_git_publish.adopt import adopt_checkout
        receipt = adopt_checkout(self.local_dir, 'main', target)
        self.assertEqual(receipt['status'], 'refused', receipt)
        self.assertIn('incoming change overlaps staged local work: README.md', receipt['reason'])
        self.assertEqual((Path(self.local_dir) / '.git/index').read_bytes(), index_before)
        self.assertEqual(readme.read_bytes(), b'working after staged\n')
        self.assertEqual(resolve_commit_sha(self.local_dir, backup_ref), backup)
        self.assertEqual(subprocess.check_output(['git', '-C', self.local_dir, 'show', backup_ref + ':README.md']), b'staged intermediate\n')

    def test_refused_merge_restores_preparatory_index(self):
        tip = self._diverged_unit()
        subprocess.run(['git', '-C', self.local_dir, 'fetch', 'origin'], check=True, capture_output=True)
        # Use the published two-parent tip so adoption really is a fast-forward.
        receipt = publish_unit(self.local_dir, tip, 'origin', 'main', ['unit.txt'])
        before = (Path(self.local_dir) / '.git/index').read_bytes()
        from shared_git_publish import adopt as adoption
        real = adoption.git
        def refuse(repo, *args, **kwargs):
            if args[0] == 'merge' or (args[0] == 'read-tree' and '-n' not in args and kwargs.get('env') is None):
                return subprocess.CompletedProcess(args, 1, b'', b'named merge refusal')
            return real(repo, *args, **kwargs)
        with patch.object(adoption, 'git', side_effect=refuse):
            result = adoption.adopt_checkout(self.local_dir, 'main', receipt['published_tip'])
        self.assertEqual(result['status'], 'refused')
        self.assertIn('named merge refusal', result['reason'])
        self.assertEqual((Path(self.local_dir) / '.git/index').read_bytes(), before)
        self.assertEqual(resolve_commit_sha(self.local_dir, 'HEAD'), tip)

    def test_another_branch_is_left_alone(self):
        tip = self._diverged_unit()
        subprocess.run(["git", "-C", self.local_dir, "checkout", "-q", "-b", "side"], check=True)
        receipt = self._publish(tip)
        self.assertEqual(receipt["adoption"]["status"], "skipped")
        self.assertEqual(resolve_commit_sha(self.local_dir, "HEAD"), tip)



class BackupRefRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='retention-test-')
        self.repo = self.tmp.name
        self.now = int(time.time())
        self.run_git('init', '-q', '-b', 'main')
        self.tree = self.run_git('write-tree')
        self.head = self.commit('Published base', 100)
        self.run_git('update-ref', 'refs/remotes/origin/main', self.head)

    def tearDown(self):
        self.tmp.cleanup()

    def run_git(self, *args, env=None):
        return subprocess.run(['git', '-C', self.repo, *args], check=True,
                              capture_output=True, env=env).stdout.decode().strip()

    def commit(self, message, age_days, parent=None):
        stamp = f'{self.now - age_days * 86400} +0000'
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t',
                   GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        args = ['commit-tree', self.tree, '-m', message]
        if parent:
            args.extend(['-p', parent])
        return self.run_git(*args, env=env)

    def ref(self, kind, number, oid):
        name = f'refs/shared-git-publish/backup/{kind}-{number:032x}'
        self.run_git('update-ref', name, oid)
        return name

    def snapshot(self, number, age, message='Preserve index before publisher fast-forward'):
        return self.ref('index', number, self.commit(message, age, self.head))

    def refs(self):
        return set(self.run_git('for-each-ref', '--format=%(refname)').splitlines())

    def test_prune_backup_refs_drops_only_snapshots_past_retention(self):
        from shared_git_publish.adopt import prune_backup_refs
        old = self.snapshot(1, 8)
        fresh = self.snapshot(2, 1)
        head = self.ref('head', 3, self.head)
        self.assertEqual(prune_backup_refs(self.repo, now=self.now), [old])
        self.assertIn(fresh, self.refs())
        self.assertIn(head, self.refs())  # same parent still has a fresh snapshot

    def test_old_commit_head_survives_until_its_index_expires(self):
        from shared_git_publish.adopt import prune_backup_refs
        index = self.snapshot(1, 1)
        head = self.ref('head', 2, self.head)
        self.assertEqual(prune_backup_refs(self.repo, now=self.now), [])
        self.assertEqual(prune_backup_refs(self.repo, now=self.now + 7 * 86400), [index, head])

    def test_manual_unknown_and_retained_refs_are_never_pruned(self):
        from shared_git_publish.adopt import prune_backup_refs
        names = ['refs/shared-git-publish/backup/old', 'refs/shared-git-publish/backup/wia-index',
                 'refs/shared-git-publish/backup/manual-index-before-123', 'refs/shared-git-publish/backup/index-not-a-uuid',
                 'refs/shared-git-publish/stash/old', 'refs/shared-git-publish/backup/retained-stash-old']
        for name in names:
            self.run_git('update-ref', name, self.head)
        self.assertEqual(prune_backup_refs(self.repo, now=self.now), [])
        self.assertTrue(set(names) <= self.refs())

    def test_unknown_snapshot_format_protects_its_head(self):
        from shared_git_publish.adopt import prune_backup_refs
        index = self.snapshot(1, 30, 'Unknown preservation format')
        head = self.ref('head', 2, self.head)
        self.assertEqual(prune_backup_refs(self.repo, now=self.now), [])
        self.assertTrue({index, head} <= self.refs())

    def test_head_not_in_origin_is_retained(self):
        from shared_git_publish.adopt import prune_backup_refs
        local = self.commit('Unpublished local commit', 20, self.head)
        head = self.ref('head', 1, local)
        published = self.ref('head', 2, self.head)
        self.assertEqual(prune_backup_refs(self.repo, now=self.now), [published])
        self.assertIn(head, self.refs())

    def test_missing_origin_keeps_head(self):
        from shared_git_publish.adopt import prune_backup_refs
        self.run_git('update-ref', '-d', 'refs/remotes/origin/main')
        head = self.ref('head', 1, self.head)
        self.assertEqual(prune_backup_refs(self.repo, now=self.now), [])
        self.assertIn(head, self.refs())

    def test_exact_seven_day_boundary_keeps_snapshot(self):
        from shared_git_publish.adopt import prune_backup_refs
        index = self.snapshot(1, 7)
        self.assertEqual(prune_backup_refs(self.repo, now=self.now), [])
        self.assertEqual(prune_backup_refs(self.repo, now=self.now + 1), [index])

    def test_ref_changed_after_listing_is_not_deleted(self):
        from shared_git_publish import adopt as adoption
        index = self.snapshot(1, 8)
        replacement = self.commit('Concurrent preservation', 0, self.head)
        real = adoption.git
        def race(repo, *args, **kwargs):
            if (args[:3] == ('update-ref', '-d', index)
                    or (args[:2] == ('update-ref', '--stdin') and index.encode() in kwargs.get('data', b''))):
                self.run_git('update-ref', index, replacement)
            return real(repo, *args, **kwargs)
        with patch.object(adoption, 'git', side_effect=race):
            self.assertEqual(adoption.prune_backup_refs(self.repo, now=self.now), [])
        self.assertEqual(self.run_git('rev-parse', index), replacement)

    def test_both_shared_parent_indices_expire_before_head(self):
        from shared_git_publish.adopt import prune_backup_refs
        first = self.snapshot(1, 8)
        second = self.snapshot(2, 9)
        head = self.ref('head', 3, self.head)
        self.assertEqual(prune_backup_refs(self.repo, now=self.now), [first, second, head])

    def test_merge_noncommit_and_uppercase_names_are_retained(self):
        from shared_git_publish.adopt import prune_backup_refs
        second = self.commit('Second parent', 20)
        stamp = f'{self.now - 20 * 86400} +0000'
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t',
                   GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        merge = self.run_git('commit-tree', self.tree, '-p', self.head, '-p', second,
                             '-m', 'Preserve index before publisher fast-forward', env=env)
        index = self.ref('index', 1, merge)
        head = self.ref('head', 2, self.head)
        noncommit = self.ref('index', 3, self.tree)
        upper = 'refs/shared-git-publish/backup/index-' + 'A' * 32
        self.run_git('update-ref', upper, self.head)
        self.assertEqual(prune_backup_refs(self.repo, now=self.now), [])
        self.assertTrue({index, head, noncommit, upper} <= self.refs())

    def test_failed_index_delete_keeps_its_head(self):
        from shared_git_publish import adopt as adoption
        index = self.snapshot(1, 8)
        head = self.ref('head', 2, self.head)
        real = adoption.git
        def fail(repo, *args, **kwargs):
            if (args[:3] == ('update-ref', '-d', index)
                    or (args[:2] == ('update-ref', '--stdin') and index.encode() in kwargs.get('data', b''))):
                return subprocess.CompletedProcess(args, 1, b'', b'ref held')
            return real(repo, *args, **kwargs)
        with patch.object(adoption, 'git', side_effect=fail):
            self.assertEqual(adoption.prune_backup_refs(self.repo, now=self.now), [])
        self.assertTrue({index, head} <= self.refs())

    def test_head_cas_race_preserves_replacement(self):
        from shared_git_publish import adopt as adoption
        head = self.ref('head', 1, self.head)
        replacement = self.commit('Concurrent head', 0, self.head)
        real = adoption.git
        def race(repo, *args, **kwargs):
            if (args[:3] == ('update-ref', '-d', head)
                    or (args[:2] == ('update-ref', '--stdin') and head.encode() in kwargs.get('data', b''))):
                self.run_git('update-ref', head, replacement)
            return real(repo, *args, **kwargs)
        with patch.object(adoption, 'git', side_effect=race):
            self.assertEqual(adoption.prune_backup_refs(self.repo, now=self.now), [])
        self.assertEqual(self.run_git('rev-parse', head), replacement)

    def test_many_heads_use_one_merged_listing_not_per_head_git(self):
        from shared_git_publish import adopt as adoption
        for n in range(200):
            self.ref('head', n, self.head)
        with patch.object(adoption, 'git', wraps=adoption.git) as calls:
            removed = adoption.prune_backup_refs(self.repo, now=self.now)
        self.assertEqual(len(removed), 200)
        self.assertFalse(any(c.args[1:2] == ('merge-base',) for c in calls.call_args_list))
        self.assertEqual(sum('--merged' in c.args for c in calls.call_args_list), 1)

    def test_listing_failure_and_non_utf8_manual_subject_are_safe(self):
        from shared_git_publish import adopt as adoption
        with patch.object(adoption, 'git', return_value=subprocess.CompletedProcess([], 1, b'', b'failure')):
            self.assertEqual(adoption.prune_backup_refs(self.repo), [])
        real = adoption.git
        def invalid(repo, *args, **kwargs):
            if args[0] == 'for-each-ref' and '--merged' not in args:
                return subprocess.CompletedProcess(args, 0, b'refs/shared-git-publish/backup/manual\0oid\0commit\00\0bad-\xff\0\n', b'')
            return real(repo, *args, **kwargs)
        with patch.object(adoption, 'git', side_effect=invalid):
            self.assertEqual(adoption.prune_backup_refs(self.repo), [])

    def test_adoption_success_is_not_changed_by_prune_exception(self):
        from shared_git_publish import adopt as adoption
        for error in (RuntimeError('listing failed'), UnicodeError('invalid subject')):
            with patch.object(adoption, '_adopt', return_value={'status': 'adopted', 'head': self.head}), \
                 patch.object(adoption, 'prune_backup_refs', side_effect=error):
                self.assertEqual(adoption.adopt_checkout(self.repo, 'main', self.head)['status'], 'adopted')

    def test_frozen_target_used_without_origin_tracking_ref(self):
        from shared_git_publish.adopt import prune_backup_refs
        self.run_git('update-ref', '-d', 'refs/remotes/origin/main')
        head = self.ref('head', 1, self.head)
        self.assertEqual(prune_backup_refs(self.repo, now=self.now, target=self.head), [head])

    def test_cas_batch_is_bounded_and_falls_back_on_rejection(self):
        from shared_git_publish import adopt as adoption
        refs = [self.ref('head', n, self.head) for n in range(201)]
        real = adoption.git
        with patch.object(adoption, 'git', wraps=real) as calls:
            removed = adoption.delete_backup_batch(self.repo, [(r, self.head) for r in refs])
        batches = [c for c in calls.call_args_list if c.args[1:3] == ('update-ref', '--stdin')]
        self.assertEqual([len(c.kwargs['data'].splitlines()) for c in batches], [200, 1])
        self.assertEqual(removed, refs)
        one = self.ref('head', 300, self.head)
        two = self.ref('head', 301, self.head)
        def reject(repo, *args, **kwargs):
            if args[:2] == ('update-ref', '--stdin'):
                return subprocess.CompletedProcess(args, 1, b'', b'batch rejected')
            return real(repo, *args, **kwargs)
        with patch.object(adoption, 'git', side_effect=reject) as calls:
            self.assertEqual(adoption.delete_backup_batch(self.repo, [(one, self.head), (two, self.head)]), [one, two])
        self.assertEqual(sum(c.args[1:3] == ('update-ref', '-d') for c in calls.call_args_list), 2)

    def test_real_non_utf8_subject_does_not_disable_listing(self):
        from shared_git_publish import adopt as adoption
        import io
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
        commit = subprocess.check_output(['git', '-C', self.repo, '-c', 'i18n.commitEncoding=ISO-8859-1',
            'commit-tree', self.tree], input=b'bad-\xff\n', env=env).decode().strip()
        self.run_git('update-ref', 'refs/shared-git-publish/backup/manual-invalid-text', commit)
        with patch.object(adoption.sys, 'stderr', new_callable=io.StringIO) as output:
            self.assertEqual(adoption.prune_backup_refs(self.repo, now=self.now), [])
            self.assertEqual(output.getvalue(), '')
        self.assertIn('refs/shared-git-publish/backup/manual-invalid-text', self.refs())


class TestAdoptionTransactions(TestPublishUnitBase):
    def setUp(self):
        super().setUp()
        self.base = self._create_initial_commit()
        subprocess.run(['git', '-C', self.local_dir, 'branch', '-M', 'main'], check=True)
        self.target = self._create_peer_commit_on_remote('README.md', b'upstream bytes\n')
        subprocess.run(['git', '-C', self.local_dir, 'fetch', 'origin', 'main'], check=True, capture_output=True)
        self.index = (Path(self.local_dir) / '.git/index').resolve()
        self.original = self.index.read_bytes()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', self.local_dir, *args]).strip()

    def pending(self):
        return self.git('for-each-ref', '--format=%(refname)', 'refs/shared-git-publish/adoption/').splitlines()

    def prepare(self):
        from shared_git_publish import adopt as adoption
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / 'index'
            copy.write_bytes(self.original)
            env = dict(os.environ, GIT_INDEX_FILE=str(copy))
            subprocess.run(['git', '-C', self.local_dir, 'read-tree', self.target], env=env, check=True)
            prepared = copy.read_bytes()
        old = (b'100644', self.git('rev-parse', self.base + ':README.md'))
        target_entry = self.git('ls-tree', self.target, '--', 'README.md').split()
        new = (target_entry[0], target_entry[2])
        with adoption.publication_lock(self.local_dir):
            record = adoption.begin_transaction(Path(self.local_dir), 'main', self.base, self.target,
                self.index, self.original, prepared, [('README.md', old, new)], set())
        return record, prepared

    def test_created_at_and_complete_raw_payload_are_reachable(self):
        record, prepared = self.prepare()
        marker = json.loads(self.git('show', record['oid'] + ':manifest.json'))
        self.assertEqual(marker['created_at'], record['created_at'])
        self.assertEqual(marker['index_path'], str(self.index))
        self.assertEqual(self.git('ls-tree', '-r', '--name-only', record['oid']), b'manifest.json')
        self.assertEqual(self.git('rev-parse', record['index_ref'] + ':README.md'),
                         self.git('rev-parse', self.base + ':README.md'))
        self.assertIn('Adoption-Created:', self.git('show', '-s', '--format=%B', record['index_ref']).decode())
        from shared_git_publish import adopt as adoption
        before = set(self.git('for-each-ref', '--format=%(refname)').splitlines())
        self.assertEqual(adoption.prune_backup_refs(self.local_dir, now=time.time() + 10 * 86400, target=self.target), [])
        self.assertEqual(set(self.git('for-each-ref', '--format=%(refname)').splitlines()), before)

    def test_cap_diagnostic_excludes_manual_and_never_evicts_young(self):
        from shared_git_publish import adopt as adoption
        self.prepare()
        for n in range(4):
            self.git('update-ref', f'refs/shared-git-publish/backup/manual-{n}', self.base)
        before = self.git('for-each-ref', '--format=%(refname) %(objectname)')
        with patch.object(adoption, 'BACKUP_PAIR_LIMIT', 1):
            with self.assertRaisesRegex(RuntimeError, 'counted_index.*1.*excluded_retained_unknown.*4'):
                adoption.enforce_backup_cap(self.local_dir, self.target)
        self.assertEqual(self.git('for-each-ref', '--format=%(refname) %(objectname)'), before)
        self.assertEqual(self.index.read_bytes(), self.original)

    def test_legacy_prune_precedes_cap_enablement(self):
        from shared_git_publish import adopt as adoption
        old_env = dict(os.environ, GIT_AUTHOR_DATE='1000000000 +0000', GIT_COMMITTER_DATE='1000000000 +0000')
        tree = self.git('rev-parse', self.base + '^{tree}').decode()
        commit = subprocess.check_output(['git', '-C', self.local_dir, 'commit-tree', tree, '-p', self.base,
            '-m', 'Preserve index before publisher fast-forward'], env=old_env).decode().strip()
        self.git('update-ref', 'refs/shared-git-publish/backup/index-' + 'a'*32, commit)
        self.git('update-ref', 'refs/shared-git-publish/backup/head-' + 'b'*32, self.base)
        with patch.object(adoption, 'BACKUP_PAIR_LIMIT', 1):
            adoption.enforce_backup_cap(self.local_dir, self.target)
        self.assertEqual(self.git('for-each-ref', '--format=%(refname)', 'refs/shared-git-publish/backup/'), b'')

    def test_pending_original_state_clears_without_mutation(self):
        from shared_git_publish import adopt as adoption
        self.prepare()
        before = self.index.read_bytes()
        with adoption.publication_lock(self.local_dir):
            self.assertEqual(adoption.recover_pending(Path(self.local_dir), 'main'), [])
        self.assertEqual(self.index.read_bytes(), before)
        self.assertEqual(self.pending(), [])

    def test_pending_restores_prepared_index_and_work_preserving_other_staging(self):
        from shared_git_publish import adopt as adoption
        record, prepared = self.prepare()
        self.index.write_bytes(prepared)
        (Path(self.local_dir) / 'README.md').write_bytes(b'upstream bytes\n')
        (Path(self.local_dir) / 'other.txt').write_bytes(b'another agent\n')
        self.git('add', '--', 'other.txt')
        staged = self.git('ls-files', '-s', '--', 'other.txt')
        with adoption.publication_lock(self.local_dir):
            self.assertEqual(adoption.recover_pending(Path(self.local_dir), 'main'), [])
        self.assertEqual(self.git('rev-parse', 'HEAD').decode(), self.base)
        self.assertEqual((Path(self.local_dir) / 'README.md').read_bytes(), b'# Init Project\n')
        self.assertEqual(self.git('ls-files', '-s', '--', 'other.txt'), staged)
        self.assertEqual(self.pending(), [])
        self.assertEqual(adoption.adopt_checkout(self.local_dir, 'main', self.target)['status'], 'adopted')

    def test_pending_completed_state_clears_on_next_invocation(self):
        from shared_git_publish import adopt as adoption
        record, prepared = self.prepare()
        self.index.write_bytes(prepared)
        (Path(self.local_dir) / 'README.md').write_bytes(b'upstream bytes\n')
        self.git('update-ref', 'refs/heads/main', self.target, self.base)
        receipt = adoption.adopt_checkout(self.local_dir, 'main', self.target)
        self.assertEqual(receipt['status'], 'adopted')
        self.assertEqual(self.pending(), [])

    def test_unexpected_path_blocks_overlap_only_and_reports_owner(self):
        from shared_git_publish import adopt as adoption
        record, prepared = self.prepare()
        self.index.write_bytes(prepared)
        (Path(self.local_dir) / 'README.md').write_bytes(b'new unproved agent work\n')
        refused = adoption.adopt_checkout(self.local_dir, 'main', self.target)
        self.assertEqual(refused['status'], 'refused')
        self.assertEqual(refused['pending_adoptions'][0]['id'], record['id'])
        self.assertIn('clear_owner', refused['pending_adoptions'][0])
        # Create a published target changing only another path from H.
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ, GIT_INDEX_FILE=str(Path(td)/'index'))
            subprocess.run(['git', '-C', self.local_dir, 'read-tree', self.base], env=env, check=True)
            blob = subprocess.check_output(['git', '-C', self.local_dir, 'hash-object', '-w', '--stdin'], input=b'disjoint\n').decode().strip()
            subprocess.run(['git', '-C', self.local_dir, 'update-index', '--add', '--cacheinfo', '100644,'+blob+',other.txt'], env=env, check=True)
            tree = subprocess.check_output(['git', '-C', self.local_dir, 'write-tree'], env=env).decode().strip()
            disjoint = self.git('commit-tree', tree, '-p', self.base, '-m', 'Other published path').decode()
        adopted = adoption.adopt_checkout(self.local_dir, 'main', disjoint)
        self.assertEqual(adopted['status'], 'adopted', adopted)
        self.assertEqual((Path(self.local_dir) / 'README.md').read_bytes(), b'new unproved agent work\n')
        self.assertEqual(len(self.pending()), 1)

    def test_publisher_held_and_standalone_lock_entry_orders(self):
        from shared_git_publish import adopt as adoption
        import fcntl
        real = fcntl.flock
        with adoption.publication_lock(self.local_dir) as held:
            with patch.object(adoption.fcntl, 'flock', wraps=real) as calls:
                result = adoption.adopt_checkout(self.local_dir, 'main', self.base, _publish_lock=held)
                self.assertEqual(result['status'], 'adopted')
                self.assertEqual(calls.call_count, 0)
        with patch.object(adoption.fcntl, 'flock', wraps=real) as calls:
            self.assertEqual(adoption.adopt_checkout(self.local_dir, 'main', self.base)['status'], 'adopted')
            self.assertEqual(calls.call_count, 1)

    def test_termination_before_merge_recovers_on_next_real_run(self):
        from shared_git_publish import adopt as adoption
        class Crash(BaseException): pass
        (Path(self.local_dir) / 'README.md').write_bytes(b'upstream bytes\n')
        real = adoption.git
        def stop(repo, *args, **kwargs):
            if args[0] == 'merge':
                raise Crash()
            return real(repo, *args, **kwargs)
        with patch.object(adoption, 'git', side_effect=stop):
            with self.assertRaises(Crash):
                adoption.adopt_checkout(self.local_dir, 'main', self.target)
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(adoption.adopt_checkout(self.local_dir, 'main', self.target)['status'], 'adopted')
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.git('diff', '--cached', '--name-only'), b'')

    def test_termination_after_worktree_before_head_ref_recovers(self):
        from shared_git_publish import adopt as adoption
        class Crash(BaseException): pass
        target = self._create_peer_commit_on_remote('new.txt', b'new bytes\n')
        self.git('fetch', 'origin', 'main')
        (Path(self.local_dir) / 'new.txt').write_bytes(b'new bytes\n')
        real = adoption.git
        def stop(repo, *args, **kwargs):
            if args[:2] == ('update-ref', 'refs/heads/main'):
                raise Crash()
            return real(repo, *args, **kwargs)
        with patch.object(adoption, 'git', side_effect=stop):
            with self.assertRaises(Crash):
                adoption.adopt_checkout(self.local_dir, 'main', target)
        self.assertEqual(self.git('rev-parse', 'HEAD').decode(), self.base)
        self.assertEqual(len(self.pending()), 1)
        receipt = adoption.adopt_checkout(self.local_dir, 'main', target)
        self.assertEqual(receipt['status'], 'adopted', receipt)
        self.assertEqual(self.pending(), [])

    def crash_child(self, stage, target):
        code = r"""
import os, sys
from pathlib import Path
from shared_git_publish import adopt as a
repo, target, stage = sys.argv[1:]
real = a.git
def stop(root, *args, **kw):
    if stage == 'before-merge' and args[0] == 'merge': os._exit(92)
    if args[:2] == ('update-ref', 'refs/heads/main') and stage == 'before-head': os._exit(92)
    if stage == 'partial-work' and args[:3] == ('read-tree', '-m', '-u') and '-n' not in args:
        (Path(repo)/'README.md').write_bytes(real(repo, 'show', target+':README.md').stdout)
        os._exit(92)
    result = real(root, *args, **kw)
    if args[:2] == ('update-ref', 'refs/heads/main') and stage == 'after-head': os._exit(92)
    return result
a.git = stop
print(a.adopt_checkout(repo, 'main', target))
"""
        return subprocess.run([sys.executable, '-c', code, self.local_dir, target, stage],
                              capture_output=True, text=True, timeout=20)

    def test_process_exit_after_index_replacement_recovers(self):
        from shared_git_publish import adopt as adoption
        (Path(self.local_dir)/'README.md').write_bytes(b'upstream bytes\n')
        result = self.crash_child('before-merge', self.target)
        self.assertEqual(result.returncode, 92, result.stdout + result.stderr)
        self.assertEqual(len(self.pending()), 1)
        receipt = adoption.adopt_checkout(self.local_dir, 'main', self.target)
        self.assertEqual(receipt['status'], 'adopted', receipt)
        self.assertEqual(self.pending(), [])

    def test_process_exit_during_worktree_and_after_head_recovers(self):
        from shared_git_publish import adopt as adoption
        target = self._create_peer_commit_on_remote('new.txt', b'new bytes\n')
        self.git('fetch', 'origin', 'main')
        (Path(self.local_dir)/'new.txt').write_bytes(b'new bytes\n')
        result = self.crash_child('partial-work', target)
        self.assertEqual(result.returncode, 92, result.stdout + result.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD').decode(), self.base)
        self.assertEqual((Path(self.local_dir)/'README.md').read_bytes(), b'upstream bytes\n')
        receipt = adoption.adopt_checkout(self.local_dir, 'main', target)
        self.assertEqual(receipt['status'], 'adopted', receipt)
        self.assertEqual(self.pending(), [])
        # Separate real process exits after successful update-ref but before cleanup.
        next_target = self._create_peer_commit_on_remote('next.txt', b'next bytes\n')
        self.git('fetch', 'origin', 'main')
        (Path(self.local_dir)/'next.txt').write_bytes(b'next bytes\n')
        result = self.crash_child('after-head', next_target)
        self.assertEqual(result.returncode, 92, result.stdout + result.stderr)
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(adoption.adopt_checkout(self.local_dir, 'main', next_target)['status'], 'adopted')
        self.assertEqual(self.pending(), [])

    def test_no_ref_or_checkout_mutation_when_cap_has_no_slots(self):
        from shared_git_publish import adopt as adoption
        readme=Path(self.local_dir)/'README.md'
        readme.write_bytes(b'upstream bytes\n')  # Requires an overlapping index plan.
        before = self.git('for-each-ref', '--format=%(refname) %(objectname)')
        with patch.object(adoption, 'BACKUP_PAIR_LIMIT', 0):
            receipt = adoption.adopt_checkout(self.local_dir, 'main', self.target)
        self.assertEqual(receipt['status'], 'refused')
        self.assertIn('required_pairs', receipt['reason'])
        self.assertEqual(self.git('for-each-ref', '--format=%(refname) %(objectname)'), before)
        self.assertEqual(self.index.read_bytes(), self.original)
        self.assertEqual(self.pending(), [])

    def test_creation_record_not_old_commit_date_controls_completed_pair_age(self):
        from shared_git_publish import adopt as adoption
        with patch.dict(os.environ, {'GIT_AUTHOR_DATE':'1000000000 +0000','GIT_COMMITTER_DATE':'1000000000 +0000'}):
            record, prepared = self.prepare()
        self.index.write_bytes(prepared)
        (Path(self.local_dir)/'README.md').write_bytes(b'upstream bytes\n')
        self.git('update-ref', 'refs/heads/main', self.target, self.base)
        adoption.adopt_checkout(self.local_dir, 'main', self.target)
        self.assertEqual(self.pending(), [])
        self.assertEqual(adoption.prune_backup_refs(self.local_dir, target=self.target), [])
        self.assertEqual(adoption.prune_backup_refs(self.local_dir, now=record['created_at']+8*86400,
            target=self.target), [record['index_ref'], record['head_ref']])

    def test_genuine_local_publication_does_not_retake_adopter_lock(self):
        from shared_git_publish import adopt as adoption
        import fcntl
        (Path(self.local_dir)/'unit.txt').write_bytes(b'local committed work\n')
        self.git('add', '--', 'unit.txt')
        self.git('commit', '-m', 'Local unit')
        real = fcntl.flock
        def nonblocking(fd, operation):
            return real(fd, operation | fcntl.LOCK_NB)
        with patch.object(adoption.fcntl, 'flock', side_effect=nonblocking) as calls:
            receipt = adoption.adopt_checkout(self.local_dir, 'main', self.target)
        self.assertEqual(receipt['status'], 'adopted', receipt)
        self.assertIn('published_local_tip', receipt)
        self.assertEqual(calls.call_count, 1)

    def test_bad_manifest_is_quarantined_without_blocking_valid_recovery(self):
        from shared_git_publish import adopt as adoption
        self.prepare()
        blob = subprocess.check_output(['git','-C',self.local_dir,'hash-object','-w','--stdin'],
            input=b'{invalid json').decode().strip()
        tree = subprocess.check_output(['git','-C',self.local_dir,'mktree'],
            input=f'100644 blob {blob}\tmanifest.json\n'.encode()).decode().strip()
        oid = self.git('commit-tree',tree,'-p',self.base,'-m','Bad manifest').decode()
        marker = adoption.pending_prefix() + 'f'*32
        self.git('update-ref',marker,oid)
        receipt = adoption.adopt_checkout(self.local_dir,'main',self.target)
        self.assertEqual(receipt['status'],'adopted',receipt)
        self.assertEqual(self.pending(),[])
        quarantined = [e for e in receipt['pending_events'] if e['status']=='quarantined']
        self.assertEqual(len(quarantined),1)
        self.assertEqual(self.git('rev-parse',quarantined[0]['retained_ref']).decode(),oid)

    def test_recovery_preserves_original_file_permissions(self):
        from shared_git_publish import adopt as adoption
        readme = Path(self.local_dir)/'README.md'
        readme.chmod(0o600)
        record, prepared = self.prepare()
        self.index.write_bytes(prepared)
        readme.write_bytes(b'upstream bytes\n')
        readme.chmod(0o644)
        with adoption.publication_lock(self.local_dir):
            self.assertEqual(adoption.recover_pending(Path(self.local_dir), 'main'), [])
        self.assertEqual(readme.stat().st_mode & 0o777, 0o600)
        self.assertEqual(readme.read_bytes(), b'# Init Project\n')

    def test_process_exit_during_recovery_resumes_conditionally(self):
        from shared_git_publish import adopt as adoption
        record, prepared = self.prepare()
        self.index.write_bytes(prepared)
        (Path(self.local_dir)/'README.md').write_bytes(b'upstream bytes\n')
        code = r"""
import os,sys
from shared_git_publish import adopt as a
real=a.restore_raw
def stop(*args,**kwargs):
    real(*args,**kwargs)
    os._exit(93)
a.restore_raw=stop
a.adopt_checkout(sys.argv[1],'main',sys.argv[2])
"""
        result = subprocess.run([sys.executable, '-c', code, self.local_dir, self.target],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 93, result.stdout+result.stderr)
        lock = Path(str(self.index)+'.lock')
        self.assertTrue(lock.exists())
        # Controlled passage beyond the documented stale-lock age, no real sleep.
        os.utime(lock, (time.time()-31, time.time()-31))
        receipt = adoption.adopt_checkout(self.local_dir, 'main', self.target)
        self.assertEqual(receipt['status'], 'adopted', receipt)
        self.assertEqual(self.pending(), [])
        self.assertFalse(lock.exists())

    def test_recorded_prepared_index_version_is_recoverable(self):
        from shared_git_publish import adopt as adoption
        third = subprocess.check_output(
            ['git', '-C', self.local_dir, 'hash-object', '-w', '--stdin'], input=b'known prepared version\n').decode().strip()
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td)/'index'
            copy.write_bytes(self.original)
            env = dict(os.environ, GIT_INDEX_FILE=str(copy))
            subprocess.run(['git', '-C', self.local_dir, 'update-index', '--cacheinfo',
                '100644,'+third+',README.md'], env=env, check=True)
            prepared = copy.read_bytes()
        old = (b'100644', self.git('rev-parse', self.base+':README.md'))
        new = (b'100644', self.git('rev-parse', self.target+':README.md'))
        with adoption.publication_lock(self.local_dir):
            record = adoption.begin_transaction(Path(self.local_dir), 'main', self.base, self.target,
                self.index, self.original, prepared, [('README.md', old, new)], set())
        self.index.write_bytes(prepared)
        with adoption.publication_lock(self.local_dir):
            self.assertEqual(adoption.recover_pending(Path(self.local_dir), 'main'), [])
        self.assertEqual(self.git('rev-parse', ':README.md'), old[1])
        self.assertEqual(self.pending(), [])

    def test_pending_overlap_includes_deleted_side_of_rename(self):
        from shared_git_publish import adopt as adoption
        record, prepared = self.prepare()
        self.index.write_bytes(prepared)
        readme = Path(self.local_dir)/'README.md'
        readme.write_bytes(b'unexpected agent work\n')
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ, GIT_INDEX_FILE=str(Path(td)/'index'))
            subprocess.run(['git', '-C', self.local_dir, 'read-tree', self.base], env=env, check=True)
            blob = self.git('rev-parse', self.base+':README.md').decode()
            subprocess.run(['git', '-C', self.local_dir, 'update-index', '--force-remove', 'README.md'], env=env, check=True)
            subprocess.run(['git', '-C', self.local_dir, 'update-index', '--add', '--cacheinfo',
                '100644,'+blob+',renamed.md'], env=env, check=True)
            tree = subprocess.check_output(['git', '-C', self.local_dir, 'write-tree'], env=env).decode().strip()
            renamed = self.git('commit-tree', tree, '-p', self.base, '-m', 'Rename').decode()
        receipt = adoption.adopt_checkout(self.local_dir, 'main', renamed)
        self.assertEqual(receipt['status'], 'refused')
        self.assertIn('pending_adoption', receipt['reason'])
        self.assertEqual(readme.read_bytes(), b'unexpected agent work\n')
        self.assertEqual(len(self.pending()), 1)

    def test_symlink_transition_recovery_restores_raw_file(self):
        from shared_git_publish import adopt as adoption
        blob = subprocess.check_output(['git','-C',self.local_dir,'hash-object','-w','--stdin'],
            input=b'link-target').decode().strip()
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ, GIT_INDEX_FILE=str(Path(td)/'index'))
            subprocess.run(['git','-C',self.local_dir,'read-tree',self.base], env=env, check=True)
            subprocess.run(['git','-C',self.local_dir,'update-index','--cacheinfo',
                '120000,'+blob+',README.md'], env=env, check=True)
            tree = subprocess.check_output(['git','-C',self.local_dir,'write-tree'], env=env).decode().strip()
            self.target = self.git('commit-tree',tree,'-p',self.base,'-m','Symlink').decode()
        record, prepared = self.prepare()
        self.index.write_bytes(prepared)
        readme = Path(self.local_dir)/'README.md'
        readme.unlink()
        readme.symlink_to('link-target')
        with adoption.publication_lock(self.local_dir):
            self.assertEqual(adoption.recover_pending(Path(self.local_dir),'main'), [])
        self.assertFalse(readme.is_symlink())
        self.assertEqual(readme.read_bytes(), b'# Init Project\n')
        self.assertEqual(self.pending(), [])

    def test_filtered_crlf_working_payload_is_restored_byte_exactly(self):
        from shared_git_publish import adopt as adoption
        self.git('config','core.autocrlf','true')
        readme = Path(self.local_dir)/'README.md'
        readme.write_bytes(b'# Init Project\r\n')
        record, prepared = self.prepare()
        self.index.write_bytes(prepared)
        readme.write_bytes(b'upstream bytes\r\n')
        with adoption.publication_lock(self.local_dir):
            self.assertEqual(adoption.recover_pending(Path(self.local_dir),'main'), [])
        self.assertEqual(readme.read_bytes(), b'# Init Project\r\n')
        self.assertEqual(self.pending(), [])

    def test_clean_fast_forward_has_no_transaction_refs_or_payload(self):
        from shared_git_publish import adopt as adoption
        with patch.object(adoption,'begin_transaction',wraps=adoption.begin_transaction) as begin, \
             patch.object(adoption,'target_fingerprint',wraps=adoption.target_fingerprint) as target, \
             patch.object(adoption,'BACKUP_PAIR_LIMIT',0):
            receipt = adoption.adopt_checkout(self.local_dir,'main',self.target)
        self.assertEqual(receipt['status'],'adopted',receipt)
        begin.assert_not_called()
        target.assert_not_called()
        self.assertEqual(self.git('for-each-ref','--format=%(refname)','refs/shared-git-publish/backup/','refs/shared-git-publish/'),b'')

    def test_only_novel_before_bytes_are_pinned_by_oid(self):
        from shared_git_publish import adopt as adoption
        readme = Path(self.local_dir)/'README.md'
        readme.write_bytes(b'raw unpublished bytes\r\n')
        with patch.object(adoption,'git',wraps=adoption.git) as calls:
            record, prepared = self.prepare()
        self.assertEqual(self.git('ls-tree','-r','--name-only',record['oid']),b'before-0\nmanifest.json')
        self.assertEqual(subprocess.check_output(['git','-C',self.local_dir,'show',record['oid']+':before-0']),readme.read_bytes())
        writes = [c for c in calls.call_args_list if c.args[1:2]==('hash-object',) and '-w' in c.args]
        self.assertEqual(len(writes),2)
        self.assertFalse(any(c.args[1:3]==('cat-file','blob') for c in calls.call_args_list))

    def test_unapplied_marker_clears_after_owner_edits_again(self):
        from shared_git_publish import adopt as adoption
        record, prepared = self.prepare()
        readme = Path(self.local_dir)/'README.md'
        readme.write_bytes(b'new owner edit after refusal\n')
        events=[]
        with adoption.publication_lock(self.local_dir):
            self.assertEqual(adoption.recover_pending(Path(self.local_dir),'main',events),[])
        self.assertEqual(self.pending(),[])
        self.assertEqual(readme.read_bytes(),b'new owner edit after refusal\n')
        self.assertEqual(self.index.read_bytes(),self.original)
        self.assertEqual(events[0]['status'],'cleared_unapplied')

    def test_symlinked_checkout_index_path_resolves_to_same_marker(self):
        from shared_git_publish import adopt as adoption
        alias = Path(self.test_dir)/'alias'
        alias.symlink_to(self.local_dir,target_is_directory=True)
        record, prepared = self.prepare()
        data=json.loads(self.git('show',record['oid']+':manifest.json'))
        data['index_path']=str(alias/'.git/index')
        blob=subprocess.check_output(['git','-C',self.local_dir,'hash-object','-w','--stdin'],
            input=json.dumps(data).encode()).decode().strip()
        tree=subprocess.check_output(['git','-C',self.local_dir,'mktree'],
            input=f'100644 blob {blob}\tmanifest.json\n'.encode()).decode().strip()
        oid=self.git('commit-tree',tree,'-p',self.base,'-m','Alias path marker').decode()
        self.git('update-ref',record['ref'],oid,record['oid'])
        with adoption.publication_lock(alias):
            self.assertEqual(adoption.recover_pending(alias,'main'),[])
        self.assertEqual(self.pending(),[])

    def test_cap_headroom_at_measured_peak_for_full_retention_window(self):
        from shared_git_publish import adopt as adoption
        peak_refs_per_day, retention_days = 634, 7
        peak_week_pairs=(peak_refs_per_day*retention_days+1)//2
        rows=[{'created_at':1,'parents':[self.base]} for _ in range(peak_week_pairs)]
        heads=[('refs/shared-git-publish/backup/head-'+f'{n:032x}',self.base) for n in range(peak_week_pairs)]
        with patch.object(adoption,'prune_backup_refs',return_value=[]), \
             patch.object(adoption,'backup_inventory',return_value=(rows,heads,set(heads),15)):
            basis=adoption.enforce_backup_cap(self.local_dir,self.target)
        self.assertEqual(basis['counted_pairs'],2219)
        self.assertEqual(basis['remaining_pairs'],5973)
        self.assertEqual(basis['limit_pairs'],8192)
        self.assertGreater(basis['remaining_pairs'],peak_week_pairs*2)

    def test_bounded_manual_quarantine_retains_payload_and_refuses_changed_oid(self):
        from shared_git_publish import adopt as adoption
        record, prepared=self.prepare()
        before=self.index.read_bytes()
        with adoption.publication_lock(self.local_dir):
            destination=adoption.quarantine_pending(self.local_dir,record['ref'],record['oid'])
        self.assertEqual(self.git('rev-parse',destination).decode(),record['oid'])
        self.assertEqual(self.pending(),[])
        self.assertEqual(self.index.read_bytes(),before)
        self.git('update-ref',record['ref'],self.base)
        with adoption.publication_lock(self.local_dir),self.assertRaises(RuntimeError):
            adoption.quarantine_pending(self.local_dir,record['ref'],record['oid'])
        self.assertEqual(self.git('rev-parse',record['ref']).decode(),self.base)

    def test_transient_manifest_read_failure_never_quarantines_marker(self):
        from shared_git_publish import adopt as adoption
        record, prepared=self.prepare()
        real=adoption.git
        before=self.index.read_bytes()
        for error in (RuntimeError('transient git show failure'), OSError('temporary read failure'),
                      subprocess.SubprocessError('manifest reader interrupted')):
            with self.subTest(error=type(error).__name__):
                def fail(repo,*args,**kwargs):
                    if args[:2]==('show',record['oid']+':manifest.json'):
                        raise error
                    return real(repo,*args,**kwargs)
                with patch.object(adoption,'git',side_effect=fail), \
                     patch.object(adoption,'quarantine_pending',wraps=adoption.quarantine_pending) as quarantine:
                    receipt=adoption.adopt_checkout(self.local_dir,'main',self.target)
                self.assertEqual(receipt['status'],'refused')
                self.assertIn(str(error),receipt['reason'])
                quarantine.assert_not_called()
                self.assertEqual(self.git('rev-parse',record['ref']).decode(),record['oid'])
                self.assertEqual(self.index.read_bytes(),before)
                self.assertEqual(self.git('for-each-ref','--format=%(refname)',
                    'refs/shared-git-publish/quarantined-adoption/'),b'')


class TestHistoricalLeftovers(TestPublishUnitBase):
    def setUp(self):
        super().setUp()
        self._create_initial_commit()
        self.cmd('branch','-M','main')
        self.make_history('runner.py')

    def cmd(self,*args):
        return subprocess.check_output(['git','-C',self.local_dir,*args],stderr=subprocess.DEVNULL).strip()

    def make_history(self,path):
        self.path=path
        self.versions=[f'VALUE = {n}\n'.encode() for n in range(4)]
        commits=[]
        for n,data in enumerate(self.versions):
            (Path(self.local_dir)/path).write_bytes(data)
            self.cmd('add','--',path)
            self.cmd('commit','-m',f'Published version {n} of {path}')
            commits.append(self.cmd('rev-parse','HEAD').decode())
        self.base,self.target=commits[2:]
        self.blobs=[self.cmd('rev-parse',c+':'+path).decode() for c in commits]
        self.cmd('update-ref','refs/remotes/origin/main',self.target)
        self.cmd('update-ref','refs/heads/main',self.base,self.target)
        self.cmd('read-tree',self.base)
        (Path(self.local_dir)/path).write_bytes(self.versions[2])

    def stage(self,n):
        self.cmd('update-index','--cacheinfo','100644,'+self.blobs[n]+','+self.path)

    def case(self,index_version,working_version):
        from shared_git_publish import adopt as adoption
        self.stage(index_version)
        file=Path(self.local_dir)/self.path
        file.write_bytes(self.versions[working_version])
        result=adoption.adopt_checkout(self.local_dir,'main',self.target)
        self.assertEqual(result['status'],'adopted',result)
        # Bytes already in history carry no work: the file advances to the target version.
        self.assertEqual(file.read_bytes(),self.versions[3])
        self.assertEqual(self.cmd('rev-parse',':'+self.path).decode(),self.blobs[3])
        if result.get('index_backup_ref'):
            self.assertEqual(self.cmd('rev-parse',result['index_backup_ref']+':'+self.path).decode(),self.blobs[index_version])
        else:
            # A stale stage with nothing else to preserve needs no recovery payload.
            self.assertEqual(index_version,working_version)
        return result

    def test_published_index_and_old_work_are_independent(self):
        result=self.case(3,0)
        self.assertEqual(result['advanced_historical'],1)
        self.assertIn('runner.py',result['historical_paths'])

    def test_before_index_and_old_work_advance(self):
        self.case(2,0)

    def test_staged_and_working_same_older_blob_advance(self):
        result=self.case(0,0)
        self.assertEqual(result['advanced_historical_index'],1)

    def test_two_different_older_index_and_work_versions_advance(self):
        self.case(0,1)

    def test_historical_staged_base_with_head_work_advances(self):
        self.case(0,2)

    def test_historical_stage_with_target_work_normalizes_only_stage(self):
        result=self.case(0,3)
        self.assertEqual(result['advanced_historical_index'],1)
        self.assertEqual(result['advanced_historical'],0)

    def test_published_index_with_head_work_advances(self):
        self.case(3,2)

    def test_unproved_work_conflicting_with_published_stage_is_preserved_on_refusal(self):
        from shared_git_publish import adopt as adoption
        self.stage(0)
        file=Path(self.local_dir)/self.path
        file.write_bytes(b'VALUE = 99\n')
        index=(Path(self.local_dir)/'.git/index').read_bytes()
        result=adoption.adopt_checkout(self.local_dir,'main',self.target)
        self.assertEqual(result['status'],'refused',result)
        self.assertEqual(result['blocking'],['runner.py'])
        self.assertEqual(file.read_bytes(),b'VALUE = 99\n')
        self.assertEqual((Path(self.local_dir)/'.git/index').read_bytes(),index)

    def test_runtime_data_extensions_keep_history_refusal(self):
        from shared_git_publish import adopt as adoption
        for suffix in ('.json','.jsonl','.db','.sqlite','.sqlite3'):
            with self.subTest(suffix=suffix):
                self.make_history('state'+suffix)
                self.stage(0)
                file=Path(self.local_dir)/self.path
                file.write_bytes(self.versions[0])
                before=self.cmd('ls-files','-s','--',self.path)
                result=adoption.adopt_checkout(self.local_dir,'main',self.target)
                self.assertEqual(result['status'],'refused',result)
                self.assertEqual(file.read_bytes(),self.versions[0])
                self.assertEqual(self.cmd('ls-files','-s','--',self.path),before)

    def test_clean_filter_equivalence_is_not_raw_history_proof(self):
        """CRLF working bytes are not history-proven; they stay on disk as a modification."""
        from shared_git_publish import adopt as adoption
        self.cmd('config','core.autocrlf','input')
        self.stage(0)
        file=Path(self.local_dir)/self.path
        raw=self.versions[0].replace(b'\n',b'\r\n')
        file.write_bytes(raw)
        result=adoption.adopt_checkout(self.local_dir,'main',self.target)
        self.assertEqual(result['status'],'adopted',result)
        self.assertEqual(file.read_bytes(),raw)
        self.assertEqual(result['reset_stale_paths'],['runner.py'])

    def test_loaded_service_code_stays_and_existing_result_carries_notice(self):
        self.stage(3)
        file=Path(self.local_dir)/self.path
        file.write_bytes(self.versions[0])
        loaded={}
        exec(compile(file.read_bytes(),self.path,'exec'),loaded)
        from shared_git_publish import adopt as adoption
        result=adoption.adopt_checkout(self.local_dir,'main',self.target)
        self.assertEqual(result['status'],'adopted',result)
        self.assertEqual(loaded['VALUE'],0)
        self.assertEqual(file.read_bytes(),self.versions[3])
        self.assertIn('runner.py',result['working_code_notice'])
        self.assertIn('owning session',result['working_code_notice'])

    def test_local_only_ref_does_not_prove_published_staging(self):
        from shared_git_publish import adopt as adoption
        file=Path(self.local_dir)/self.path
        file.write_bytes(b'VALUE = 99\n')
        self.cmd('add','--',self.path)
        tree=self.cmd('write-tree').decode()
        private=self.cmd('commit-tree',tree,'-p',self.base,'-m','Local review only').decode()
        self.cmd('update-ref','refs/review/private',private)
        result=adoption.adopt_checkout(self.local_dir,'main',self.target)
        self.assertEqual(result['status'],'refused')
        self.assertEqual(file.read_bytes(),b'VALUE = 99\n')

    def test_extended_data_suffixes_wrappers_and_sqlite_sidecars_refuse(self):
        from shared_git_publish import adopt as adoption
        for name in ('state.csv','state.ndjson','state.parquet','state.pkl','data.db-wal',
                     'data.sqlite3-shm','data.db-journal','report.json.tmp','report.json.bak',
                     'data.db-wal.bak','state.csv.tmp','STATE.PARQUET.bak','report.json '):
            with self.subTest(name=name):
                self.make_history(name)
                self.stage(0)
                file=Path(self.local_dir)/self.path
                file.write_bytes(self.versions[0])
                before=self.cmd('ls-files','-s','--',self.path)
                result=adoption.adopt_checkout(self.local_dir,'main',self.target)
                self.assertEqual(result['status'],'refused',result)
                self.assertEqual(file.read_bytes(),self.versions[0])
                self.assertEqual(self.cmd('ls-files','-s','--',self.path),before)

    def test_noncode_historical_paths_are_reported_and_code_is_marked(self):
        self.make_history('guide.md')
        result=self.case(0,1)
        self.assertIn('guide.md [disk, mode=100644]',result['working_code_notice'])
        self.assertNotIn('[code',result['working_code_notice'])

    def test_ruby_source_is_marked_as_code_in_existing_notice(self):
        self.make_history('job.rb')
        result=self.case(3,0)
        self.assertIn('job.rb [code, disk, mode=100644]',result['working_code_notice'])


class TestExplicitObjectPreparation(TestPublishUnitBase):
    def setUp(self):
        super().setUp()
        self.base=self._create_initial_commit()
        self.g('branch','-M','main')
        (Path(self.local_dir)/'README.md').write_bytes(b'target bytes\n')
        self.g('add','--','README.md');self.g('commit','-m','Published target')
        self.target=self.g('rev-parse','HEAD').decode()
        self.target_blob=self.g('rev-parse',self.target+':README.md').decode()
        self.old_blob=self.g('rev-parse',self.base+':README.md').decode()
        self.g('push','origin','HEAD:refs/heads/main')
        subprocess.run(['git','-C',self.bare_dir,'config','uploadpack.allowAnySHA1InWant','true'],check=True)
        subprocess.run(['git','-C',self.bare_dir,'config','uploadpack.allowFilter','true'],check=True)
        self.g('config','remote.origin.promisor','true')
        self.g('config','remote.origin.partialclonefilter','blob:none')
        self.g('update-ref','refs/heads/main',self.base,self.target)
        self.g('read-tree',self.base)
        (Path(self.local_dir)/'README.md').write_bytes(b'# Init Project\n')
        self.g('update-index','--refresh','--','README.md')
        self.before=(Path(self.local_dir)/'.git/index').read_bytes()

    def g(self,*args):
        return subprocess.check_output(['git','-C',self.local_dir,*args],stderr=subprocess.DEVNULL).strip()

    def remove_loose(self,oid):
        path=Path(self.local_dir)/'.git/objects'/oid[:2]/oid[2:]
        self.assertTrue(path.is_file(),str(path))
        path.unlink()  # Synthetic object already present on the synthetic bare remote.

    def test_missing_target_blob_is_explicitly_fetched_before_index_lock(self):
        from shared_git_publish import adopt as adoption
        self.remove_loose(self.target_blob)
        real=adoption.git;requests=[]
        def inspect(repo,*args,**kwargs):
            if 'fetch' in args:
                self.assertFalse((Path(self.local_dir)/'.git/index.lock').exists())
                self.assertIn('--no-write-fetch-head',args)
                self.assertIn('--no-tags',args)
                requests.extend(args[args.index('origin')+1:])
            return real(repo,*args,**kwargs)
        with patch.object(adoption,'git',side_effect=inspect):
            receipt=adoption.adopt_checkout(self.local_dir,'main',self.target)
        self.assertEqual(receipt['status'],'adopted',receipt)
        self.assertIn(self.target_blob,requests)
        self.assertEqual((Path(self.local_dir)/'README.md').read_bytes(),b'target bytes\n')
        self.assertFalse((Path(self.local_dir)/'.git/FETCH_HEAD').exists())

    def test_missing_before_blob_is_fetched_without_implicit_lazy_access(self):
        from shared_git_publish import adopt as adoption
        self.remove_loose(self.old_blob)
        with patch.dict(os.environ,{'GIT_NO_LAZY_FETCH':'0'}):
            receipt=adoption.adopt_checkout(self.local_dir,'main',self.target)
        self.assertEqual(receipt['status'],'adopted',receipt)
        self.assertEqual(self.g('cat-file','blob',self.old_blob),b'# Init Project')

    def test_failed_explicit_fetch_refuses_once_without_checkout_changes(self):
        from shared_git_publish import adopt as adoption
        self.remove_loose(self.target_blob)
        real=adoption.git;calls=[]
        def fail(repo,*args,**kwargs):
            if 'fetch' in args:
                calls.append(args)
                raise RuntimeError('explicit object fetch unavailable')
            return real(repo,*args,**kwargs)
        with patch.object(adoption,'git',side_effect=fail):
            receipt=adoption.adopt_checkout(self.local_dir,'main',self.target)
        self.assertEqual(receipt['status'],'refused')
        self.assertIn('explicit object fetch unavailable',receipt['reason'])
        self.assertEqual(len(calls),1)
        self.assertEqual((Path(self.local_dir)/'.git/index').read_bytes(),self.before)
        self.assertEqual((Path(self.local_dir)/'README.md').read_bytes(),b'# Init Project\n')
        self.assertEqual(self.g('for-each-ref','--format=%(refname)','refs/shared-git-publish/backup/'),b'')

    def test_missing_tree_and_blob_are_discovered_without_full_history_refetch(self):
        from shared_git_publish import adopt as adoption
        tree=self.g('rev-parse',self.target+'^{tree}').decode()
        self.remove_loose(tree);self.remove_loose(self.target_blob)
        receipt=adoption.adopt_checkout(self.local_dir,'main',self.target)
        self.assertEqual(receipt['status'],'adopted',receipt)
        self.assertEqual(self.g('rev-parse','HEAD').decode(),self.target)

    def test_missing_older_working_base_is_fetched_outside_incoming_range(self):
        from shared_git_publish import adopt as adoption
        old=self.old_blob
        self.g('update-ref','refs/heads/main',self.target,self.base)
        self.g('read-tree',self.target)
        (Path(self.local_dir)/'README.md').write_bytes(b'new target version\n')
        self.g('add','--','README.md');self.g('commit','-m','Next published target')
        next_target=self.g('rev-parse','HEAD').decode()
        next_blob=self.g('rev-parse',next_target+':README.md').decode()
        self.g('push','origin','HEAD:refs/heads/main')
        self.g('update-ref','refs/heads/main',self.target,next_target)
        self.g('read-tree',self.target)
        self.g('update-index','--cacheinfo','100644,'+next_blob+',README.md')
        (Path(self.local_dir)/'README.md').write_bytes(b'# Init Project\n')
        self.remove_loose(old)
        real=adoption.git;requests=[]
        def inspect(repo,*args,**kwargs):
            if 'fetch' in args:
                requests.extend(args[args.index('origin')+1:])
            return real(repo,*args,**kwargs)
        with patch.object(adoption,'git',side_effect=inspect):
            receipt=adoption.adopt_checkout(self.local_dir,'main',next_target)
        self.assertEqual(receipt['status'],'adopted',receipt)
        self.assertIn(old,requests)
        self.assertEqual((Path(self.local_dir)/'README.md').read_bytes(),b'new target version\n')

    def test_explicit_fetch_does_not_substitute_a_newer_remote_tip(self):
        from shared_git_publish import adopt as adoption
        self.g('update-ref','refs/heads/main',self.target,self.base)
        self.g('read-tree',self.target)
        (Path(self.local_dir)/'README.md').write_bytes(b'even newer remote\n')
        self.g('add','--','README.md');self.g('commit','-m','Remote advances again')
        newer=self.g('rev-parse','HEAD').decode()
        self.g('push','origin','HEAD:refs/heads/main')
        self.g('update-ref','refs/heads/main',self.base,newer)
        self.g('read-tree',self.base)
        (Path(self.local_dir)/'README.md').write_bytes(b'# Init Project\n')
        self.g('update-index','--refresh','--','README.md')
        self.remove_loose(self.target_blob)
        receipt=adoption.adopt_checkout(self.local_dir,'main',self.target)
        self.assertEqual(receipt['status'],'adopted',receipt)
        self.assertEqual(self.g('rev-parse','HEAD').decode(),self.target)
        self.assertEqual((Path(self.local_dir)/'README.md').read_bytes(),b'target bytes\n')

    def publisher(self,content,remote='origin'):
        (Path(self.local_dir)/'README.md').write_bytes(content)
        return subprocess.run([*CLI, 'publish',
            '--repo',self.local_dir,'--remote',remote,'--path','README.md','--message','Missing blob fixture'],
            env=dict(os.environ,GIT_NO_LAZY_FETCH='1'),capture_output=True,text=True,timeout=30)

    def test_publisher_missing_bases_cannot_silently_overwrite_peer_change(self):
        self.remove_loose(self.old_blob);self.remove_loose(self.target_blob)
        result=self.publisher(b'owned divergent bytes\n')
        self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertIn('merge conflict',result.stdout)
        remote=subprocess.check_output(['git','-C',self.bare_dir,'show','refs/heads/main:README.md'])
        self.assertEqual(remote,b'target bytes\n')
        self.assertEqual((Path(self.local_dir)/'.git/index').read_bytes(),self.before)

    def test_empty_path_guard_still_runs_when_base_blob_was_missing(self):
        self.remove_loose(self.old_blob);self.remove_loose(self.target_blob)
        result=self.publisher(b'')
        self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertIn('refusing empty --path with nonempty base',result.stdout)
        self.assertEqual((Path(self.local_dir)/'.git/index').read_bytes(),self.before)
        self.assertEqual(subprocess.check_output(['git','-C',self.bare_dir,'show','refs/heads/main:README.md']),b'target bytes\n')

    def test_publisher_missing_blobs_use_selected_remote(self):
        self.g('remote','rename','origin','publication')
        self.remove_loose(self.old_blob);self.remove_loose(self.target_blob)
        result=self.publisher(b'owned divergent bytes\n',remote='publication')
        self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertIn('merge conflict',result.stdout)
        self.assertNotIn("does not appear to be a git repository",result.stdout)
        self.assertEqual(subprocess.check_output(['git','-C',self.bare_dir,'show','refs/heads/main:README.md']),b'target bytes\n')

    def test_preparation_expired_budget_refuses_before_git(self):
        from shared_git_publish import adopt as adoption
        from unittest.mock import patch
        with patch.object(adoption.time,'monotonic',return_value=301), patch.object(adoption.subprocess,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'300-second total budget'):
                with adoption.object_preparation_budget(deadline=300):
                    adoption.fetch_missing_objects(self.local_dir,[self.target_blob])
            run.assert_not_called()

    def test_preparation_git_timeout_uses_remaining_total_budget(self):
        from shared_git_publish import adopt as adoption
        from unittest.mock import patch
        with patch.object(adoption.time,'monotonic',return_value=295), patch.object(adoption.subprocess,'run') as run:
            run.return_value=subprocess.CompletedProcess([],0,b'',b'')
            with adoption.object_preparation_budget(deadline=300):
                adoption.git(self.local_dir,'status')
            self.assertEqual(run.call_args.kwargs['timeout'],5)



if __name__ == "__main__":
    unittest.main()
