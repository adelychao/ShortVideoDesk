from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit
from urllib.request import OpenerDirector, ProxyHandler, Request, build_opener


_LOCAL_OPENER: OpenerDirector = build_opener(ProxyHandler({}))


def open_local_url(request: Request, *, timeout: float):
    """Open an HTTP control URL on loopback without consulting system proxies."""
    parsed = urlsplit(request.full_url)
    hostname = parsed.hostname
    if parsed.scheme != "http" or hostname is None:
        raise ValueError("Local control requests must use an HTTP loopback URL")
    try:
        is_loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = hostname.casefold() == "localhost"
    if not is_loopback:
        raise ValueError("Local control requests must target a loopback address")
    return _LOCAL_OPENER.open(request, timeout=timeout)
