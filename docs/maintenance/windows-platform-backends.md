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

## Windows filesystem migration (#383, in progress)

The initial filesystem slice selects %APPDATA% for roaming configuration,
%LOCALAPPDATA% for local state/data and %TEMP% for disposable runtime data.
Windows roots are parsed using native drive/UNC rules. Display basenames
received by a POSIX Control from a Windows Executor are parsed without using
the Control host's Path class. Lock shards normalize Windows case and
separators, and the Windows locking adapter uses msvcrt.locking on a one-byte
sentinel. None of these changes assert completed Windows support.

### Implemented native security slice

On Windows, WorkGate-private directory/file creation now specifies a protected
DACL **at creation**, explicitly owning the object as the current user and
granted only to that SID and LocalSystem. Existing objects are accepted only
after handle-based owner/DACL/type verification. Private files, atomic
replacement, lock files and transfer temporary files use the native backend.
Windows file opens do not silently replace O_NOFOLLOW with zero: CreateFileW
opens the final reparse point itself, GetFileInformationByHandleEx inspects the
handle, and opens every parent component using NT RootDirectory-relative handles.
Leaf opens and atomic rename also use a verified parent directory handle, so
an adversarial parent rename cannot redirect the operation to a newly
substituted path. Windows paths and archive members now
reject native device names, ADS, traversal, case-colliding entries and
symlink/junction directory sources. POSIX behavior remains unchanged.

Native CPython 3.14 testing on the DOM Windows host is available through WSL
interop: scripts/validation/windows-files-smoke.py tests private creation,
owner/DACL checks, replacement, lock contention across processes, file and
parent reparse rejection, and safe source/update handles. The Windows-only ctypes
modules are excluded from Linux-only coverage reporting, like other native OS
backends; the native script is the substantive platform test. Full Windows CI
belongs to #391.

### Native Windows archive transfer (#383)

One-shot Windows archive pack and unpack now use verified NT handles. Packing
enumerates children through FileIdBothDirectoryInfo while holding directory
handles, checks each opened child for reparse points and types, and enforces the
configured entry/byte limits. The archive is created under a protected DACL.
Unpacking validates names/types, device names, ADS and case collisions before
creating protected staging; it denies concurrent archive writes during
validation/extraction. File creation and directory traversal are handle-relative.
Directory commit uses NT relative rename to stage/backup/publish, with safe
handle-relative rollback and recursive cleanup. Native Windows 3.14 smoke tests
cover success, resource caps, junction rejection, malformed archive refusal,
backup/recovery and parent-substitution attacks. Linux's existing archive
semantics remain unchanged.

### Durable directory import

The original receipt-driven unpack state machine now shares the native
inspect/extract/move/cleanup backend on Windows. A protected directory staging
source is verified by owner/DACL at publication. Its receipt records a SHA-256
of the archive read from a write-denying NT handle; resume checks the digest
before re-extracting. Failed publish, backed-up target restoration and lost
completion acknowledgement are covered in regression tests.

Windows transfer cancellation and abandonment now reuse existing durable
write/unpack receipts. Private temporary files, metadata and receipts are
deleted through verified NT handles. Directory abandonment performs
handle-relative backup rollback, staging removal and committed-directory
verification; cleanup of WorkGate-created staging requires its protected
owner/DACL. Plain user-file destinations are preserved when their transfer
receipts are retired. Native tests check verified private deletion and refusal
to recursively delete a non-private directory.

### Native ordinary-file access

Windows Executor read-file operations now use one verified NT no-follow handle;
a file snapshot hashes that same object, not a potentially substituted pathname.
WebUI binary upload exclusively creates the user file relative to verified
directory handles and removes a failed partial upload by its still-open handle.
The temporary scratch pruner uses native protected-file handle deletion. The
original Linux/macOS file APIs remain unchanged.

Native text-file write, edit_lines and hashline edit now use NT-handle
relative atomic publication. File snapshots for edits read and hash the
same verified open file; the publisher checks expected SHA-256 and compares
file identity before replacement. Existing Windows file owner SID and
protected/unprotected DACL are copied onto the replacement before
FileRenameInformationEx with POSIX replacement semantics, allowing the prior
verified target handle to remain open. New files use exclusive rename without
clobbering existing files. Native Windows 3.14 smoke verifies an existing
protected private file retains both owner and protected DACL after text
replacement, and refuses stale hashes. The macOS WebUI upload mock uses a
portable tracked-descriptor mapping rather than Linux-only /proc/self/fd.

WebUI regular file copy, move and delete and directory move/delete now use
native NT object handles rather than path-based Python shutil/os.rename.
Regular file copying uses a verified source, exclusive destination, and
owner/DACL-preserving copy with original-handle cleanup. Windows editor
content uses a digest from its verified read handle, rather than hashing
the same pathname in a separate open. Binary/image preview samples also use
no-follow native handles. The HTTP WebUI dispatch is covered in regression
tests and basic operations run on native Windows 3.14.

**Still blocked (Draft #395):** Windows directory/link copy, symlink or
junction delete/move, recursive ordinary filesystem enumeration, and patch
and Skill/MCP file operations still require native handle-safe semantics.
Independent untrusted writers can still race final target replacement between
an identity check and rename; further native concurrent-mutation and
crash/restart testing must establish the intended guarantee before this
is declared fully safe.
Native concurrent replacement and sharing races, process-level crash/restart
recovery, and cross-executor copy/download/upload integration also require
further testing. The tests in this slice exercise native Win32 filesystem
primitives; full Windows Executor/Control integration and CI/release support
remain separate work (#390–#392). Do not merge this Draft until the full
acceptance scope is verified.
