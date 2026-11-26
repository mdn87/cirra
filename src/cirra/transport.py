"""Bounded JSON transport. Callers construct routes; redirects never carry credentials."""

import json
import random
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from cirra.domain import (
    CirraError,
    ProviderAccessDenied,
    ProviderRateLimited,
    ReadUnavailable,
    canonical,
    number,
)
from cirra.github import NoRedirect, rate_limit_reset
from cirra.reviewer import unique_object


class RemoteUncertain(CirraError):
    """A write may have succeeded. Reconcile before another write."""


class RequestNotSent(ReadUnavailable):
    """The local deadline expired before sending the request."""


class ResourceMissing(CirraError):
    """The provider could not return the selected resource."""


class JSONClient:
    def __init__(self, origin, headers, *, timeout=60, opener=None, max_bytes=8 * 1024 * 1024):
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise CirraError("provider instance must be an explicit HTTPS origin")
        number(timeout, "HTTP timeout", 1, 600)
        self.origin, self.headers = origin.rstrip("/"), dict(headers)
        self.deadline, self.max_bytes = time.monotonic() + timeout, max_bytes
        self.opener = opener if opener is not None else build_opener(NoRedirect())
        self.last_link = ""

    def request(self, method, route, params=None, body=None, *, raw_text=False):
        if (
            method not in ("GET", "POST", "PATCH", "PUT")
            or not route.startswith("/")
            or "?" in route
        ):
            raise CirraError("invalid provider request")
        url = self.origin + route + ("?" + urlencode(params, doseq=True) if params else "")
        headers = {"Accept": "application/json", "User-Agent": "cirra", **self.headers}
        content = canonical(body).encode() if body is not None else None
        if content is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, headers=headers, data=content, method=method)
        for attempt in range(3 if method == "GET" else 1):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise RequestNotSent("provider request deadline exceeded before sending")
            try:
                with self.opener.open(request, timeout=min(10, remaining)) as response:
                    chunks, count = [], 0
                    read = getattr(response, "read1", response.read)
                    while True:
                        if time.monotonic() >= self.deadline:
                            raise TimeoutError()
                        chunk = read(min(65536, self.max_bytes + 1 - count))
                        if not chunk:
                            break
                        count += len(chunk)
                        if count > self.max_bytes:
                            raise ValueError()
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    data = (
                        raw.decode("utf-8")
                        if raw_text
                        else json.loads(raw, object_pairs_hook=unique_object)
                        if raw
                        else None
                    )
                    self.last_link = response.headers.get("Link", "")
                    more = bool(response.headers.get("X-Next-Page")) or bool(
                        re.search(r'rel="next"', self.last_link)
                    )
                    return data, more
            except HTTPError as exc:
                status = exc.code
                limited = (
                    status == 429
                    or status == 403
                    and (
                        exc.headers.get("X-RateLimit-Remaining") == "0"
                        or exc.headers.get("RateLimit-Remaining") == "0"
                        or "Retry-After" in exc.headers
                    )
                )
                retry_at = rate_limit_reset(exc.headers) if limited else None
                exc.close()
                if limited:
                    raise ProviderRateLimited(
                        f"provider rate limit (HTTP {status})", retry_at
                    ) from None
                if status in (401, 403):
                    raise ProviderAccessDenied(
                        f"provider refused request (HTTP {status}); check permissions or target"
                    ) from None
                if method == "GET" and status in (404, 410):
                    raise ResourceMissing(
                        f"provider refused request (HTTP {status}); check permissions or target"
                    ) from None
                if method != "GET" and status >= 500:
                    raise RemoteUncertain(
                        "provider write outcome is uncertain; reconcile"
                    ) from None
                if method == "GET" and status in (500, 502, 503, 504) and attempt == 2:
                    raise ReadUnavailable(f"provider read unavailable (HTTP {status})") from None
                if status not in (500, 502, 503, 504) or attempt == 2:
                    raise CirraError(
                        f"provider refused request (HTTP {status}); check permissions or target"
                    ) from None
            except (URLError, OSError, ValueError, UnicodeError, RecursionError, CirraError):
                if method != "GET":
                    raise RemoteUncertain(
                        "provider write outcome is uncertain; reconcile"
                    ) from None
                if attempt == 2:
                    raise ReadUnavailable("provider read failed after bounded retries") from None
            time.sleep(
                min(
                    random.uniform(0.15, 0.35) * 2**attempt,
                    max(0, self.deadline - time.monotonic()),
                )
            )
        raise CirraError("provider read did not complete")


def pages(get, route, *, params=None, max_pages=10):
    number(max_pages, "page limit", 1, 100, integral=True)
    result, seen = [], set()
    for page in range(1, max_pages + 1):
        items, more = get(route, {**(params or {}), "per_page": 100, "page": page})
        if not isinstance(items, list):
            raise CirraError("provider page is not a list")
        for item in items:
            if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] in seen:
                raise CirraError("malformed or duplicate provider page item")
            seen.add(item["id"])
            result.append(item)
        if not more:
            return result
    raise CirraError("provider page limit reached")
