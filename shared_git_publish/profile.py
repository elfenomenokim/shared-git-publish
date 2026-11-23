"""Extension points for a publisher embedded in another workflow."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Profile:
    lock_name: str = 'shared-git-publish.lock'
    backup_prefix: str = 'refs/shared-git-publish/backup/'
    retained_prefix: str = 'refs/shared-git-publish/'
    creation_trailer: str = 'Adoption-Created'
    transaction_trailer: str = 'Adoption-Id'
    writer_trailer: str = 'Writer'
    clear_owner: str = 'checkout operator'
    scratch_prefix: str = 'shared-git-publish-'
    publish_local_on_adopt: bool = False
    check_repo: Optional[Callable] = None
    check_path: Optional[Callable] = None
    format_message: Optional[Callable] = None
    receive_pack: Optional[Callable] = None
    slow_snapshot: Optional[Callable] = None
    remote_checkout: Optional[Callable] = None
    flow_maps: Optional[Callable] = None


profile = ContextVar('publication_profile', default=Profile())


@contextmanager
def use_profile(selected):
    token = profile.set(selected)
    try:
        yield
    finally:
        profile.reset(token)


def commit_message(message):
    format_message = profile.get().format_message
    return format_message(message) if format_message else message
