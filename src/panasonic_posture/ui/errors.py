"""Translate stable service errors without exposing diagnostic details."""
import re
from ..session.errors import ERROR_MESSAGES, error_text
from ..session.failures import describe_error as message_of
from .common import _


class ServiceUnavailable(Exception):
    """The UI has no current service connection."""


def unknown_method(error: BaseException) -> bool:
    return bool(re.search(r'UnknownMethod|No such method|unknown method', message_of(error), re.I))


def describe_error(error: BaseException) -> str:
    text = message_of(error)
    if isinstance(error, ServiceUnavailable) or re.search(
        r'ServiceUnknown|NameHasNoOwner|No such name|no usable bus|service unavailable', text, re.I
    ):
        return _('Background service unavailable')
    if re.search(r'AccessDenied|AuthFailed|NotAuthorized|access-denied', text, re.I):
        return error_text('access-denied')
    text = text.split('GDBus.Error:')[-1]
    parts = text.split(':')
    code = parts[1].strip() if '.' in parts[0] and len(parts) > 1 else parts[0].strip()
    return error_text(code if code in ERROR_MESSAGES else None)
