"""Read the Windows BIOS identity used only for profile selection."""


def read_machine_identity() -> tuple[str, str]:
    """Return normalized BIOS strings without imposing a model allowlist."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\BIOS') as key:
        vendor = winreg.QueryValueEx(key, 'SystemManufacturer')[0]
        product = winreg.QueryValueEx(key, 'SystemProductName')[0]
    return str(vendor or '').strip(), str(product or '').strip()


__all__ = ['read_machine_identity']
