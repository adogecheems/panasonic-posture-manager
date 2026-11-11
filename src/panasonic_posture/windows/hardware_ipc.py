"""Compatibility facade for the split hardware pipe, RPC and server modules."""
from .hardware_pipe import (
    CLIENT_ACCESS, MAX_CLIENTS, NativePipes, PIPE_NAME, PIPE_SDDL,
)
from .hardware_rpc import (
    HardwareConnection, HardwareOperationError, RPCConnection,
    validated_hardware_state,
)
from .hardware_server import AUTH_SECONDS, CLIENT_TIMEOUT, ConsoleGuard, HardwareServer, Slot

__all__ = [
    'AUTH_SECONDS', 'CLIENT_ACCESS', 'CLIENT_TIMEOUT', 'ConsoleGuard',
    'HardwareConnection', 'HardwareOperationError', 'HardwareServer', 'MAX_CLIENTS',
    'NativePipes', 'PIPE_NAME', 'PIPE_SDDL', 'RPCConnection', 'Slot',
    'validated_hardware_state',
]
