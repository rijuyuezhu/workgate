"""Public rendering gates and fresh Chromium JavaScript fallback."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from workgate.hosted_chat import web_render as render
from workgate.hosted_chat.web_research import WebResearchError

if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
    _BROWSER_CACHE = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
elif sys.platform == "darwin":
    _BROWSER_CACHE = str(Path.home() / "Library" / "Caches" / "ms-playwright")
elif sys.platform == "win32":
    _BROWSER_CACHE = str(
        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ms-playwright"
    )
else:
    _BROWSER_CACHE = str(
        Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
        / "ms-playwright"
    )


def route(
    url="https://example.org/script.js", method="GET", resource_type="script"
):
    return SimpleNamespace(
        request=SimpleNamespace(
            url=url,
            method=method,
            resource_type=resource_type,
            headers={
                "accept": "*/*",
                "cookie": "secret",
                "authorization": "Bearer secret",
            },
        ),
        abort=AsyncMock(),
        fulfill=AsyncMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,kind,url",
    [
        ("POST", "fetch", "https://example.org/"),
        ("GET", "image", "https://example.org/image"),
        ("GET", "websocket", "https://example.org/"),
        ("GET", "script", "http://127.0.0.1/"),
        ("GET", "script", "file:///etc/passwd"),
    ],
)
async def test_blocked_resources(monkeypatch, method, kind, url):
    def unexpected(_url):
        pytest.fail("blocked resource reached transport")

    monkeypatch.setattr(render, "PublicHTTPTransport", unexpected)
    request = route(url, method, kind)
    await render.RenderBudget().handle(request)
    request.abort.assert_awaited_once()
    request.fulfill.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "redirect",
        "private_redirect",
        "no_location",
        "encoding",
        "stream",
        "network",
        "bytes",
        "count",
    ],
)
async def test_resource_budget(monkeypatch, mode):
    seen = []

    async def response(request):
        seen.append(request)
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        assert request.headers["accept-encoding"] == "identity"
        if mode == "network":
            raise httpx.ConnectError("unreachable")
        if mode in {"redirect", "private_redirect", "no_location"}:
            headers = (
                {}
                if mode == "no_location"
                else {
                    "location": "http://127.0.0.1/"
                    if mode == "private_redirect"
                    else "/next"
                }
            )
            return httpx.Response(
                302, headers=headers, stream=httpx.ByteStream(b"")
            )
        headers = {
            "content-type": "application/javascript",
            "set-cookie": "token=secret",
            "alt-svc": "h3",
        }
        if mode == "encoding":
            headers["content-encoding"] = "gzip"
            return httpx.Response(
                200, headers=headers, stream=httpx.ByteStream(b"bad")
            )
        if mode == "stream":
            headers["content-type"] = "text/event-stream"
        return httpx.Response(
            200, headers=headers, stream=httpx.ByteStream(b"hello")
        )

    monkeypatch.setattr(
        render, "PublicHTTPTransport", lambda _: httpx.MockTransport(response)
    )
    budget = render.RenderBudget()
    if mode == "bytes":
        monkeypatch.setattr(render, "_MAX_BYTES", 2)
    if mode == "count":
        monkeypatch.setattr(render, "_RENDER_REQUESTS", 0)
    request = route()
    await budget.handle(request)
    if mode in {"success", "redirect"}:
        request.fulfill.assert_awaited_once()
        headers = request.fulfill.call_args.kwargs["headers"]
        assert "set-cookie" not in headers and "alt-svc" not in headers
        request.abort.assert_not_awaited()
    else:
        request.abort.assert_awaited_once()
        request.fulfill.assert_not_awaited()
    assert budget.exhausted == (mode in {"bytes", "count"})
    assert len(seen) == (0 if mode == "count" else 1)


@pytest.mark.asyncio
async def test_real_chromium_shell(monkeypatch):
    from playwright.async_api import Error, async_playwright

    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", _BROWSER_CACHE)
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                chromium_sandbox=True, timeout=5000
            )
        except Error as exc:
            pytest.skip(f"Sandboxed Chromium is not available: {exc}")
        await browser.close()

    seen = []

    async def response(request):
        seen.append(str(request.url))
        assert request.method == "GET"
        if request.url.path == "/":
            body = b"""<html><title>Public paper</title><body>
            <p id="paper">Loading...</p><script src="/paper.js"></script>
            </body></html>"""
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                stream=httpx.ByteStream(body),
            )
        assert request.url.path == "/paper.js"
        body = b"""document.querySelector('#paper').textContent = 'Rendered research paper.';
        fetch('http://127.0.0.1/private').catch(() => {});
        fetch('/write', {method:'POST', body:'no'}).catch(() => {});"""
        return httpx.Response(
            200,
            headers={"content-type": "application/javascript"},
            stream=httpx.ByteStream(body),
        )

    monkeypatch.setattr(
        render, "PublicHTTPTransport", lambda _: httpx.MockTransport(response)
    )
    monkeypatch.setattr(render, "_RENDER_SETTLE", 0.1)
    result = await render.render_html("https://example.org/")
    assert result["text"] == "Rendered research paper."
    assert seen == ["https://example.org/", "https://example.org/paper.js"]


@pytest.mark.asyncio
async def test_render_deadline(monkeypatch):
    monkeypatch.setattr(render, "_RENDER_DEADLINE", 0)
    with pytest.raises(WebResearchError, match="deadline"):
        await render.render_html("https://example.org/")
