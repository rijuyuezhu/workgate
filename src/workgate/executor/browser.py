"""Executor-owned structured browser automation for shared sessions."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import os
import re
import secrets
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from pydantic import TypeAdapter, ValidationError

from ..errors import BrowserUnavailableError
from ..schemas.input_models.browser import BrowserActionsArg
from .config import ExecutorConfig
from .errors import ExecutorOperationFailure
from .tool_session.store import ToolSessionStore

_MAX_BROWSER_SESSIONS = 8
_MAX_PAGES_PER_BROWSER = 16
_MAX_SNAPSHOT_ELEMENTS = 200
_MAX_SNAPSHOT_TEXT_CHARS = 100_000
_MAX_ELEMENT_METADATA_CHARS = 2_000
_MAX_EVENT_CHARS = 2_000
_MAX_EVENTS = 50
_MAX_TIMEOUT_MS = 120_000
_MAX_WAIT_MS = 30_000
_MAX_FULL_PAGE_SCREENSHOT_DIMENSION = 16_384
_MAX_SCREENSHOT_PIXELS = 40_000_000
_IDLE_TIMEOUT_S = 60 * 60
_IDLE_REAP_INTERVAL_S = 60
_REF_ATTRIBUTE = "data-workgate-browser-ref"
_REF_RE = re.compile(r"^e[1-9][0-9]*$")
_BROWSER_ACTIONS_ADAPTER = TypeAdapter(BrowserActionsArg)


@lru_cache(maxsize=16)
def _browser_capability_available_cached(
    browsers_path: str | None,
    xdg_cache_home: str | None,
    home: str | None,
) -> bool:
    del browsers_path, xdg_cache_home, home

    def probe() -> bool:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                return Path(playwright.chromium.executable_path).is_file()
        except Exception:
            return False

    # Hello/session orientation can call this from an asyncio event loop, while
    # Playwright's sync API deliberately rejects same-thread async-loop usage.
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            return bool(executor.submit(probe).result(timeout=5))
        except Exception:
            return False


def browser_capability_available() -> bool:
    """Return whether Playwright and its Chromium executable are installed."""
    if importlib.util.find_spec("playwright") is None:
        return False
    return _browser_capability_available_cached(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),
        os.environ.get("XDG_CACHE_HOME"),
        os.environ.get("HOME"),
    )


def _new_browser_id() -> str:
    return f"browser_{secrets.token_urlsafe(18)}"


def _new_page_id() -> str:
    return f"page_{secrets.token_urlsafe(9)}"


def _bounded(value: object, maximum: int = _MAX_EVENT_CHARS) -> str:
    text = str(value)
    return text if len(text) <= maximum else text[:maximum]


def _navigation_url(value: str) -> str:
    url = value.strip()
    if url == "about:blank":
        return url
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "browser navigation only permits http://, https://, or about:blank URLs"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("browser navigation does not permit URL credentials")
    return url


@dataclass(slots=True)
class BrowserPageState:
    page_id: str
    page: Any
    refs: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class BrowserSessionState:
    browser_session_id: str
    owner_session_id: str
    playwright: Any
    browser: Any
    context: Any
    created_at: float
    last_used_at: float
    pages: dict[str, BrowserPageState] = field(default_factory=dict)
    errors: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=_MAX_EVENTS)
    )
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserService:
    """Own ephemeral Chromium resources scoped to Workgate sessions."""

    def __init__(self, config: ExecutorConfig, store: ToolSessionStore) -> None:
        self._config = config
        self._store = store
        self._sessions: dict[str, BrowserSessionState] = {}
        self._cleanup_pending: dict[str, BrowserSessionState] = {}
        self._closing: dict[str, BrowserSessionState] = {}
        self._starting = 0
        self._lock = asyncio.Lock()
        self._cleanup_lock = asyncio.Lock()
        self._closed = False
        self._reaper_task: asyncio.Task[None] | None = None

    async def manage(
        self,
        owner_session_id: str,
        *,
        action: str,
        browser_session_id: str | None = None,
        url: str | None = None,
        headless: bool = True,
        width: int = 1440,
        height: int = 1000,
        wait_until: str = "domcontentloaded",
    ) -> dict[str, Any]:
        """Start, list, or close browsers owned by one Workgate session."""
        self._require_owner(owner_session_id)
        normalized = action.strip().lower()
        if normalized == "start":
            result = await self.start(
                owner_session_id,
                url=url,
                headless=headless,
                width=width,
                height=height,
                wait_until=wait_until,
            )
            self._touch_owner(owner_session_id)
            return result
        if normalized == "list":
            await self._cleanup_idle()
            async with self._lock:
                sessions = [
                    state
                    for state in self._sessions.values()
                    if state.owner_session_id == owner_session_id
                ]
            result = {
                "sessions": [
                    await self._session_summary(state) for state in sessions
                ],
                "cleanup_pending": sorted(
                    browser_id
                    for browser_id, state in self._cleanup_pending.items()
                    if state.owner_session_id == owner_session_id
                ),
            }
            self._touch_owner(owner_session_id)
            return result
        if normalized == "close":
            if not browser_session_id:
                raise ValueError(
                    "browser_session_id is required for action=close"
                )
            result = await self.close(owner_session_id, browser_session_id)
            self._touch_owner(owner_session_id)
            return result
        raise ValueError("action must be start, list, or close")

    async def start(
        self,
        owner_session_id: str,
        *,
        url: str | None = None,
        headless: bool = True,
        width: int = 1440,
        height: int = 1000,
        wait_until: str = "domcontentloaded",
    ) -> dict[str, Any]:
        """Start one isolated ephemeral Chromium context."""
        self._require_owner(owner_session_id)
        if self._closed:
            raise RuntimeError("browser service is closed")
        if not browser_capability_available():
            raise BrowserUnavailableError(
                "Playwright is not installed on this executor",
            )
        if wait_until not in {
            "load",
            "domcontentloaded",
            "networkidle",
            "commit",
        }:
            raise ValueError("invalid wait_until")
        width = max(320, min(int(width), 7680))
        height = max(240, min(int(height), 4320))
        target_url = _navigation_url(url) if url else None

        await self._cleanup_idle()
        async with self._lock:
            if (
                len(self._sessions)
                + len(self._cleanup_pending)
                + len(self._closing)
                + self._starting
                >= _MAX_BROWSER_SESSIONS
            ):
                raise ValueError(
                    f"at most {_MAX_BROWSER_SESSIONS} browser sessions may be active"
                )
            self._starting += 1

        playwright = None
        browser = None
        context = None
        inserted = False
        state: BrowserSessionState | None = None
        try:
            try:
                from playwright.async_api import async_playwright
            except (
                ImportError
            ) as exc:  # pragma: no cover - availability precheck
                raise BrowserUnavailableError(
                    "Playwright is not installed on this executor",
                ) from exc

            try:
                playwright = await async_playwright().start()
                browser = await playwright.chromium.launch(
                    headless=bool(headless)
                )
            except Exception as exc:
                raise BrowserUnavailableError(
                    "Chromium is unavailable on this executor; install the Playwright Chromium runtime",
                ) from exc

            context = await browser.new_context(
                viewport={"width": width, "height": height}
            )
            now = time.time()
            state = BrowserSessionState(
                browser_session_id=_new_browser_id(),
                owner_session_id=owner_session_id,
                playwright=playwright,
                browser=browser,
                context=context,
                created_at=now,
                last_used_at=now,
            )
            page_state = await self._ensure_page(state)
            if target_url:
                await page_state.page.goto(
                    target_url, wait_until=wait_until, timeout=60_000
                )
            async with self._lock:
                if self._closed:
                    raise RuntimeError("browser service is closed")
                self._sessions[state.browser_session_id] = state
                self._starting -= 1
                inserted = True
            self._ensure_reaper()
            self._touch_owner(owner_session_id)
            return await self._session_summary(
                state, current_page_id=page_state.page_id
            )
        except Exception, asyncio.CancelledError:
            if inserted and state is not None:
                async with self._lock:
                    self._sessions.pop(state.browser_session_id, None)
            if state is not None:
                try:
                    await self._close_state(state)
                except Exception:
                    async with self._lock:
                        self._cleanup_pending[state.browser_session_id] = state
                    self._ensure_reaper()
            else:
                if context is not None:
                    with contextlib.suppress(Exception):
                        await context.close()
                if browser is not None:
                    with contextlib.suppress(Exception):
                        await browser.close()
                if playwright is not None:
                    with contextlib.suppress(Exception):
                        await playwright.stop()
            raise
        finally:
            if not inserted:
                async with self._lock:
                    if self._starting > 0:
                        self._starting -= 1

    async def close(
        self, owner_session_id: str, browser_session_id: str
    ) -> dict[str, Any]:
        """Close one browser after verifying Workgate-session ownership."""
        async with self._cleanup_lock:
            return await self._close_owned_browser(
                owner_session_id, browser_session_id
            )

    async def _close_owned_browser(
        self, owner_session_id: str, browser_session_id: str
    ) -> dict[str, Any]:
        state = await self._take_owned(owner_session_id, browser_session_id)
        try:
            await self._close_state(state)
        except Exception, asyncio.CancelledError:
            async with self._lock:
                self._closing.pop(browser_session_id, None)
                self._cleanup_pending[browser_session_id] = state
            raise
        finally:
            async with self._lock:
                if browser_session_id not in self._cleanup_pending:
                    self._closing.pop(browser_session_id, None)
        return {
            "browser_session_id": browser_session_id,
            "closed": True,
        }

    async def snapshot(
        self,
        owner_session_id: str,
        browser_session_id: str,
        *,
        page_id: str | None = None,
        include_text: bool = True,
        max_text_chars: int = _MAX_SNAPSHOT_TEXT_CHARS,
        max_elements: int = 100,
        screenshot_path: str | None = None,
        full_page: bool = False,
    ) -> dict[str, Any]:
        """Capture bounded page state and refresh short-lived element refs."""
        state = await self._get_owned(owner_session_id, browser_session_id)
        async with state.lock:
            current = await self._select_page(state, page_id)
            page = current.page
            state.last_used_at = time.time()
            bounded_text_chars = max(
                0, min(int(max_text_chars), _MAX_SNAPSHOT_TEXT_CHARS)
            )
            bounded_elements = max(
                1, min(int(max_elements), _MAX_SNAPSHOT_ELEMENTS)
            )
            elements = await self._capture_interactive_elements(
                current, bounded_elements
            )
            text: str | None = None
            text_truncated = False
            if include_text:
                data = await page.locator("body").evaluate(
                    """(element, limit) => {
                      const text = element.innerText || '';
                      return {
                        text: text.slice(0, limit),
                        truncated: text.length > limit
                      };
                    }""",
                    bounded_text_chars,
                )
                text = str(data["text"])
                text_truncated = bool(data["truncated"])

            rendered_path = None
            if screenshot_path is not None:
                rendered_path = await self._screenshot(
                    owner_session_id,
                    page,
                    screenshot_path,
                    full_page=bool(full_page),
                )

            await self._sync_pages(state)
            self._touch_owner(owner_session_id)
            return {
                "browser_session_id": browser_session_id,
                "page_id": current.page_id,
                "title": _bounded(await page.title()),
                "url": _bounded(page.url, 8192),
                "pages": await self._page_summaries(state),
                "text": text,
                "text_truncated": text_truncated,
                "interactive_elements": elements,
                "errors": list(state.errors),
                "screenshot_path": rendered_path,
            }

    async def act(
        self,
        owner_session_id: str,
        browser_session_id: str,
        actions: list[dict[str, Any]],
        *,
        page_id: str | None = None,
        timeout_ms: int = 30_000,
    ) -> dict[str, Any]:
        """Perform a bounded sequence of high-level actions."""
        try:
            actions = cast(
                list[dict[str, Any]],
                _BROWSER_ACTIONS_ADAPTER.validate_python(actions),
            )
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors(include_input=False)
            )
            raise ValueError(f"invalid browser actions: {details}") from None
        bounded_timeout = max(1, min(int(timeout_ms), _MAX_TIMEOUT_MS))
        state = await self._get_owned(owner_session_id, browser_session_id)
        results: list[dict[str, Any]] = []
        async with state.lock:
            current = await self._select_page(state, page_id)
            state.last_used_at = time.time()
            for index, raw_action in enumerate(actions):
                if not isinstance(raw_action, dict):
                    raise ValueError(f"actions[{index}] must be an object")
                action = str(raw_action.get("action") or "").strip().lower()
                if not action:
                    raise ValueError(f"actions[{index}].action is required")
                before_url = current.page.url
                result, current = await self._run_action(
                    state,
                    current,
                    action,
                    raw_action,
                    bounded_timeout,
                )
                if current.page.url != before_url:
                    current.refs.clear()
                results.append({"index": index, "action": action, **result})
            await self._sync_pages(state)
            self._touch_owner(owner_session_id)
            return {
                "browser_session_id": browser_session_id,
                "page_id": current.page_id,
                "title": _bounded(await current.page.title()),
                "url": _bounded(current.page.url, 8192),
                "pages": await self._page_summaries(state),
                "results": results,
            }

    async def close_owned(self, owner_session_id: str) -> list[str]:
        """Close every browser owned by one Workgate session."""
        async with self._cleanup_lock:
            return await self._close_all_owned(owner_session_id)

    async def _close_all_owned(self, owner_session_id: str) -> list[str]:
        async with self._lock:
            states = [
                state
                for state in (
                    *self._sessions.values(),
                    *self._cleanup_pending.values(),
                )
                if state.owner_session_id == owner_session_id
            ]
            for state in states:
                self._sessions.pop(state.browser_session_id, None)
                self._cleanup_pending.pop(state.browser_session_id, None)
                self._closing[state.browser_session_id] = state
        closed: list[str] = []
        first_error: BaseException | None = None
        for state in states:
            try:
                await self._close_state(state)
                closed.append(state.browser_session_id)
            except BaseException as exc:
                async with self._lock:
                    self._closing.pop(state.browser_session_id, None)
                    self._cleanup_pending[state.browser_session_id] = state
                if first_error is None:
                    first_error = exc
            finally:
                async with self._lock:
                    if state.browser_session_id not in self._cleanup_pending:
                        self._closing.pop(state.browser_session_id, None)
        if first_error is not None:
            raise first_error
        return closed

    async def aclose(self) -> None:
        """Close all executor-owned browser processes and make the service final."""
        self._closed = True
        reaper = self._reaper_task
        self._reaper_task = None
        if reaper is not None:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
        async with self._cleanup_lock:
            async with self._lock:
                unique = {
                    state.browser_session_id: state
                    for state in (
                        *self._sessions.values(),
                        *self._cleanup_pending.values(),
                        *self._closing.values(),
                    )
                }
                states = list(unique.values())
                self._sessions.clear()
                self._cleanup_pending.clear()
                self._closing.clear()
                for state in states:
                    self._closing[state.browser_session_id] = state
            first_error: BaseException | None = None
            for state in states:
                try:
                    await self._close_state(state)
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                finally:
                    async with self._lock:
                        self._closing.pop(state.browser_session_id, None)
            if first_error is not None:
                raise first_error

    async def _run_action(
        self,
        state: BrowserSessionState,
        current: BrowserPageState,
        action: str,
        data: dict[str, Any],
        timeout_ms: int,
    ) -> tuple[dict[str, Any], BrowserPageState]:
        page = current.page
        if action == "navigate":
            url = _navigation_url(str(data.get("url") or ""))
            wait_until = str(
                data.get("wait_until") or "domcontentloaded"
            ).strip()
            if wait_until not in {
                "load",
                "domcontentloaded",
                "networkidle",
                "commit",
            }:
                raise ValueError("invalid wait_until")
            response = await page.goto(
                url, wait_until=wait_until, timeout=timeout_ms
            )
            current.refs.clear()
            return {
                "status": response.status if response is not None else None,
                "url": _bounded(page.url, 8192),
            }, current
        if action == "new_page":
            await self._sync_pages(state)
            if len(state.pages) >= _MAX_PAGES_PER_BROWSER:
                raise ValueError(
                    f"at most {_MAX_PAGES_PER_BROWSER} pages may be open in one browser session"
                )
            page = await state.context.new_page()
            current = self._register_page(state, page)
            raw_url = str(data.get("url") or "").strip()
            if raw_url:
                await page.goto(
                    _navigation_url(raw_url),
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
            return {
                "page_id": current.page_id,
                "url": _bounded(page.url, 8192),
            }, current
        if action == "close_page":
            await page.close()
            await self._sync_pages(state)
            current = await self._select_page(state, None)
            return {
                "closed": True,
                "page_id": current.page_id,
            }, current
        if action == "wait":
            milliseconds = max(0, min(int(data.get("ms", 1000)), _MAX_WAIT_MS))
            await page.wait_for_timeout(milliseconds)
            return {"waited_ms": milliseconds}, current
        if action == "wait_for_text":
            text = str(data.get("text") or "")
            if not text:
                raise ValueError("wait_for_text requires text")
            await page.get_by_text(text).first.wait_for(timeout=timeout_ms)
            return {"matched": _bounded(text)}, current
        if action == "wait_for_url":
            url = str(data.get("url") or "")
            if not url:
                raise ValueError("wait_for_url requires url")
            await page.wait_for_url(url, timeout=timeout_ms)
            return {"url": _bounded(page.url, 8192)}, current

        target = str(data.get("target") or "").strip()
        if not target:
            raise ValueError(f"{action} requires target")
        locator = self._locator(current, target)
        if action == "click":
            await locator.click(timeout=timeout_ms)
        elif action == "fill":
            await locator.fill(str(data.get("value") or ""), timeout=timeout_ms)
        elif action == "type":
            await locator.press_sequentially(
                str(data.get("value") or ""), timeout=timeout_ms
            )
        elif action == "select":
            value = data.get("value")
            if isinstance(value, list):
                await locator.select_option(
                    [str(item) for item in value], timeout=timeout_ms
                )
            else:
                await locator.select_option(
                    str(value or ""), timeout=timeout_ms
                )
        elif action == "press":
            key = str(data.get("key") or "").strip()
            if not key:
                raise ValueError("press requires key")
            await locator.press(key, timeout=timeout_ms)
        elif action == "check":
            await locator.check(timeout=timeout_ms)
        elif action == "uncheck":
            await locator.uncheck(timeout=timeout_ms)
        elif action == "hover":
            await locator.hover(timeout=timeout_ms)
        else:
            raise ValueError(
                "unsupported browser action; use navigate, new_page, close_page, "
                "click, fill, type, select, press, check, uncheck, hover, wait, "
                "wait_for_text, or wait_for_url"
            )
        return {"target": target}, current

    async def _capture_interactive_elements(
        self, page_state: BrowserPageState, max_elements: int
    ) -> list[dict[str, Any]]:
        token = secrets.token_urlsafe(9)
        raw = await page_state.page.locator(
            "a,button,input,textarea,select,[role='button'],[role='link'],"
            "[contenteditable='true']"
        ).evaluate_all(
            """(elements, payload) => {
              const [attribute, token, maxElements, maxMetadataChars] = payload;
              const clip = (value) =>
                typeof value === 'string'
                  ? value.slice(0, maxMetadataChars)
                  : null;
              const visible = elements.filter((element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.visibility !== 'hidden'
                  && style.display !== 'none'
                  && rect.width > 0
                  && rect.height > 0;
              }).slice(0, maxElements);
              return visible.map((element, index) => {
                const ref = `e${index + 1}`;
                const marker = `${token}-${ref}`;
                element.setAttribute(attribute, marker);
                const text = (
                  element.innerText
                  || (
                    ['button', 'submit', 'reset'].includes(
                      (element.getAttribute('type') || '').toLowerCase()
                    ) ? element.value : ''
                  )
                  || element.getAttribute('aria-label')
                  || element.getAttribute('title')
                  || ''
                ).trim().slice(0, 500);
                return {
                  ref,
                  marker,
                  tag: element.tagName.toLowerCase(),
                  role: clip(element.getAttribute('role')),
                  type: clip(element.getAttribute('type')),
                  text,
                  name: clip(element.getAttribute('name')),
                  placeholder: clip(element.getAttribute('placeholder')),
                  href: clip(element.href || null),
                  disabled: Boolean(element.disabled),
                };
              });
            }""",
            [
                _REF_ATTRIBUTE,
                token,
                max_elements,
                _MAX_ELEMENT_METADATA_CHARS,
            ],
        )
        page_state.refs = {
            str(item["ref"]): f'[{_REF_ATTRIBUTE}="{item.pop("marker")}"]'
            for item in raw
        }
        return raw

    def _locator(self, page_state: BrowserPageState, target: str) -> Any:
        selector = page_state.refs.get(target)
        if selector is None:
            if _REF_RE.fullmatch(target):
                raise ValueError(
                    f"browser ref {target} is stale or unknown; take a new snapshot"
                )
            selector = target
        return page_state.page.locator(selector).first

    async def _screenshot(
        self,
        owner_session_id: str,
        page: Any,
        screenshot_path: str,
        *,
        full_page: bool,
    ) -> str:
        session = self._store.require_session(owner_session_id)
        raw = screenshot_path.strip()
        if not raw:
            raise ValueError("screenshot_path must not be empty")
        if Path(raw).suffix.lower() != ".png":
            raise ValueError("screenshot_path must end in .png")
        target = self._store.resolve_session_path(
            session,
            raw,
            must_exist=False,
            allow_missing_parent=False,
            follow_final_symlink=False,
        )
        if full_page:
            metrics = await page.evaluate(
                """() => {
                  const body = document.body;
                  const root = document.documentElement;
                  const width = Math.ceil(Math.max(
                    body ? body.scrollWidth : 0,
                    body ? body.offsetWidth : 0,
                    root ? root.scrollWidth : 0,
                    root ? root.offsetWidth : 0,
                    root ? root.clientWidth : 0
                  ));
                  const height = Math.ceil(Math.max(
                    body ? body.scrollHeight : 0,
                    body ? body.offsetHeight : 0,
                    root ? root.scrollHeight : 0,
                    root ? root.offsetHeight : 0,
                    root ? root.clientHeight : 0
                  ));
                  return {width, height};
                }"""
            )
            width = max(0, int(metrics.get("width", 0)))
            height = max(0, int(metrics.get("height", 0)))
            if (
                width > _MAX_FULL_PAGE_SCREENSHOT_DIMENSION
                or height > _MAX_FULL_PAGE_SCREENSHOT_DIMENSION
                or width * height > _MAX_SCREENSHOT_PIXELS
            ):
                raise ValueError(
                    "full-page browser screenshot exceeds the configured dimension limit"
                )
        payload = await page.screenshot(type="png", full_page=full_page)
        if len(payload) > self._config.max_view_image_bytes:
            raise ValueError(
                "browser screenshot exceeds the configured image byte limit"
            )
        try:
            with target.open("xb") as output:
                output.write(payload)
        except FileExistsError:
            raise FileExistsError(
                "browser screenshots never overwrite an existing path"
            ) from None
        workdir = Path(session.workdir).resolve()
        return target.relative_to(workdir).as_posix()

    def _require_owner(self, owner_session_id: str) -> None:
        self._store.admit_active_session(owner_session_id)

    def _touch_owner(self, owner_session_id: str) -> None:
        self._store.touch_session(owner_session_id)

    async def _get_owned(
        self, owner_session_id: str, browser_session_id: str
    ) -> BrowserSessionState:
        self._require_owner(owner_session_id)
        await self._cleanup_idle()
        async with self._lock:
            state = self._sessions.get(browser_session_id)
        if state is None or state.owner_session_id != owner_session_id:
            raise ValueError(f"unknown browser session: {browser_session_id}")
        try:
            connected = bool(state.browser.is_connected())
        except Exception:
            connected = False
        if not connected:
            async with self._lock:
                self._sessions.pop(browser_session_id, None)
                self._closing[browser_session_id] = state
            try:
                await self._close_state(state)
            except Exception, asyncio.CancelledError:
                async with self._lock:
                    self._cleanup_pending[browser_session_id] = state
            finally:
                async with self._lock:
                    self._closing.pop(browser_session_id, None)
            raise ExecutorOperationFailure(
                "browser_crashed",
                "browser process is no longer connected",
            )
        return state

    async def _take_owned(
        self, owner_session_id: str, browser_session_id: str
    ) -> BrowserSessionState:
        self._require_owner(owner_session_id)
        async with self._lock:
            state = self._sessions.get(browser_session_id)
            if state is None:
                state = self._cleanup_pending.get(browser_session_id)
            if state is None or state.owner_session_id != owner_session_id:
                raise ValueError(
                    f"unknown browser session: {browser_session_id}"
                )
            self._sessions.pop(browser_session_id, None)
            self._cleanup_pending.pop(browser_session_id, None)
            self._closing[browser_session_id] = state
            return state

    async def _cleanup_idle(self) -> None:
        async with self._cleanup_lock:
            await self._cleanup_idle_locked()

    async def _cleanup_idle_locked(self) -> None:
        cutoff = time.time() - _IDLE_TIMEOUT_S
        async with self._lock:
            candidates = list(self._cleanup_pending)
            candidates.extend(
                state.browser_session_id
                for state in self._sessions.values()
                if state.last_used_at < cutoff
            )
        for browser_session_id in dict.fromkeys(candidates):
            async with self._lock:
                state = self._cleanup_pending.get(browser_session_id)
                if state is not None and state.lock.locked():
                    continue
                if state is not None:
                    self._cleanup_pending.pop(browser_session_id, None)
                if state is None:
                    state = self._sessions.get(browser_session_id)
                    if (
                        state is None
                        or state.last_used_at >= cutoff
                        or state.lock.locked()
                    ):
                        continue
                    self._sessions.pop(browser_session_id, None)
                self._closing[browser_session_id] = state
            try:
                await self._close_state(state)
            except asyncio.CancelledError:
                async with self._lock:
                    self._cleanup_pending[browser_session_id] = state
                raise
            except Exception:
                async with self._lock:
                    self._cleanup_pending[browser_session_id] = state
            finally:
                async with self._lock:
                    self._closing.pop(browser_session_id, None)

    def _ensure_reaper(self) -> None:
        if self._closed:
            return
        task = self._reaper_task
        if task is not None and not task.done():
            return
        self._reaper_task = asyncio.create_task(
            self._reaper_loop(), name="workgate-browser-idle-reaper"
        )

    async def _reaper_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(_IDLE_REAP_INTERVAL_S)
            await self._cleanup_idle()

    async def _ensure_page(
        self, state: BrowserSessionState
    ) -> BrowserPageState:
        await self._sync_pages(state)
        if state.pages:
            return next(iter(state.pages.values()))
        return self._register_page(state, await state.context.new_page())

    async def _select_page(
        self, state: BrowserSessionState, page_id: str | None
    ) -> BrowserPageState:
        await self._sync_pages(state)
        if page_id:
            page = state.pages.get(page_id)
            if page is None:
                raise ValueError(f"unknown browser page: {page_id}")
            return page
        if state.pages:
            return next(reversed(state.pages.values()))
        return self._register_page(state, await state.context.new_page())

    async def _sync_pages(self, state: BrowserSessionState) -> None:
        live_pages = [
            page for page in state.context.pages if not page.is_closed()
        ]
        overflow = live_pages[_MAX_PAGES_PER_BROWSER:]
        for page in overflow:
            try:
                await page.close()
            except Exception as exc:
                raise ExecutorOperationFailure(
                    "browser_page_cleanup_failed",
                    "failed to close a browser page that exceeded the per-session page limit",
                ) from exc
        if overflow:
            live_pages = live_pages[:_MAX_PAGES_PER_BROWSER]
        live_ids = {id(page) for page in live_pages}
        for page_id, page_state in list(state.pages.items()):
            if id(page_state.page) not in live_ids:
                state.pages.pop(page_id, None)
        known = {id(item.page) for item in state.pages.values()}
        for page in live_pages:
            if id(page) not in known:
                self._register_page(state, page)

    def _register_page(
        self, state: BrowserSessionState, page: Any
    ) -> BrowserPageState:
        for item in state.pages.values():
            if item.page is page:
                return item
        page_state = BrowserPageState(page_id=_new_page_id(), page=page)
        state.pages[page_state.page_id] = page_state
        page.on(
            "pageerror",
            lambda error, pid=page_state.page_id: state.errors.append(
                {
                    "page_id": pid,
                    "kind": "pageerror",
                    "message": _bounded(error),
                }
            ),
        )
        page.on(
            "console",
            lambda message, pid=page_state.page_id: self._record_console_error(
                state, pid, message
            ),
        )
        page.on(
            "requestfailed",
            lambda request, pid=page_state.page_id: state.errors.append(
                {
                    "page_id": pid,
                    "kind": "requestfailed",
                    "method": _bounded(request.method, 32),
                    "url": _bounded(request.url, 8192),
                    "failure": _bounded(request.failure),
                }
            ),
        )
        page.on(
            "response",
            lambda response, pid=page_state.page_id: (
                state.errors.append(
                    {
                        "page_id": pid,
                        "kind": "http_error",
                        "method": _bounded(response.request.method, 32),
                        "url": _bounded(response.url, 8192),
                        "message": f"HTTP {response.status}",
                    }
                )
                if response.status >= 400
                else None
            ),
        )
        return page_state

    @staticmethod
    def _record_console_error(
        state: BrowserSessionState, page_id: str, message: Any
    ) -> None:
        if message.type == "error":
            state.errors.append(
                {
                    "page_id": page_id,
                    "kind": "console",
                    "message": _bounded(message.text),
                }
            )

    async def _session_summary(
        self,
        state: BrowserSessionState,
        *,
        current_page_id: str | None = None,
    ) -> dict[str, Any]:
        await self._sync_pages(state)
        return {
            "browser_session_id": state.browser_session_id,
            "current_page_id": current_page_id,
            "pages": await self._page_summaries(state),
            "created_at": state.created_at,
            "last_used_at": state.last_used_at,
        }

    @staticmethod
    async def _page_summaries(
        state: BrowserSessionState,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in state.pages.values():
            if item.page.is_closed():
                continue
            rows.append(
                {
                    "page_id": item.page_id,
                    "title": _bounded(await item.page.title()),
                    "url": _bounded(item.page.url, 8192),
                }
            )
        return rows

    @staticmethod
    async def _close_state(state: BrowserSessionState) -> None:
        try:
            await state.context.close()
        finally:
            try:
                await state.browser.close()
            finally:
                await state.playwright.stop()
