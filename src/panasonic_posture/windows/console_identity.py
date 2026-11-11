"""Authenticated Windows console identities shared across process boundaries."""
from dataclasses import dataclass

from ..service.ports import SessionOwner


@dataclass(frozen=True)
class ConsoleIdentity:
    """A process identity resolved by the OS, never supplied by a client."""

    sid: str
    logon_id: int
    session_id: int
    process_id: int
    created_at: int

    def __post_init__(self) -> None:
        if not isinstance(self.sid, str) or not self.sid.startswith('S-1-'):
            raise ValueError('invalid Windows SID')
        if any(type(value) is not int or value <= 0 for value in (
                self.logon_id, self.session_id, self.process_id, self.created_at)):
            raise ValueError('invalid Windows process or logon identity')

    @property
    def owner(self) -> SessionOwner:
        return SessionOwner(
            'windows-console',
            f'{self.sid}:{self.logon_id}:{self.session_id}:{self.process_id}:{self.created_at}',
        )


@dataclass(frozen=True)
class ConsoleUser:
    """The ordinary unlocked console user eligible to host a session agent."""

    sid: str
    logon_id: int
    session_id: int
    unlocked: bool

    def __post_init__(self) -> None:
        ConsoleIdentity(self.sid, self.logon_id, self.session_id, 1, 1)
        if self.sid == 'S-1-5-18' or type(self.unlocked) is not bool:
            raise ValueError('an ordinary console user is required')

    @property
    def key(self) -> tuple[str, int, int]:
        return self.sid, self.logon_id, self.session_id


__all__ = ['ConsoleIdentity', 'ConsoleUser']
