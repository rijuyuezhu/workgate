# Windows filesystem support

WorkGate follows the upstream **trusted-workspace** model on Windows. The
filesystem implementation uses Python's pathlib, os, shutil, tarfile and
ordinary temporary files. It does **not** wrap NT kernel handles, maintain
custom Windows ACLs, or implement a second file/patch engine.

## Small platform differences

- Application config lives under %APPDATA%/workgate; local state and data live
  under %LOCALAPPDATA%/workgate. Host-independent Windows syntax is parsed
  with PureWindowsPath.
- Supervised Control/Executor `--config` paths must use their canonical
  absolute spelling. Windows 8.3 aliases and symlink/junction paths are
  rejected with an actionable error before Parent registration, rather than
  failing later during readiness or weakening config Apply rollback checks.
- Private cross-process locks use Python's msvcrt.locking on Windows and
  fcntl.flock on POSIX. Other file operations use shared Python stdlib code.
- A missing default workdir falls back to the current drive root.
- Cross-executor filenames and archive member checks understand Windows
  drives, UNC paths, reserved devices and case-insensitive names without
  depending on the Control host OS.
- Text editing, WebUI file operations, Skill/MCP management, directory
  transfers and transaction receipts reuse the common cross-platform code.
- apply_patch uses upstream's git apply --check followed by git apply.

## Trust model and limitations

These operations are intended for **trusted working directories**, not hostile
workspaces concurrently mutated by an attacker. Python path checks, Git patch
preflight and filesystem renames do not guarantee protection against malicious
symlink/junction substitution or make a multi-file patch atomic.

Optional stronger patch isolation is tracked separately in
[#402](https://github.com/rijuyuezhu/workgate/issues/402). It does not
justify native NT abstractions in #383.

## Shell support (#384)

Windows defaults to PowerShell; source installations use their managed Python
interpreter. Commands retain their native syntax: PowerShell
selects UTF-8 for its output, and cmd.exe selects code page 65001. External
programs may still emit different encodings. PowerShell preserves native
executable exit codes; Python subprocess handles trusted .exe, .cmd and .bat
without a custom launcher.

Bounded Windows commands now use a native kill-on-close Job Object (#385).
The existing bounded-runner waits for a one-byte startup gate until it is
attached to that job. Any binding failure refuses execution; normal return,
timeout or cancellation closes the job and kills surviving descendants.
The durable job runner also uses a gated Job Object for command attempts;
its native completion and forced-stop descendant cleanup are covered by smoke
tests. The shared Parent/Child Supervisor now owns individual Control and
Executor roles as well as Colocate. On Windows, the Parent and each role Child
join nested kill-on-close Job Objects; role restarts preserve containment.
Private IPC uses a socketpair and an explicit inherited Windows handle list,
not a second service or Colocate-specific supervisor. Native smoke tests cover
forced Parent exit, descendant cleanup, independent role restart and shared
configuration Apply. Colocate user services use Windows Task Scheduler or Linux
systemd; `install-service`, `start-service`, `stop-service`, `restart-service`,
`status-service` and `uninstall-service` are exposed through the same CLI.
Frozen Windows Colocate service installation is not yet supported; use the
native source `pythonw.exe` launcher instead.

## Interactive terminal (#386)

On native Windows, pywinpty supplies ConPTY for interactive PowerShell/pwsh.
Existing persistent-shell operations and the WebUI terminal bridge share one
Executor-owned ConPTY registry. Multiple raw WebUI attachments receive
independent bounded output streams; connecting a new client replays the
bounded recent output tail, while disconnecting one preserves the shell.
The terminal runtime closes its ConPTY children on Executor shutdown and
native abrupt-owner termination was verified as well.

Unlike detached POSIX tmux sessions, these PTYs are **not restart-durable**:
after Executor restart, their persisted reservations are reconciled against
an empty live registry. Interactive cmd.exe is intentionally not supported;
non-interactive cmd.exe commands still use the #384 shell adapter. No separate
WebSocket protocol or session manager is introduced.

## Native Windows functional checks

- scripts/validation/windows-stdlib-smoke.py: Windows paths, file locks,
  credentials, Skills and WebUI file CRUD.
- scripts/validation/windows-transfer-smoke.py: resume in a separate Windows
  process, checksums and directory archive transfers.
- scripts/validation/windows-patch-smoke.py: multi-file git apply, unified
  diffs, and failed preflight.
- scripts/validation/windows-shell-smoke.py: PowerShell/cmd Unicode, quoted
  paths, batch execution, exit status and environment filters.
- scripts/validation/windows-job-smoke.py: Job Object descendant teardown.
- scripts/validation/windows-contained-shell-smoke.py: bounded command timeout,
  cancellation, startup gate and refused assignment.
- scripts/validation/windows-durable-job-smoke.py: tracked runner log/status,
  background descendant cleanup and forced stop.
- scripts/validation/windows-tracked-jobs-smoke.py: job lifecycle,
  owner isolation, retries and new-process state reconciliation on native Windows.
- scripts/validation/windows-user-service-smoke.py: native Task Scheduler
  source `pythonw.exe`, least-privilege owner, Unicode/quote paths, stop/uninstall,
  and Colocate Windows Job Object descendant cleanup. CI runs this scenario
  against real per-user Task Scheduler tasks.
- scripts/validation/windows-colocate-job-smoke.py: launcher/role/grandchild
  containment on normal and forced colocate termination.
- scripts/validation/windows-conpty-owner-smoke.py: abrupt Executor-owner
  termination does not leave an interactive PowerShell behind.
- scripts/validation/windows-conpty-transport-smoke.py: native Windows
  Control + Executor, real WebUI API and two authenticated WebSocket terminal
  clients sharing one PowerShell, with independent detach and continued use.
  This full native-process scenario is enforced in the Windows Python 3.14 CI lane.
- scripts/validation/windows-pairing-approval-smoke.py: native
  `executor connect` device-code approval and denial against disposable
  loopback Controls in both `auth_mode: none` and **OAuth-protected** modes.
  Tests real signed `executor:use` owner bearer approval, 401 for missing,
  invalid or wrong-audience tokens, 403 for insufficient `shell:read` scope,
  and no persisted credential after an explicit denial. Localhost bypass is
  disabled even in the isolated OAuth test. The bearer is issued directly by
  the test using temporary shared signing state; this covers authorization
  enforcement, **not** the interactive browser OAuth consent/code exchange.
  Real human-approved two-host OAuth pairing was verified separately in #392.
- scripts/validation/windows-linux-pairing-smoke.py: **manual WSL / native
  Windows cross-OS** acceptance on DOM, with owner-approved Windows device-code
  pairing against a Linux Control, fresh authenticated online activity and
  restart using the same issued Executor identity. Both sides run the same
  checkout, but keep private state on their respective native filesystems.
  The Linux Control listens on loopback only, and no installed service is
  touched. Run with `uv run python scripts/validation/windows-linux-pairing-smoke.py
  --windows-python /mnt/c/path/to/.venv/Scripts/python.exe
  --windows-temp /mnt/c/Users/USER/AppData/Local/Temp` (one shell command).
  This proves protocol interoperability across WSL and Windows, **not**
  routing, firewall or network behavior between two physical hosts; no
  portable CI runner has both OS stacks available for this smoke.
- scripts/validation/windows-paired-service-smoke.py: a separately running
  Control authenticates a pre-provisioned native Windows Executor started by
  a **disposable limited-user Task Scheduler task**. Stop and restart must
  re-establish authenticated activity without changing the paired identity.
  For elevated test runners, only the current account is granted access to the
  test directory; the production service permission model is unchanged.
- scripts/validation/windows-conpty-shell-smoke.py: WorkGate persistent
  shell CRUD, two concurrent WebUI bridges, session limit and restart.

Native Windows pytest (`tests/contracts/test_windows_native_runner.py`) now
verifies actual ConPTY natural EOF and child cleanup on shell close, alongside
the mandatory multi-client WebSocket acceptance above. These replace the two
earlier standalone ConPTY smoke scripts.

Linux/macOS tests cover the shared implementation.

## Desktop GUI automation (#389)

Native Windows Executors reuse the existing session-owned GUI service with a
Windows UI Automation backend (`uiautomation`, selected only on Windows). GUI
capability is offered only when the Windows process belongs to a logged-in
user session with an accessible Default input desktop and a foreground window.
Session 0, WSL, locked/disconnected sessions and inaccessible secure desktops
do not offer desktop automation. The Executor itself remains available when GUI
is unavailable; native GUI calls then report an explicit unavailable error.

The backend reads UIA window/element identities, bounds and semantic patterns on
one COM-initialized worker. HWND reuse, changed UIA identities, stale elements
and moved windows invalidate observations. Physical input additionally requires
the selected HWND to become foreground; failing focus never silently sends
keystrokes to another application. Coordinates are in per-monitor physical
pixels, including negative origins for other monitors.

Screenshots use a short-lived, timeout-bounded Win32 `PrintWindow` child to
capture **only the requested window**, rather than cropping a full-desktop image.
The shared GUI service enforces session ownership, screenshot/element bounds,
action limits and snapshot cleanup. The managed source Python invokes the isolated capture module through a
timeout-bounded process.

Mandatory `windows-2022` native CI runs `windows-gui-task-smoke.py --ci`
and inspects the **actual** desktop state: if interactive, it requires a real
disposable Tk window's UIA enumeration, bounded capture and physical input to
**pass**; if headless, it requires that the Executor does not advertise GUI
capability and real UIA enumeration fails closed. Neither case treats a
`skipped` result as CI success. To test just the no-desktop safety guard on a
known noninteractive host, run `--assert-headless` (no task/input created).

`python scripts/validation/windows-gui-task-smoke.py` can run on **native**
Windows in a disposable logged-in Task Scheduler user task. It creates a
temporary Tk window, tests UIA enumeration and isolated capture, then attempts
focus, mouse, keyboard and scroll within that window. If an otherwise logged-in
session cannot take foreground input (for instance while disconnected), the
script reports a **skip** for physical input rather than claiming it passed.
It always unregisters the disposable task and removes the temporary files.

## Native Windows source enrollment and global Agent Skills (#390, #457)

The published GitHub PowerShell `deploy/source/bootstrap.ps1` installs one
pinned, uv-managed Control, Executor-only or Colocate role, retaining actual
user-level Task Scheduler and OAuth owner-approved pairing. Unlike the
retired Control-hosted binary bootstrap, it requires no bundled Workgate EXE
or Control-proxied downloadable archive. Historic frozen installation owners
must preserve their original profile/identity until an explicit source
migration and rollback have been validated; see #457.

For global Agent Skills discovery, Windows defaults to
`%APPDATA%\agents\skills` rather than a POSIX `~/.config` path. An absolute
`XDG_CONFIG_HOME` override remains supported, consistent with the existing
source priority (project > managed > global).

### Versioned source Executor updates (#458)

Managed versioned source Executors prepare the published pinned Git release
without replacing the running native service. An owner-checked source switch
preserves the Executor profile and attempts rollback on failure; user-owned
source checkouts are not silently mutated. Retained persisted frozen ownership
values are historical compatibility data, not a supported new binary install.

## Windows x86-64 native CI and release (#391, #457)

Native `windows-2022` Python 3.14 CI runs Task Scheduler, ConPTY, GUI,
paired-service, security and real source-role smoke tests. The source + uv
release publishes a Python wheel/sdist and a verified native Windows ARM64
cryptography dependency Wheel, not a platform-specific Workgate EXE archive.
The installed Windows native service runs `pythonw.exe` from its pinned
versioned source environment.
