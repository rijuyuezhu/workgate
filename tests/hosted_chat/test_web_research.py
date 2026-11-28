"""Native public search and DNS-pinned read-only downloader."""

import asyncio
import socket
from urllib.parse import parse_qs, urlsplit

import httpcore
import httpx
import pytest

from workgate.hosted_chat import web_research as web


@pytest.mark.parametrize(
    "url",
    [
        "",
        "file:///etc/passwd",
        "ftp://example.org/a",
        "http://localhost./",
        "https://private.local/",
        "https://127.0.0.1/",
        "http://169.254.169.254/",
        "https://[::ffff:127.0.0.1]/",
        "https://224.0.0.1/",
        "https://[fe80::1%25eth0]/",
        "https://user:password@example.org/",
        "https://example.org:1234/",
        "https://example.org:0/",
        "https://example.org/a b",
        "https://example.org/\\x",
    ],
)
def test_reject_nonpublic_urls(url):
    with pytest.raises(web.WebResearchError):
        web._safe_url(url)


def test_url_normalization():
    assert (
        web._safe_url("https://EXAMPLE.org#section") == "https://example.org/"
    )
    assert (
        web._safe_url("https://example.org/a?q=1#x")
        == "https://example.org/a?q=1"
    )


@pytest.mark.parametrize(
    "addresses,allowed",
    [
        (["8.8.8.8", "1.1.1.1"], True),
        (["8.8.8.8", "10.0.0.1"], False),
        (["100.100.100.200"], False),
        (["::ffff:169.254.169.254"], False),
        (["ff02::1"], False),
        ([], False),
    ],
)
def test_public_dns_all_addresses_checked(monkeypatch, addresses, allowed):
    def dns(host, port, **kwargs):
        return [
            (
                socket.AF_INET6 if ":" in ip else socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                (ip, port),
            )
            for ip in addresses
        ]

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    if allowed:
        assert web._public_ips("example.org", 443) == addresses
    else:
        with pytest.raises(httpcore.ConnectError):
            web._public_ips("example.org", 443)


@pytest.mark.asyncio
async def test_public_transport_dials_pinned_ip(monkeypatch):
    seen = []

    class Socket:
        async def connect_tcp(self, host, port, **kwargs):
            seen.append((host, port))
            return object()

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", port)),
        ],
    )
    transport = web.PublicHTTPTransport("https://example.org/a")
    backend = transport._pool._network_backend
    monkeypatch.setattr(backend, "_backend", Socket())
    assert await backend.connect_tcp("example.org", 443, timeout=5) is not None
    assert seen == [("8.8.8.8", 443)]
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("other.example.org", 443, timeout=5)
    await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "private_redirect",
        "redirect_loop",
        "oversize",
        "encoding",
        "binary",
        "http_error",
        "timeout",
        "missing_location",
        "network",
    ],
)
async def test_download_limits_and_redirects(monkeypatch, mode):
    seen = []

    async def reply(request):
        seen.append(str(request.url))
        if mode == "timeout":
            await asyncio.sleep(1)
        if mode == "network":
            raise httpx.ConnectError("test")
        if mode == "private_redirect":
            return httpx.Response(
                302, headers={"location": "http://127.0.0.1/"}
            )
        if mode == "missing_location":
            return httpx.Response(302)
        if mode == "redirect_loop":
            return httpx.Response(302, headers={"location": "/again"})
        if mode == "success" and request.url.host == "example.org":
            return httpx.Response(
                302, headers={"location": "https://other.example.org/page"}
            )
        status = 403 if mode == "http_error" else 200
        headers = {
            "content-type": "image/png" if mode == "binary" else "text/plain"
        }
        if mode == "encoding":
            headers["content-encoding"] = "gzip"
        return httpx.Response(
            status,
            headers=headers,
            stream=httpx.ByteStream(b"x" * (33 if mode == "oversize" else 3)),
        )

    monkeypatch.setattr(
        web, "PublicHTTPTransport", lambda _: httpx.MockTransport(reply)
    )
    if mode == "timeout":
        monkeypatch.setattr(web, "_FETCH_DEADLINE", 0.01)
    if mode == "success":
        assert await web._download("https://example.org/a", max_bytes=32) == (
            "https://other.example.org/page",
            "text/plain",
            b"xxx",
        )
        assert len(seen) == 2
    else:
        with pytest.raises(web.WebResearchError):
            await web._download("https://example.org/a", max_bytes=32)
        assert len(seen) == (5 if mode == "redirect_loop" else 1)


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"query": ""},
        {"query": " "},
        {"query": 1},
        {"query": "a" * 401},
        {"query": "good", "unexpected": True},
    ],
    ids=["missing", "empty", "blank", "type", "long", "extra"],
)
def test_invalid_query(args):
    with pytest.raises(web.WebResearchError):
        web.validate_search(args)


@pytest.mark.asyncio
async def test_ddgs_normalization_and_site_filter(monkeypatch):
    monkeypatch.setattr(
        web,
        "_ddgs",
        lambda query: [
            {
                "href": "https://arxiv.org/abs/123#page",
                "title": "Paper",
                "body": "Summary",
            },
            {"href": "https://arxiv.org/abs/123", "title": "duplicate"},
            {"href": "https://notarxiv.org/abs/123", "title": "wrong"},
            {"href": "https://127.0.0.1/", "title": "private"},
            {
                "href": "https://arxiv.org/abs/456",
                "title": "a" * 400,
                "body": "b" * 800,
            },
        ],
    )
    result = await web.web_search("  site:arxiv.org diffusion  ")
    assert result["query"] == "site:arxiv.org diffusion"
    assert [item["url"] for item in result["results"]] == [
        "https://arxiv.org/abs/123",
        "https://arxiv.org/abs/456",
    ]
    assert len(result["results"][1]["title"]) == 300
    assert len(result["results"][1]["snippet"]) == 700


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "invalid", "doctype", "empty"])
async def test_bing_fallback(monkeypatch, mode):
    def fail(query):
        raise RuntimeError("rate limited")

    async def download(url, *, max_bytes):
        assert urlsplit(url).hostname == "www.bing.com"
        assert parse_qs(urlsplit(url).query) == {
            "q": ["site:arxiv.org diffusion language model"],
            "format": ["rss"],
        }
        assert max_bytes == 1024 * 1024
        rows = [
            "<item><link>https://example.org/1</link><title>diffusion language model</title></item>",
            "<item><link>https://arxiv.org/abs/1</link><title>wrong</title></item>",
        ]
        rows += [
            f"<item><link>https://arxiv.org/abs/{i}</link><title>diffusion language model</title><description>&lt;b&gt;Summary&lt;/b&gt;</description></item>"
            for i in range(2, 15)
        ]
        body = {
            "success": "<rss><channel>" + "".join(rows) + "</channel></rss>",
            "invalid": "<",
            "doctype": '<!DOCTYPE rss [<!ENTITY test "test">]><rss/>',
            "empty": "<rss><channel/></rss>",
        }[mode]
        return url, "application/rss+xml", body.encode()

    monkeypatch.setattr(web, "_ddgs", fail)
    monkeypatch.setattr(web, "_download", download)
    if mode in {"invalid", "doctype"}:
        with pytest.raises(web.WebResearchError, match="invalid response"):
            await web.web_search("site:arxiv.org diffusion language model")
    else:
        result = await web.web_search("site:arxiv.org diffusion language model")
        assert len(result["results"]) == (10 if mode == "success" else 0)
        if mode == "success":
            assert result["results"][0]["snippet"] == "Summary"
        else:
            assert "message" in result
