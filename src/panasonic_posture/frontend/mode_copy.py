"""Single source of user-facing names and descriptions for manual modes."""
from typing import Callable


def translated_mode_copy(_: Callable[[str], str]):
    """Translate the shared mode copy for either platform UI."""
    return (
        ('auto', _('Automatic'), _('Switch automatically with the current posture')),
        ('laptop', _('Laptop'), _('Best for typing and precise work')),
        ('tent', _('Tent'), _('Designed for use in tent form')),
        ('stand', _('Stand'), _('Designed for viewing in stand form')),
    )


MODE_COPY = translated_mode_copy(str)
