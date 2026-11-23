"""Command-line entry point and compatibility renderers."""
import argparse
import json
import signal
import subprocess
import sys

from .adopt import adopt_checkout, git
from .profile import profile
from .publish import parser as publish_parser
from .publish import publish
from .unit import publish_unit, run_git

SUCCESSFUL_UNIT = ('published', 'up_to_date')
CLI_ERRORS = (OSError, RuntimeError, subprocess.SubprocessError)


def terminate_on_sigterm():
    def terminate(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)


def unit_parser(parser, *, legacy=False):
    parser.add_argument('--repo', required=True)
    parser.add_argument('--tip', required=True, help='already-committed tip (entire outgoing history)')
    parser.add_argument('--remote', required=legacy, default='origin')
    parser.add_argument('--branch', required=legacy, default='main')
    parser.add_argument('--path', dest='paths', action='append', default=[], help='explicit path in the unit')
    parser.add_argument('--message', '-m', help='merge commit message if the histories diverged')
    parser.add_argument('--no-adopt', action='store_true', help='publish without advancing this checkout')
    return parser


def run_unit(args):
    result = publish_unit(args.repo, args.tip, args.remote, args.branch, args.paths, args.message)
    if result['status'] in SUCCESSFUL_UNIT and not args.no_adopt:
        root = run_git(['git', '-C', args.repo, 'rev-parse', '--show-toplevel'], cwd=args.repo).stdout.strip()
        tip = result['published_tip']
        result['adoption'] = adopt_checkout(root, args.branch, tip, remote=args.remote,
                                            publish_local=profile.get().publish_local_on_adopt)
        remote_checkout = profile.get().remote_checkout
        if remote_checkout:
            result['remote_checkout'] = remote_checkout(root, args.remote, tip)
    return result


def legacy_publish(argv=None):
    parser = publish_parser(argparse.ArgumentParser(
        description='Publish owned working files without the shared index. Unstaged foreign edits cannot be distinguished.'))
    args = parser.parse_args(argv)
    if not args.path and not args.delete:
        parser.error('at least one --path or --delete is required')
    try:
        print(json.dumps(publish(args)))
    except CLI_ERRORS as error:
        print(json.dumps({'status': 'error', 'error': str(error)}), file=sys.stderr)
        return 1
    return 0


def legacy_unit(argv=None):
    parser = unit_parser(argparse.ArgumentParser(
        description='Bounded Git-publication helper for explicit committed work units.'), legacy=True)
    result = run_unit(parser.parse_args(argv))
    print(json.dumps(result, indent=2))
    return 0 if result['status'] in SUCCESSFUL_UNIT else 1


def adopt_target(repo, target, remote):
    resolved = git(repo, 'rev-parse', '--verify', '--end-of-options', target + '^{commit}', check=False)
    if resolved.returncode:
        return {'status': 'refused', 'reason': 'target does not resolve to a commit'}
    branch = git(repo, 'symbolic-ref', '--short', '-q', 'HEAD', check=False)
    if branch.returncode:
        return {'status': 'skipped', 'reason': 'HEAD is detached'}
    result = adopt_checkout(repo, branch.stdout.decode().strip(), resolved.stdout.decode().strip(),
                            publish_local=False, remote=remote)
    if result['status'] == 'adopted' and result['head_before'] == result['head']:
        result['status'] = 'up_to_date'
    return result


def main(argv=None):
    terminate_on_sigterm()
    parser = argparse.ArgumentParser(prog='shared-git-publish',
                                     description='Publish explicit Git work and advance shared checkouts.')
    commands = parser.add_subparsers(dest='command', required=True)
    publish_parser(commands.add_parser('publish', help='publish owned working files'))
    unit_parser(commands.add_parser('publish-commit', help='publish an already-committed unit'))
    adopt = commands.add_parser('adopt', help='advance the current checkout')
    adopt.add_argument('--repo', required=True)
    adopt.add_argument('--target', required=True)
    adopt.add_argument('--remote', default='origin', help='source of missing objects')
    args = parser.parse_args(argv)

    if args.command == 'adopt':
        try:
            result = adopt_target(args.repo, args.target, args.remote)
        except CLI_ERRORS as error:
            result = {'status': 'refused', 'reason': str(error)}
        print(json.dumps({'schema_version': 1, 'adoption': result}))
        return {'adopted': 0, 'up_to_date': 0, 'refused': 2, 'skipped': 3}[result['status']]

    try:
        result = publish(args) if args.command == 'publish' else run_unit(args)
    except CLI_ERRORS as error:
        result = {'status': 'error', 'error': str(error)}
    adoption = result.pop('adoption', None)
    if adoption is None:
        reason = 'disabled by --no-adopt' if args.no_adopt else (
            'working paths were unchanged' if result['status'] == 'unchanged' else 'publication failed')
        adoption = {'status': 'skipped', 'reason': reason}
    print(json.dumps({'schema_version': 1, 'publication': result, 'adoption': adoption}))
    return 0 if result['status'] in (*SUCCESSFUL_UNIT, 'unchanged') else 1
