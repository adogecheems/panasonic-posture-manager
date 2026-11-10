"""Pure presentation decisions over already validated service snapshots."""
from enum import StrEnum

POSES = frozenset({'laptop', 'tent', 'stand', 'tablet'})


def notice_token(state: dict | None) -> tuple | None:
    if state is None:
        return None
    return state.get('instance'), state.get('pending_notification'), state.get('pending_prompt')


def notice_confirmable(state: dict | None, notice: tuple | None) -> bool:
    """A clicked notice is only actionable while its exact snapshot is current."""
    return bool(
        state and notice is not None and notice_token(state) == notice
        and state.get('supported') and state.get('session_connected')
        and not state.get('error_code') and state.get('hardware') is False
        and notice[2] in ('tent', 'stand')
    )


def displayed_pose(state: dict) -> str:
    applied = (state.get('applied') or {}).get('mode')
    if applied == 'hardware':
        return 'tablet'
    if applied in POSES:
        return applied
    manual = state.get('manual')
    if not state.get('error_code') and manual in POSES and (state.get('target') or {}).get('mode') == manual:
        return manual
    return state.get('pose') if state.get('pose') in POSES else 'unknown'


class Detail(StrEnum):
    NONE = ''
    DISCONNECTED = 'disconnected'
    ERROR = 'error'
    SESSION = 'session'
    UNSUPPORTED = 'unsupported'
    SWITCH = 'switch'
    SENSOR = 'sensor'
    SLOW = 'slow'
    COMPATIBLE = 'compatible'


class PostureModel:
    """Keep the last drawing and one generation-scoped two-second deadline."""

    def __init__(self) -> None:
        self.state: dict | None = None
        self.pose = 'laptop'
        self.transition: tuple | None = None
        self.deadline: float | None = None
        self.slow = False

    def update(self, state: dict, now: float) -> None:
        self.state = state
        mode = state['target']['mode']
        pending = (not state['error_code'] and state['session_connected']
                   and state['supported'] and mode != 'unavailable'
                   and (state['applied'] or {}).get('mode') != mode)
        key = (state['instance'], state['generation'], mode) if pending else None
        if key != self.transition:
            self.transition = key
            self.deadline = now + 2 if pending else None
            self.slow = False
        pose = displayed_pose(state)
        if pose != 'unknown':
            self.pose = pose
        self.advance(now)

    def advance(self, now: float) -> None:
        self.slow = self.deadline is not None and now >= self.deadline

    def disconnect(self) -> None:
        self.state = None
        self.transition = None
        self.deadline = None
        self.slow = False

    @property
    def can_return_auto(self) -> bool:
        return bool(self.state and self.state['supported']
                    and self.state['manual'] in POSES
                    and self.state['hardware'] is not True)

    @property
    def detail(self) -> Detail:
        state = self.state
        if state is None:
            return Detail.DISCONNECTED
        if state['error_code']:
            return Detail.ERROR
        if not state['session_connected']:
            return Detail.SESSION
        if not state['supported']:
            return Detail.UNSUPPORTED
        if state['target']['mode'] == 'unavailable':
            if state['hardware'] is None:
                return Detail.SWITCH
            if state['sensor_error'] or state['sensor_source'] == 'starting':
                return Detail.SENSOR
            return Detail.NONE
        if self.slow:
            return Detail.SLOW
        return Detail.COMPATIBLE if state['support_tier'] == 'compatible' else Detail.NONE
