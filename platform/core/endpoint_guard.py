"""Qualify a customer-supplied HTTPS endpoint before anything connects to it (#159, #87).

A client can point Model Garden at their own MCP server or REST API. That is a server-side request to an address a customer
typed, so it must not become a way to reach the sidecar's own network, a cloud metadata service or another tenant. This module is
the credential-free check; it makes **no network call itself** (name resolution is an injected ``resolver``) and holds no
credential. A deployment runs ``qualify`` when the endpoint is added and ``assert_pinned`` on every connection.

- **Syntax first.** Only ``https``, a plain host name or public IP literal, no user information, no whitespace, control or
  backslash characters, no percent-encoding in the host, no query or fragment unless the caller allows a query, and an allowed
  port (443 by default). Host names are ASCII only (an IDN must be given as punycode, so lookalike names are visible), labels are
  valid, and single-label names and the local/internal suffixes (``localhost``, ``.local``, ``.internal``, ``.lan``, ``.home``,
  ``.corp``, ``.intranet``, ``.private``, ``.localdomain``) are refused.
- **No IP-literal tricks.** A host that *looks* numeric (decimal ``2130706433``, hex ``0x7f000001``, octal ``0177.0.0.1``, short
  ``127.1``) is refused unless it is a strict dotted quad, and then it is judged by address, not by text.
- **Public addresses only.** Loopback, private (RFC 1918 and IPv6 unique-local), link-local (including ``169.254.169.254``),
  carrier-grade NAT, multicast, reserved, documentation, benchmarking, site-local and unspecified addresses are refused, and so
  are IPv4-mapped, 6to4, NAT64 and Teredo forms whose embedded IPv4 address is not public.
- **Resolution is checked and pinned.** ``check_resolution`` requires every address a name resolves to to be public (one private
  answer among public ones refuses the lot, which is how DNS rebinding mixes answers) and returns the set to pin. ``assert_pinned``
  is what the connector calls with the address it is about to use, so a name that resolves differently later is refused.
- **No redirects off the origin.** ``redirect_allowed`` accepts only a redirect to the same scheme, host and port; a customer
  endpoint cannot bounce a request, and a bearer token, to another host.
- **Optional egress allow-list.** ``egress`` is a tuple of host names or ``*.example.com`` patterns (one label); when given, the
  host must match. A private network needs a separately approved bridge and is never reached through this path.
"""
from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any
from urllib.parse import urlsplit

DEFAULT_PORTS = (443,)
MAX_URL = 2048
MAX_ADDRESSES = 16
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_FORBIDDEN_URL_CHARS = re.compile(r"[\x00-\x20\x7f\\]")
_NUMERIC_LOOKING = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)$", re.IGNORECASE)
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".corp", ".intranet", ".private", ".localdomain", ".home.arpa")
_EXTRA_BLOCKED = tuple(ipaddress.ip_network(item) for item in (
    "100.64.0.0/10",      # carrier-grade NAT
    "192.0.0.0/24",       # IETF protocol assignments
    "198.18.0.0/15",      # benchmarking
    "240.0.0.0/4",        # reserved
    "fec0::/10",          # deprecated site-local
    "2001::/32",          # Teredo
    "2001:db8::/32",      # documentation
))
Resolver = Callable[[str], Iterable[str]]


class EndpointError(ValueError):
    """Raised when an endpoint, a resolution or a redirect is not acceptable; nothing is contacted."""


def _embedded_v4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address in ipaddress.ip_network("64:ff9b::/96"):
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def is_public_ip(value: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for an address on the public internet (none of the blocked categories above)."""
    try:
        address = ipaddress.ip_address(value) if isinstance(value, str) else value
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        embedded = _embedded_v4(address)
        if embedded is not None:
            return is_public_ip(embedded)
    if (address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or address.is_reserved
            or address.is_unspecified or not address.is_global):
        return False
    return not any(address in network for network in _EXTRA_BLOCKED if network.version == address.version)


def _host(raw: str) -> tuple[str, bool]:
    """The cleaned host and whether it is an IP literal; raises ``EndpointError`` for anything not a plain public-looking host."""
    host = raw.lower().rstrip(".") if not raw.startswith("[") else raw
    if not host or len(host) > 253:
        raise EndpointError("the endpoint needs a host name")
    if host.startswith("["):
        if not host.endswith("]"):
            raise EndpointError("a malformed IPv6 address")
        try:
            address = ipaddress.IPv6Address(host[1:-1])
        except ValueError as exc:
            raise EndpointError("a malformed IPv6 address") from exc
        if "%" in host or not is_public_ip(address):
            raise EndpointError("the endpoint is an address that is not on the public internet")
        return str(address), True
    if "%" in host or not host.isascii():
        raise EndpointError("host names are plain ASCII (give an international name as punycode, xn--…)")
    labels = host.split(".")
    if _NUMERIC_LOOKING.match(labels[-1]):
        try:
            address = ipaddress.IPv4Address(host)  # strict dotted quad only; decimal, hex, octal and short forms raise
        except ValueError as exc:
            raise EndpointError("a numeric host must be a plain dotted IPv4 address") from exc
        if not is_public_ip(address):
            raise EndpointError("the endpoint is an address that is not on the public internet")
        return str(address), True
    if len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        raise EndpointError("the host must be a fully qualified public name")
    if host == "localhost" or any(host.endswith(suffix) for suffix in _LOCAL_SUFFIXES):
        raise EndpointError("local and internal names are not reachable through this path")
    return host, False


def _matches(host: str, pattern: str) -> bool:
    pattern = pattern.lower().rstrip(".")
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return host.endswith(suffix) and "." not in host[: -len(suffix)] and len(host) > len(suffix)
    return host == pattern


def parse_endpoint(
    url: Any, *, allow_ports: Sequence[int] = DEFAULT_PORTS, allow_query: bool = False, egress: Sequence[str] | None = None
) -> dict[str, Any]:
    """The checked parts of an endpoint URL (``scheme``, ``host``, ``port``, ``path``, ``ipLiteral``, and ``query`` if allowed)."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL or _FORBIDDEN_URL_CHARS.search(url):
        raise EndpointError("the endpoint is a single-line https address")
    try:
        parts = urlsplit(url)
        port = parts.port
        netloc = parts.netloc
    except ValueError as exc:
        raise EndpointError("the endpoint address is malformed") from exc
    if parts.scheme != "https":
        raise EndpointError("only https endpoints are accepted")
    if "@" in netloc or parts.username is not None or parts.password is not None:
        raise EndpointError("an endpoint carries no user information; credentials are entered separately")
    if parts.fragment or (parts.query and not allow_query):
        raise EndpointError("an endpoint has no fragment" + ("" if allow_query else " or query"))
    hostname = netloc.rsplit(":", 1)[0] if port is not None else netloc
    host, is_ip = _host(hostname)
    effective = 443 if port is None else port
    if effective not in tuple(allow_ports):
        raise EndpointError(f"port {effective} is not allowed (allowed: {', '.join(str(item) for item in allow_ports)})")
    path = parts.path or "/"
    if "/../" in path + "/" or path.endswith("/..") or "%2e" in path.lower() or "%2f" in path.lower():
        raise EndpointError("the path may not climb or hide separators")
    if egress is not None and not any(_matches(host, pattern) for pattern in egress):
        raise EndpointError("the host is not on this client's egress allow-list")
    result: dict[str, Any] = {"scheme": "https", "host": host, "port": effective, "path": path, "ipLiteral": is_ip}
    if parts.query:
        result["query"] = parts.query
    return result


def check_resolution(host: str, resolver: Resolver) -> list[str]:
    """The public addresses ``host`` resolves to, to be pinned; refuses an empty answer or any non-public address among them."""
    try:
        addresses = sorted({str(ipaddress.ip_address(item)) for item in resolver(host)})
    except (ValueError, OSError) as exc:
        raise EndpointError("the host did not resolve to valid addresses") from exc
    if not addresses:
        raise EndpointError("the host did not resolve")
    if len(addresses) > MAX_ADDRESSES:
        raise EndpointError("the host resolves to too many addresses")
    if not all(is_public_ip(item) for item in addresses):
        raise EndpointError("the host resolves to an address that is not on the public internet")
    return addresses


def qualify(
    url: str,
    resolver: Resolver,
    *,
    allow_ports: Sequence[int] = DEFAULT_PORTS,
    allow_query: bool = False,
    egress: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Syntax and resolution in one call; returns the parts plus ``pinned``, the addresses a connection may use."""
    parts = parse_endpoint(url, allow_ports=allow_ports, allow_query=allow_query, egress=egress)
    pinned = [parts["host"]] if parts["ipLiteral"] else check_resolution(parts["host"], resolver)
    return {**parts, "pinned": pinned}


def assert_pinned(address: str, pinned: Iterable[str]) -> str:
    """Refuse a connection to an address that is not in the pinned set (or is not public), whatever the name now resolves to."""
    try:
        normal = str(ipaddress.ip_address(address))
    except ValueError as exc:
        raise EndpointError("the connection address is not an IP address") from exc
    if not is_public_ip(normal) or normal not in {str(ipaddress.ip_address(item)) for item in pinned}:
        raise EndpointError("the connection address is not one this endpoint was qualified with")
    return normal


def redirect_allowed(current_url: str, location: str) -> bool:
    """Only a redirect that stays on the same https origin; the target is re-checked with ``parse_endpoint`` on the caller's terms."""
    try:
        now = urlsplit(current_url)
        target = urlsplit(location)
        origin = ("https", now.hostname, now.port or 443)
        if target.scheme and (target.scheme.lower(), target.hostname, target.port or 443) != origin:
            return False
        if not target.scheme and target.netloc and (target.hostname, target.port or 443) != origin[1:]:
            return False
        return now.scheme == "https" and not target.username and not _FORBIDDEN_URL_CHARS.search(location)
    except ValueError:
        return False
