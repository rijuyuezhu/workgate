# Architecture and ownership boundaries

This page records the stable architecture contracts of the project: process
composition, dependency direction, runtime ownership, session authority,
durability, executor trust, machine-runtime constraints, and protocol/UI
boundaries. Architecture tests exact-freeze membership only where membership is
itself a security, packaging, public-surface, or process contract. Ordinary
internal files may move or split without being added to a central filename
inventory as long as the dependency and ownership invariants remain true.

!!! note
    This page describes the architecture currently implemented during the
    #123 migration. The canonical target contracts for that migration live in
    [Control/executor architecture](architecture/control-executor.md).

## Dependency direction

The target application structure separates process entry points, control delivery
adapters, shared HTTP infrastructure, Human UI behavior, transport-neutral
operations, and domain services:

```text
workgate/
  main.py                 argparse root and command registration
  protocol/               dependency-light control/executor wire contracts
  control/
    mcp/                   MCP control-plane adapter and middleware
    http/                  REST/tool HTTP control-plane adapter
  executor/
    jobs/                  shell-backed job lifecycle and durable runner
    tool_session/          machine session, grounding, and resource ownership
    terminal/              executor terminal runtime and bridge
    ...                    files/search/shell/integration implementations
  http/                    transport-neutral ASGI and HTTP infrastructure
  ui/
    ...                    transport-neutral Human UI core and runtimes
    http/                  Human UI HTTP adapters and routes
  tools/                   public tool contracts, metadata, and registration
  jobs/                    process-neutral/control-managed job mechanisms
  schemas/                 shared cross-domain contracts
  config/                  user settings plus resolved role-specific views
  agent_bridge/            shared Agent Bridge contracts/mechanisms
  utils/                   small dependency-leaf technical primitives
```

MCP and REST/tool HTTP delivery adapters live under `control`. The executor
process composition owner and all machine-authoritative session, filesystem,
shell/job, PTY, and local-integration implementations live under `executor`.
Human UI delivery adapters live in `ui/http`, and transport-neutral ASGI
infrastructure lives in `http`. The obsolete `executors` and `server` packages
have been removed and must not be restored.

The package root is frozen to `__init__.py`, `main.py`, `errors.py`, and
`version.py`. Every other implementation must live in an explicitly owned
subpackage; `tests/test_architecture.py` enforces this allowlist.

General rules:

- `main.py` owns only the root argparse parser and composes domain-owned CLI
  registration functions. Command modules load settings and invoke their own
  runtime handlers; `main.py` must not inspect `sys.argv` or import runtime apps.
- Control delivery adapters may compose public tools, OAuth, executor trust and
  routing, shared HTTP infrastructure, control-owned integrations, and UI route
  contributions. They must not import executor implementation modules or read
  executor workspace/path/command/machine policy.
- `http` must not import control delivery adapters or Human UI implementations.
- UI core must not import control delivery adapters. `ui/http` may depend on UI
  core and explicit control-side adapter seams.
- `tools`, `jobs`, `agent_bridge`, and `composition` are shared mechanism/contract
  layers. They must not depend on executor implementation modules. Machine
  behavior belongs under `executor`; public declarations stay fail-closed until
  role composition binds them to their owner.
- `schemas`, `protocol`, and small `utils` modules remain dependency-light shared
  contracts/primitives and must not become alternate homes for machine policy.
- `utils` is for small dependency-leaf technical primitives, not a holding area
  for domain algorithms or large workflows.

## CLI composition and registration

The command-line interface is one explicit argparse tree. `main.py` creates the
root parser, requires a public subcommand, invokes domain registration functions,
and dispatches the handler stored by argparse. It contains no command-specific
arguments, settings loading, runtime imports, or `argv[0]` special cases. The
private durable-job runner is also a normal argparse subcommand and is labeled
internal in help rather than parsed by a separate code path.

Runtime commands are explicit: `server`, `tui`, `mcp`, `worker`, `version`, and
the labeled internal `job-runner`. Running `workgate` without a command is
an argparse error.
Global `--version` remains an argparse version action. Settings flags follow the
command that consumes them, for example `workgate server --mode mcp` and
`workgate tui --port 8765`.

The control runtime is entered by the transport host, not by domain code.
REST HTTP owns it through the FastAPI application lifespan. MCP-over-HTTP owns
it through the outer Starlette lifespan that also owns the SDK session manager.
MCP stdio instead uses FastMCP's low-level server lifespan because stdio has one
`Server.run()` for the process lifetime. Compatibility calls to `run_http()` or
`run_mcp()` that do not supply a runtime construct one before building the host,
so those public runner paths retain the same ownership invariant. The process
runtime must not be attached to FastMCP's low-level lifespan for MCP-over-HTTP:
the SDK enters it once per MCP session, which is a narrower lifecycle than the
control process.

`ControlRuntime` and `ExecutorRuntime` each expose a frozen role-specific
configuration snapshot. Newly migrated root decisions read those views: control
owns server/auth/UI/state/admission policy, while executor owns workspace,
path/command policy, machine concurrency, and executable paths. Both runtimes
temporarily retain a clearly named `legacy_settings` bridge for components that
still consume the monolithic `Settings`; that bridge is migration debt, not a
shared-authority contract.

`ControlRuntime` also owns one `ControlState` backed by the same synchronous
`StateStore` used by the existing durable domains. It restores final executor
trust/revocation and control session binding/lifecycle facts before live actors
start; executor bearer plaintext and presence are not part of that registry.
Approved OAuth clients use that same store, while authorization codes remain in
the process-local `OAuthState`. Human UI connection/session registries and other
ordinary live coordination are likewise rebuilt empty after control restart;
this persistence seam is intentionally not a command database or workflow log.

Executor trust and delivery state follow the same ownership rule. `ControlRuntime`
constructs the control state, executor transport, pairing service, and session
coordinator explicitly. The control lifespan stops admission and interrupts
pending executor calls before restoring compatibility state-store bindings; no
module-level remote-manager runtime or local-execution fallback participates.

Managed background Jobs are control-owned rather than module-owned.
`ControlRuntime` constructs one `ManagedJobsRuntime`; its handler registry,
asyncio tasks, and cross-process liveness leases are scoped to that owner. The
`session_copy` managed handler is registered explicitly during control
composition. Executor shell-backed jobs have a separate executor-owned runtime.
Shutdown stops managed-job admission and cancels/awaits owned tasks before UI,
OAuth, executor transport, or shared-store teardown, so cancellation can commit
`stopped` (or durably journal the deferred store update) before its lease is
released. The remaining compatibility binding is reversible and non-owning.

Terminal live state is similarly process-owned rather than module-owned.
`ControlRuntime` and `ExecutorRuntime` each construct a fresh `TerminalRuntime`.
Its bridge and ConPTY registries bind async work to the owning event loop and
stop admission together during shutdown. Raw bridge operations are cancelled
and bridge timers/process attachments are closed before ConPTY sessions are
force-closed, because a bridge may hold a raw attachment to one of those shells.
The terminal modules retain only reversible, non-owning compatibility pointers
for legacy consumers that have not yet moved to explicit registry capabilities.

Rejected alternatives:

- implicit server startup with no subcommand: it makes root options double as one
  command's contract and prevents a clear extensible command tree.
- inspecting `argv[0]` for `job-runner`: it bypasses argparse composition, help,
  validation, and module-owned registration.
- defining all parsers and handlers in `main.py`: it couples the entry point to
  every runtime and makes command ownership unclear.

## `control/mcp`: MCP control delivery adapter

The `control/mcp` package owns framework-specific MCP composition and runtime
policy. It converts transport-neutral tool registries and shared services into a
FastMCP server, then runs that server over stdio or wraps the MCP SDK's ASGI app
for HTTP delivery. The HTTP wrapper installs the same `ui/http` routes used by
the REST control adapter before the catch-all MCP mount. The browser shell and assets
remain public, while Human UI APIs retain OAuth or trusted-loopback TUI checks.

Allowed dependencies include tools, operations, OAuth adapters, remote route
contributions, the shared `ui/http/routes.py` route contract, delivery-adapter-neutral
`http` infrastructure, audit recording, and MCP SDK types. Lower-level packages
must not import this control adapter. `control/cli.py`
selects and invokes it after argparse dispatch; `main.py` imports only that registrar.

Rejected ownership alternatives:

- `server/mcp`: “server” obscures that this is one selectable control adapter beside
  REST/tool HTTP delivery and Human UI delivery.
- `http`: stdio execution, FastMCP registration, MCP sessions, and MCP tool
  wrappers are MCP delivery-adapter concerns, not shared HTTP infrastructure.
- `tools`: tool declarations are transport-neutral; FastMCP registration and
  MCP-specific presentation must depend on tools, not the reverse.

## `control/http`: REST/tool HTTP control adapter

The `control/http` package owns the FastAPI application that exposes local tool
registries as REST endpoints. It defines the REST error representation, applies
tool-route timeout and cache policy, records HTTP-routed tool invocations, and
composes public route contributions and authentication into one runnable app.

Allowed dependencies include tools, operations, OAuth adapters, remote route
contributions, delivery-adapter-neutral `http` infrastructure, framework-specific
FastAPI/Starlette types, and the explicit `ui.http.routes.human_ui_routes`
composition contract. Lower-level packages must not import this control adapter.

Rejected ownership alternatives:

- `server/http`: “server” conflates executable REST composition with Human UI
  delivery and shared HTTP infrastructure.
- top-level `http`: generic request limits, health, and download response
  mechanics are shared; REST tool registration and error envelopes are not.
- `tools`: REST route generation consumes transport-neutral tool registries;
  putting it in `tools` would make the domain layer own a specific delivery adapter.
- `ui/http`: the REST control adapter consumes UI route contributions, but it also runs
  correctly without Human UI and owns all non-UI REST tool behavior.

## `http`: delivery-adapter-neutral HTTP infrastructure

The `http` package contains ASGI and HTTP behavior needed by more than one control
delivery adapter. It does not decide which adapter runs and does not own REST tools,
MCP protocol behavior, OAuth business rules, remote-worker business logic, or
Human UI workflows.

Allowed dependencies include configuration contracts, audit recording,
transport-neutral operations, version reporting, and Starlette ASGI types.
The package must not import `workgate.control` or UI modules, and no removed `server`
namespace may be reintroduced as an intermediary.

`tests/test_http_route_parity.py` locks the shared `/healthz`, `/readyz`,
`/version`, and `/download/{token}` route signatures, order, installation, and
public-matcher classification across both REST and MCP-over-HTTP control adapters.

Rejected ownership alternatives:

- `server/shared`: the name ties reusable HTTP infrastructure to an overloaded
  server package and hides that the code is delivery-adapter neutral.
- `utils`: these modules expose concrete Starlette/ASGI contracts and HTTP
  semantics; they are not general-purpose technical helpers.
- `control/http` or `control/mcp`: both control adapters consume the behavior, so
  either location would reverse the other adapter's dependency direction.


## Shared mechanisms and role composition

Shared packages contain contracts and mechanisms that have genuine consumers in
both roles; they do not provide an alternate machine-execution layer. Control and
executor construction pass narrow stores, configs, and callables into shared
mechanisms instead of making those mechanisms depend on a process runtime object.
Machine-facing implementations move under `executor`; control-owned orchestration
moves under `control`.

Rejected ownership alternatives:

- restoring a top-level `ops/` implementation namespace: machine-facing behavior now has an explicit executor owner, while public/control orchestration belongs under `control` or a narrow shared mechanism package;
- moving machine implementations under `tools`: executors should not depend on public tool registration merely to perform local work;
- putting role-specific runtime state into `utils`: ownership and lifecycle policy are domain behavior, not dependency-leaf helpers.

## `jobs`: shared mechanisms and executor shell-job ownership

The top-level `jobs` package retains process-neutral durable job records, recovery helpers, and control-managed job machinery. Shell-backed execution, subprocess runner lifecycle, and machine-session coupling live under `executor/jobs/`.

This split lets control-managed work such as background session copy retain durable job semantics without gaining executor shell authority. Executor shell jobs can consume shared job records/mechanisms, but shared `jobs` modules must not import executor implementations.

## `tools`: public contracts and registration

The `tools` package owns public tool declarations, schemas/adapters, metadata, and registration. Machine-facing declarations are fail-closed on their own and become executable only when role composition routes them to the bound executor. Control-owned tools are routed to control services explicitly.

Machine implementations do not live under `tools`: filesystem/search/shell/job/PTY/local-integration behavior belongs under `executor`. Shared tool machinery must therefore remain independent of executor implementation modules.

## `executor/tool_session`: machine workspace-session state

`executor/tool_session` owns durable executor-side session metadata, grounding snapshots, machine-session admission, persistent-shell resource ownership, and retention helpers. Control separately owns the public session identity/binding/lifecycle projection; both sides use the same opaque `session_id` but do not share one physical session store.

Filesystem layout and atomic storage mechanics remain below these owners in `persistence`.

## `persistence`: shared private-state layout and file-store primitives

The `persistence` package owns the canonical directory layout below the configured
state root and the small filesystem primitives shared by durable repositories. It
does not own domain schemas, retention policy, migrations, or application-level
state transitions. Session metadata, snapshots, Todo, session-local Audit, jobs,
OAuth, downloads, executor trust/session state, and UI modules retain their own
validation and lifecycle rules while resolving paths through this package.

Rejected ownership alternatives:

- `utils`: the layout and store define a stateful cross-domain contract rather
  than a small stateless helper.
- `config`: configuration selects the state root, but it must not own filesystem
  mutation, locking, serialization, or domain directory structure.
- one repository per domain with independently constructed paths: that recreates
  the inconsistent storage layout and permissions this boundary is intended to
  eliminate.
- compatibility or migration logic in the shared store: old layouts are not read
  or migrated; each current repository uses only the canonical layout.

## `telemetry`: transport-neutral host and process observations

The `telemetry` package collects best-effort runtime observations without
choosing how they are displayed. It may depend on configuration and operating
system APIs, but it must not import UI projections, control delivery adapters,
HTTP adapters, audit presentation, or role-composition services.

Rejected ownership alternatives:

- `ui/dashboard.py`: sampling is useful independently of a particular view model
  and should not force non-UI consumers to depend on Dashboard semantics.
- `utils`: sampling owns process-wide state and a coherent telemetry contract;
  it is not a small stateless helper.
- `ops`: collecting observations is a capability, not a user-triggered tool use
  case or command workflow.

## `ui`: transport-neutral Human UI core

The `ui` package owns Human UI view models, native-client runtime contracts, and
UI-specific security behavior. UI core must not import control delivery adapters
or HTTP route adapters. Control-side adapters may invoke UI core capabilities
when serving the Human UI, but those capabilities remain internal rather than
public tools.

Rejected ownership alternatives:

- top-level `dashboard.py`: the name exposes no package boundary and previously
  mixed generic host sampling with UI projection.
- `telemetry`: audit activity labels, alerts, health presentation, and redaction
  choices are view-model policy rather than raw observations.
- `control/http`: Dashboard projection and image decoding belong below the
  delivery adapter because they are independent of query parameters and JSON
  response construction.
- top-level `image_preview.py`: a package-root file hid that thumbnail generation
  is a Human UI rendering capability rather than a project-wide utility.
- top-level `ui_security.py`: the old location obscured that the local-token
  bypass is narrowly scoped Human UI policy, not a general authentication or
  transport-security facility.
- top-level `tui_runtime.py`: package-root placement hid that executable
  discovery, embedded-payload extraction, and process launch are one native UI
  runtime contract.
- `release`: release code builds and embeds the sidecar, but runtime resolution
  and extraction policy belongs to the client that consumes it.
- duplicated runtime/release literals: executable names are a cross-layer artifact
  contract, so `ui/contracts.py` owns them and the Bun-side mirror is generated and
  checked rather than maintained as an independent source of truth.
- `oauth`: OAuth middleware consumes the UI trust decision, but credential
  creation and loopback UI namespace rules must remain owned by the UI domain.

### `ui/static`: packaged browser assets

The `ui/static` directory owns the immutable HTML, CSS, JavaScript, third-party
license, syntax-highlighting, and terminal-rendering assets served by
`ui/http/routes.py`. Keeping the assets under the UI domain makes their product
ownership explicit while preserving their package-data role; they contain no
Python modules and are deliberately excluded from the trimmed remote-worker
runtime.

The directory contains exactly the browser shell (`index.html`), Human UI styles
(`web.css`), the classic bootstrap controller (`web.js`), feature ES modules such
as `dashboard.js`, `remotes.js`, `audit.js`, `sessions.js`, `terminal.js`, and
`files.js`, the shared stateless Audit renderer/parser (`audit_view.js`), the
OpenTUI console bridge, syntax highlighter, terminal renderer, vendored xterm
bundle and stylesheet, and the corresponding license notice. Build and
architecture gates reject symlinks, Python files, unexpected assets, and
restoration of the former top-level `ui_static` path.

The browser controller presents agent state through one Sessions control surface:
machine and recent/all session selection precede session details, Todo, and the
session-local Audit view. The separate Global Audit panel deliberately retains
machine-wide lifecycle, control-plane, and other events that are not owned by one
session. The browser never exposes worker-internal session identifiers.

Rejected ownership alternatives:

- top-level `ui_static`: the name exposed package mechanics without expressing
  that these files belong exclusively to the Human UI product surface.
- `ui/http/static`: HTTP adapters serve the files, but the same assets are UI
  product resources packaged independently of any specific control delivery adapter.
- `control/http`: the REST control adapter composes routes and middleware; it must not
  own browser presentation resources.

### `ui/http`: Human UI delivery adapters

The `ui/http` package owns Starlette request parsing, authorization checks,
response normalization, route composition, and WebSocket delivery for the Human
UI. It may consume transport-neutral UI core and domain capabilities, but it must
not depend on either control delivery adapter. The REST control adapter consumes only
`ui.http.routes.human_ui_routes` during application composition; no intermediate
`server` namespace remains.

Rejected ownership alternatives:

- `server/http`: that path mixed one product surface with a generic “server”
  namespace and obscured the separation between MCP and REST control adapters.
- `control/http`: the REST control adapter assembles middleware and route contributions;
  it should not own Human UI feature adapters or static assets.
- transport-neutral `ui` core modules: Starlette requests, responses, scopes, and
  WebSockets are delivery details and must not leak into reusable UI contracts.
- `utils`: validation and normalization here implement the Human UI HTTP schema,
  not generally reusable primitives.

## `terminal`: interactive terminal backends and lifecycle

The `terminal` package owns terminal-emulation backends and the bounded lifecycle
operations needed by local tools, legacy remote-worker execution, and Human UI adapters. It
may depend on configuration, audit, schemas, and low-level operation helpers, but
it must not depend on control delivery adapters, HTTP route adapters, or UI presentation.

Rejected ownership alternatives:

- top-level `conpty.py`, `terminal_bridge.py`, and `tmux_helper.py`: package-root
  placement exposed implementation details without identifying the interactive-
  terminal capability they serve.
- `utils`: ConPTY and bridge registries own stateful process, capability, and
  stream lifecycles rather than reusable stateless helpers.
- public tool registries: shell declarations select an operation but should not
  own backend implementations or terminal lifecycle state.
- `ui/http`: Human UI adapters consume terminal bridges, but executor terminal
  lifetime remains below HTTP delivery.

## `audit`: redacted event persistence and query

The `audit` package owns the transport-neutral lifecycle of bounded, redacted
audit events. Every session-owned event is appended to both the global log and
that session's colocated log; events without a session remain global-only. The
global log remains authoritative for payload-object retention, while local logs
provide direct per-session reads without re-scanning unrelated records. Audit may
consume configuration, persistence primitives, tool-session identity, redaction,
and the current payload store, but it must not depend on control delivery adapters, HTTP
route adapters, Human UI presentation, or terminal implementations.

Rejected ownership alternatives:

- top-level `audit.py` and `audit_payloads.py`: they split one audit persistence
  domain across unrelated package-root files and made implementation paths look
  like supported public APIs.
- `utils`: audit policy includes redaction, retention, event semantics, payload
  references, and public query behavior rather than generic serialization.
- `control` delivery adapters or `ui/http`: those layers emit and present audit events, but the
  same event store is shared across every delivery surface.

## Patch operation ownership

Patch application is split into orchestration and domain mechanics inside `ops`:

Rejected ownership alternatives:

- top-level `patch_ops.py`: package-root placement obscured that the parser exists
  solely to support the apply-patch operation.
- `utils`: envelope grammar, hunk matching, cwd containment, and Git diff
  rendering are cohesive patch-domain policy rather than generally reusable
  helpers.

## Agent Bridge data and status boundaries

Agent Bridge configuration and discovery follow a one-way data dependency graph.
`models.py` is the pure contract leaf; auth, Skill scanning, and source discovery
consume those contracts. Public status projection sits above those domains so
models never import credential or presentation behavior.

Rejected ownership alternatives:

- `AgentCapabilityRegistry.config_status()`: placing auth-aware redaction on the
  data object forced `models.py` to import `auth.py`, reversing the intended
  dependency direction.
- defining `SkillSource` in `sources.py`: model and fingerprint annotations then
  had to point back into discovery policy.
- `utils`: these contracts and projections are specific to the Agent Bridge
  product surface rather than generic serialization helpers.

## Executor machine-state ownership

Executor-owned machine state is physically colocated with executor authority.
`executor/tool_session/` owns workspace-session records, grounding snapshots,
path resolution, and persistent-shell resource ownership. `executor/jobs/` owns
shell-backed job lifecycle and the durable subprocess runner. The top-level
`jobs/` package retains only process-neutral/control-managed job mechanisms.

Control keeps only logical session identity/binding and orchestration state. Its
runtime carries a resolved `ControlConfig` that intentionally has no
`workspace_root`, path/command denylist, shell executable, or executor file/search
limits. Public machine-tool descriptions therefore state that the bound executor
enforces its own policy instead of advertising the control host's values.

## `release`: artifact construction and verification

The `release` package owns build-time artifact assembly and validation. It is
outside runtime execution paths and must not depend on control delivery adapters,
HTTP adapters, executor runtime state, or tool operations. Its only project-local
dependency is the dependency-leaf OpenTUI filename contract in `ui/contracts.py`.

Rejected ownership alternatives:

- top-level `platform_wheel.py`: package-root placement made release tooling look
  like a runtime capability.
- `ui` or `terminal`: those packages consume the native assets at runtime, while
  release code constructs and proves the distributable payloads.
- `utils`: wheel metadata, binary format validation, deterministic archives, and
  release locking form one build-time domain rather than general helpers.

## Large-module reassessment

The architecture-hardening review compares large stateful modules by responsibility
clusters, dependency direction, compatibility surface, and focused test ownership.
Accepted decompositions preserve stable facades rather than moving code by size alone:

- `jobs/runtime.py` is now a compatibility/orchestration facade over dedicated
  lifecycle, shell, managed, persistence, recovery, state, and runner modules while
  preserving the durable job format and trimmed worker-runtime contract.
- Human UI terminal delivery is split into `terminal_protocol.py`,
  `terminal_websocket.py`, and the stable `terminals.py` facade. The split was only
  accepted together with typed protocol-contract tests and local/remote adapter-parity
  coverage, so REST and WebSocket consumers keep the same normalized machine-scoped
  behavior and existing monkeypatch/import surfaces remain valid.
- `remote/transfer_gateway.py` remains intact. Its ticket store, spool identity,
  authorization, range handling, cleanup, and router composition jointly enforce the
  transfer security and TOCTOU model; no narrower security contract currently
  justifies separating them.

Further large-module decomposition must start from a concrete behavior, dependency,
or testability problem and complete its own focused validation and compatibility
closure.
