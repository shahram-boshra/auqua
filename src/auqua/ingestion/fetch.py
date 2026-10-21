# SPDX-FileCopyrightText: 2026 Asadollah Boshra
# SPDX-License-Identifier: MIT
"""HTTPS retrieval into the raw store (BP4 sections 4.1 and 4.4; Strategy section 7).

Standard library only (``urllib.request``): the default TLS context verifies
certificates and host names, and it is never replaced. Each request has an
explicit timeout and a ``User-Agent``; requests to one source are spaced by
its configured rate limit. HTTP 429 and 5xx responses, timeouts and connection
errors are retried with capped exponential backoff and full jitter (AWS
Architecture Blog, "Exponential Backoff And Jitter"), never sooner than a
``Retry-After`` the server sent (RFC 9110 section 10.2.3; RFC 6585 section 4),
for at most ``max_attempts`` attempts. Redirects are followed only to
``https://`` URLs. The response streams into the raw store with a size cap.
"""

import email.utils
import http.client
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import IO, Protocol

from auqua.config.ingestion import IngestionConfig
from auqua.ingestion.errors import FetchError
from auqua.ingestion.raw import RawFile, RawStore

USER_AGENT = "auqua-ingestion (+https://github.com/shahram-boshra/auqua)"
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_CHUNK = 1 << 16
_MS = 1000


class Response(Protocol):
    """The part of an HTTP response the fetcher uses."""

    status: int

    def read(self, size: int = -1, /) -> bytes:
        """Read up to ``size`` bytes."""
        ...

    def close(self) -> None:
        """Release the connection."""
        ...


type Opener = Callable[[urllib.request.Request, float], Response]


class HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to ``https://`` URLs (no downgrade to plain HTTP)."""

    def redirect_request(  # noqa: PLR0913, PLR0917 - signature of the standard handler
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        """Refuse a redirect that leaves HTTPS."""
        if not newurl.startswith("https://"):
            raise FetchError(f"redirect from {req.full_url} to non-https {newurl!r} refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(HttpsOnlyRedirects())


def _urlopen(request: urllib.request.Request, timeout: float) -> Response:
    response: Response = _OPENER.open(request, timeout=timeout)
    return response


def retry_after_s(value: str | None, now_s: float) -> float | None:
    """Seconds to wait from a ``Retry-After`` value (delay-seconds or HTTP-date), or ``None``."""
    if value is None:
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    try:
        when = email.utils.parsedate_to_datetime(text)
    except TypeError, ValueError:
        return None
    return max(0.0, when.timestamp() - now_s)


@dataclass
class Fetcher:
    """Rate-limited, retrying HTTPS client writing into a :class:`RawStore`.

    ``opener``, ``sleep``, ``monotonic``, ``wall_s`` and ``rng`` are injectable
    for tests; the defaults are the standard library's.
    """

    config: IngestionConfig
    store: RawStore
    opener: Opener = _urlopen
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    wall_s: Callable[[], float] = time.time
    rng: random.Random = field(default_factory=random.Random)
    _last_request: dict[str, float] = field(default_factory=dict)

    def _space(self, profile: str) -> None:
        limit = self.config.rate_limits.get(profile)
        if limit is None:
            return
        interval = float(Decimal(1) / limit)
        last = self._last_request.get(profile)
        if last is not None:
            wait = last + interval - self.monotonic()
            if wait > 0:
                self.sleep(wait)
        self._last_request[profile] = self.monotonic()

    def _backoff_s(self, attempt: int, retry_after: float | None) -> float:
        cap = self.config.backoff_cap_ms / _MS
        ceiling = min(cap, self.config.backoff_base_ms / _MS * 2**attempt)
        delay = self.rng.uniform(0, ceiling)
        return max(delay, retry_after) if retry_after is not None else delay

    def get(self, url: str, *, profile: str) -> RawFile:
        """Retrieve ``url`` for source ``profile`` into the raw store.

        Raises:
            FetchError: not HTTPS, a non-retryable status, the attempts are
                exhausted, or the body exceeds ``max_raw_bytes``.
        """
        if not url.startswith("https://"):
            raise FetchError(f"only https:// URLs are fetched, got {url!r}")
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
        last_error = "no attempt"
        for attempt in range(self.config.max_attempts):
            self._space(profile)
            retry_after: float | None = None
            try:
                response = self.opener(request, float(self.config.http_timeout_s))
            except urllib.error.HTTPError as error:
                with error:  # an HTTPError holds the response; release it
                    if error.code not in _RETRY_STATUS:
                        raise FetchError(f"{url}: HTTP {error.code}") from error
                    retry_after = retry_after_s(error.headers.get("Retry-After"), self.wall_s())
                    last_error = f"HTTP {error.code}"
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                last_error = type(error).__name__
            else:
                try:
                    return self.store.put(_chunks(response), max_bytes=self.config.max_raw_bytes)
                except (TimeoutError, ConnectionError) as error:
                    last_error = type(error).__name__
                finally:
                    response.close()
            if attempt + 1 < self.config.max_attempts:
                self.sleep(self._backoff_s(attempt, retry_after))
        raise FetchError(f"{url}: {self.config.max_attempts} attempts failed ({last_error})")


def _chunks(response: Response) -> Iterator[bytes]:
    return iter(lambda: response.read(_CHUNK), b"")
