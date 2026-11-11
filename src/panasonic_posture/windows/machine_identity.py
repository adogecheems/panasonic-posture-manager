"""Single source of truth for the verified Windows machine identity."""

SUPPORTED_WINDOWS_MACHINE = ('Panasonic Corporation', 'CFQV8-1L')


def read_machine_identity() -> tuple[str, str]:
    """Read only the BIOS identity strings; callers own support decisions."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\BIOS') as key:
        vendor = winreg.QueryValueEx(key, 'SystemManufacturer')[0]
        product = winreg.QueryValueEx(key, 'SystemProductName')[0]
    return vendor, product


__all__ = ['SUPPORTED_WINDOWS_MACHINE', 'read_machine_identity']
