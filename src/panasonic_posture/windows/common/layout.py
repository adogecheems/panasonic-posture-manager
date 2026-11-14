"""Locate the repository or installed bundle that contains Windows modules."""
from pathlib import Path


def distribution_root(module_file: str | Path) -> Path:
    """Return the root above either ``src`` or the installed ``app`` tree."""
    path = Path(module_file).resolve()
    try:
        app_root = path.parents[3]
        platform_root = path.parents[1]
        package_root = path.parents[2]
        root = path.parents[4]
    except IndexError as error:
        raise ValueError('Windows module path is outside the product layout') from error
    if (
        app_root.name.casefold() not in {'app', 'src'}
        or platform_root.name.casefold() != 'windows'
        or package_root.name.casefold() != 'panasonic_posture'
    ):
        raise ValueError('Windows module path is outside the product layout')
    return root


__all__ = ['distribution_root']
