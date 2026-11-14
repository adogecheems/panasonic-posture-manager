"""Contract tests for release lookup through the public jsproxy endpoint."""
import json
import unittest

from panasonic_posture.windows.app.update_check import (
    GITHUB_RELEASE_API,
    PROXY_ENDPOINT,
    UpdateCheckError,
    parse_latest_release,
    parse_release_redirect,
    proxied_release_redirect_url,
    proxied_release_url,
)


class ReleaseLookupTests(unittest.TestCase):
    def test_jsproxy_uses_the_http_route_without_exposing_a_second_endpoint(self):
        self.assertEqual(PROXY_ENDPOINT, 'https://proxy.mmoe.work/')
        self.assertEqual(
            proxied_release_url(),
            'https://proxy.mmoe.work/http/' + GITHUB_RELEASE_API,
        )
        self.assertEqual(
            proxied_release_redirect_url(),
            'https://proxy.mmoe.work/http/'
            'https://github.com/adogecheems/panasonic-posture-manager/releases/latest',
        )

    def test_newer_release_is_detected_and_normalized(self):
        result = parse_latest_release(
            json.dumps({'tag_name': 'v2.5.1'}).encode(), '2.4.0'
        )
        self.assertEqual(result.current, '2.4.0')
        self.assertEqual(result.latest, '2.5.1')
        self.assertTrue(result.update_available)

    def test_same_or_older_release_is_not_an_update(self):
        for tag in ('v2.4.0', '2.4', 'v2.3.9'):
            with self.subTest(tag=tag):
                result = parse_latest_release(
                    json.dumps({'tag_name': tag}).encode(), '2.4.0'
                )
                self.assertFalse(result.update_available)

    def test_untrusted_or_malformed_release_metadata_is_rejected(self):
        for payload in (
            b'not json',
            b'{"tag_name": "<b>2.5.0</b>"}',
            b'{"name": "v2.5.0"}',
            b'[]',
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(UpdateCheckError):
                    parse_latest_release(payload, '2.4.0')

    def test_release_redirect_is_a_bounded_api_limit_fallback(self):
        result = parse_release_redirect(
            'https://github.com/adogecheems/panasonic-posture-manager/'
            'releases/tag/v2.5.0',
            '2.4.0',
        )
        self.assertEqual(result.latest, '2.5.0')
        self.assertTrue(result.update_available)
        for location in (
            'https://example.com/adogecheems/panasonic-posture-manager/releases/tag/v2.5.0',
            'https://github.com/other/repo/releases/tag/v2.5.0',
            'https://github.com/adogecheems/panasonic-posture-manager/releases/tag/<b>2.5.0</b>',
        ):
            with self.subTest(location=location):
                with self.assertRaises(UpdateCheckError):
                    parse_release_redirect(location, '2.4.0')
