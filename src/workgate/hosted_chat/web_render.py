"""Fresh, bounded JavaScript rendering for public document shells (upstream #419)."""

import asyncio
import time
from urllib.parse import urljoin

import httpx

from .web_documents import html_problem, parse_document
from .web_research import (
    _MAX_BYTES,
    PublicHTTPTransport,
    WebResearchError,
    _safe_url,
)

_RENDER_DEADLINE = 25
_RENDER_REQUESTS = 64
_RENDER_SETTLE = 1.5
_RENDER_SLOTS = asyncio.Semaphore(2)
_REQUEST_HEADERS = {
    "accept",
    "accept-language",
    "user-agent",
    "referer",
    "origin",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-dest",
    "content-type",
}
_RESPONSE_HEADERS = {
    "content-type",
    "location",
    "content-security-policy",
    "access-control-allow-origin",
    "access-control-allow-methods",
    "access-control-allow-headers",
    "access-control-expose-headers",
    "cross-origin-resource-policy",
    "cross-origin-opener-policy",
    "cross-origin-embedder-policy",
    "x-content-type-options",
}


class RenderBudget:
    def __init__(self):
        self.requests = 0
        self.bytes = 0
        self.exhausted = False
        self.slots = asyncio.Semaphore(8)

    async def handle(self, route):
        request = route.request
        if request.method not in {"GET", "HEAD"} or request.resource_type in {
            "image",
            "media",
            "font",
            "eventsource",
            "websocket",
        }:
            await route.abort()
            return
        if self.requests >= _RENDER_REQUESTS:
            self.exhausted = True
            await route.abort()
            return
        self.requests += 1
        try:
            async with self.slots:
                safe = _safe_url(request.url)
                headers = {
                    key: value
                    for key, value in request.headers.items()
                    if key.lower() in _REQUEST_HEADERS
                }
                headers["accept-encoding"] = "identity"
                async with (
                    httpx.AsyncClient(
                        transport=PublicHTTPTransport(safe),
                        trust_env=False,
                        follow_redirects=False,
                        timeout=httpx.Timeout(12, connect=5),
                    ) as client,
                    client.stream(
                        request.method, safe, headers=headers
                    ) as response,
                ):
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise WebResearchError("Redirect has no Location")
                        _safe_url(urljoin(safe, location))
                    if (
                        response.headers.get(
                            "content-encoding", "identity"
                        ).lower()
                        != "identity"
                    ):
                        raise WebResearchError(
                            "Encoded browser resource is not supported"
                        )
                    if (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                        == "text/event-stream"
                    ):
                        raise WebResearchError(
                            "Streaming browser resource is not supported"
                        )
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        self.bytes += len(chunk)
                        if self.bytes > _MAX_BYTES:
                            self.exhausted = True
                            raise WebResearchError(
                                "Browser render exceeds download limit"
                            )
                        body.extend(chunk)
                    await route.fulfill(
                        status=response.status_code,
                        headers={
                            key: value
                            for key, value in response.headers.items()
                            if key.lower() in _RESPONSE_HEADERS
                        },
                        body=bytes(body),
                    )
        except WebResearchError, httpx.HTTPError:
            await route.abort()


async def render_html(url: str) -> dict:
    """Browser traffic is intercepted; unhandled network traffic hits a deny proxy."""
    safe = _safe_url(url)
    try:
        from playwright.async_api import Error as BrowserError
        from playwright.async_api import async_playwright
    except ImportError:
        raise WebResearchError(
            "JavaScript rendering requires Playwright Chromium"
        ) from None

    def reject(_reader, writer):
        writer.close()

    try:
        async with asyncio.timeout(_RENDER_DEADLINE), _RENDER_SLOTS:
            proxy = await asyncio.start_server(reject, "127.0.0.1", 0)
            async with proxy, async_playwright() as playwright:
                port = proxy.sockets[0].getsockname()[1]
                browser = await playwright.chromium.launch(
                    headless=True,
                    chromium_sandbox=True,
                    proxy={
                        "server": f"http://127.0.0.1:{port}",
                        "bypass": "<-loopback>",
                    },
                    args=[
                        "--disable-quic",
                        "--disable-background-networking",
                        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                    ],
                )
                try:
                    context = await browser.new_context(
                        service_workers="block",
                        accept_downloads=False,
                    )
                    budget = RenderBudget()
                    pending = set()

                    async def handle(route):
                        task = asyncio.current_task()
                        pending.add(task)
                        try:
                            await budget.handle(route)
                        except BrowserError:
                            pass  # Resource was closed or replaced by navigation.
                        finally:
                            pending.discard(task)

                    async def reject_socket(socket):
                        await socket.close()

                    try:
                        await context.route("**/*", handle)
                        await context.route_web_socket("**/*", reject_socket)
                        page = await context.new_page()
                        response = await page.goto(
                            safe, wait_until="domcontentloaded"
                        )
                        if response is None or response.status >= 400:
                            raise WebResearchError(
                                "Browser rendering could not access the page"
                            )
                        previous = None
                        stable_since = time.monotonic()
                        while True:
                            if budget.exhausted:
                                raise WebResearchError(
                                    "Browser rendering exceeded resource limits"
                                )
                            markup = await page.evaluate(
                                "(limit) => { const html = document.documentElement.outerHTML; "
                                "return html.length <= limit ? html : null; }",
                                _MAX_BYTES,
                            )
                            if (
                                markup is None
                                or len(payload := markup.encode("utf-8"))
                                > _MAX_BYTES
                            ):
                                raise WebResearchError(
                                    "Rendered HTML exceeds download limit"
                                )
                            resolved = _safe_url(page.url)
                            document = await asyncio.to_thread(
                                parse_document,
                                "text/html; charset=utf-8",
                                payload,
                                resolved,
                            )
                            problem = html_problem(document)
                            if problem and "access denied" in problem:
                                raise WebResearchError(problem)
                            current = document["text"]
                            now = time.monotonic()
                            if current != previous or pending:
                                previous, stable_since = current, now
                            if (
                                not problem
                                and not pending
                                and now - stable_since >= _RENDER_SETTLE
                            ):
                                return document
                            await asyncio.sleep(0.4)
                    finally:
                        await context.close()
                        tasks = list(pending)
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                finally:
                    await browser.close()
    except TimeoutError:
        raise WebResearchError(
            "JavaScript rendering exceeded its deadline"
        ) from None
    except BrowserError:
        raise WebResearchError(
            "JavaScript rendering failed; sandboxed Playwright Chromium is required"
        ) from None
