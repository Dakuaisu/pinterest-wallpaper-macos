"""Bounded, policy-checked Requests downloads for Pinterest sources.

Deadlines use time.monotonic() and are cooperative, not hard cancellation:
DNS, transport retries and slow reads require a supervising process for a
strict wall-clock limit. Sessions must honor Requests' response-hook protocol.
"""

import math
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import requests
from requests.structures import CaseInsensitiveDict

DEFAULT_MAX_BYTES = 20 * 1024 * 1024
FEED_PATH = '/resource/BoardFeedResource/get/'
BOARD_DOMAINS = (
    'pinterest.com', 'pinterest.co.uk', 'pinterest.ca', 'pinterest.com.au', 'pinterest.de',
    'pinterest.fr', 'pinterest.it', 'pinterest.es', 'pinterest.jp', 'pinterest.com.mx', 'pinterest.com.br',
)
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_WAIT = 5


class UnsafeURL(requests.RequestException):
    """The URL is outside the selected source policy."""


class ResponseTooLarge(requests.RequestException):
    """The declared or observed body exceeds the configured limit."""


class UnsupportedEncoding(requests.RequestException):
    """A response uses content encoding that cannot be strictly bounded."""


class DeadlineExceeded(requests.Timeout):
    """The cooperative monotonic deadline has elapsed."""


class TooManyRedirects(requests.TooManyRedirects):
    """More than five redirect hops were requested."""


class UnsafeRetryPolicy(requests.RequestException):
    """An adapter could consume a status response before the response hook."""


class _Redirect(Exception):
    def __init__(self, status, location, url):
        self.status = status
        self.location = location
        self.url = url
        super().__init__(status, location, url)


def is_board_host(host):
    """Pinterest redirects boards to per-country subdomains (in., br., uk., ...)."""
    return any(host == domain or host.endswith('.' + domain) for domain in BOARD_DOMAINS)


def _validate_url(url, kind):
    try:
        parts = urlsplit(url)
        host = parts.hostname
        valid_authority = (parts.scheme == 'https' and host is not None
                           and parts.username is None and parts.password is None
                           and parts.port in (None, 443))
    except ValueError as error:
        raise UnsafeURL(url) from error
    if not valid_authority or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise UnsafeURL(url)
    if kind == 'image':
        allowed = host == 'pinimg.com' or host.endswith('.pinimg.com')
    elif kind in ('board', 'rss'):
        allowed = is_board_host(host)
    elif kind == 'feed':
        allowed = host == 'www.pinterest.com' and parts.path == FEED_PATH
    else:
        raise ValueError(f'Unknown URL policy: {kind}')
    if not allowed:
        raise UnsafeURL(url)


def _remaining(deadline, timeout):
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DeadlineExceeded('HTTP deadline elapsed')
    return min(timeout, remaining)


def _redirect_hook(response, **kwargs):
    if response.status_code in REDIRECT_STATUSES and 'Location' in response.headers:
        event = _Redirect(response.status_code, response.headers['Location'], response.url)
        response.close()
        raise event
    return response


def _check_adapter(session, url):
    if isinstance(session, requests.Session):
        adapter = session.get_adapter(url)
        if isinstance(adapter, requests.adapters.HTTPAdapter):
            retry = adapter.max_retries
            if retry.status_forcelist or (retry.total and retry.respect_retry_after_header):
                raise UnsafeRetryPolicy('Disable adapter status and Retry-After retries')


def _retry_wait(value, retry_number):
    delay = 0 if retry_number == 1 else 0.5 * (2 ** (retry_number - 1))
    if value:
        value = value.strip()
        if value.isascii() and value.isdecimal():
            delay = float(value) if len(value) < 10 else math.inf
        else:
            try:
                date = parsedate_to_datetime(value)
                delay = max(0, date.timestamp() - time.time())
            except (TypeError, ValueError, OverflowError, OSError):
                pass
    return delay


def get_response(session, url, *, headers, params=None, max_bytes=DEFAULT_MAX_BYTES,
                 deadline=None, timeout=30, kind='image'):
    """Return a buffered Response after checking URLs, redirect hops and body size.

    `deadline` is an absolute monotonic timestamp. The response's original
    stream is always closed. Standard HTTPAdapters must disable status and
    Retry-After retries; retryable statuses raise RetryError after three retries.
    Only identity-encoded responses are accepted, even if requested otherwise.
    """
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError('max_bytes must be a nonnegative integer')
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be positive and finite')
    if deadline is not None and not math.isfinite(deadline):
        raise ValueError('deadline must be finite')
    request_headers = CaseInsensitiveDict(headers)
    request_headers['Accept-Encoding'] = 'identity'
    current = url
    query = params
    hop = 0
    retries = 0
    while True:
        _validate_url(current, kind)
        _check_adapter(session, current)
        budget = _remaining(deadline, timeout)
        try:
            response = session.get(current, headers=request_headers, params=query,
                                   stream=True, allow_redirects=False, timeout=budget,
                                   hooks={'response': _redirect_hook})
            # A Session-compatible test double may return without dispatching hooks.
            _redirect_hook(response)
        except _Redirect as redirect:
            if hop == 5:
                raise TooManyRedirects('More than five redirects') from None
            next_url = urljoin(redirect.url, redirect.location)
            if urlsplit(current).hostname != urlsplit(next_url).hostname:
                request_headers.pop('Authorization', None)
                request_headers.pop('Cookie', None)
            current, query = next_url, None
            hop += 1
            continue
        if response.status_code in RETRY_STATUSES:
            status = response.status_code
            retry_after = response.headers.get('Retry-After')
            response.close()
            if retries == 3:
                raise requests.exceptions.RetryError(f'HTTP status {status} after retries')
            retries += 1
            wait = _retry_wait(retry_after, retries)
            if wait > MAX_RETRY_WAIT or wait >= _remaining(deadline, timeout):
                raise requests.exceptions.RetryError('Retry delay exceeds available budget')
            if wait:
                time.sleep(wait)
            continue
        try:
            _remaining(deadline, timeout)
            encoding = response.headers.get('Content-Encoding', '').strip().lower()
            if encoding and encoding != 'identity':
                raise UnsupportedEncoding(encoding)
            length = response.headers.get('Content-Length')
            if length is not None and length.isascii() and length.isdecimal() and int(length) > max_bytes:
                raise ResponseTooLarge('Content-Length exceeds limit')
            body = bytearray()
            for chunk in response.iter_content(chunk_size=min(65536, max_bytes + 1)):
                _remaining(deadline, timeout)
                if len(chunk) > max_bytes - len(body):
                    raise ResponseTooLarge('Response body exceeds limit')
                body.extend(chunk)
            _remaining(deadline, timeout)
        finally:
            response.close()
            response.raw.close()
        response._content = bytes(body)
        response._content_consumed = True
        return response
