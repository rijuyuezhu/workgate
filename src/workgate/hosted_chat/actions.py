"""SQLite Agent Actions port of LibreChat agents/actions.js and api/actions/update.ts.

Credentials live in an encrypted column and never become part of Agent configs,
model schemas, HTTP responses, or execution error strings.
"""

import json
import re
import secrets
from collections.abc import Callable
from urllib.parse import urlsplit

from .action_spec import (
    DELIMITER,
    encoded_domain,
    legacy_domain,
    normalize_action_tool_name,
    operations,
    parse_spec,
    tool_name,
    validate_domain,
)
from .agents import AgentRepository
from .credential_cipher import CredentialCipher
from .storage import _json, _now

CREDENTIALS = ("api_key", "oauth_client_id", "oauth_client_secret")
CHANGE_CREDENTIAL_MESSAGE = (
    "Action credentials must be re-entered when changing the domain, "
    "OpenAPI server URL, or authentication settings"
)
AUTH_FIELDS = (
    "type",
    "authorization_type",
    "custom_auth_header",
    "authorization_url",
    "client_url",
    "scope",
    "token_exchange_method",
)


def _target(value: str | None, *, domain: bool = False) -> str:
    if not value:
        return ""
    url = urlsplit(value if "://" in value else "https://" + value)
    if not url.hostname:
        return (value or "").lower().strip().rstrip("/")
    return (
        url.scheme.lower()
        + "://"
        + url.netloc.lower()
        + (
            ""
            if domain
            else url.path.rstrip("/") + ("?" + url.query if url.query else "")
        )
    )


def _auth_type(meta: dict) -> str:
    return (meta.get("auth") or {}).get("type", "none")


def _validate_auth(auth: dict) -> None:
    if any(
        not isinstance(value, str) or len(value) > 4096
        for value in auth.values()
    ):
        raise ValueError("Invalid Action authentication fields")
    kind = auth.get("type", "none")
    if kind not in {"none", "service_http", "oauth"}:
        raise ValueError("Invalid Action authentication type")
    if kind == "service_http":
        if auth.get("authorization_type") not in {"basic", "bearer", "custom"}:
            raise ValueError("Invalid Action API Key authorization type")
        header = auth.get("custom_auth_header", "")
        if auth.get("authorization_type") == "custom" and (
            not isinstance(header, str)
            or not header.isascii()
            or not header.replace("-", "").isalnum()
            or header.lower()
            in {
                "host",
                "content-length",
                "connection",
                "transfer-encoding",
                "cookie",
                "proxy-authorization",
                "upgrade",
                "te",
                "trailer",
                "accept-encoding",
                "content-type",
                "expect",
            }
        ):
            raise ValueError("Invalid custom authentication header")
    if kind == "oauth":
        if auth.get("token_exchange_method") not in {
            "default_post",
            "basic_auth_header",
        }:
            raise ValueError("Invalid OAuth token exchange method")
        if not isinstance(auth.get("scope"), str):
            raise ValueError("Invalid OAuth scope")
        for key in ("authorization_url", "client_url"):
            url = urlsplit(auth.get(key) or "")
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.fragment
            ):
                raise ValueError("Invalid OAuth authorization/token URL")


class ActionRepository:
    def __init__(self, database):
        self.database = database
        self.agents = AgentRepository(database)
        self.cipher = CredentialCipher(database)

    @staticmethod
    def _binding(
        owner: str, agent_id: str, action_id: str, version: int
    ) -> str:
        return _json(["agent-action", owner, agent_id, action_id, version])

    def _credentials(self, row) -> dict:
        ciphertext = row["credentials"]
        return (
            self.cipher.unseal(
                ciphertext,
                binding=self._binding(
                    row["owner"], row["agent_id"], row["id"], row["version"]
                ),
            )
            if ciphertext
            else {}
        )

    @staticmethod
    def _public(row) -> dict:
        return {
            "action_id": row["id"],
            "agent_id": row["agent_id"],
            "type": "action_prototype",
            "version": int(row["version"]),
            "metadata": json.loads(row["metadata"]),
        }

    def list(self, owner: str) -> list[dict]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM action_records WHERE owner=? ORDER BY created_at,id",
                (owner,),
            ).fetchall()
            return [self._public(row) for row in rows]

    def _bound(self, db, owner: str, agent_id: str):
        return db.execute(
            "SELECT * FROM agents WHERE id=? AND owner=?", (agent_id, owner)
        ).fetchone()

    def get_for_tool(
        self, owner: str, agent_id: str, name: str
    ) -> tuple[dict, dict, dict] | None:
        with self.database.read_snapshot() as db:
            return self._get_for_tool(db, owner, agent_id, name)

    def _get_for_tool(
        self, db, owner: str, agent_id: str, name: str
    ) -> tuple[dict, dict, dict] | None:
        agent = self._bound(db, owner, agent_id)
        if agent is None:
            return None
        config = json.loads(agent["config"])
        if name not in {
            normalize_action_tool_name(t) for t in config.get("tools", [])
        }:
            return None
        rows = db.execute(
            "SELECT * FROM action_records WHERE owner=? AND agent_id=?",
            (owner, agent_id),
        ).fetchall()
        for row in rows:
            metadata = json.loads(row["metadata"])
            if encoded_domain(metadata["domain"]) + DELIMITER + row[
                "id"
            ] not in config.get("actions", []):
                continue
            op_id, _, suffix = name.rpartition(DELIMITER)
            if suffix != encoded_domain(metadata["domain"]).replace("---", "_"):
                continue
            spec = parse_spec(metadata["raw_spec"])
            operation = operations(spec).get(op_id)
            if operation and operation["method"] in {
                "get",
                "post",
                "put",
                "patch",
                "delete",
            }:
                return (
                    metadata,
                    self._credentials(row),
                    {
                        **operation,
                        "server": spec["servers"][0]["url"],
                        "action_id": row["id"],
                        "action_version": row["version"],
                    },
                )
        return None

    def tool_schemas(
        self,
        owner: str,
        agent: dict,
        *,
        selected_tools=None,
        non_oauth_only: bool = False,
    ) -> list[dict]:
        selected = set(agent.get("tools") or [])
        active = (
            {normalize_action_tool_name(name) for name in selected_tools}
            if selected_tools is not None
            else None
        )
        with self.database.read_snapshot() as db:
            live = self._bound(db, owner, agent["id"])
            if live is None:
                return []
            config = json.loads(live["config"])
            refs = set(agent.get("actions") or []) & set(
                config.get("actions") or []
            )
            selected &= set(config.get("tools") or [])
            rows = db.execute(
                "SELECT * FROM action_records WHERE owner=? AND agent_id=?",
                (owner, agent["id"]),
            ).fetchall()
        tools = []
        for row in rows:
            metadata = json.loads(row["metadata"])
            if non_oauth_only and _auth_type(metadata) == "oauth":
                continue
            domain = metadata["domain"]
            if encoded_domain(domain) + DELIMITER + row["id"] not in refs:
                continue
            for op in operations(parse_spec(metadata["raw_spec"])).values():
                name = tool_name(op["name"], domain)
                if (
                    name in selected
                    and (
                        active is None
                        or normalize_action_tool_name(name) in active
                    )
                    and op["method"]
                    in {"get", "post", "put", "patch", "delete"}
                ):
                    func = {
                        "name": normalize_action_tool_name(name),
                        "description": op["description"],
                        "parameters": op["parameters"],
                    }
                    # LibreChat's toObjectTool only adds strict for x-strict.
                    if op["strict"]:
                        func["strict"] = True
                    tools.append({"type": "function", "function": func})
        return tools

    def save(
        self,
        owner: str,
        agent_id: str,
        body: dict,
        *,
        check_grant: Callable[[], None] = lambda: None,
    ) -> tuple[dict, dict]:
        check_grant()
        if set(body) - {"action_id", "metadata", "functions"}:
            raise ValueError("Unknown Action request field")
        functions = body.get("functions")
        if not isinstance(functions, list) or not functions:
            raise ValueError("No functions provided")
        incoming = body.get("metadata")
        if not isinstance(incoming, dict):
            raise ValueError("Action metadata required")
        action_id = body.get("action_id") or secrets.token_urlsafe(12)
        if not isinstance(action_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", action_id
        ):
            raise ValueError("Invalid Action ID")
        # Initialize before entering the action transaction; the shared
        # cipher has its own key-fingerprint write transaction.
        if any(
            isinstance(incoming.get(key), str) and incoming[key]
            for key in CREDENTIALS
        ):
            if self.agents.get(owner, agent_id) is None:
                raise KeyError("Agent not found for adding action")
            self.cipher.initialize()
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            check_grant()
            row = self._bound(db, owner, agent_id)
            if row is None:
                raise KeyError("Agent not found for adding action")
            previous = db.execute(
                "SELECT * FROM action_records WHERE id=?", (action_id,)
            ).fetchone()
            if previous and (
                previous["owner"] != owner or previous["agent_id"] != agent_id
            ):
                raise PermissionError("Action does not belong to this agent")
            old_meta = json.loads(previous["metadata"]) if previous else {}
            old_creds = self._credentials(previous) if previous else {}
            permitted = {"domain", "raw_spec", "privacy_policy_url", "auth"}
            if any(key not in permitted | set(CREDENTIALS) for key in incoming):
                raise ValueError("Unknown Action metadata field")
            if "auth" in incoming and not isinstance(incoming["auth"], dict):
                raise ValueError("Invalid Action auth metadata")
            if isinstance(incoming.get("auth"), dict) and (
                set(incoming["auth"]) - set(AUTH_FIELDS)
            ):
                raise ValueError("Unknown Action auth field")
            new_meta = {
                **old_meta,
                **{
                    key: value
                    for key, value in incoming.items()
                    if key in permitted and value is not None
                },
            }
            if "domain" not in new_meta or "raw_spec" not in new_meta:
                raise ValueError("Action requires domain and raw_spec")
            spec = parse_spec(new_meta["raw_spec"])
            validate_domain(new_meta["domain"], spec["servers"][0]["url"])
            ops = operations(spec)
            if not ops:
                raise ValueError("OpenAPI spec contains no operations")
            incoming_names = [
                (f.get("function") or {}).get("name")
                if isinstance(f, dict) and isinstance(f.get("function"), dict)
                else None
                for f in functions
            ]
            if (
                not all(isinstance(name, str) for name in incoming_names)
                or set(incoming_names) != set(ops)
                or len(set(incoming_names)) != len(incoming_names)
            ):
                raise ValueError(
                    "Provided functions must match OpenAPI operations"
                )
            _validate_auth(new_meta.get("auth") or {"type": "none"})

            target_changed = bool(previous) and (
                _target(old_meta.get("domain"), domain=True)
                != _target(new_meta.get("domain"), domain=True)
                or _target(
                    parse_spec(old_meta["raw_spec"])["servers"][0]["url"]
                )
                != _target(spec["servers"][0]["url"])
                or any(
                    (old_meta.get("auth") or {}).get(k)
                    != (new_meta.get("auth") or {}).get(k)
                    for k in AUTH_FIELDS
                )
            )
            provided = {
                key: incoming[key]
                for key in CREDENTIALS
                if isinstance(incoming.get(key), str) and incoming[key]
            }
            if target_changed:
                kind = _auth_type(new_meta)
                needed = (
                    ("api_key",)
                    if kind == "service_http"
                    else (
                        ("oauth_client_id", "oauth_client_secret")
                        if kind == "oauth"
                        else ()
                    )
                )
                if any(
                    key in old_creds and key not in provided for key in needed
                ):
                    raise ValueError(CHANGE_CREDENTIAL_MESSAGE)
            creds = (
                {**old_creds, **provided} if not target_changed else provided
            )
            if _auth_type(new_meta) == "service_http" and not creds.get(
                "api_key"
            ):
                raise ValueError("Action API key is required")
            if _auth_type(new_meta) == "oauth" and not all(
                creds.get(k) for k in ("oauth_client_id", "oauth_client_secret")
            ):
                raise ValueError("Action OAuth client credentials required")
            kind = _auth_type(new_meta)
            needed = (
                ("api_key",)
                if kind == "service_http"
                else ("oauth_client_id", "oauth_client_secret")
                if kind == "oauth"
                else ()
            )
            creds = {k: creds[k] for k in needed if k in creds}
            if any(len(value) > 16384 for value in creds.values()):
                raise ValueError("Action credential exceeds size limit")
            config = json.loads(row["config"])
            actions = [
                ref
                for ref in config.get("actions", [])
                if not ref.endswith(DELIMITER + action_id)
            ]
            actions.append(
                encoded_domain(new_meta["domain"]) + DELIMITER + action_id
            )
            former = set()
            if previous:
                old_domain = old_meta["domain"]
                former = {
                    tool_name(name, old_domain)
                    for name in operations(parse_spec(old_meta["raw_spec"]))
                }
                former.update(
                    name + DELIMITER + legacy_domain(old_domain)
                    for name in operations(parse_spec(old_meta["raw_spec"]))
                )
            new_names = [tool_name(name, new_meta["domain"]) for name in ops]
            normalized = {
                normalize_action_tool_name(name) for name in new_names
            }
            if len(normalized) != len(new_names) or any(
                not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
                for name in normalized
            ):
                raise ValueError("Invalid or colliding Action tool name")
            other_names = {
                normalize_action_tool_name(name)
                for name in config.get("tools", [])
                if name not in former
            }
            # Include inactive records: a later Agent selection must never
            # make the same normalized name resolve to two Actions.
            for other in db.execute(
                "SELECT metadata FROM action_records WHERE owner=? AND agent_id=? AND id<>?",
                (owner, agent_id, action_id),
            ):
                meta = json.loads(other["metadata"])
                other_names.update(
                    normalize_action_tool_name(tool_name(name, meta["domain"]))
                    for name in operations(parse_spec(meta["raw_spec"]))
                )
            if normalized & other_names:
                raise ValueError(
                    "Action tool name collides with an existing tool"
                )
            config_tools = [
                name
                for name in config.get("tools", [])
                if name not in former
                and name not in new_names
                and name != "legacy-" + action_id
            ]
            config_tools.extend(new_names)
            updated = self.agents._update_locked(
                db, owner, agent_id, {"actions": actions, "tools": config_tools}
            )
            now = _now()
            version = int(previous["version"]) + 1 if previous else 1
            encrypted = (
                self.cipher.seal(
                    creds,
                    binding=self._binding(owner, agent_id, action_id, version),
                )
                if creds
                else None
            )
            db.execute(
                "INSERT INTO action_records(id,owner,agent_id,metadata,credentials,version,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata,"
                "credentials=excluded.credentials,version=excluded.version,updated_at=excluded.updated_at",
                (
                    action_id,
                    owner,
                    agent_id,
                    _json(new_meta),
                    encrypted,
                    version,
                    now,
                    now,
                ),
            )
            if previous and (target_changed or creds != old_creds):
                db.execute(
                    "DELETE FROM action_oauth_tokens WHERE action_id=?",
                    (action_id,),
                )
            if previous:
                db.execute(
                    "DELETE FROM action_oauth_flows WHERE action_id=?",
                    (action_id,),
                )
            saved = db.execute(
                "SELECT * FROM action_records WHERE id=?", (action_id,)
            ).fetchone()
            assert updated is not None and saved is not None
            check_grant()
            return updated, self._public(saved)

    def delete(
        self,
        owner: str,
        agent_id: str,
        action_id: str,
        *,
        check_grant: Callable[[], None] = lambda: None,
    ) -> bool:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            check_grant()
            row = self._bound(db, owner, agent_id)
            if row is None:
                raise KeyError("Agent not found for deleting action")
            action = db.execute(
                "SELECT * FROM action_records WHERE id=? AND owner=? AND agent_id=?",
                (action_id, owner, agent_id),
            ).fetchone()
            if action is None:
                return False
            metadata = json.loads(action["metadata"])
            names = {
                tool_name(op, metadata["domain"])
                for op in operations(parse_spec(metadata["raw_spec"]))
            }
            names.update(
                op + DELIMITER + legacy_domain(metadata["domain"])
                for op in operations(parse_spec(metadata["raw_spec"]))
            )
            config = json.loads(row["config"])
            self.agents._update_locked(
                db,
                owner,
                agent_id,
                {
                    "actions": [
                        ref
                        for ref in config.get("actions", [])
                        if not ref.endswith(DELIMITER + action_id)
                    ],
                    "tools": [
                        name
                        for name in config.get("tools", [])
                        if name not in names
                    ],
                },
            )
            db.execute(
                "DELETE FROM action_records WHERE id=? AND agent_id=?",
                (action_id, agent_id),
            )
            check_grant()
            return True
