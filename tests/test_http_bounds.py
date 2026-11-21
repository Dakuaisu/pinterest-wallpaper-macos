import io
import time
import unittest
from unittest import mock

import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.response import HTTPResponse

import pinterest_wallpaper as main
import wallpaper_http as http


class Raw(io.BytesIO):
    def __init__(self, body):
        super().__init__(body)
        self.reads = 0

    def read(self, *args, **kwargs):
        self.reads += 1
        return super().read(*args, **kwargs)

    def release_conn(self):
        pass


class Adapter(BaseAdapter):
    def __init__(self, specs):
        self.specs = list(specs)
        self.responses = []
        self.requests = []

    def send(self, request, **kwargs):
        self.requests.append((request, kwargs))
        status, headers, body = self.specs.pop(0)
        response = requests.Response()
        response.status_code = status
        response.headers = CaseInsensitiveDict(headers)
        response.url = request.url
        response.request = request
        response.raw = Raw(body)
        self.responses.append(response)
        return response

    def close(self):
        pass


def session_for(*specs):
    session = requests.Session()
    session.trust_env = False
    adapter = Adapter(specs)
    session.mount('https://', adapter)
    return session, adapter


class BoundsTests(unittest.TestCase):
    def test_buffers_response_and_closes_raw(self):
        session, adapter = session_for((200, {'Content-Type': 'application/json'}, b'{"ok":true}'))
        result = http.get_response(session, 'https://i.pinimg.com/a', headers={}, kind='image')
        self.assertEqual(result.json(), {'ok': True})
        self.assertTrue(adapter.responses[0].raw.closed)
        self.assertEqual(adapter.requests[0][0].headers['Accept-Encoding'], 'identity')

    def test_relative_redirect_never_reads_redirect_body(self):
        session, adapter = session_for((302, {'Location': '/next'}, b'x' * 100), (200, {}, b'ok'))
        result = http.get_response(session, 'https://www.pinterest.com/a', headers={}, kind='board')
        self.assertEqual(result.content, b'ok')
        self.assertEqual(adapter.requests[1][0].url, 'https://www.pinterest.com/next')
        self.assertEqual(adapter.responses[0].raw.reads, 0)
        self.assertTrue(adapter.responses[0].raw.closed)

    def test_blocked_redirect_before_connect(self):
        session, adapter = session_for((302, {'Location': 'https://evil.example/'}, b'x'))
        with self.assertRaises(http.UnsafeURL):
            http.get_response(session, 'https://i.pinimg.com/a', headers={}, kind='image')
        self.assertEqual(len(adapter.requests), 1)
        self.assertTrue(adapter.responses[0].raw.closed)
        self.assertEqual(adapter.responses[0].raw.reads, 0)

    def test_board_redirect_to_a_country_subdomain_is_followed(self):
        session, adapter = session_for((302, {'Location': 'https://br.pinterest.com/a'}, b''), (200, {}, b'ok'))
        result = http.get_response(session, 'https://www.pinterest.com/a', headers={}, kind='board')
        self.assertEqual(result.content, b'ok')
        self.assertEqual(adapter.requests[1][0].url, 'https://br.pinterest.com/a')

    def test_url_policy(self):
        bad = [('image', 'https://evilpinimg.com/a'), ('image', 'http://i.pinimg.com/a'),
               ('image', 'https://user@i.pinimg.com/a'), ('image', 'https://i.pinimg.com:444/a'),
               ('board', 'https://www.pinterest.com.evil/a'),
               ('feed', 'https://www.pinterest.com/other')]
        for kind, url in bad:
            with self.subTest(url=url), self.assertRaises(http.UnsafeURL):
                http.get_response(session_for()[0], url, headers={}, kind=kind)
        for kind, url in [('board', 'https://in.pinterest.com/a'),
                          ('rss', 'https://www.pinterest.com/a.rss'),
                          ('feed', 'https://www.pinterest.com/resource/BoardFeedResource/get/')]:
            with self.subTest(url=url):
                session, _ = session_for((200, {}, b'ok'))
                self.assertEqual(http.get_response(session, url, headers={}, kind=kind).content, b'ok')

    def test_oversized_declared_and_streamed_body_closed(self):
        for headers, body in [({'Content-Length': '100'}, b'x'), ({}, b'abcdef')]:
            with self.subTest(headers=headers):
                session, adapter = session_for((200, headers, body))
                with self.assertRaises(http.ResponseTooLarge):
                    http.get_response(session, 'https://i.pinimg.com/a', headers={}, kind='image', max_bytes=5)
                self.assertTrue(adapter.responses[0].raw.closed)

    def test_compression_rejected_before_reading(self):
        session, adapter = session_for((200, {'Content-Encoding': 'gzip'}, b'compressed'))
        with self.assertRaises(http.UnsupportedEncoding):
            http.get_response(session, 'https://i.pinimg.com/a', headers={}, kind='image')
        self.assertEqual(adapter.responses[0].raw.reads, 0)
        self.assertTrue(adapter.responses[0].raw.closed)

    def test_expired_deadline_and_timeout_budget(self):
        session, adapter = session_for((200, {}, b'ok'))
        with self.assertRaises(http.DeadlineExceeded):
            http.get_response(session, 'https://i.pinimg.com/a', headers={}, kind='image', deadline=time.monotonic() - 1)
        self.assertEqual(adapter.requests, [])
        with mock.patch.object(http.time, 'monotonic', return_value=100):
            http.get_response(session, 'https://i.pinimg.com/a', headers={}, kind='image', deadline=103, timeout=30)
        self.assertEqual(adapter.requests[0][1]['timeout'], 3)

    def test_redirect_limit(self):
        session, adapter = session_for(*[(302, {'Location': '/a'}, b'x')] * 6)
        with self.assertRaises(http.TooManyRedirects):
            http.get_response(session, 'https://i.pinimg.com/a', headers={}, kind='image')
        self.assertEqual(len(adapter.requests), 6)
        self.assertTrue(all(r.raw.closed and r.raw.reads == 0 for r in adapter.responses))

    def test_deadline_elapses_during_stream_and_closes(self):
        session, adapter = session_for((200, {}, b'body'))
        with mock.patch.object(http.time, 'monotonic', side_effect=[0, 0, 2]), self.assertRaises(http.DeadlineExceeded):
            http.get_response(session, 'https://i.pinimg.com/a', headers={}, deadline=1)
        self.assertTrue(adapter.responses[0].raw.closed)

    def test_session_get_double_can_return_response_without_dispatching_hook(self):
        response = requests.Response()
        response.status_code = 200
        response.headers = CaseInsensitiveDict()
        response.url = 'https://i.pinimg.com/a'
        response.raw = Raw(b'ok')
        session = mock.Mock()
        session.get.return_value = response
        result = http.get_response(session, response.url, headers={})
        self.assertEqual(result.content, b'ok')
        self.assertTrue(response.raw.closed)


class ProductionAdapterTests(unittest.TestCase):
    def setUp(self):
        self.session = main.make_session()
        self.session.trust_env = False
        self.addCleanup(self.session.close)
        self.adapter = self.session.get_adapter('https://i.pinimg.com/a')

    def safe_status_policy(self):
        self.adapter.max_retries = self.adapter.max_retries.new(
            status=0, status_forcelist=(), respect_retry_after_header=False)

    def test_unsafe_status_policy_rejected_before_adapter_send(self):
        from urllib3.util.retry import Retry
        self.adapter.max_retries = Retry(total=3, status_forcelist=[503])
        with mock.patch.object(HTTPSConnectionPool, '_make_request') as send, self.assertRaises(requests.RequestException):
            http.get_response(self.session, 'https://i.pinimg.com/a', headers={})
        send.assert_not_called()

    def test_urllib3_unsafe_policy_drains_body_before_hook(self):
        from urllib3.util.retry import Retry
        self.adapter.max_retries = Retry(total=1, status_forcelist=[503])
        body = Raw(b'error body')
        responses = [HTTPResponse(body=body, status=503, preload_content=False),
                     HTTPResponse(body=io.BytesIO(b'ok'), status=200, preload_content=False)]
        with mock.patch.object(HTTPSConnectionPool, '_make_request', side_effect=responses):
            result = self.session.get('https://i.pinimg.com/a', stream=True)
        result.close()
        self.assertGreater(body.reads, 0)

    def test_retry_after_adapter_policy_rejected_even_with_empty_forcelist(self):
        from urllib3.util.retry import Retry
        self.adapter.max_retries = Retry(total=3, status=0, status_forcelist=(),
                                         respect_retry_after_header=True)
        with mock.patch.object(HTTPSConnectionPool, '_make_request') as send, self.assertRaises(http.UnsafeRetryPolicy):
            http.get_response(self.session, 'https://i.pinimg.com/a', headers={})
        send.assert_not_called()

    def test_status_retry_closes_unread_body_before_success(self):
        self.safe_status_policy()
        raw = Raw(b'x' * 100)
        responses = [HTTPResponse(body=raw, status=503, preload_content=False),
                     HTTPResponse(body=io.BytesIO(b'ok'), status=200, preload_content=False)]
        with mock.patch.object(HTTPSConnectionPool, '_make_request', side_effect=responses) as send, mock.patch.object(http.time, 'sleep'):
            result = http.get_response(self.session, 'https://i.pinimg.com/a', headers={}, max_bytes=2)
        self.assertEqual(result.content, b'ok')
        self.assertEqual(send.call_count, 2)
        self.assertEqual(raw.reads, 0)
        self.assertTrue(raw.closed)

    def test_exhausted_status_retries_are_closed_without_reading(self):
        self.safe_status_policy()
        bodies = [Raw(b'oversized') for _ in range(4)]
        responses = [HTTPResponse(body=body, status=429, preload_content=False) for body in bodies]
        with mock.patch.object(HTTPSConnectionPool, '_make_request', side_effect=responses) as send, mock.patch.object(http.time, 'sleep'), self.assertRaises(requests.exceptions.RetryError):
            http.get_response(self.session, 'https://i.pinimg.com/a', headers={}, max_bytes=1)
        self.assertEqual(send.call_count, 4)
        self.assertTrue(all(body.closed and body.reads == 0 for body in bodies))

    def test_retry_after_larger_than_budget_does_not_sleep(self):
        self.safe_status_policy()
        body = Raw(b'oversized')
        response = HTTPResponse(body=body, status=503, headers={'Retry-After': '999999'}, preload_content=False)
        with mock.patch.object(HTTPSConnectionPool, '_make_request', return_value=response), mock.patch.object(http.time, 'sleep') as sleep, self.assertRaises((http.DeadlineExceeded, requests.exceptions.RetryError)):
            http.get_response(self.session, 'https://i.pinimg.com/a', headers={},
                              deadline=time.monotonic() + 1)
        sleep.assert_not_called()
        self.assertTrue(body.closed)
        self.assertEqual(body.reads, 0)


if __name__ == '__main__':
    unittest.main()
