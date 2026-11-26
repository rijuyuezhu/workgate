"""Adapt pinned test_webui_web_research_edges.py DNS tests to provider origins.

Upstream v6.0.3 blob dbcc666c6346a7525e99b1de1b20f7b7a28910fa.
"""

import asyncio
import socket

import httpcore
import httpx
import pytest

from workgate.hosted_chat import network


def dns_answers(*ips):
    def resolve(host, port, **kwargs):
        return [
            (
                socket.AF_INET6 if ":" in ip else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (ip, port),
            )
            for ip in ips
        ]

    return resolve


@pytest.mark.asyncio
async def test_network_backend_connects_only_pinned_allowed_ips(monkeypatch):
    addresses = []

    class FakeSocketBackend:
        async def connect_tcp(self, host, port, **kwargs):
            addresses.append((host, port, kwargs["timeout"]))
            return object()

        async def sleep(self, seconds):
            return None

    monkeypatch.setattr(
        socket, "getaddrinfo", dns_answers("8.8.8.8", "1.1.1.1")
    )
    backend = network._ProviderNetworkBackend(
        "public.example.org", 443, https=True, allow_private_http=False
    )
    monkeypatch.setattr(backend, "_backend", FakeSocketBackend())
    assert (
        await backend.connect_tcp("public.example.org", 443, timeout=6)
        is not None
    )
    assert addresses == [("8.8.8.8", 443, 6)]
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_unix_socket("/run/container.sock")
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("other.example.org", 443, timeout=6)
    monkeypatch.setattr(
        socket, "getaddrinfo", dns_answers("8.8.8.8", "169.254.169.254")
    )
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("public.example.org", 443, timeout=6)
    assert len(addresses) == 1
    await backend.sleep(0)


@pytest.mark.parametrize(
    "host,https,private,ips,allowed",
    [
        ("localhost", False, False, ("127.0.0.1", "::1"), True),
        ("localhost", False, False, ("127.0.0.1", "8.8.8.8"), False),
        ("server.tailnet", False, True, ("100.64.1.2", "fd00::1"), True),
        ("server.tailnet", False, True, ("10.0.0.1", "8.8.8.8"), False),
        ("server.tailnet", False, False, ("10.0.0.1",), False),
        ("server.tailnet", True, False, ("10.0.0.1",), True),
        ("provider.example", True, False, ("8.8.8.8",), True),
        ("provider.example", True, False, ("::ffff:169.254.169.254",), False),
        ("provider.example", True, False, ("100.100.100.200",), False),
        ("provider.example", True, False, ("224.0.0.1",), False),
        ("provider.example", True, False, ("0.0.0.0",), False),
        ("provider.example", True, False, (), False),
    ],
)
def test_dial_time_address_policy(
    monkeypatch, host, https, private, ips, allowed
):
    monkeypatch.setattr(socket, "getaddrinfo", dns_answers(*ips))
    kwargs = {
        "loopback": host == "localhost",
        "private_http": private,
        "https": https,
    }
    if allowed:
        assert network._resolved_ips(host, 443, **kwargs) == list(ips)
    else:
        with pytest.raises(httpcore.ConnectError):
            network._resolved_ips(host, 443, **kwargs)


@pytest.mark.asyncio
async def test_connection_timeout_and_fallback(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", dns_answers("8.8.8.8", "1.1.1.1")
    )
    backend = network._ProviderNetworkBackend(
        "provider.example", 443, https=True, allow_private_http=False
    )

    class FakeBackend:
        async def connect_tcp(self, host, port, **kwargs):
            if host == "8.8.8.8":
                raise httpcore.ConnectError("first failed")
            return object()

    monkeypatch.setattr(backend, "_backend", FakeBackend())
    assert (
        await backend.connect_tcp("provider.example", 443, timeout=1)
        is not None
    )

    class FailedBackend:
        async def connect_tcp(self, *args, **kwargs):
            raise httpcore.ConnectError("no socket")

    monkeypatch.setattr(backend, "_backend", FailedBackend())
    with pytest.raises(httpcore.ConnectError, match="no socket"):
        await backend.connect_tcp("provider.example", 443, timeout=1)

    class SlowBackend:
        async def connect_tcp(self, *args, **kwargs):
            await asyncio.sleep(10)

    monkeypatch.setattr(backend, "_backend", SlowBackend())
    with pytest.raises(httpcore.ConnectTimeout):
        await backend.connect_tcp("provider.example", 443, timeout=0.01)


@pytest.mark.asyncio
async def test_origin_and_redirect_do_not_forward_credentials(monkeypatch):
    requests = []

    async def reply(reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        requests.append(headers)
        writer.write(
            b"HTTP/1.1 302 Found\r\nLocation: http://outside.invalid/\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(reply, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        async with network.provider_client(base) as client:
            response = await client.get(
                base + "/models", headers={"Authorization": "Bearer secret"}
            )
            assert response.status_code == 302
            assert len(requests) == 1 and b"Host: 127.0.0.1:" in requests[0]
            assert b"Authorization: Bearer secret" in requests[0]
            for url, headers in (
                ("http://outside.invalid/", {}),
                (base, {"Host": "outside.invalid"}),
            ):
                with pytest.raises(
                    httpx.ConnectError, match="configured provider origin"
                ):
                    await client.get(url, headers=headers)
            with pytest.raises(
                httpx.ConnectError, match="configured provider origin"
            ):
                await client.get(base + "/models", follow_redirects=True)
            assert len(requests) == 2


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example",
        "ftp://127.0.0.1",
        "https://user:pass@host",
        "https://host?x=y",
        "https://host/#fragment",
    ],
)
def test_invalid_transport_origins(url):
    with pytest.raises(ValueError):
        network.ProviderHTTPTransport(url)


@pytest.mark.asyncio
async def test_real_tls_preserves_hostname_and_sni(tmp_path, monkeypatch):
    import ssl
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    client_context = ssl.create_default_context(cafile=str(cert_path))
    names = []
    server_context.sni_callback = lambda _socket, hostname, _context: (
        names.append(hostname)
    )
    requests = []

    async def reply(reader, writer):
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(
        reply, "127.0.0.1", 0, ssl=server_context
    )
    async with server:
        assert server.sockets
        port = server.sockets[0].getsockname()[1]
        base = f"https://localhost:{port}"
        monkeypatch.setattr(
            httpx, "create_ssl_context", lambda **kwargs: client_context
        )
        async with network.provider_client(base) as client:
            assert (await client.get(base + "/models")).text == "ok"
            assert names == ["localhost"]
            assert f"Host: localhost:{port}".encode() in requests[0]
            request = client.build_request("GET", base)
            request.extensions["sni_hostname"] = "outside.invalid"
            with pytest.raises(httpx.ConnectError):
                await client.send(request)
            assert names == ["localhost"]


def test_invalid_dns_answers(monkeypatch):
    kwargs = {"loopback": False, "private_http": False, "https": True}
    for value in ("bad", "2001:4860::1%eth0"):
        monkeypatch.setattr(socket, "getaddrinfo", dns_answers(value))
        with pytest.raises(httpcore.ConnectError):
            network._resolved_ips("provider.example", 443, **kwargs)

    def failed(*args, **kwargs):
        raise socket.gaierror("unresolved")

    monkeypatch.setattr(socket, "getaddrinfo", failed)
    with pytest.raises(httpcore.ConnectError):
        network._resolved_ips("provider.example", 443, **kwargs)
