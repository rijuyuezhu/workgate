"""Durable approval-bound Action OAuth interruptions and browser claims."""

import asyncio
import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.hosted_chat.test_action_oauth_tokens import body
from workgate.hosted_chat import action_oauth_flow as oauth
from workgate.hosted_chat.action_effects import ActionEffects
from workgate.hosted_chat.action_spec import normalize_action_tool_name
from workgate.hosted_chat.actions import ActionRepository
from workgate.hosted_chat.agent_runtime import AgentContext, AgentLoopState
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.approval_claims import ApprovalClaimStore
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
from workgate.hosted_chat.provider_common import ModelToolCall, ProviderConfig

BASE = "https://controller.example.org"


def grant():
    return {"shell:read", "shell:write"}, None


@pytest.fixture
async def started(tmp_path):
    db = SQLiteDatabase(tmp_path)
    agent = AgentRepository(db).create(
        "alice", {"name": "Agent", "provider": "openai", "model": "m"}
    )
    repo = ActionRepository(db)
    agent, saved = repo.save("alice", agent["id"], body())
    job, _ = GenerationStore(db).begin(
        "alice", {"text": "ping", "agent_id": agent["id"], "model": "m"}
    )
    lease = ActorLease.claim(db, "alice", job.run_id)
    assert lease is not None
    checkpoint = OwnedLoopCheckpoint(lease)
    context = AgentContext(
        ProviderConfig("openai", "m", "https://model.invalid", "fake"),
        [],
        agent=agent,
    )
    call = ModelToolCall(
        "oauth-call", normalize_action_tool_name(agent["tools"][0]), {}
    )
    state = AgentLoopState(
        context,
        [{"role": "user", "content": "ping"}],
        step=1,
        model_turn=1,
        pending_calls=[call],
        tool_step_started=True,
    )
    await checkpoint.save(state, [call])
    return (
        db,
        repo,
        agent,
        saved,
        job,
        context,
        call,
        ActionEffects(checkpoint, current_grant=grant, oauth_enabled=True),
    )


def approve(started, decision="approve"):
    db, _, _, _, job, context, call, effects = started
    effects.bind(call)
    approvals = ApprovalClaimStore(db)
    pending = approvals.pause(effects.checkpoint, [call])
    approvals.claim(
        "alice",
        job.run_id,
        pending["actionId"],
        [{"tool_call_id": call.call_id, "decision": decision}],
    )
    lease = ActorLease.claim(db, "alice", job.run_id)
    assert lease is not None
    checkpoint = OwnedLoopCheckpoint(lease)
    assert checkpoint.restore(context) is not None
    return ActionEffects(checkpoint, current_grant=grant, oauth_enabled=True)


def pause(started):
    db, repo, _, _, job, _, call, _ = started
    effects = approve(started)
    flows = oauth.ActionOAuthFlows(repo)
    pending = flows.pause(effects, call)
    assert ApprovalClaimStore(db).pending("alice", job.run_id) == pending
    return flows, pending


def begin(flows, pending):
    browser = flows.begin(
        "alice",
        pending["runId"],
        pending["actionId"],
        BASE,
        check_grant=lambda: None,
    )
    query = parse_qs(urlsplit(browser.authorization_url).query)
    return browser, query


@pytest.mark.asyncio
async def test_exact_approved_flow_survives_restart(started, monkeypatch):
    db, repo, _, saved, job, context, call, effects = started
    with pytest.raises(PermissionError):
        oauth.ActionOAuthFlows(repo).pause(effects, call)
    flows, pending = pause(started)
    browser, query = begin(flows, pending)
    state = query["state"][0]
    assert query["client_id"] == ["client"]
    assert "client_secret" not in query
    assert query["redirect_uri"] == [oauth.callback_uri(BASE)]
    with db.read_snapshot() as conn:
        record = conn.execute("SELECT * FROM action_oauth_flows").fetchone()
        assert (
            record["state_hash"] == hashlib.sha256(state.encode()).hexdigest()
        )
        assert record["used_at"] is None
    restarted = oauth.ActionOAuthFlows(
        ActionRepository(SQLiteDatabase(db.root.parent))
    )
    with pytest.raises(PermissionError):
        restarted.claim(state, "wrong", oauth.callback_uri(BASE))
    with pytest.raises(PermissionError):
        restarted.claim(state, browser.cookie, "https://other.example.org")
    claim = restarted.claim(state, browser.cookie, oauth.callback_uri(BASE))
    assert (
        base64.urlsafe_b64encode(
            hashlib.sha256(claim.verifier.encode()).digest()
        )
        .decode()
        .rstrip("=")
        == query["code_challenge"][0]
    )
    with pytest.raises(PermissionError):
        restarted.claim(state, browser.cookie, oauth.callback_uri(BASE))
    seen = []

    async def exchange(endpoint, data, credentials, method):
        seen.append(data)
        return {"access_token": "oauth-access"}

    monkeypatch.setattr(oauth, "exchange", exchange)
    await restarted.complete(claim, "code")
    assert seen == [
        {
            "grant_type": "authorization_code",
            "code": "code",
            "redirect_uri": oauth.callback_uri(BASE),
            "code_verifier": claim.verifier,
        }
    ]
    assert (
        await restarted.tokens.access_token("alice", saved["action_id"], 1)
        == "oauth-access"
    )
    assert ApprovalClaimStore(db).pending("alice", job.run_id) is None
    lease = ActorLease.claim(db, "alice", job.run_id)
    assert lease is not None
    checkpoint = OwnedLoopCheckpoint(lease)
    assert checkpoint.restore(context) is not None
    batch = ApprovalClaimStore(db).approved_batch(checkpoint)
    assert batch is not None and batch.entries[0].tool_call() == call


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["abort", "edit", "supersede", "expire"])
async def test_callback_cannot_settle_after_authority_changes(
    started, monkeypatch, mode
):
    db, repo, agent, saved, job, _, _, _ = started
    flows, pending = pause(started)
    browser, query = begin(flows, pending)
    claim = flows.claim(
        query["state"][0], browser.cookie, oauth.callback_uri(BASE)
    )
    entered = asyncio.Event()
    released = asyncio.Event()

    async def exchange(*unused):
        entered.set()
        await released.wait()
        return {"access_token": "test-result"}

    monkeypatch.setattr(oauth, "exchange", exchange)
    task = asyncio.create_task(flows.complete(claim, "code"))
    await entered.wait()
    if mode == "abort":
        assert ApprovalClaimStore(db).abort_pending(
            "alice", job.run_id, pending["actionId"]
        )
    elif mode == "edit":
        patch = body()
        patch["action_id"] = saved["action_id"]
        repo.save("alice", agent["id"], patch)
    elif mode == "supersede":
        begin(flows, pending)
    else:
        with db.connect() as conn:
            conn.execute("UPDATE action_oauth_flows SET expires_at=0")
    released.set()
    with pytest.raises((PermissionError, KeyError)):
        await task
    with db.read_snapshot() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM action_oauth_tokens").fetchone()[
                0
            ]
            == 0
        )


@pytest.mark.asyncio
async def test_denied_review_does_not_pause(started):
    _, repo, _, _, _, _, call, _ = started
    effects = approve(started, "reject")
    with pytest.raises(PermissionError):
        oauth.ActionOAuthFlows(repo).pause(effects, call)


@pytest.mark.asyncio
async def test_begin_fences_owner_grant_and_old_state(started):
    db, _, _, _, _, _, _, _ = started
    flows, pending = pause(started)
    with pytest.raises(KeyError):
        flows.begin(
            "bob",
            pending["runId"],
            pending["actionId"],
            BASE,
            check_grant=lambda: None,
        )
    with pytest.raises(PermissionError):
        flows.begin(
            "alice", pending["runId"], "wrong", BASE, check_grant=lambda: None
        )
    checks = []

    def revoked():
        checks.append(True)
        if len(checks) == 2:
            raise PermissionError("revoked")

    with pytest.raises(PermissionError):
        flows.begin(
            "alice",
            pending["runId"],
            pending["actionId"],
            BASE,
            check_grant=revoked,
        )
    with db.read_snapshot() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM action_oauth_flows").fetchone()[
                0
            ]
            == 0
        )
    first, q1 = begin(flows, pending)
    second, q2 = begin(flows, pending)
    with pytest.raises(PermissionError):
        flows.claim(q1["state"][0], first.cookie, oauth.callback_uri(BASE))
    with pytest.raises(PermissionError):
        flows.claim(q2["state"][0], first.cookie, oauth.callback_uri(BASE))
    flows.claim(q2["state"][0], second.cookie, oauth.callback_uri(BASE))


@pytest.mark.parametrize(
    "base",
    [
        "http://public.example.org",
        "https://user:pass@example.org",
        "https://example.org/path",
        "https://example.org/?q=1",
        "",
    ],
)
def test_invalid_callback_origin(base):
    with pytest.raises(ValueError):
        oauth.callback_uri(base)


def test_loopback_callback_origin():
    assert (
        oauth.callback_uri("http://127.0.0.1:1234/")
        == "http://127.0.0.1:1234/api/ui/chat/actions/oauth/callback"
    )


@pytest.mark.asyncio
async def test_changed_token_cannot_claim_external_effect(started, monkeypatch):
    db, repo, _, saved, _, _, call, _ = started
    effects = approve(started)
    tokens = oauth.ActionOAuthFlows(repo).tokens
    args = ("alice", saved["action_id"], 1)
    tokens.store_code_tokens(*args, {"access_token": "old-test"})
    token = await effects.prepare_oauth(call)
    assert token == "old-test"
    tokens.store_code_tokens(*args, {"access_token": "new-test"})
    with pytest.raises(PermissionError, match="authorization changed"):
        await effects.execute(call, oauth_access=token)
    with db.read_snapshot() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM agent_native_effect_calls"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_refresh_revocation_clears_uncertain_rotation(
    started, monkeypatch
):
    db, repo, _, saved, _, _, call, _ = started
    effects = approve(started)
    tokens = oauth.ActionOAuthFlows(repo).tokens
    args = ("alice", saved["action_id"], 1)
    tokens.store_code_tokens(
        *args,
        {
            "access_token": "old-test",
            "expires_in": 0,
            "refresh_token": "test-refresh",
        },
    )
    from workgate.hosted_chat import action_oauth_tokens

    revoked = False

    async def exchange(*unused):
        nonlocal revoked
        revoked = True
        return {"access_token": "new-test"}

    def current_grant():
        if revoked:
            raise PermissionError("revoked")
        return grant()

    effects.current_grant = current_grant
    monkeypatch.setattr(action_oauth_tokens, "exchange", exchange)
    with pytest.raises(PermissionError, match="revoked"):
        await effects.prepare_oauth(call)
    assert await tokens.access_token(*args) is None
    with db.read_snapshot() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM agent_native_effect_calls"
            ).fetchone()[0]
            == 0
        )
