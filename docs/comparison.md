# Comparison with upstream

Workgate originated as a fork of
[`fwerkor/local-shell-mcp`](https://github.com/fwerkor/local-shell-mcp) and is now
independently named and maintained. Both projects provide controlled shell,
filesystem, remote-worker, job, file-link, audit, Skill, and human-interface
capabilities for MCP clients, but they are no longer drop-in replacements for one
another.

This page compares the product models and major user-visible capabilities. It is
not a claim that one branch contains every commit from the other. The upstream
column remains based on upstream release `v3.1.4`, reviewed on July 31, 2026;
the Workgate column is maintained with the current repository surface as it evolves.

## The main distinction

Workgate makes an explicit **agent/workspace session** the owner of operational
context. A client starts a session on a paired executor, receives one stable shared
`session_id`, and uses that identity for files, commands, jobs, Todos, Audit,
transfers, cwd changes, and teardown.

Upstream exposes operations more directly. Its normal tools accept local context
or an optional remote `machine`, without requiring the same durable workspace
session abstraction first.

That difference shapes the public tool names, lifecycle rules, remote model,
state recovery, and UI behavior in each project.

## Major functional differences

| Area | Workgate | Upstream | User-visible consequence |
|---|---|---|---|
| Workspace context | Requires `session_start`; operations are owned by a durable `session_id`, and `session_end` explicitly releases the session. | Tools are invoked directly against the configured workspace, with optional machine selection where supported. | Workgate is better suited to clients that need explicit, inspectable context instead of relying on conversational memory to remember cwd, machine, and related state. |
| Executor-backed execution | Starts a workspace session on a paired executor, then reuses the same shared `session_id` for read, edit, shell, job, PTY, Todo, Audit, and transfer tools. | Normal execution tools can select a remote machine directly; separate remote administration tools manage workers. | Workgate has a uniform control/executor session contract, while upstream avoids a mandatory session-creation step. |
| Public tool surface | Uses compact session-owned tools such as `bash`, `read`, `search`, `job`, and `session_copy`, plus persistent-shell companion tools. | Uses direct domain tools such as `run_shell_tool`, `grep_search`, `shell_*`, `job_*`, and `transfer_path`. | Prompts and integrations written for one project generally need adaptation for the other. |
| Jobs and lifecycle recovery | Unifies shell jobs and controller-managed work, including background copies, in one durable `job` surface with retry, cancel, lost-state recovery, ownership leases, and session-retention protection. | Provides tracked jobs and persistent shells through its direct tool model. | Workgate treats long-running work and its owning context as one lifecycle domain, including across controller restarts and multiple server processes. |
| Cross-workspace transfer | `session_copy` copies between two existing sessions on the same or different executors. Large cross-executor transfers can use private resumable HTTP streaming while retaining durable retry state. | `transfer_path` moves files or directories between controller and worker endpoints using the upstream machine-oriented model. | Both support transfer, but Workgate binds both endpoints and retry state to explicit shared sessions and managed jobs. |
| Agent capabilities | Keeps the dynamic Agent Bridge: clients can discover Skills, inspect configured upstream MCP servers, authorize them, and invoke selected bridged tools through server-managed credentials. | Provides a fixed three-tool Skills workflow and intentionally removed the earlier dynamic MCP bridge. | Choose Workgate when one control server must broker reusable Skills and additional MCP servers; choose upstream when a fixed, smaller capability surface is preferred. |
| Browser automation | Does not expose public structured browser-automation tools. Browser tooling may still be run through an authorized shell, and real Chromium E2E is used to test the Human UI. | Exposes structured page-text extraction, capture, and Playwright script tools locally and on capable remote workers. | Upstream is the direct choice when browser automation must be a first-class MCP API. |
| Human interfaces | Keeps a browser-native management UI as the default and offers native OpenTUI plus an authenticated browser OpenTUI Console against the same APIs. | Presents compatible Web UI and OpenTUI modes through its shared interface backend. | Both provide browser and terminal interfaces, but their default presentation, adapters, and state model differ. |
| Audit and sensitive data | Applies uniform credential redaction, bounded records, executor-backed session queries, and optional recovery of one retained sanitized payload through the separate `audit:full` scope. | Provides audit inspection within its own direct-operation and machine model. | Workgate favors conservative retention and explicit recovery authorization rather than retaining unrestricted raw inputs. |
| Resource lifecycle | Adds bounded durable sessions and snapshots, cross-process admission locks, managed-job and persistent-shell liveness leases, bounded-command descendant containment, and fail-closed teardown when ownership is uncertain. | Maintains its own command, job, shell, worker, and transfer limits without Workgate's session-owned lifecycle layer. | Workgate accepts more serialization and fail-closed behavior in exchange for stronger coordination when several server processes share one state directory. |
| Documentation | Maintains one canonical English documentation set. | Maintains multilingual documentation and localized navigation. | Upstream currently serves more documentation languages; Workgate avoids duplicated translations that could become stale. |
| Releases | Uses an independent 4.x release line, Python 3.14+, executables, and platform wheels with embedded OpenTUI runtimes. | Uses its independent 3.x release line and currently supports Python 3.11+. | Packages, configuration, state, and release assets must not be mixed between the two projects. |

## Shared foundation

The projects still share the same broad purpose and many operational outcomes:

- controlled shell and Python execution;
- persistent terminals and tracked background work;
- workspace-confined file inspection and mutation;
- executors that connect outbound to a control service;
- OAuth-protected MCP and HTTP operation;
- file links, Audit, Todos, Skills, and human interfaces;
- standalone and Python deployment paths.

A feature may therefore exist in both projects while using different public tool
names, state ownership, modules, security checks, and recovery rules.

## Which project fits which workflow?

Prefer Workgate when you need:

- explicit local or remote workspace sessions;
- session-owned cwd, jobs, Todos, Audit, and transfers;
- durable controller-managed work and teardown semantics;
- the dynamic Agent Bridge for configured MCP servers;
- Workgate's browser-native operations UI and release matrix.

Prefer upstream when you need:

- direct tools without first creating a workspace session;
- first-class structured browser and Playwright automation;
- upstream's fixed Skills-only capability model;
- multilingual documentation;
- compatibility with upstream prompts, configuration, and releases.

Neither choice is a compatibility mode for the other. Do not assume that tool
names, OAuth state, worker identities, durable state files, or release artifacts
can be moved between them without a reviewed migration.

## Detailed evidence

Use the following resources for deeper inspection:

- [Upstream repository](https://github.com/fwerkor/local-shell-mcp)
- [GitHub code comparison](https://github.com/fwerkor/local-shell-mcp/compare/main...rijuyuezhu:workgate:main)
- [Detailed Workgate and upstream differences](maintenance/fork-upstream-differences.md)
- [Chronological upstream commit review](maintenance/upstream-commit-review.md)

The GitHub comparison is useful for source history, but it is not a complete
feature comparison. The projects often implement the same outcome through
different APIs, and several important differences are Workgate-specific product choices
rather than missing upstream commits.
