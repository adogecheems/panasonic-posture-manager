"""Lightweight posture labels shared by the tray and the full window."""


def mode_label(mode, _):
    if mode == 'auto':
        return _('Automatic')
    if mode == 'laptop':
        return _('Laptop')
    if mode == 'tent':
        return _('Tent')
    if mode == 'stand':
        return _('Stand')
    if mode == 'tablet':
        return _('Tablet')
    return _('Waiting for the desktop to get ready…')


__all__ = ['mode_label']
