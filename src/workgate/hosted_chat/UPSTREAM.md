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

Ask User contract follows `webui/ask_user_tools.py` at the pinned source
(blob `0eaec2c6f3acf8c009733872e720f055b8dc3498`) and source tests
(`bcace1ed888be37cad6448bc842e44e379a567ac`). The exported definition,
manifest, legacy single and batched question formats, UTF-16 limits and
resume validation match the original HITL oracle. The optional tool is
independent of machine grants; its durable question and execution lifecycle
are integrated separately.

Ask User reuses the owned human-action transaction and checkpoint interruption
rather than introducing a parallel state store. A question is bound to a lone
saved tool call and, if explicitly reviewed, its exact claimed edited input.
Validated single/batched answers are consumed once and remain recoverable
across actor replacement at that checkpoint. The shared abort/expiry lifecycle
also applies to questions. Catalog selection and execution integration follow.

The private foreground tool node exposes the pinned optional Ask User schema
only through explicit caller/saved-Agent selection. It requires a lone valid
question, applies legacy review policy (including edited questions), and
settles the exact validated answer through the ordinary durable receipt path.
No Executor operation or machine grant is needed. Pending review and question
interruptions retire the actor until an authenticated caller claims the next
stage. Public route admission remains separate.

Foreground text steering admission adapts pinned `webui/steering.py` (blob
`dd91e6ff644257ab64ee8dc0a1ff112f0161e2b3`): owner and generation epoch
fencing, FIFO queue, idempotent retries, preemption arming, cancellation and
terminal leftovers. A caller-supplied submission validator precedes admission;
media and event-trigger ingress are not exposed by this unit. Event budgets
share the generation protocol constants. Queue delivery and public admission
remain separate integration work.

Foreground steering delivery consumes queued text and quotes at the pinned
stream, post-tool and terminal boundaries. Public parts and FIFO consumption
commit together under the owning actor lease, and the ordered content
projection is shared with final messages. Observable model output is sealed
before stream/terminal delivery; a recovered pending tool edge reuses exact
receipts before reinserting missing deterministic steer IDs. Uncommitted tool
proposals can be preempted, but settled work is not undone. Media, graph
recipient election and public route activation remain separate.

Graph construction and owner admission use the final `webui/graph_topology.py`
(blob `8e075ec8c3a3c0a1199c573489c35acf1616819a`) and original agents SDK
constructor vectors/live oracle at `c4ffb9b1b78421bf63eba3e56993017f922387f3`.
The differential oracle tests construction, reachability, grouped prerequisites,
handoff scopes and parallel capability, not execution. Workgate checks malformed
endpoint shape before owner lookup and rejects missing or inaccessible members.
Saved authoring and StateGraph execution are separate dependent units.

Saved foreground graph authoring extends the pinned Agent validation and
revision repository with edges, member/entry/result IDs, and max handoffs.
Ownership and topology validation occur before committing a revision or
restore. The source origin placeholder is expanded on create/update. We
also preserve the topology's validated handoffScope and rebind explicit
self destinations/member/entry/result references on duplication so a copied
cyclic or self-entry graph remains a graph of the copy. Stored JSON does not
introduce executable conditional callbacks. Graph execution remains private.

Graph handoff contracts adapt pinned `webui/handoff.py` (blob
`b44dd394fe70472b89ca74d60786269603e8673f`) and its original SDK differential
oracle. Shared handoff budget and identity-union checks, turn/conversation
scope, transfer commands, parallel Send aggregation and recipient context
projection match the source. Destination ownership is checked on definition
and invocation; saved JSON authoring never creates executable condition
resolvers. StateGraph execution and checkpoint integration remain separate.

Graph checkpoint adapter and canonical message reducers adapt pinned
`webui/graph_checkpoint.py` (blob `45661a6667534c713a464d5663c4ef258c394e0c`)
and `webui/message_reducers.py` (blob `94c352240be542c1900198c88401d6dafcbae590`).
The adapter requires a live WorkGate generation actor lease for reads and
writes, checks the graph namespace and conversation, and filters decoded
metadata before limiting results. The SDK's typed serialization preserves
Send and pending-write values; real LangGraph restart/resume tests verify
that a completed node is not rerun. Graph execution remains unexposed.

The private graph runner adapts pinned `webui/graph_runtime.py` (blob
`2f82f83339a77b41c3c6ae51b9344d2233aba577`). Official Python StateGraph
owns the same direct-edge supersteps, grouped join barriers, prompted fan-in,
dynamic Command/Send routing and designated result. A Control-supplied
member callback owns native execution and durable receipts; no new scheduler.
The graph marker binds the revision and finite recursion limit to the
checkpoint namespace, refuses restart after checkpoint loss, and validates
root membership, live grants and each transfer. Tests retain the original
real JS message-class and graph traces; the live npm oracle is development-only.
Native member dispatch, graph-aware public output and human/steer integration
remain dependent work.

Each foreground StateGraph task uses its own owned loop checkpoint namespace
and recovery marker, corresponding to the pinned AgentRunner's
`/graph:<agent>:<activation>` namespace. Graph member identity, root revision,
membership and actor lease are checked before restore, receipt replay and
inside checkpoint writes. Provider call IDs remain unchanged in model history;
a derived activation-scoped native identity is available to the native
dispatcher. Root-only human claims cannot be published from member checkpoints
until graph-aware claim recovery is connected.

The native graph driver binds each official StateGraph activation to the same
checkpointed Agent loop and native tool node used by foreground turns. Provider
resolution and content admission remain caller-owned. Transfer tools come only
from live declared edges; their source Commands are settled with the same tool
receipts and projected by StateGraph, never by an unrestricted MCP fallback.
Provider correlation IDs remain round-local in handoff history. Member output
stays private until graph-aware event, approval and steering integration.

Graph human actions retain the source single pending-action protocol with
member namespace binding. The official SDK interrupts the member task and
persists settled sibling writes before Control publishes an action and
releases the actor lease. Claims verify both the exact member tool edge and
the latest SDK task interruption, including its proposal and activation.
Prior SDK interrupt positions replay against their durable action IDs;
approval and Ask User can occur successively at the same tool edge. Missing
graph checkpoints or changed graph membership cannot authorize a resume.

Foreground graph steering adapts the pinned source recipient_namespace
delivery map to the owned activation namespace. One SQLite FIFO drain
atomically publishes public steer parts and records its private recipient;
only that member may reconcile delivery after a crash. The shared model
history projection serves ordinary and graph turns. Tool-only preemption,
post-tool transfer delivery, parallel election and actor replacement are
covered with the existing native loop and SDK graph.

### Subagent authoring and configuration (J3)

The private delegation configuration follows the pinned source:

| Workgate file | Upstream source at the pin | Git blob |
| --- | --- | --- |
| `subagents.py` | `src/local_shell_mcp/webui/subagents.py` (configuration, schema and projection) | `f36dc5d6e17ed63bb79de6c6eb145c76ce03012e` |
| `agent_validation.py` | `src/local_shell_mcp/webui/agent_validation.py` (team authoring) | `b500411155f878f295a682697cf9a0211532dbab` |
| `tests/hosted_chat/test_subagent_config.py` | `tests/test_webui_subagents_source_port.py` (configuration and oracle cases) | `6e8c12969ce0fe7d3dba87529264b0cd306f29c5` |
| `tests/fixtures/hosted_chat/subagent_reference.cjs` | `tests/fixtures/librechat/subagent_reference.cjs` | `64858b708c9636c88778be1f77cc4b0a6af817f5` |
| `tests/fixtures/hosted_chat/subagent_source_hashes.json` | `tests/fixtures/librechat/subagent_source_hashes.json` | `29afaedecdde1e2a9e43e4ac85c349a36db4f9c8` |

Saved enabled targets are checked in the existing owner-scoped repository
transaction and resolved again on expansion. The stricter direct-only team
topology keeps explicit entry/result, grouped fan-in, acyclicity, and source
prompt restrictions. Duplicate target IDs fail at authoring rather than
during a model turn. Expansion retains source depth/count limits and explicit
recursion budgets; child configuration omits Ask User and inherited discovery
or summary state. This is not a permission or execution boundary: the schema
builders are not wired to runtime tool advertisement, team execution is not
enabled, and shareFiles/background/continuation require their own integration.

The private invocation adapter also ports `SubagentInvocation` and the
foreground path of `invoke_subagent` from the same pinned source. It
preserves isolated child instructions/task input, selected-tool hints,
explicit depth and recursion budgets, parent edge identity and final text.
Workgate additionally fences the saved parent and target revisions, checks
live inherited grants before admission and before returning results, and
does not return raw provider exception strings (which may contain secrets).
Per-effect inherited ACL enforcement, artifact prompt composition, and
detached execution remain separate integration work;
the callback cannot be reached from a public generation route.

Foreground child checkpoint ownership is a Workgate-native adaptation. The
parent generation actor admits a child against its exact started tool
checkpoint and immutable invocation digest. Separate private checkpoint
namespaces preserve isolated history and deterministic replay across actor
replacement. Parent and all child target revisions are checked live, nested
children cannot enlarge inherited scope or depth, and a replacement grant
cannot extend the original admitted grant. The existing saver and loop
checkpoint implement the recovery obligation; no second scheduler is
introduced. Native effect authorization and team runtime execution remain
gated until their dependent units.

The native child authority unit extends the existing intent ledger and tool
node. Admission and each live effect require the current parent edge; the
ledger uses the child scope's deterministic call identity and capped grant.
Every ancestor contributes a permission/tool-selection ceiling. Historical
cleanup resolves the original admitted revision chain without requiring the
parent edge to stay current: advancing a completed edge is not revocation.
Missing provenance or changed authority fails closed. Cached result
projection rechecks the current ACL. Child tools requiring human approval
are conservatively blocked until child-scoped human request integration;
no parent approval claim is borrowed. The private child model-loop host and
public delegation advertisement remain separate units.

Foreground delegation now has a private single-Agent host over the native
model loop. An explicit trusted caller supplies provider resolution and
content authorization. Ordinary self and saved children use private
checkpoints and final-text-only results; nested children inherit the same
native authorization ceilings. Recovery validates the original disclosure
ceiling and all admitted descendant revisions before returning a result.
The native node depends on a narrow delegation interface, not the runner.
Team graphs, detached children, and public route integration remain gated.

Delegated team checkpoint ownership reuses the same generation actor and
SQLite/SDK saver. Its scheduler namespace and each member activation are
private to the admitted child, with all member revisions pinned. The member
loop cannot use the scheduler namespace or another member's storage.
Native team graph execution and historical native-effect provenance are
separate integration units; the team schema is not advertised yet.

Team delegation reuses the official StateGraph driver and native member
loop. The synthetic root definition is pinned to the admitted child digest;
all team members retain the parent authorization ceiling and isolated native
call identity. Only the designated result member is projected to the parent.
No parent steering or human interrupt is applied inside a private child team;
team advertisement requires the optional graph runtime. Detached child
leases and public routing remain separate work.

Detached reservations now materialize one private generation under the
reserved conversation identity, using the task token rather than a second
foreground actor lease. Terminal generation, result, thread history and
child registration settle in one SQLite transaction. Ordinary admission and
message mutation cannot reopen or overwrite a registered child transcript;
active children must settle before conversation deletion. Native execution
and background tool advertisement remain gated on inherited effect authority.

Detached single-Agent execution now has a private checkpoint scope carrying
its original admission, parent conversation authority and machine-access
ceiling. Native resources remain owned by the child conversation; permissions,
approval settings and historical revocation use the original parent chain.
Detached team/nested execution and host scheduling are separate units.

The detached single-Agent worker now drives the existing native model loop
under the task lease and an explicit live authenticated caller. Recovery
reuses the private checkpoint and native call ledger. Cancellation, grant
revocation and timeout stop the worker; cooperative shutdown releases the
lease for recovery. Provider resolution and content authorization remain
injected by the caller. Public scheduling and team/nested execution remain
separate units.

Detached root-parent task controls adapt the pinned host's list, poll and
cancel operations. The current committed tool edge and owner/conversation/
Agent scope fence each invocation; terminal result claims and replay receipts
commit atomically, retiring a pending wakeup in the same transaction. Result
replay rechecks the original grant and disclosure ceiling. Worker scheduling,
steering controls and nested control authority remain separate units.

Detached root-parent controls now accept steer, queue, interrupt and message
withdrawal. Selection reserves a boundary before checkpointing; withdrawal
cannot race a reserved delivery. Applied receipts are acknowledged only from
the durable child head, allowing a replacement lease to retry safely. The
private native worker injects messages at tool/turn/preemption boundaries;
unconsumed controls are marked failed at terminal settlement. A provider
stream interrupt is cooperative at stream boundaries, not an independent
provider cancellation mechanism.

Child completion wakeup registration now shares the detached reservation
transaction. It uses the original root reply identity and owner preference;
private nested/member edges have no independent root continuation. Manual
result collection retires the pending intent atomically. The envelope contains
routing metadata only, not a persisted execution credential. Wakeup dispatch
and authenticated continuation remain separate units.

Completion continuation admission now uses the ordinary generation store
inside the same SQLite transaction as its one-shot result claim and dispatch
lease. An authenticated caller supplies a live grant and original disclosure
ceiling. The continuation follows the latest persisted assistant descendant
of the original reply; cancelled, deleted, expired and disabled sources do
not dispatch. The wakeup token is its generation actor, and ordinary actor
claim cannot adopt that generation. Terminal reconciliation, backoff and
native host scheduling remain separate units.

Completion dispatch now defers waiting sources using the pinned readiness
backoff without charging attempts. Transient failures retain the admitted
run and one-shot claim across at most eight attempts; fenced exhaustion
interrupts the run and restores manual result availability. Terminal
reconciliation and owner-scoped dead letters/requeue do not execute work.
A requeue allocates a new request identity and still requires live authority.

The authenticated completion worker reuses the foreground native loop and
its public-delta/checkpoint recovery. It resolves the exact persisted text
branch, caps scopes and tool selection to admission, and rechecks live
machine disclosure on every loop boundary. A separate dispatch heartbeat
owns the wakeup token; permission loss is terminal rather than retryable.
Human interruptions and full route/attachment projection remain gated.

Detached team workers reuse the existing private StateGraph runtime and
member-native authority under the detached task token. The original input
prefix is preserved while only the designated result member is appended to
the durable task transcript. Team steering remains disabled until a durable
member recipient election is implemented; ordinary cancellation still works.

Detached team steering elects a single durable member activation inside
its existing child-run state, without a database schema change. Interrupt
recipients are elected before preemption; steer/queue use the existing model
loop boundaries. The exact member checkpoint must contain the reserved
message before acknowledgment; replacement workers retain the election.

Detached single-Agent execution restores only the admitted foreground child
catalogue, with immutable depth, original task-actor and native authority.
The pinned background-deny rule still prohibits a detached worker from
launching another detached task; isolated teams carry no nested catalogue.
Executed descendant revisions are checked again before task settlement,
manual result disclosure, thread continuation, and completion wakeup.

### L6 artifact prompt selection

| Workgate file | Upstream source at the pin | Git blob |
| --- | --- | --- |
| `artifact_prompts.py` | `src/local_shell_mcp/webui/artifact_prompts.py` | `4d5cf9ec2cfeaf71b1c5f96add17c679df2f6587` |
| `artifact_prompt_snapshot.json` | `src/local_shell_mcp/webui/artifact_prompt_snapshot.json` | `d8af319e0268e8d546eb95ea893f1bb3fb383173` |
| `tests/hosted_chat/test_artifact_prompts.py` | `tests/test_webui_artifact_prompts.py` (template oracle and admission cases) | `4e3a732891d40444bf8bd30cd41054e2e58280bb` |

The packaged template data is unchanged. Foreground admission persists the
effective mode; retries reuse the original mode and the actual resolved
provider transport determines the template. Manual compaction and internal
completion wakeups do not add artifact prompts. No artifact execution,
preview, editing, or additional tool grant is introduced in this slice.

### L6 transactional artifact editing

| Workgate file | Upstream source at the pin | Git blob |
| --- | --- | --- |
| `artifacts.py` | `src/local_shell_mcp/webui/artifacts.py` | `bbe17ae4afb1918fd3d988e5e4078f581b19a895` |
| `artifact_edits.py`, `control/http/hosted_chat_artifacts.py` | `src/local_shell_mcp/webui/artifact_routes.py` | `e43587fc717dd100aadf05e224412cdf789c551a` |
| `tests/hosted_chat/test_artifacts_source.py` | `tests/test_webui_artifacts_source.py` | `7ceb26e1a47f0f24043dc22a6aa17a813ea74a55` |

The UTF-16 and fence-aware parser follows the original Node oracle. The
native edit transaction checks owner, live write grant, content policy,
terminal response identity, and the ordered canonical artifact sequence.
Visible text, canonical model steps and the final replay DTO change together;
the original streamed deltas and tool receipts remain untouched. Edited
responses carry user-submitted path markers and are excluded from inferred
model-origin semantic provenance. Active conversations and private child
threads are read-only. Inconsistent legacy text/content representations
or artifacts split across incompatible content parts fail closed.

### L7 static Office previews

Renderer and binary/hostile-input test vectors are ported from pinned
`src/local_shell_mcp/webui/office_preview.py` (blob
`d80b1d1ef18af14765829d0d469e68bcdd0f3f20`) and the upstream
`test_webui_office_preview.py` and `test_webui_office_boundary_regressions.py`.
Only escaped static HTML is generated, with optional xlrd for legacy XLS.
WorkGate additionally rejects duplicate ZIP members, excessive member/sheet
counts and aggregate rendered cell counts. Publication and browser isolation
are separate adapters; no model-facing or public route is introduced here.

The native publication adapter returns a private preview locator with the
owned attachment descriptor. Preview HTML is generated on demand from
checksum-verified original bytes, not embedded in event payloads or passed
to a model. The read route rechecks live authorization and attachment identity
after conversion, serves generated HTML under a sandboxed no-network CSP,
and preserves the original binary download if conversion is unavailable.

### M1 OpenAPI Action import

The pinned `webui/action_spec.py` (blob
`1b85a5be3506b98014403a34075bda4332d9cdb1`) defines operation projection,
names and domain binding. WorkGate additionally bounds expanded YAML trees
and projected schemas, rejects YAML merge-key expansion, malformed
and ambiguous parameter layouts, non-JSON values, and unsafe path syntax.
The schema module does not authorize network requests; Agent-bound
credential storage and transport are separate integration boundaries.

The Action repository maps pinned `webui/actions.py` (blob
`70c9c60616457103ebfe981131eebe51d18a7fda`) onto native Agent revisions
and the existing private credential envelope. Credentials are bound to
owner, Agent, Action ID and version; target/authentication changes require
fresh credentials, and inactive records participate in collision checks.
Agent selection and live Action references are both required. Invocation
and external OAuth flow handling are separate layers.

Action HTTP execution adapts pinned `webui/action_runtime.py` (blob
`757d0bf2c48ebcb76712cd41ef8fa861a1b40eca`) and reuses the origin-bound
DNS-pinned provider dialer without adopting its broader private-provider
policy. Actions default to public destinations only. Operators may restrict
domains with `WORKGATE_ACTION_ALLOWED_DOMAINS` and explicitly permit
private host:port targets with `WORKGATE_ACTION_ALLOWED_ADDRESSES`.
Cloud metadata, link-local, multicast and unspecified addresses remain
denied. The transport enforces a total deadline, bounded JSON requests and
uncompressed responses, disables redirects and ambient proxies, and scrubs
credential echoes. External effects still require the foreground approval
and durable receipt adapter.

Action authoring maps pinned `webui/action_routes.py` (blob
`79a70ed280faa1fef357624870662e8a010e52b3`) to the protected native Chat
Agent routes. List requires a live read grant; save/delete require write,
including a final authorization check inside the Agent/Action transaction.
The request body is capped and errors do not expose submitted credentials.
No public Action invocation endpoint is provided.

The foreground Action effect adapter binds the exact original checkpoint
proposal and Action version before human review. It uses the existing
finite-effect ledger with a distinct Action identity, requires an exact
approval and live write grant, and commits the claim before HTTP execution.
A missing response, HTTP failure, or interrupted settlement is permanently
uncertain; recovery does not offer the request again. This slice initially
supports only an interactive root; no child, scheduled or external-event
actor inherits Action authority. OAuth activation remains separate.

Interactive foreground generations explicitly admit the Action capability.
The native tool node advertises only live selected non-OAuth Action
definitions, preflights their arguments, and requires approve/reject
review with the exact HTTP target before releasing the saved batch.
The existing policy's explicit deny still wins. Replayed Action receipts
are checked against the finite-effect ledger. Manual compaction, graph
members, subagents and unattended actors do not receive this capability.

### M2 OAuth Action token ownership

Pinned `webui/action_oauth.py` (blob
`d281133840c5da926055b8754051a5c2e5778f55`) supplies the OAuth code
and refresh exchange contract. The native adapter shares the Action
DNS-pinned HTTPS transport and credential cipher, validates bounded token
responses, and binds sealed tokens to the owner, Agent, Action revision
and token kind. Refresh is claimed durably before exchange; interrupted
or expired claims require fresh authorization rather than replaying an
uncertain rotation. Editing any Action revision invalidates its tokens.
Browser callback, saved authorization flow and foreground resume are
separate slices and are not enabled here.

The native OAuth interruption retains the exact approved checkpoint and
does not create an effect claim before authorization. A browser start
requires a fresh owner write grant. One-use hashed state and an encrypted
browser-bound PKCE verifier bind the callback to the run, proposal,
Action revision, and configured redirect origin. Token settlement and
release of the saved interruption share a transaction; abort, Action
edits, expiration, and superseding flows prevent late settlement.
The callback cannot create an execution lease. Route wiring and resumed
OAuth execution remain separate.

The OAuth browser adapter uses the native protected UI start route and a
single OAuth-exempt GET callback. The explicit configured Control origin,
fresh owner write grant and exact pending identity are checked at start;
the callback checks the configured host, bounded query and flow-specific
HttpOnly, SameSite=Lax cookie (Secure for HTTPS). It returns static
no-store/no-referrer HTML without provider data. Successful authorization
does not launch execution; the caller must reattach with a fresh grant.

Interactive root OAuth tools are advertised only with a valid configured
Control callback origin. An exact approved batch acquires required live
tokens before any sibling effect is released. Missing authorization pauses
the saved proposal; reattachment rechecks the native grant and approval.
A settled effect is replayed without token acquisition, an uncertain
claim is never retried, and a replaced or expired access token cannot
create a new effect claim. Credentials stay inside the transport.

### M3 public web search

Pinned `webui/web_research.py` (blob
`f24dbfc21316f0570b943400d69e6b5c856d3e54`) supplies the optional DDGS
search and Bing RSS fallback, result shape, site filter and public document
download contract. The DDGS dependency uses its own search-engine networking;
the native fallback and document downloader use the shared origin-pinned
transport, no ambient proxies, no credentials, standard ports, bounded
responses and a total deadline. Each redirect is checked independently.
Results are bounded and untrusted; search does not retrieve result URLs.
External MCP search providers remain independent.

Native search follows the pinned generation request precedence and live
global/conversation composer preferences (default on for newly admitted
interactive roots). The protected preferences patch preserves other
feature fields. The model-loop node requires a live read grant, validates
the query before dispatch, respects native approval policy, and checks
selection again before disclosing or replaying a result. Manual
compaction and unattended or member actors receive no new capability.

### M4 static document extraction

Pinned web research extraction and passage-window behavior is isolated in
`web_documents.py`. It extracts readable HTML and PDF text (no OCR), keeps
page/heading offsets, enforces incremental UTF-8 text and page budgets,
and maps Unicode case-folded matches back to original character positions.
The locked pypdf reader is used with context-local parsing limits. Access
challenge pages and empty JavaScript shells are detected without bypassing
them. Cache and fetch activation are separate.

Public document reads reuse the DNS-pinned downloader, with the pinned
arXiv HTML/PDF alternative. The optional cache uses private owner-hashed
directories, atomic writes and a cross-process eviction lock. It validates
owner/request bindings, URL, text, format and offsets before reuse, expires
after 24 hours and enforces both count and byte quotas. Invalid or unavailable
cache entries never prevent a fresh public read. Static security challenges
are returned as unavailable, not bypassed.

The native web capability now exposes both search and static fetch under
the same persisted root admission and live composer policy. Fetch arguments
are validated before approval/dispatch, and cached results are subject to
the same live-grant and disclosure checks as search. Browser rendering is
not enabled by this slice.

### M6 scheduled chats

The recurrence engine and owner-scoped schedule records adapt pinned
`webui/schedule_routes.py` (blob `e48b09e15135c66f1d5f35dc5d3a8e5c28086a51`).
Calendar recurrence preserves timezone gaps and repeated folds, including
sparse cron dates. Configuration edits are transactional and revision-checked;
non-timing edits preserve the current due occurrence. Dependencies are
checked against owned live resources; disabled schedules can be paused even
after their dependencies have disappeared. Dispatch and worker activation
are separate from configuration storage.

The schedule occurrence and its tool-free generation admission are committed
together; request IDs are reserved to the internal path. The first actor
lease transaction marks the occurrence started, preventing replay after
the execution boundary even if the lease is later released or expires.
Terminal accounting is idempotent. Active occurrences must settle before
deleting their schedule. No scheduler or automatic generation launch is
enabled by this repository slice.

The Control supervisor dispatches admitted scheduled occurrences as
model-only streams. The actor loads the live owner-bound Agent revision,
server-owned API-key model profile, and verified file context, checks
content protection, and bounds elapsed time and generated bytes. No native
tool catalog, account OAuth capability, memory mutation or child graph
execution is inherited. The existing supervisor capacity and shutdown
lifecycle applies; losing an actor after the start fence is terminal.

Schedule list/create/edit/delete and run-now use the native protected
UI boundary, with explicit owner read/write scopes. Manual runs admit
an occurrence before waking the supervised dispatcher and return its
conversation and run identifiers. The generation status and event stream
are readable under an owner read grant; attach, abort, steering and
approval routes cannot reinterpret a scheduled occurrence as a native
foreground generation.

### M7 external Agent events

The source-key repository adapts pinned `webui/event_triggers.py`
(blob `4d5ba1cdd4c103916f6848ccfe6f457ef74aadda`). Source keys
are high-entropy bearer credentials stored only as SHA-256 hashes,
owner-scoped and restricted to event modes. Revocation invalidates
pending/leased deliveries in the same transaction. An event credential
is never a Controller OAuth principal or a native machine grant.

External delivery admission reconstructs the principal, source and
continuation target inside a single owner/source-bound SQLite transaction.
It preserves pinned delivery identity, ordered lanes and bounded child
batch coalescing, includes the effective lane in retry fingerprints, and
rejects expected machine actions or steering into native-granted runs.
The queue is not an execution authority; no worker is activated here.
