"""Provider-origin transport adapted from final upstream v6.0.3 web_research.py.

Source blob f24dbfc21316f0570b943400d69e6b5c856d3e54; see LICENSE.upstream.
The pinned dialer preserves the request hostname for HTTP Host and TLS SNI.
"""

import asyncio
import ipaddress
import socket
from typing import cast
from urllib.parse import urlsplit

import httpcore
import httpx

_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "fc00::/7",
    )
)
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def _allowed_ip(
    ip: str, *, loopback: bool, private_http: bool, https: bool
) -> bool:
    if "%" in ip:
        return False
    address = ipaddress.ip_address(ip)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if loopback:
        return address.is_loopback
    if (
        address.is_unspecified
        or address.is_multicast
        or address.is_link_local
        or address.is_reserved
    ):
        return False
    # This shared-address-space endpoint is used for cloud instance metadata.
    if address == ipaddress.ip_address("100.100.100.200"):
        return False
    private = any(address in network for network in _PRIVATE_NETWORKS)
    if https:
        return address.is_global or private
    return private_http and private


def validate_provider_endpoint(
    value: str, *, allow_private_http: bool = False
) -> None:
    """Validate the configured URL and literal IPs without making a request."""
    if (
        type(allow_private_http) is not bool
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
        or "\\" in value
    ):
        raise ValueError("Invalid model provider URL")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid model provider URL") from None
    if (
        not host
        or parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or "#" in value
        or "?" in value
        or port == 0
        or "%" in host
    ):
        raise ValueError("Invalid model provider URL")
    https = parsed.scheme == "https"
    loopback = host in _LOOPBACK_NAMES
    if not (https or loopback or allow_private_http):
        raise ValueError("Private HTTP provider requires explicit opt-in")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return  # DNS addresses must be checked at dial time.
    if not _allowed_ip(
        host, loopback=loopback, private_http=allow_private_http, https=https
    ):
        raise ValueError(
            "Provider destination is outside the configured network policy"
        )


def _resolved_ips(
    host: str, port: int, *, loopback: bool, private_http: bool, https: bool
) -> list[str]:
    """Resolve at connection time; never dial the hostname after validation."""
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ips = list(
            dict.fromkeys(
                str(item[4][0])
                for item in addresses
                if item[0] in {socket.AF_INET, socket.AF_INET6}
            )
        )
        if ips and all(
            _allowed_ip(
                ip, loopback=loopback, private_http=private_http, https=https
            )
            for ip in ips
        ):
            return ips
    except ValueError, OSError:
        pass
    raise httpcore.ConnectError(
        "Provider destination is unresolved or outside the configured network policy"
    )


class _ProviderNetworkBackend(httpcore.AsyncNetworkBackend):
    """Dial only vetted addresses for the single configured provider origin."""

    def __init__(
        self, host: str, port: int, *, https: bool, allow_private_http: bool
    ):
        self._backend = cast(
            httpcore.AsyncNetworkBackend, httpcore.AnyIOBackend()
        )
        self.host = host
        self.port = port
        self.https = https
        self.loopback = host in _LOOPBACK_NAMES
        self.private_http = allow_private_http

    async def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        if (host, port) != (self.host, self.port):
            raise httpcore.ConnectError("Provider origin changed")
        try:
            async with asyncio.timeout(timeout):
                ips = await asyncio.to_thread(
                    _resolved_ips,
                    host,
                    port,
                    loopback=self.loopback,
                    private_http=self.private_http,
                    https=self.https,
                )
                failure = None
                for ip in ips:
                    try:
                        return await self._backend.connect_tcp(
                            ip,
                            port,
                            timeout=timeout,
                            local_address=local_address,
                            socket_options=socket_options,
                        )
                    except (
                        httpcore.ConnectError,
                        httpcore.ConnectTimeout,
                    ) as exc:
                        failure = exc
                if failure is not None:
                    raise failure
                raise httpcore.ConnectError("No allowed provider destination")
        except TimeoutError:
            raise httpcore.ConnectTimeout(
                "Provider connection timed out"
            ) from None

    async def connect_unix_socket(
        self, path, timeout=None, socket_options=None
    ):
        raise httpcore.ConnectError(
            "Unix sockets are not valid provider destinations"
        )

    async def sleep(self, seconds):
        await self._backend.sleep(seconds)


def _origin(url: httpx.URL) -> tuple[str, str, int]:
    return (
        url.scheme,
        url.raw_host.decode("ascii"),
        url.port
        if url.port is not None
        else (443 if url.scheme == "https" else 80),
    )


class ProviderHTTPTransport(httpx.AsyncHTTPTransport):
    """HTTPX transport limited to the configured provider origin and vetted IPs."""

    def __init__(self, base_url: str, *, allow_private_http: bool = False):
        validate_provider_endpoint(
            base_url, allow_private_http=allow_private_http
        )
        url = httpx.URL(base_url)
        scheme, host, port = _origin(url)
        super().__init__(trust_env=False)
        self.origin = (scheme, host, port)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=True, trust_env=False),
            network_backend=_ProviderNetworkBackend(
                host,
                port,
                https=scheme == "https",
                allow_private_http=allow_private_http,
            ),
        )

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        if (
            _origin(request.url) != self.origin
            or request.url.username
            or request.url.password
            or request.headers.get("host", "").lower()
            != request.url.netloc.decode("ascii").lower()
            or "sni_hostname" in request.extensions
        ):
            raise httpx.ConnectError(
                "Request does not match the configured provider origin",
                request=request,
            )
        return await super().handle_async_request(request)


def provider_client(
    base_url: str,
    *,
    allow_private_http: bool = False,
    timeout: float | httpx.Timeout = 10,
) -> httpx.AsyncClient:
    """No ambient proxy, redirect or cross-origin credential forwarding."""
    return httpx.AsyncClient(
        transport=ProviderHTTPTransport(
            base_url, allow_private_http=allow_private_http
        ),
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    )
