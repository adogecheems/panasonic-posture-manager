"""Map validated Windows UI operations onto the shared posture engine."""
from __future__ import annotations

from collections.abc import Callable

from ..service.engine import Engine


def dispatch_engine_operation(
    engine: Engine,
    method: str,
    params: dict,
    *,
    before_retry: Callable[[], None] | None = None,
) -> None:
    """Apply one already-validated operation without ticking the engine.

    Transport and session entry points keep their own authority checks,
    response construction and scheduling. This function owns only the common
    command-to-engine mapping, so every route advances the same operations.
    """
    if method == 'set_mode':
        engine.set_mode(params['mode'])
    elif method == 'set_preference':
        field = params['field']
        if field == 'tent_enabled':
            engine.set_tent_enabled(params['enabled'])
        elif field == 'tent_prompt':
            engine.set_tent_prompt(params['enabled'])
        elif field == 'stand_prompt':
            engine.set_stand_prompt(params['enabled'])
        else:
            raise ValueError('unknown preference')
    elif method == 'retry':
        if before_retry is not None:
            before_retry()
        fallback = params.get('fallback_mode')
        if fallback is not None:
            engine.set_mode(fallback)
        engine.retry()
    elif method == 'confirm_prompt':
        if params['prompt'] == 'tent':
            engine.confirm_tent(params['epoch'])
        elif params['prompt'] == 'stand':
            engine.confirm_stand(params['epoch'])
        else:
            raise ValueError('unknown prompt')
    elif method == 'calibrate':
        engine.calibrate_sensor()
    elif method == 'reset_calibration':
        engine.reset_sensor_calibration()
    else:
        raise ValueError('unknown method')


__all__ = ['dispatch_engine_operation']
