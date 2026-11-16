"""LocalSystem ownership of the single Windows convertible-slate DWORD."""
from panasonic_posture.windows.hardware.slate_journal import SlateJournal, SlateLease


class RegistrySlateValue:
    key = r'SYSTEM\CurrentControlSet\Control\PriorityControl'
    name = 'ConvertibleSlateMode'

    def read(self) -> bool:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, self.key) as handle:
            value, kind = winreg.QueryValueEx(handle, self.name)
        if kind != winreg.REG_DWORD or value not in (0, 1):
            raise OSError('invalid convertible-slate registry value')
        return value == 0

    def set(self, slate: bool) -> None:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, self.key, 0,
                            winreg.KEY_SET_VALUE) as handle:
            winreg.SetValueEx(handle, self.name, 0, winreg.REG_DWORD, int(not slate))


class RegistryPosture:
    def __init__(self, device: RegistrySlateValue, journal: SlateJournal):
        self.device, self.journal = device, journal
        self.loaded = False
        self.pending: SlateLease | None = None

    def _load(self) -> None:
        if not self.loaded:
            self.pending = self.journal.load()
            self.loaded = True

    def _write(self, desired: bool) -> None:
        self.device.set(desired)
        if self.device.read() != desired:
            raise OSError('convertible-slate registry readback failed')

    def apply(self, desired: bool) -> None:
        self._load()
        current = self.device.read()
        original = self.pending.original if self.pending else current
        if current != desired:
            if self.pending is None:
                self.journal.save(original, desired)
                self.pending = SlateLease(original, desired)
            self._write(desired)
        if self.pending and desired == original:
            self.journal.clear()
            self.pending = None

    def release(self, physical: bool | None) -> None:
        if physical is None:
            # Preserve the protected journal, including a corrupt one, while
            # the physical source is unavailable across startup or resume.
            if self.pending or (not self.loaded and self.journal.path.exists()):
                raise OSError('physical state unavailable during registry recovery')
            return
        self._load()
        if self.pending is None:
            return
        # A real fold supersedes the original laptop value saved in tent mode.
        desired = physical or self.pending.original
        if self.device.read() != desired:
            self._write(desired)
        self.journal.clear()
        self.pending = None
