"""Release metadata lookup through the public jsproxy deployment."""
from dataclasses import dataclass
import json
import re
from urllib.parse import unquote, urlsplit


PROXY_ENDPOINT = 'https://proxy.mmoe.work/'
GITHUB_REPOSITORY = 'adogecheems/panasonic-posture-manager'
GITHUB_RELEASE_API = (
    f'https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest'
)
GITHUB_RELEASE_REDIRECT = (
    f'https://github.com/{GITHUB_REPOSITORY}/releases/latest'
)
MAX_RELEASE_RESPONSE = 256 * 1024
_VERSION = re.compile(r'[vV]?(\d+(?:\.\d+){0,3})')


@dataclass(frozen=True)
class UpdateInfo:
    current: str
    latest: str
    update_available: bool


class UpdateCheckError(ValueError):
    """The release response is missing or does not match its contract."""


def proxied_release_url() -> str:
    """Return the jsproxy HTTP route for GitHub's latest-release endpoint."""
    return f'{PROXY_ENDPOINT}http/{GITHUB_RELEASE_API}'


def proxied_release_redirect_url() -> str:
    """Return the jsproxy route used when the shared GitHub API is limited."""
    return f'{PROXY_ENDPOINT}http/{GITHUB_RELEASE_REDIRECT}'


def _version(value: object) -> tuple[str, tuple[int, ...]]:
    if not isinstance(value, str):
        raise UpdateCheckError('version must be a string')
    match = _VERSION.fullmatch(value.strip())
    if match is None:
        raise UpdateCheckError('invalid release version')
    release = match.group(1)
    parts = tuple(int(part) for part in release.split('.'))
    return release, parts


def parse_latest_release(payload: bytes, current: str) -> UpdateInfo:
    """Validate a GitHub release payload and compare its tag with ``current``."""
    if not isinstance(payload, bytes) or len(payload) > MAX_RELEASE_RESPONSE:
        raise UpdateCheckError('invalid release response')
    try:
        document = json.loads(payload.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpdateCheckError('invalid release response') from error
    if not isinstance(document, dict):
        raise UpdateCheckError('invalid release response')

    latest_version, latest_parts = _version(document.get('tag_name'))
    return _compare(current, latest_version, latest_parts)


def parse_release_redirect(location: str, current: str) -> UpdateInfo:
    """Extract a release tag from jsproxy's rewritten GitHub redirect header."""
    try:
        target = urlsplit(location)
    except (TypeError, ValueError) as error:
        raise UpdateCheckError('invalid release redirect') from error
    prefix = f'/{GITHUB_REPOSITORY}/releases/tag/'
    if (target.scheme != 'https' or target.netloc != 'github.com' or
            not target.path.startswith(prefix) or target.query or target.fragment):
        raise UpdateCheckError('invalid release redirect')
    tag = unquote(target.path[len(prefix):])
    latest_version, latest_parts = _version(tag)
    return _compare(current, latest_version, latest_parts)


def _compare(current: str, latest_version: str,
             latest_parts: tuple[int, ...]) -> UpdateInfo:
    current_version, current_parts = _version(current)
    width = max(len(current_parts), len(latest_parts))
    current_key = current_parts + (0,) * (width - len(current_parts))
    latest_key = latest_parts + (0,) * (width - len(latest_parts))
    return UpdateInfo(
        current=current_version,
        latest=latest_version,
        update_available=latest_key > current_key,
    )
