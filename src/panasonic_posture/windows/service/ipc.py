"""Compatibility facade for the split hardware pipe, RPC and server modules."""
from panasonic_posture.windows.ipc.pipe import (
    CLIENT_ACCESS,
    MAX_CLIENTS,
    NativePipes,
    PIPE_NAME,
    PIPE_SDDL,
    PipePoll,
    PipeState,
)
from panasonic_posture.windows.ipc.rpc import (
    HardwareConnection,
    HardwareOperationError,
    RPCConnection,
    validated_hardware_state,
)
from panasonic_posture.windows.service.server import (
    AUTH_SECONDS,
    CLIENT_TIMEOUT,
    ConsoleGuard,
    HardwareServer,
    Slot,
)

__all__ = [
    'AUTH_SECONDS', 'CLIENT_ACCESS', 'CLIENT_TIMEOUT', 'ConsoleGuard',
    'HardwareConnection', 'HardwareOperationError', 'HardwareServer', 'MAX_CLIENTS',
    'NativePipes', 'PIPE_NAME', 'PIPE_SDDL', 'PipePoll', 'PipeState',
    'RPCConnection', 'Slot',
    'validated_hardware_state',
]
