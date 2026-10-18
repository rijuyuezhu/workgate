# Native platform boundaries for Windows support

Tracking issue: #381; foundation: #382. Historical references: upstream
fwerkor/local-shell-mcp v5.0.0, and WorkGate before Windows removal in #272.

## Design constraints

The workgate.platform package owns host-specific capabilities. It does not own
Executor/Control authorization, user sessions, policy, audit, durable jobs,
transfers or the terminal wire protocol. Prefer small capability-specific
contracts over one platform manager with unrelated methods. Select a backend
only when that capability is used; unimplemented backends must fail explicitly.

The foundation currently provides:

- host_os: normalized host identification, never assuming unknown OSes are POSIX.
- new_process_group_kwargs and terminate_process_tree: POSIX subprocess
  session creation and bounded process-group termination. The internal
  bounded runner still owns cleanup of escaped descendants; Windows Job
  Object containment remains to be implemented in #385.
- TerminalAttachment: byte-stream protocol (read_wait, write_all, resize,
  close_sync) for one live WebUI client connection. The POSIX tmux/PTY
  attachment and tmux resolver now live in platform/posix/.
- Existing GuiBackend: already a domain-specific protocol. Its Linux/macOS
  implementations remain intact pending native Windows GUI (#389). Native
  per-user services still use ExecutorServiceManager; refactoring those
  internals is part of #388.

## Behavioral invariants

| Capability | Portable guarantee | OS-specific work remaining |
| --- | --- | --- |
| Bounded command | Preserve stdout/stderr/exit behavior; on timeout or cancellation, remove descendants or report failure | Windows Job Objects or equivalent containment (#384/#385) |
| Terminal | Authoritative ownership, bounded streams/sizes, detach one client without ending the shell, explicit restart semantics | Windows ConPTY lifecycle, concurrent attachments, possible durable broker (#386) |
| Files | Private credentials, exclusive locks, safe handles/identity, atomic transfers and traversal protection | Windows ACLs, junctions/reparse points, sharing rules (#383) |
| Service | Install/start/stop/status/uninstall without modifying credentials or creating competing supervisors | Task Scheduler and Colocate lifecycle (#388) |
| GUI | Scoped captures/actions, permissions, freshness checks, explicit capability discovery | Windows UI Automation, interactive desktop restrictions (#389) |
| Bootstrap | Authenticated pairing, verified runtime updates and artifact ownership | PowerShell installation/upgrade (#390) |

Windows and unsupported hosts must not accidentally inherit Linux/macOS
defaults. In particular, tmux-pty attachment is not equivalent to an in-process
ConPTY shell across Executor restarts; report capabilities and limitations
accurately until tested.

## Incremental migration

The foundation extracts POSIX PTY attachment and process-group termination
from generic shell transport. Nonblocking PTY setup now precedes child launch,
so a setup error cannot orphan a new attached child. It centralizes host
selection for GUI/services without moving those implementations yet. It does not claim Windows or release support.
Follow-up issues #383-#391 own their Windows implementations. Native Windows,
Linux and macOS parity and final security/KISS review are in #392.
