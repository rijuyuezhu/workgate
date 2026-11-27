# Hosted Chat source provenance

Source: `fwerkor/local-shell-mcp` v6.0.3, commit `f0ecf8f8039345f24d0de4402983766787f4574e`. The upstream MIT license is retained in `LICENSE.upstream`.

| Workgate file | Upstream source at the pin | Git blob |
| --- | --- | --- |
| `database.py` | `src/local_shell_mcp/sqlite_store.py` | `584b13ce57787ca617924c4e7f1f166d0c3becde` |
| `tests/hosted_chat/test_database.py` | `tests/test_sqlite_store.py` (Chat-relevant cases) | `29352f9fd435e64aa8328418398340c1c76cdfe8` |
| `storage.py` | `src/local_shell_mcp/webui/storage.py` | `46eebf1cad3f7a70c9dc872a1d9c8d6c680674b6` |
| `attachment_history.py` (canonical file DTO) | `src/local_shell_mcp/webui/attachments.py` (read projection) | `da80834bb588dc3950225f0674d8e2cba56a0751` |
| `attachment_history.py` (history links and events) | `src/local_shell_mcp/webui/tool_attachments.py` (read projection) | `20838698ea30fb3f6b4439ae37169841412eabac` |
| `tests/hosted_chat/test_storage.py` | `tests/test_webui_storage.py` | `08a3c5eff8a5481423ca49ef1d811e637da878eb` |

The database imports the final Hosted Chat table definitions, FTS projection, indexes and wakeup triggers. Workgate uses a dedicated private Chat database, not upstream's native state tables or migration code. New installations start with the final schema rather than replaying historical migrations. Native Workgate persistence, authorization, audit and service lifecycle are separate. No Chat runtime is started by these modules.

The initial differential schema check compared the exact pinned upstream DDL against the initialized Workgate database: 41 Chat table definitions unchanged; excluded upstream `state`, `state_append_chunks`, `state_append_lengths`, `legacy_imports`, and the generic `workspaces` table (native Workgate workspace state remains authoritative). SQLite application ID and schema version are Workgate-owned.

The repository retains final React DTOs, pagination, bookmark filters and preference merging. Adaptations enforce owner-scoped project and agent references within write transactions, consistent history snapshots, literal wildcard searches and bounded cursor decoding. The canonical attachment history projection joins persisted messages to their owning runs, ignores deleted files and retains trusted memory events. Attachment mutation routes and the generation runner are not imported in this unit; their source will be integrated with the corresponding lifecycle and permission contracts.

The owned SQLite checkpoint saver follows these final sources:

| Workgate file | Upstream source at the pin | Git blob |
| --- | --- | --- |
| `checkpoints.py` | `src/local_shell_mcp/webui/checkpoints.py` | `ab4df86cf08b3f66bb78980f965a5aaf97f572cb` |
| `tests/hosted_chat/test_checkpoints.py` | `tests/test_webui_checkpoints.py` | `d30f1cbc24b190be323167b4dd604eb6b0e76577` |
| `tests/fixtures/hosted_chat/checkpoint_reference.cjs` | `tests/fixtures/librechat/checkpoint_reference.cjs` | `ae363cf8d7dbd3e26b596c7d18816f11b5784a06` |
| `tests/fixtures/hosted_chat/checkpoint_sources.json` | `tests/fixtures/librechat/checkpoint_sources.json` | `fd7cec9c7fc771ee5d686013c60db70a00d15e70` |

The source anchoring and pending-write slot semantics remain intact, including
the original differential oracle. Existing foreground runs require their
matching owner, thread, namespace and live actor lease for mutations. The
source checkpoint size ceiling also bounds pending writes in the same tuple.
Multi-query tuple reads use one SQLite snapshot, and terminal cleanup is
owner-bound. The saver is private storage; graph SDK serialization, detached
host authority, authenticated recovery and tool-side-effect reconciliation
are integrated separately. Construction does not launch any generation.

Provider profile storage and sampling are adapted from the final upstream sources:

| Workgate file | Upstream source at the pin | Git blob |
| --- | --- | --- |
| `provider_profiles.py` | `src/local_shell_mcp/webui/provider_profiles.py` | `f4cc2abde8895eb47e8abbca99b1a59f7e582cc3` |
| `sampling.py` | `src/local_shell_mcp/webui/sampling.py` | `74350d3276bf72f704c3aa9b6ba8b5738387a335` |
| `tests/hosted_chat/test_provider_profiles.py` | `tests/test_webui_provider_profiles.py` (repository cases) | `3feef623b70da2f6a2de1fd4e1e366763684e5d5` |
| `tests/hosted_chat/test_sampling.py` | `tests/test_webui_sampling.py` (validation/merge cases) | `5bcc7e81ae65aaf0449f9976c8cf483123b9e9bc` |

The repository retains the final channel names, protocol aliases, redacted catalog DTO, reference guards, and model sampling validation. Explicit Control-owned database and cipher instances replace global HTTP-route dependencies. API keys are encrypted and bound to channel, provider and endpoint. A deleted channel name cannot be reused while retained history references it. A private HTTP endpoint requires explicit opt-in; resolved-address validation is required in the transport unit before any requests. Unsaved built-in defaults carry no ambient credentials. Automatic discovery and account OAuth are not exposed by this private unit; their final source integrations and route/transport acceptance remain pending. Generation sampling snapshot tests follow the generation store import.


Tool batch execution is adapted from the final upstream ToolNode port:

| Workgate file | Upstream source at the pin | Git blob |
| --- | --- | --- |
| `agent_execution.py` | `src/local_shell_mcp/webui/agent_execution.py` | `5c5e8bfbc1c7a491b65bf06adfdcf150855de629` |
| `tests/hosted_chat/test_agent_execution.py` | `tests/test_webui_agent_execution.py` | `54d9c469c880cbb515a1af4f81b5e1ac2c107ad7` |
| `tests/fixtures/hosted_chat/tool_batch_reference.cjs` | `tests/fixtures/librechat/tool_batch_reference.cjs` | `230b14f18f37554949159451475ff9417e19d76c` |

Sibling settlement, interrupt-first ordering and the original replay oracle
are preserved. The native complete-batch validator rejects invalid proposals
before any dispatch. Callback mutations cannot rewrite the cached proposal or
result. Completion is cached only after the persistence callback succeeds;
reconciliation of uncertain native side effects belongs to the native intent
ledger, not this orchestration helper.


Human approval proposal/decision handling follows the final pinned sources:

| Workgate file | Upstream source at the pin | Git blob |
| --- | --- | --- |
| `approval_runtime.py` | `src/local_shell_mcp/webui/approval_runtime.py` | `e12100e03427bd3e6272352b10c80496767640d1` |
| `tool_approval.py` | `src/local_shell_mcp/webui/tool_approval.py` | `6b7a644bf480fc0fa392b8afe187fd7739372bf3` |
| `tests/hosted_chat/test_approval_runtime.py` | `tests/test_webui_approval_runtime.py` | `54df7f86bcd9aa6d951e4ce2610ad9e8b0bf83dc` |
| `tests/hosted_chat/test_tool_approval_source_port.py` | `tests/test_webui_tool_approval_source_port.py` | `c029d030df225c54a9f74c76552f5a58b814b539` |
| `tests/fixtures/hosted_chat/hitl_reference.cjs` | `tests/fixtures/librechat/hitl_reference.cjs` | `dfaaae0ee0d4a13e9ec95dc95535c3b1c7b7e9af` |
| `tests/fixtures/hosted_chat/approval_runtime_sources.json` | `tests/fixtures/librechat/approval_runtime_sources.json` | `da96d7e050d5adc1ff2f5f7fdd7b25c633175e30` |

The source decision mapper and differential oracles are retained. The
prepared batch validates every original and edited call before dispatch;
immutable JSON decisions cannot be changed by caller mutations. Result
compaction reuses the existing source port. Approval never grants native
permissions. The upstream direct local-file dispatch test is replaced here
by an edited-call callback/permission regression; its native Workgate
integration belongs to the dispatcher acceptance unit. Durable claims and
approval settings are separate lifecycle contracts.


Model discovery and refresh are adapted from the final upstream catalog service:

| Workgate file | Upstream source at the pin | Git blob |
| --- | --- | --- |
| `model_discovery.py` | `src/local_shell_mcp/webui/model_discovery.py` | `145ea6abf8113cee559ac71ca5a9ffe9c80b3701` |
| `tests/hosted_chat/test_model_discovery.py` | `tests/test_webui_provider_profiles.py` (discovery cases) | `3feef623b70da2f6a2de1fd4e1e366763684e5d5` |

The catalog preserves source protocol pagination, filtering, context-window
overrides, bounded requests, failure retention and opt-in refresh. Workgate
uses the same endpoint validation and pinned transport for both profile
configuration and requests; saved API keys are decrypted only for their
bound endpoint. Automatic save probes outside the SQLite write transaction,
then compares the exact saved snapshot before committing the new profile.
Refresh also compares the prior row, preventing stale replies from overwriting
edits or restoring deleted profiles. Account authorization remains a separate
unit. The refresh coroutine is not started by importing these modules.


Model-facing tool and media projection uses these final upstream sources:

| Workgate file | Upstream source at the pin | Git blob |
| --- | --- | --- |
| `mcp_content.py` | `src/local_shell_mcp/webui/mcp_content.py` | `524dd4396577ca6734e6c5da10bedf61a416fc67` |
| `mcp_projection.py` | `src/local_shell_mcp/webui/mcp_projection.py` | `b17c27ca8c6516c93720c5aaf8a176b3d010555f` |
| `provider_multimodal.py` | `src/local_shell_mcp/webui/provider_multimodal.py` | `33ce92d0dee596ce8a16ed8e06c21a9dd035ec4a` |
| `result_projection.py` | `src/local_shell_mcp/webui/approval_runtime.py` (UTF-16 truncation) | `e12100e03427bd3e6272352b10c80496767640d1` |
| `tests/hosted_chat/test_provider_projection.py` | `tests/test_webui_message_parity.py` (MCP cases) | `f3f229bfca58601f2823ee666dfe0209d686b136` |
| same | `tests/test_webui_pr355_edge_coverage.py` (projection cases) | `1f20e8d244165912a1384440aa6217a7226dcc73` |
| same | `tests/test_webui_steer_media.py` (native media cases) | `e9876cc4923caa39b5bfd36075faa00b7c5b9dc5` |
| `tests/fixtures/hosted_chat/mcp_content_vectors.json` | `tests/fixtures/librechat/mcp_content_vectors.json` | `89ca4369f1d6a2d9a05c4d42271483684e70c823` |
| `tests/fixtures/hosted_chat/approval_runtime_reference.cjs` | `tests/fixtures/librechat/approval_runtime_reference.cjs` | `34587d8e224e7cf9b3d09933d73e322b665d9c3f` |

The original source oracle and its license/source metadata remain intact.
Projection recognizes the native `call_agent_mcp_tool` result, including
structured content, without interpreting ordinary tool JSON as MCP media.
It retains raw redacted results and projects complete batches at their
original positions. Remote image URLs and active UI HTML are not projected;
native MCP Apps authorization and rendering remain separate. Inline image/PDF
parts use the same bounded canonical base64 validator. The model-facing
result budget remains bounded even when history is reloaded. Actual attachment
ownership, durable generation, protocol serialization and app rendering
are integrated in their respective units; importing these helpers does
not start a service or send a request.


Native Task/Session intent ownership adapts the final durable task source:

| Workgate file | Upstream source at the pin | Git blob |
| --- | --- | --- |
| `durable_tasks.py` | `src/local_shell_mcp/webui/durable_tasks.py` (reservation, ownership and cancellation) | `9b50208b63aac266387b4e3b6b8e9ce6128b17e4` |
| `tests/hosted_chat/test_durable_tasks.py` | `tests/test_webui_durable_tasks.py` (intent, ownership and cancellation cases) | `b079a14a09425775c2d47b6ae7ec7c35fd9babfc` |

The ledger reuses the pinned durable-task and run-cancellation tables. Native
Control Task/Session IDs are reserved before dispatch; a transactionally
claimed intent is uncertain until an exact native response or read-only lookup
confirms it. Replaying the proposal cannot claim dispatch again, change its
identity, widen the stored grant, or adopt another conversation's native work.
A late positive receipt does not clear a cancellation latch. The final
upstream revocation/reconciliation functions also have a Control-owned
Task/Session adapter in `control/hosted_chat_cancellation.py`, with paired
tests in `tests/hosted_chat/test_native_cancellation.py`. Cleanup uses the
reserved native identity and never force-releases an offline Session. An
unclaimed reservation can stop without native evidence; a claimed reservation
requires positive termination evidence. Tracked Bash/Python Jobs retain the
exact native Session and Executor binding in the private intent ledger; the
initial, unreleased schema includes the nullable native Session identity.
Job creation uses one reserved native ID and recovery uses exact native poll,
not another start. A positively absent parent Session permits cleanup without
fabricating a Job receipt. Cancelling a Job does not terminate an otherwise
reused Session. PTY creation shares the same native resource ledger and uses
an exact owner-checked Shell lookup. The private `chat-shell-...` identity
includes 128 bits of entropy and an optional bounded human-readable prefix;
public starts cannot reuse that generated namespace. Cleanup records positive
native evidence before killing a Shell, so a lost kill reply can be reconciled
without terminating an unrelated Shell or its reused parent Session.
The final upstream retry contract is adapted to the native reusable Job ID:
the private intent records the expected attempt and a distinct operation key.
Native retry admission checks the attempt under the Session lifecycle lock and
persists that key with the pending retry transition. Lookup and cleanup match
the attempt and key rather than trusting the reusable Job ID alone. A newer
or conflicting attempt settles cleanup without a fabricated receipt or a stop
of unrelated work. The private adapter takes one retry target per durable
intent. Background managed copy reserves a Control-owned Job with the original
source and destination Sessions and Executor bindings. Admission checks source
read and destination write authority separately; cancellation is fenced to
the original copy attempt. Managed-copy retry derives both endpoints from the
owned native Job intent, rechecks both grants and uses checkpoint-aware native
retry admission. It shares exact-attempt classification with shell Jobs;
rejected retries never release another attempt's lease. A native multi-Job
retry call is preflighted and reserved atomically (at most 64 distinct targets);
each intent has its own key and the full immutable call fingerprint. Dispatch
settles siblings independently and preserves request order on success. A
definitive refusal or uncertain reply does not replay other native attempts.
Native Job polling uses the latest confirmed conversation-owned attempt and
read access to all bound endpoints. The private lookup supports the native
bounded log-tail length; a superseding public retry is never projected as
owned work. Authorization and ownership are rechecked before publishing the
result. Owned Job listing uses bounded exact-attempt metadata batches, without
reading logs. It filters out unrelated, superseded and unauthorized Jobs before
computing counts or pagination. Explicit cancellation preflights the complete
owned batch, records immutable target attempts before stopping native work, and
retains pending cleanup if a response is unconfirmed. Replaying an older call
never selects a newer attempt. The private unified Job dispatcher validates
mutually exclusive operations and canonicalizes public defaults before using
the owned adapters. Mutation replay identities cannot cross retry and cancel
ledgers. Session file inspection uses the canonical native MCP schema, router,
watchdog and result projection; the explicit read-tool classification and
periodic live authorization recheck follow the final pinned lsm_tool_registry.py.
Only confirmed owned Sessions are eligible. PTY inspection further intersects
confirmed conversation-owned Shell identities with native Session inventory;
read receipts must match the exact reserved Shell and lists rebuild their
model-visible text from filtered metadata. Explicit PTY cancellation uses
the same durable call ledger and native cleanup path as Jobs. A lost stop
response is reconciled by exact native absence; it does not invent a successful
kill receipt or authorize another Shell. Owned PTY input and resize reuse
the native MCP dispatcher. An immutable effect claim is committed before
invocation, a confirmed matching receipt is persisted before model disclosure,
and uncertain calls are never resent. Native file mutations and bounded
Bash/Python execution share the effect ledger, retaining native grounding,
watchdog, and result projection. Tracked and PTY execution never fall through
to bounded execution. Native Task metadata and Todo/plan operations use the
same canonical Task owner, with only conversation-owned attached Session IDs
disclosed. Session workdir changes retain native read scope and exact binding;
explicit Session close requires the live execute grant and positive native
absence, never force release. Cancellation latches related owned child
intents for exact reconciliation, without manufacturing individual stop
receipts or closing the attached Task. Task finish and cancel preserve native
plan guards and independent Session lifecycle. Only an explicit matching
deletion receipt retires Task ownership; uncertain deletion is not replayed.
General native tool dispatch and
runtime wiring follow.


Agent authoring starts with the final upstream repository and validators.

| Workgate file | Upstream source at the pin | Git blob |
| --- | --- | --- |
| `agents.py` | `src/local_shell_mcp/webui/agents.py` | `78384395bf93b00642c6a45fb7d7bbb7a3c01446` |
| `agent_validation.py` | `src/local_shell_mcp/webui/agent_validation.py` | `b500411155f878f295a682697cf9a0211532dbab` |
| `tests/hosted_chat/test_agents.py` | `tests/test_webui_agents.py` (repository cases) | `60319c018f5b002edd5d87c72ed7e7440a5968f1` |
| `tests/fixtures/hosted_chat/agent_parameter_vectors.json` | `tests/fixtures/librechat/agent_parameter_vectors.json` | `e10ecc3a4acd1dac652eb9e243b14f4a67b89bd1` |
| `tests/fixtures/hosted_chat/agent_core_validation_vectors.json` | `tests/fixtures/librechat/agent_validation_vectors.json` (core authoring fields only) | `e8a81f5e7c16abc6d6530e78061f50be68799ba5` |

The first authoring unit keeps the original field allowlist, nullable model,
parameter normalization, owner-bound CRUD, search, cursor pagination, clone
and revision history. Empty provider identity is rejected. Revision restore
replaces the configuration exactly and creates a new revision atomically.
Named channel references, including prior revisions, remain in the existing
provider deletion guard; deleting an Agent does not rewrite old conversations.
Graph, Actions, machine ACL, memory, Skills, and resource fields are introduced
by their own integration units. No authoring selection itself grants tool
access, and these repository helpers register no HTTP routes.

Generation ownership and protocol negotiation use the final pinned upstream:

| Workgate file | Upstream source at the pin | Git blob |
| --- | --- | --- |
| `generation_actor_lease.py` | `src/local_shell_mcp/webui/generation_actor_lease.py` (lease primitives) | `c14feb325a56ad29585050ce747c20f3b6bacc0b` |
| `generation_protocol.py` | `src/local_shell_mcp/webui/generation_protocol.py` | `9af2e8be0ad4ac8e11de081f4d949cee328b46a7` |
| `tests/hosted_chat/test_generation_actor_lease.py` | `tests/test_webui_generation_actor_lease.py` (lease primitives) | `e539bbadad0a5c5f10b2f88ff4f8902fde0b35d1` |
| `tests/hosted_chat/test_generation_protocol.py` | `tests/test_webui_generation_protocol.py` (negotiation cases) | `b578a4b662f5e3f2acbb0e3a134052c86f18ec35` |
| `tests/fixtures/hosted_chat/protocol.cjs` | `tests/fixtures/librechat/protocol.cjs` | `f4774d9b698cd0d073d0d9fe191e0238f0ad6e6d` |

Lease claims and renewals take their clock sample after acquiring the write
transaction. The same owner, live run, token and expiration fence renewal,
validation and active-run discovery. Callers performing durable mutations
must validate the matching lease within their own write transaction. The
pinned negotiation header and downgrade-only behavior are retained. Run
storage, authenticated SSE recovery, checkpoints and runner lifecycle tests
follow in their corresponding integration units; no actor or heartbeat is
started merely by importing these modules.

The ordinary generation store follows the final pinned generation repository:

| Workgate file | Upstream source at the pin | Git blob |
| --- | --- | --- |
| `generations.py` | `src/local_shell_mcp/webui/generations.py` (admission, event replay and terminal persistence) | `afc6ca41ea27925af48cc4d7b4af0a5bf1a154fc` |
| `tests/hosted_chat/test_generations.py` | `tests/test_webui_generations.py` (repository cases) | `a0eef99f6f97519f13b6c546a9ac0f2e936fbb64` |

Admission resolves the stored channel without decrypting credentials in the
same transaction as run, request, message and conversation writes. Ownership
and parent-message references are checked before publication. The first
negotiated protocol is immutable on retry. Generic public text/reasoning
and step events and terminal writes require the matching live actor lease.
Tool completions, human actions, steering, attachment admission and checkpoint
recovery remain their own transactional contracts; this private unit registers
no generation routes or startup execution. Runner-level total output and
resource budgets remain part of runtime acceptance.

The private model/tool loop follows the final pinned Agent runtime:

| Workgate file | Upstream source at the pin | Git blob |
| --- | --- | --- |
| `agent_runtime.py` | `src/local_shell_mcp/webui/agent_runtime.py` | `443e1fff953d12b6712618d17d9c45e1d7491e79` |
| `tests/hosted_chat/test_agent_runtime.py` | `tests/test_webui_agent_runtime.py` (runtime cases) | `57c4a4c17827457928cba1dd7f04666642ad6470` |

The source model/tools cycle, checkpoint boundaries, provider reasoning and
usage events, pre-event retry policy, human/steering callbacks and graph
handoff slots are retained. Complete proposals (including resumed ones) must
pass the native batch validator before checkpointing or dispatch. The
no-progress signature ignores provider tool-call correlation IDs so identical
outcomes cannot evade it by changing IDs. Semantic-index typing and the
route-level safe provider error test follow with their corresponding modules.
This private loop does not itself launch workers or expose routes.

The private owned loop-checkpoint bridge adapts the pinned AgentRunner's
`_snapshot`, `_save`, and state reconstruction from
`src/local_shell_mcp/webui/agent_runner.py` (blob
`ba059c4de763236ed1e6d4b7aaeb74ff1eff6b01`). It binds the event-actor saver to
an authenticated generation lease and its persisted conversation, preserves
pending proposals, started-tool boundaries, partial text, graph commands and
no-progress counters, and reconstructs the loop with a fresh caller-provided
AgentContext. It does not persist ProviderConfig or tool catalog grants.
Completed sibling receipt recovery and graph handoff restoration follow in
separate integration units.

The owned loop's tool receipts adapt the pinned AgentRunner completion and
recovery paths. Each started tool edge stores the original proposal and
structured result in immutable sibling-write slots. Recovery validates the
entire batch before dispatching only unsettled siblings, with fresh actor and
grant checks even for cached results. Native durable intent recovery remains
responsible for uncertain effects before a completion receipt was committed.

The private native tool node connects the model-facing catalog, exact native
bindings and owned sibling receipt execution to the pinned Agent loop. New
proposals resolve all targets before checkpointing; recovery preflights only
unsettled proposals, avoiding reexecution of completed sibling effects. The
existing native adapters enforce current machine ACLs on each effect; replay
returns historical owned results under a live principal and current Agent
selection, not a new Executor operation. Native MCP result envelopes reuse the
provider artifact formatter, with a bounded text projection and without
repeating identical structured fallback text. Foreground human approval and
live lifecycle orchestration are still required before route activation.

The private foreground loop driver adapts pinned AgentRunner streaming and
generation lifecycle callbacks (generation_routes.py). It retains the owner
fence, renews the running actor lease, persists text/reasoning deltas, and
records the last observable delta cursor in each loop checkpoint. Recovery
cannot replay an already observable uncheckpointed stream, and the persisted
run recovery marker prevents an expired/pruned checkpoint from becoming a
fresh generation. Provider failures expose safe category/status messages.
The driver requires a caller-supplied authorization callback; no HTTP route,
human-review admission, steering queue, or UI step lifecycle is activated.

Approval mode preferences and risk classification follow the final pinned
`webui/approval_settings.py` (blob `0630dbfa0bdb6c807ae0824737ee2a6fc9df39d6`)
and its source tests (`9eb2f2d8895bbe95617ad8205130ca19f55edb96`). Native
read and semantic Task/plan operations preserve the corresponding source
classification. Exact bounded `ls`/`pwd` are the only shell exceptions, and
native asynchronous/PTY execution, environment overrides, Session termination
and Job mutation require review in dangerous mode. Approval never changes
native authorization. Owner-scoped settings and versioned Agent configuration
are private pending human-action route integration.

Foreground human-review admission and resolution (`approval_claims.py`) adapt
`generations.py` pause/claim/recovery and `AgentRunner._pause` at the pinned
source. The owned checkpoint interrupt, pending action, actor retirement and
public pending event commit together; a one-shot validated resolution retains
its original started-edge identity through actor replacement. Expired or
missing checkpoints cannot authorize a continuation. Review remains private
until the execution and authenticated route units; a pending action with lost
checkpoint evidence needs terminal cancellation/recovery before exposure.

The foreground native tool node now composes the pinned approval-mode policy
and exact-edge review claims with durable sibling execution. An approved edit
uses the edited arguments for fresh native target/permission validation while
its receipt remains bound to the original model proposal. Rejected and denied
calls settle without native invocation. All unresolved effective targets and
caller-supplied content checks precede effects; changed policy is checked again
at dispatch. A newly required review on an already claimed edge fails closed
rather than silently expanding the claim. Public human-action routes and
content-filter admission before checkpointing remain separate integration work.

Pending approval finalization shares the normal generation final-message and
unread-event transaction. Explicit owner abort uses the native run
cancellation latch, whereas expiry or unavailable checkpoint evidence only
interrupts the generation. An exact action ID fences abort/expiry against
concurrent resume claims; neither transition invokes a native tool.
