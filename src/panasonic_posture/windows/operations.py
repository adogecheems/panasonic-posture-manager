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
        getattr(engine, 'set_' + params['field'])(params['enabled'])
    elif method == 'retry':
        if before_retry is not None:
            before_retry()
        fallback = params.get('fallback_mode')
        if fallback is not None:
            engine.set_mode(fallback)
        engine.retry()
    elif method == 'confirm_prompt':
        getattr(engine, 'confirm_' + params['prompt'])(params['epoch'])
    elif method == 'calibrate':
        engine.calibrate_sensor()
    elif method == 'reset_calibration':
        engine.reset_sensor_calibration()
    else:
        raise ValueError('unknown method')


__all__ = ['dispatch_engine_operation']
