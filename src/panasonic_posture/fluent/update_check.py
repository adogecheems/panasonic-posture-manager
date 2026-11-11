"""Asynchronous GitHub release check over the configured jsproxy endpoint."""
import logging

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest

from ..windows.update_check import (
    PROXY_ENDPOINT,
    parse_latest_release,
    parse_release_redirect,
    proxied_release_redirect_url,
    proxied_release_url,
)

log = logging.getLogger(__name__)


class UpdateChecker(QObject):
    """Run one bounded update request without blocking the UI owner loop."""

    completed = pyqtSignal(object)
    failed = pyqtSignal()

    def __init__(self, current: str, *, manager=None, timeout_ms: int = 8000,
                 parent=None) -> None:
        super().__init__(parent)
        self.current = current
        self.manager = manager or QNetworkAccessManager(self)
        self.timeout = QTimer(self)
        self.timeout.setSingleShot(True)
        self.timeout.timeout.connect(self._timed_out)
        self.timeout_ms = timeout_ms
        self.reply = None
        self.fallback = False

    def check(self) -> bool:
        if self.reply is not None:
            return False
        self.fallback = False
        try:
            self._request(proxied_release_url(), b'application/vnd.github+json')
        except Exception as error:
            log.warning('cannot start update check: %s', error)
            self.failed.emit()
            return False
        self.timeout.start(self.timeout_ms)
        return True

    def _request(self, url: str, accept: bytes) -> None:
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b'Accept', accept)
        request.setRawHeader(b'Origin', PROXY_ENDPOINT.rstrip('/').encode('ascii'))
        request.setRawHeader(b'Referer', PROXY_ENDPOINT.encode('ascii'))
        request.setRawHeader(
            b'User-Agent', f'PanasonicPostureManager/{self.current}'.encode('ascii')
        )
        self.reply = self.manager.get(request)
        self.reply.finished.connect(self._finished)

    def _finished(self) -> None:
        reply, self.reply = self.reply, None
        if reply is None:
            return
        try:
            status = reply.attribute(
                QNetworkRequest.Attribute.HttpStatusCodeAttribute
            )
            if not self.fallback and status != 200:
                self.fallback = True
                self._request(proxied_release_redirect_url(), b'text/html')
                return
            self.timeout.stop()
            if not self.fallback:
                info = parse_latest_release(bytes(reply.readAll().data()), self.current)
            else:
                location = bytes(reply.rawHeader(b'--location')).decode('ascii')
                if not location:
                    location = bytes(reply.rawHeader(b'Location')).decode('ascii')
                if not location and '/releases/tag/' in reply.url().toString():
                    location = reply.url().toString().split('/http/', 1)[-1]
                info = parse_release_redirect(location, self.current)
        except Exception as error:  # A Qt slot must never let a response error escape.
            self.timeout.stop()
            log.warning('update check failed: %s', error)
            self.failed.emit()
        else:
            self.completed.emit(info)
        finally:
            reply.deleteLater()

    def _timed_out(self) -> None:
        reply, self.reply = self.reply, None
        if reply is not None:
            reply.abort()
            reply.deleteLater()
            self.failed.emit()

    def shutdown(self) -> None:
        self.timeout.stop()
        reply, self.reply = self.reply, None
        if reply is not None:
            reply.abort()
            reply.deleteLater()
