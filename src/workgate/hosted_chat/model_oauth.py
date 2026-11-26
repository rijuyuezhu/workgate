"""Account authorization adapter and loopback callback from pinned v6.0.3.

Source: f0ecf8f8039345f24d0de4402983766787f4574e,
webui/model_oauth.py (fab8ab54c6e84a9d1caaeefa75bb6c16d622b00f).
See LICENSE.upstream. No route or provider activation here.

OAuth state and credentials never enter chat history or the browser's storage.
ChatGPT's OSS flow uses a loopback callback, not a hosted OAuth redirect.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import math
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt

from .model_discovery import _fetch_models
from .network import provider_client

TTL = 600
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_TOKEN_CHARS = 65536
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
AUTH_URL = ISSUER + "/api/accounts/authorize"
JWKS_URL = ISSUER + "/.well-known/jwks.json"


class ReauthorizationRequired(ValueError):
    pass


async def _identity_request(
    method: str, url: str, **kwargs
) -> tuple[int, object]:
    """Fetch only the configured identity origin; cap the complete response."""
    async with (
        asyncio.timeout(15),
        provider_client(ISSUER, timeout=10) as client,
        client.stream(method, url, **kwargs) as response,
    ):
        content = bytearray()
        async for chunk in response.aiter_bytes():
            if len(content) + len(chunk) > _MAX_RESPONSE_BYTES:
                raise ValueError("Identity response is too large")
            content.extend(chunk)
        try:
            value = json.loads(content)
        except ValueError:
            value = None
        return response.status_code, value


async def _token(data: dict) -> dict:
    try:
        status, result = await _identity_request("POST", TOKEN_URL, data=data)
        if (
            status in (400, 401)
            and isinstance(result, dict)
            and result.get("error")
            in (
                "invalid_grant",
                "invalid_client",
                "invalid_refresh_token",
                "token_expired",
                "refresh_token_expired",
                "refresh_token_invalidated",
                "refresh_token_reused",
            )
        ):
            raise ReauthorizationRequired(
                "ChatGPT authorization expired; sign in again"
            )
        if status != 200 or not isinstance(result, dict):
            raise ValueError("Invalid token response")
        return result
    except (httpx.HTTPError, TimeoutError, ValueError) as exc:
        if isinstance(exc, ReauthorizationRequired):
            raise
        raise ValueError("ChatGPT token exchange failed") from None


def _credentials(result: dict, previous: dict) -> dict:
    scopes = result.get("scope", previous.get("scope", ""))
    if not isinstance(scopes, str) or not {
        "chatgpt.tokens.use.direct",
        "resource.invoke",
    } <= set(scopes.split()):
        raise ReauthorizationRequired("ChatGPT plan usage was not authorized")
    expires = result.get("expires_in")
    if type(expires) is not int or not 0 < expires <= 86400:
        raise ValueError("Invalid OAuth token expiry")
    if str(result.get("token_type", "")).lower() != "bearer":
        raise ValueError("Invalid OAuth token type")
    for key in ("access_token", "refresh_token"):
        token = result.get(key)
        if (
            not isinstance(token, str)
            or not token
            or len(token) > _MAX_TOKEN_CHARS
            or any(ord(c) <= 32 or ord(c) == 127 for c in token)
        ):
            raise ValueError("Invalid OAuth token response")
    identity = result.get("id_token")
    if identity is not None and (
        not isinstance(identity, str)
        or not identity
        or len(identity) > _MAX_TOKEN_CHARS
    ):
        raise ValueError("Invalid OAuth identity token")
    earliest = result.get("earliest_refresh_at", 0)
    if type(earliest) not in (int, float) or not math.isfinite(earliest):
        earliest = 0
    return {
        **previous,
        "scope": scopes,
        "earliest_refresh_at": earliest,
        "access_token": result["access_token"],
        "refresh_token": result["refresh_token"],
        "expires_at": time.time() + expires,
        **(
            {"id_token": result["id_token"]}
            if isinstance(result.get("id_token"), str)
            else {}
        ),
    }


async def _verify_id(token: str, client_id: str, nonce: str | None) -> dict:
    try:
        if (
            not isinstance(token, str)
            or not token
            or len(token) > _MAX_TOKEN_CHARS
        ):
            raise ValueError("Invalid identity token")
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if (
            header.get("alg") != "RS256"
            or not isinstance(kid, str)
            or not 0 < len(kid) <= 1024
        ):
            raise ValueError("Invalid identity key")
        status, document = await _identity_request("GET", JWKS_URL)
        if (
            status != 200
            or not isinstance(document, dict)
            or not isinstance(document.get("keys"), list)
        ):
            raise ValueError("Invalid identity keys")
        keys = [
            key
            for key in document["keys"]
            if isinstance(key, dict)
            and key.get("kid") == kid
            and key.get("kty") == "RSA"
            and key.get("alg", "RS256") == "RS256"
            and key.get("use", "sig") == "sig"
        ]
        if len(keys) != 1:
            raise ValueError("Ambiguous or missing identity key")
        key = jwt.PyJWK.from_dict(keys[0], algorithm="RS256")
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=ISSUER,
            options={"require": ["exp", "sub", "iss", "aud"]},
        )
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise ValueError("Missing identity")
        if nonce is not None and claims.get("nonce") != nonce:
            raise ValueError("Invalid nonce")
        if (
            "azp" in claims
            or (isinstance(claims["aud"], list) and len(claims["aud"]) > 1)
        ) and claims.get("azp") != client_id:
            raise ValueError("Invalid authorized party")
        return claims
    except httpx.HTTPError, TimeoutError, jwt.PyJWTError, ValueError, TypeError:
        raise ValueError("Could not verify ChatGPT identity") from None


@dataclass(frozen=True)
class ChatGPTAuthorization:
    id: str = "chatgpt"
    label: str = "ChatGPT"
    protocol: str = "openai-responses"
    base_url: str = RESOURCE

    def authorize(
        self,
        redirect: str,
        host: str,
        state: str,
        nonce: str,
        verifier: str,
        previous: dict,
    ) -> str:
        params = {
            "client_id": previous.get("client_id", "dynamic_agent_client"),
            "ext_agent_host_id": host,
            "response_type": "code",
            "redirect_uri": redirect,
            "scope": SCOPE,
            "resource": RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()
            )
            .decode()
            .rstrip("="),
        }
        if not previous:
            params["agent_name_hint"] = "Workgate"
        # Do not expose retained ID tokens in browser-facing authorization URLs.
        return AUTH_URL + "?" + urlencode(params)

    async def exchange(
        self,
        query: dict,
        redirect: str,
        nonce: str,
        verifier: str,
        previous: dict,
    ) -> dict:
        if query.get("error"):
            raise ValueError("ChatGPT authorization was declined or failed")
        client_id = query.get("client_id") or previous.get("client_id")
        if (
            not isinstance(client_id, str)
            or not client_id.startswith("oaiapp_")
            or (previous and client_id != previous["client_id"])
        ):
            raise ValueError("Invalid ChatGPT client registration")
        code = query.get("code")
        if not isinstance(code, str) or not code:
            raise ValueError("Missing authorization code")
        result = await _token(
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect,
                "resource": RESOURCE,
            }
        )
        credentials = _credentials(result, {"client_id": client_id})
        claims = await _verify_id(
            credentials.get("id_token", ""), client_id, nonce
        )
        if (
            previous
            and previous.get("subject")
            and previous["subject"] != claims["sub"]
        ):
            raise ValueError(
                "Select the original ChatGPT account to reauthorize this channel"
            )
        return {**credentials, "subject": claims["sub"]}

    async def refresh(self, credentials: dict) -> dict:
        result = await _token(
            {
                "grant_type": "refresh_token",
                "client_id": credentials["client_id"],
                "refresh_token": credentials["refresh_token"],
                "resource": RESOURCE,
            }
        )
        updated = _credentials(result, credentials)
        if "id_token" in result:
            claims = await _verify_id(
                updated["id_token"], credentials["client_id"], None
            )
            if claims["sub"] != credentials["subject"]:
                raise ReauthorizationRequired(
                    "ChatGPT authorization identity changed"
                )
        return updated

    async def catalog(self, token: str) -> dict:
        return await _fetch_models(
            self.protocol, self.base_url, token, allow_empty=True, account=True
        )


# New account providers register an adapter, not a new inference protocol.
ADAPTERS = {"chatgpt": ChatGPTAuthorization()}


def adapter(method: str):
    if not isinstance(method, str) or method not in ADAPTERS:
        raise ValueError("Unsupported account authorization")
    return ADAPTERS[method]


def callback_query(raw: str, redirect: str, state: str) -> dict:
    """Accept only the exact redirect for this one-time authorization attempt."""
    if not isinstance(raw, str) or not raw or len(raw) > 32768:
        raise ValueError("Invalid authorization callback")
    target, expected = urlsplit(raw.strip()), urlsplit(redirect)
    if (
        (target.scheme, target.netloc, target.path)
        != (expected.scheme, expected.netloc, expected.path)
        or target.fragment
        or target.username is not None
        or target.password is not None
    ):
        raise ValueError(
            "Callback address does not match this authorization attempt"
        )
    query = parse_qs(target.query, keep_blank_values=True, max_num_fields=20)
    if any(
        len(values) != 1 for values in query.values()
    ) or not hmac.compare_digest(
        query.get("state", [""])[0].encode(), state.encode()
    ):
        raise ValueError("Invalid callback state")
    if bool(query.get("error", [""])[0]) == bool(query.get("code", [""])[0]):
        raise ValueError("Expected exactly one authorization code or error")
    return {key: values[0] for key, values in query.items()}


async def loopback(auth: ChatGPTAuthorization, host: str, previous: dict):
    """Return the authorization URL and internal one-shot completion/cleanup callbacks."""
    state, nonce, verifier = (secrets.token_urlsafe(48) for _ in range(3))
    future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()

    async def callback(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        accepted = False
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            parts = line.decode("ascii").split()
            if len(parts) != 3 or parts[0] != "GET":
                raise ValueError("Invalid callback")
            if not parts[1].startswith("/") or parts[1].startswith("//"):
                raise ValueError("Invalid callback path")
            query = callback_query(
                redirect.removesuffix("/auth/callback") + parts[1],
                redirect,
                state,
            )
            if future.done():
                raise ValueError("Duplicate callback")
            future.set_result(query)
            accepted = True
        except ValueError, UnicodeError, TimeoutError, OSError:
            pass
        finally:
            body = (
                b"Authorization received. Return to Workgate."
                if accepted
                else b"Invalid authorization callback."
            )
            writer.write(
                b"HTTP/1.1 "
                + (b"200 OK" if accepted else b"400 Bad Request")
                + b"\r\nContent-Type: text/plain\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            try:
                await writer.drain()
            except OSError:
                pass
            finally:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()

    listener = await asyncio.start_server(
        callback, "127.0.0.1", 0, limit=32768, start_serving=False
    )
    redirect = (
        f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}/auth/callback"
    )
    try:
        url = auth.authorize(redirect, host, state, nonce, verifier, previous)
        await listener.start_serving()
    except BaseException:
        listener.close()
        await listener.wait_closed()
        raise

    async def complete():
        try:
            query = await asyncio.wait_for(future, TTL)
            return await auth.exchange(
                query, redirect, nonce, verifier, previous
            )
        finally:
            listener.close()
            await listener.wait_closed()

    def submit(raw: str):
        if future.done():
            raise ValueError("Authorization callback has already been received")
        future.set_result(callback_query(raw, redirect, state))

    def close():
        listener.close()
        if not future.done():
            future.cancel()

    return url, complete, submit, close
