"""One-use browser OAuth flow for an already approved foreground Action."""

import base64
import hashlib
import json
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .action_effects import ActionEffects
from .action_oauth_tokens import ActionOAuthTokens, exchange
from .action_runtime import _allowed_domain
from .actions import ActionRepository
from .approval_claims import ApprovalClaimStore
from .checkpoints import SQLiteCheckpointSaver
from .credential_cipher import CredentialUnavailable
from .generation_protocol import EVENT_LIMIT, MAX_EVENT_BYTES
from .generations import _json
from .provider_common import ModelToolCall
from .storage import _now

_FLOW_TTL = 600
_CALLBACK = "/api/ui/chat/actions/oauth/callback"
_RESERVED = {
    "client_id",
    "client_secret",
    "code",
    "code_verifier",
    "scope",
    "redirect_uri",
    "access_type",
    "response_type",
    "state",
    "code_challenge",
    "code_challenge_method",
}


def callback_uri(base: str) -> str:
    parsed = urlsplit(base)
    if (
        not parsed.hostname
        or not (
            parsed.scheme == "https"
            or (
                parsed.scheme == "http"
                and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            )
        )
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Configure an explicit HTTPS Control callback origin")
    return base.rstrip("/") + _CALLBACK


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _cookie_binding(flow) -> str:
    return json.dumps(
        [
            "action-oauth-browser",
            flow["owner"],
            flow["action_id"],
            flow["action_version"],
            flow["run_id"],
            flow["call_id"],
            flow["state_hash"],
            flow["redirect_uri"],
        ]
    )


@dataclass(frozen=True)
class BrowserFlow:
    authorization_url: str = field(repr=False)
    cookie: str = field(repr=False)
    expires_at: int = 0


@dataclass(frozen=True)
class CallbackClaim:
    state_hash: str
    owner: str
    action_id: str
    version: int
    run_id: str
    call_id: str
    redirect_uri: str
    verifier: str = field(repr=False)
    metadata: dict = field(repr=False)
    credentials: dict = field(repr=False)


class ActionOAuthFlows:
    def __init__(self, repository: ActionRepository):
        self.repo = repository
        self.database = repository.database
        self.tokens = ActionOAuthTokens(repository)
        self.approvals = ApprovalClaimStore(self.database)

    def pause(self, effects: ActionEffects, call: ModelToolCall) -> dict:
        lease = effects.lease
        checkpoint = effects.checkpoint
        if lease.database.path != self.database.path:
            raise PermissionError(
                "OAuth checkpoint belongs to another database"
            )
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state, edge, proof, record = effects._context(db, call)
            effects._approved(state, edge, proof, call)
            if record[0].get("auth", {}).get("type") != "oauth":
                raise ValueError("Expected OAuth Action")
            row = self.approvals._row(db, lease.owner, lease.run_id, "running")
            pending = {
                "actionId": "action_" + uuid.uuid4().hex,
                "streamId": row["conversation_id"],
                "conversationId": row["conversation_id"],
                "runId": lease.run_id,
                "responseMessageId": state["responseMessageId"],
                "createdAt": int(time.time() * 1000),
                "namespace": "",
                "payload": {
                    "type": "action_oauth",
                    "action_id": proof["actionId"],
                    "action_version": proof["version"],
                    "tool_call_id": call.call_id,
                    "tool_name": call.name,
                },
            }
            state["approvalCheckpointId"] = proof["checkpointId"]
            self.approvals._verify_oauth_pending(
                db, lease.owner, row, state, pending, edge
            )
            event = _json({"event": "on_pending_action", "data": pending})
            if (
                len(event.encode()) > MAX_EVENT_BYTES
                or db.execute(
                    "SELECT count(*) FROM generation_events WHERE run_id=?",
                    (lease.run_id,),
                ).fetchone()[0]
                >= EVENT_LIMIT
            ):
                raise ValueError("OAuth interruption exceeds event limit")
            checkpoint.saver._put_writes(
                db,
                checkpoint.active,
                [
                    (
                        "__interrupt__",
                        {"id": pending["actionId"], "value": pending},
                    )
                ],
                "human",
            )
            state["pendingAction"] = pending
            effects._grant()
            db.execute(
                "UPDATE runs SET state=?,status='requires_action',updated_at=? WHERE id=?",
                (_json(state), _now(), lease.run_id),
            )
            db.execute(
                "DELETE FROM generation_actor_leases WHERE run_id=?",
                (lease.run_id,),
            )
            db.execute(
                "INSERT INTO generation_events(run_id,data) VALUES(?,?)",
                (lease.run_id, event),
            )
            return pending

    def _pending(self, db, saver, run_id):
        owner = saver.owner
        row = self.approvals._row(db, owner, run_id, "requires_action")
        state = json.loads(row["state"])
        pending = state.get("pendingAction")
        if not isinstance(pending, dict) or self.approvals._expired(pending):
            raise PermissionError("OAuth authorization is unavailable")
        edge = self.approvals._pending_edge(db, saver, row, state, pending)
        record = self.approvals._verify_oauth_pending(
            db, owner, row, state, pending, edge
        )
        return state, pending, record

    def begin(
        self,
        owner: str,
        run_id: str,
        pending_id: str,
        base: str,
        *,
        check_grant: Callable[[], None],
    ) -> BrowserFlow:
        redirect = callback_uri(base)
        opaque = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        now = int(time.time())
        saver = SQLiteCheckpointSaver(self.database, owner)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            check_grant()
            _, pending, (meta, creds, op) = self._pending(db, saver, run_id)
            if pending["actionId"] != pending_id:
                raise PermissionError("OAuth authorization was superseded")
            auth = meta["auth"]
            target = urlsplit(auth["authorization_url"])
            token_target = urlsplit(auth["client_url"])
            if (
                not target.hostname
                or not token_target.hostname
                or not all(
                    _allowed_domain(host)
                    for host in (target.hostname, token_target.hostname)
                )
            ):
                raise PermissionError("OAuth provider domain not allowed")
            query = parse_qsl(
                target.query, keep_blank_values=True, max_num_fields=64
            )
            if any(k in _RESERVED for k, _ in query):
                raise ValueError(
                    "OAuth authorization URL contains reserved parameters"
                )
            query += [
                ("client_id", creds["oauth_client_id"]),
                ("scope", auth["scope"]),
                ("redirect_uri", redirect),
                ("access_type", "offline"),
                ("response_type", "code"),
                ("state", opaque),
                ("code_challenge", challenge),
                ("code_challenge_method", "S256"),
            ]
            url = urlunsplit(
                (
                    target.scheme,
                    target.netloc,
                    target.path,
                    urlencode(query),
                    "",
                )
            )
            if len(url) > 8192:
                raise ValueError("OAuth authorization URL exceeds limit")
            action_id = op["action_id"]
            flow = {
                "state_hash": _hash(opaque),
                "owner": owner,
                "action_id": action_id,
                "action_version": op["action_version"],
                "run_id": run_id,
                "call_id": pending["payload"]["tool_call_id"],
                "redirect_uri": redirect,
                "expires_at": now + _FLOW_TTL,
            }
            cookie = self.repo.cipher.seal(
                {"verifier": verifier}, binding=_cookie_binding(flow)
            )
            db.execute(
                "DELETE FROM action_oauth_flows WHERE expires_at<=? OR (owner=? AND action_id=?)",
                (now, owner, action_id),
            )
            db.execute(
                "INSERT INTO action_oauth_flows(state_hash,owner,action_id,action_version,run_id,call_id,redirect_uri,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                tuple(flow.values()),
            )
            check_grant()
            return BrowserFlow(url, cookie, flow["expires_at"])

    def _current(self, db, saver, key):
        flow = db.execute(
            "SELECT * FROM action_oauth_flows WHERE state_hash=?", (key,)
        ).fetchone()
        if (
            flow is None
            or flow["owner"] != saver.owner
            or flow["expires_at"] <= int(time.time())
        ):
            raise PermissionError("OAuth authorization expired or superseded")
        _, pending, record = self._pending(db, saver, flow["run_id"])
        payload = pending["payload"]
        if (
            payload["action_id"],
            payload["action_version"],
            payload["tool_call_id"],
        ) != (flow["action_id"], flow["action_version"], flow["call_id"]):
            raise PermissionError("OAuth authorization changed")
        return flow, record

    def claim(self, state: str, cookie: str, redirect: str) -> CallbackClaim:
        if not 20 <= len(state) <= 256 or not 1 <= len(cookie) <= 8192:
            raise PermissionError("Invalid OAuth browser binding")
        key = _hash(state)
        with self.database.read_snapshot() as db:
            row = db.execute(
                "SELECT owner FROM action_oauth_flows WHERE state_hash=?",
                (key,),
            ).fetchone()
        if row is None:
            raise PermissionError("OAuth authorization expired or superseded")
        saver = SQLiteCheckpointSaver(self.database, row["owner"])
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            flow, (meta, creds, _) = self._current(db, saver, key)
            if flow["used_at"] is not None or flow["redirect_uri"] != redirect:
                raise PermissionError("OAuth callback was used or changed")
            try:
                proof = self.repo.cipher.unseal(
                    cookie, binding=_cookie_binding(flow)
                )
            except CredentialUnavailable:
                raise PermissionError("Invalid OAuth browser binding") from None
            verifier = proof.get("verifier")
            if not isinstance(verifier, str) or not 43 <= len(verifier) <= 128:
                raise PermissionError("Invalid OAuth verifier")
            db.execute(
                "UPDATE action_oauth_flows SET used_at=? WHERE state_hash=?",
                (int(time.time()), key),
            )
            return CallbackClaim(
                key,
                flow["owner"],
                flow["action_id"],
                flow["action_version"],
                flow["run_id"],
                flow["call_id"],
                redirect,
                verifier,
                meta,
                creds,
            )

    def _verify_claim(self, db, saver, claim):
        flow, _ = self._current(db, saver, claim.state_hash)
        if flow["used_at"] is None or (
            flow["owner"],
            flow["action_id"],
            flow["action_version"],
            flow["run_id"],
            flow["call_id"],
            flow["redirect_uri"],
        ) != (
            claim.owner,
            claim.action_id,
            claim.version,
            claim.run_id,
            claim.call_id,
            claim.redirect_uri,
        ):
            raise PermissionError("OAuth callback changed")

    async def complete(self, claim: CallbackClaim, code: str) -> None:
        if not isinstance(code, str) or not 1 <= len(code) <= 4096:
            raise ValueError("Invalid OAuth authorization code")
        saver = SQLiteCheckpointSaver(self.database, claim.owner)
        with self.database.read_snapshot() as db:
            self._verify_claim(db, saver, claim)
        auth = claim.metadata["auth"]
        result = await exchange(
            auth["client_url"],
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": claim.redirect_uri,
                "code_verifier": claim.verifier,
            },
            claim.credentials,
            auth["token_exchange_method"],
        )
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._verify_claim(db, saver, claim)
            action = self.tokens._action(
                db, claim.owner, claim.action_id, claim.version
            )
            self.tokens._store(
                db, action, result, now=int(time.time()), preserve_refresh=False
            )
            row = self.approvals._row(
                db, claim.owner, claim.run_id, "requires_action"
            )
            state = json.loads(row["state"])
            state.pop("pendingAction")
            state.pop("approvalCheckpointId")
            db.execute(
                "UPDATE runs SET state=?,status='running',updated_at=? WHERE id=?",
                (_json(state), _now(), claim.run_id),
            )
            db.execute(
                "DELETE FROM action_oauth_flows WHERE state_hash=?",
                (claim.state_hash,),
            )
