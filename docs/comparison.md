# Comparison with upstream

Workgate originated as a fork of
[`fwerkor/local-shell-mcp`](https://github.com/fwerkor/local-shell-mcp) and is now
independently named and maintained. Both projects provide controlled shell,
filesystem, remote-execution, job, file-link, audit, Skill, and human-interface
capabilities for MCP clients, but they are no longer drop-in replacements for one
another.

This page compares the product models and major user-visible capabilities. It is
not a claim that one branch contains every commit from the other. The upstream
column is periodically checked against upstream `main`; the Workgate column
tracks the current repository surface.

## The main distinction

Workgate separates explicit **semantic tasks** from **execution sessions**. A durable
`task_id` owns objective, progress, plan, Todo compatibility, and task-wide Audit
history. A client starts an execution session on a paired executor, receives one
stable `session_id`, and uses that identity for files, commands, jobs, browser,
transfers, cwd changes, and teardown. Sessions may attach to a task, but that
attachment never acts as hidden executor/workdir routing authority.

Upstream exposes operations more directly. Its normal tools accept local context
or an optional remote `machine`, without requiring the same durable workspace
session abstraction first.

That difference shapes the public tool names, lifecycle rules, remote model,
state recovery, and UI behavior in each project.

## Major functional differences

| Area | Workgate | Upstream | User-visible consequence |
|---|---|---|---|
| Executor-backed execution | Starts an execution session on a paired executor, then reuses its `session_id` for read, edit, shell, job, PTY, browser, and transfer tools; those operations remain bound to that executor/workdir. | Normal execution tools can select a remote machine directly; separate remote administration tools manage workers. | Workgate has a uniform control/executor session contract, while upstream avoids a mandatory execution-session step. |
| Durable task handoff | `task` and `task_plan` own durable objective/progress/plan state under a machine-independent `task_id`; `read_todos`/`write_todos` project the same plan. A task can attach zero or many execution sessions. | `session_manage` and `plan_manage` use a separate Logical Session identity that is deliberately independent of machine/cwd. | Both provide durable semantic handoff; Workgate names the semantic identity `task_id` so it cannot be confused with executor/workdir sessions. |
| Public tool surface | Uses compact execution-session tools such as `bash`, `read`, `search`, `job`, and `session_copy`, plus task-scoped `task`/`task_plan`/Todo tools and audit queries by explicit task and/or execution session. | Uses direct domain tools such as `run_shell_tool`, `grep_search`, `shell_*`, `job_*`, and `transfer_path`, plus Logical Session task tools. | Prompts and integrations written for one project generally need adaptation for the other. |
| Jobs and lifecycle recovery | Unifies shell jobs and control-managed work, including background copies, in one durable `job` surface with retry, cancel, lost-state recovery, ownership leases, and session-retention protection. | Provides tracked jobs and persistent shells through its direct tool model. | Workgate treats long-running work and its owning context as one lifecycle domain, including across control restarts and multiple control processes. |
| Cross-workspace transfer | `session_copy` copies between two existing sessions on the same or different executors. Large cross-executor transfers can use private resumable HTTP streaming while retaining durable retry state. | `transfer_path` moves files or directories between controller and worker endpoints using the upstream machine-oriented model. | Both support transfer, but Workgate binds both endpoints and retry state to explicit execution sessions and managed jobs. |
| Agent capabilities | Keeps the dynamic Agent Bridge: clients can discover Skills, inspect configured upstream MCP servers, authorize them, and invoke selected bridged tools through server-managed credentials. | Provides a fixed three-tool Skills workflow and intentionally removed the earlier dynamic MCP bridge. | Choose Workgate when one control server must broker reusable Skills and additional MCP servers; choose upstream when a fixed, smaller capability surface is preferred. |
| Browser automation | Exposes session-owned `browser_session`, `browser_snapshot`, and `browser_act` on the bound executor, with bounded snapshots, ref-based actions, explicit executor-local named profiles, workdir-confined storage-state import/export, and no script escape hatch. | Exposes structured browser sessions plus persistent profile/storage-state support and arbitrary Playwright scripts on capable workers. | Both provide reusable browser authentication state; Workgate keeps persistence executor-local/session-routed and deliberately omits arbitrary scripts. |
| Human interfaces | Keeps a browser-native management UI as the default and offers native OpenTUI plus an authenticated browser OpenTUI Console against the same APIs. | Presents compatible Web UI and OpenTUI modes through its shared interface backend. | Both provide browser and terminal interfaces, but their default presentation, adapters, and state model differ. |
| Audit and sensitive data | Applies uniform credential redaction, bounded records, task/session queries, and optional recovery of one retained sanitized payload through the separate `audit:full` scope. | Provides audit inspection within its own direct-operation and machine model. | Workgate favors conservative retention and explicit recovery authorization rather than retaining unrestricted raw inputs. |
| Resource lifecycle | Adds bounded durable sessions and snapshots, cross-process admission locks, managed-job and persistent-shell liveness leases, bounded-command descendant containment, and fail-closed teardown when ownership is uncertain. | Maintains its own command, job, shell, worker, and transfer limits without Workgate's session-owned lifecycle layer. | Workgate accepts more serialization and fail-closed behavior in exchange for stronger coordination when several server processes share one state directory. |
| Documentation | Maintains one canonical English documentation set. | Maintains multilingual documentation and localized navigation. | Upstream currently serves more documentation languages; Workgate avoids duplicated translations that could become stale. |
| Releases | Uses an independent release line, Python 3.14+, executables, and platform wheels with embedded OpenTUI runtimes. | Uses its own release line and currently supports Python 3.11+. | Packages, configuration, state, and release assets must not be mixed between the two projects. |

## Shared foundation

The projects still share the same broad purpose and many operational outcomes:

- controlled shell and Python execution;
- persistent terminals and tracked background work;
- session-workdir-anchored file inspection and mutation, with machine-wide executor filesystem authority;
- executors that connect outbound to a control service;
- OAuth-protected MCP and HTTP operation;
- file links, Audit, Todos, Skills, and human interfaces;
- standalone and Python deployment paths.

A feature may therefore exist in both projects while using different public tool
names, state ownership, modules, security checks, and recovery rules.

## Which project fits which workflow?

Prefer Workgate when you need:

- explicit execution sessions bound to paired executors, plus independent `task_id` state for durable task progress;
- durable control-managed work and explicit teardown semantics;
- the dynamic Agent Bridge for configured MCP servers;
- Workgate's browser-native operations UI and release matrix.

Prefer upstream when you need:

- direct tools without first creating an execution session;
- arbitrary Playwright scripts;
- upstream's fixed Skills-only capability model;
- multilingual documentation;
- compatibility with upstream prompts, configuration, and releases.

Neither choice is a compatibility mode for the other. Do not assume that tool
names, OAuth state, executor or worker identities, durable state files, or release
artifacts can be moved between them without a reviewed migration.

## Detailed evidence

Use the following resources for deeper inspection:

- [Upstream repository](https://github.com/fwerkor/local-shell-mcp)
- [GitHub code comparison](https://github.com/fwerkor/local-shell-mcp/compare/main...rijuyuezhu:workgate:main)
- [Historical fork/upstream migration audit](maintenance/fork-upstream-differences.md)
- [Chronological upstream commit review](maintenance/upstream-commit-review.md)

The GitHub comparison is useful for source history, but it is not a complete
feature comparison. The projects often implement the same outcome through
different APIs, and several important differences are Workgate-specific product choices
rather than missing upstream commits.
