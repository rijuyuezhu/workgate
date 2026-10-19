# Windows filesystem support

WorkGate follows the upstream **trusted-workspace** model on Windows. The
filesystem implementation uses Python's pathlib, os, shutil, tarfile and
ordinary temporary files. It does **not** wrap NT kernel handles, maintain
custom Windows ACLs, or implement a second file/patch engine.

## Small platform differences

- Application config lives under %APPDATA%/workgate; local state and data live
  under %LOCALAPPDATA%/workgate. Host-independent Windows syntax is parsed
  with PureWindowsPath.
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

Windows defaults to PowerShell; native source installations use their Python
interpreter, while frozen builds require an external Python on PATH (or an
explicit python_bin). Commands retain their native syntax: PowerShell
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
tests. Native colocate attaches its parent process to a kill-on-close Job
before launching Control and Executor; role descendants and nested bounded
command Job Objects are covered when colocate exits, including forced stop. Full tracked-job
recovery and service-level lifecycle remain separate work in #387 and #388.

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
  and Colocate Windows Job Object descendant cleanup.
- scripts/validation/windows-colocate-job-smoke.py: launcher/role/grandchild
  containment on normal and forced colocate termination.
- scripts/validation/windows-conpty-owner-smoke.py: abrupt Executor-owner
  termination does not leave an interactive PowerShell behind.
- scripts/validation/windows-conpty-transport-smoke.py: native Windows
  Control + Executor, real WebUI API and two authenticated WebSocket terminal
  clients sharing one PowerShell, with independent detach and continued use.
- scripts/validation/windows-conpty-smoke.py: native terminal resize,
  independent subscribers, detachment and natural EOF.
- scripts/validation/windows-conpty-shell-smoke.py: WorkGate persistent
  shell CRUD, two concurrent WebUI bridges, session limit and restart.
- scripts/validation/windows-conpty-descendant-smoke.py: child and descendant
  cleanup when an interactive ConPTY closes.

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
action limits and snapshot cleanup. Source Python is supported; frozen Windows
window-capture entrypoint integration is deferred to #391.

`python scripts/validation/windows-gui-task-smoke.py` can run on **native**
Windows in a disposable logged-in Task Scheduler user task. It creates a
temporary Tk window, tests UIA enumeration and isolated capture, then attempts
focus, mouse, keyboard and scroll within that window. If an otherwise logged-in
session cannot take foreground input (for instance while disconnected), the
script reports a **skip** for physical input rather than claiming it passed.
It always unregisters the disposable task and removes the temporary files.

## PowerShell enrollment and global Agent Skills (#390)

Native Windows uses the public `/executor/v1/bootstrap.ps1` PowerShell endpoint
and the same checksum/release proxy as POSIX, with a
`workgate-windows-x86_64.zip` archive containing
`workgate-windows-x86_64/workgate.exe`. Only the expected ZIP entry is
extracted, with a bounded decompression budget. The Windows release build that
produces this artifact belongs to #391; the endpoint otherwise fails closed
instead of providing a mismatched POSIX runtime. Persistent fresh-machine
bootstrap installs a per-user Task Scheduler service; it does not require
administrator elevation, WSL, or Bash.

For global Agent Skills discovery, Windows defaults to
`%APPDATA%\agents\skills` rather than a POSIX `~/.config` path. An absolute
`XDG_CONFIG_HOME` override remains supported, consistent with the existing
source priority (project > managed > global).

### Control-managed frozen runtime updates (#390)

Windows Executors cannot replace their own mapped `.exe` in place. A
control-managed Windows update validates the Control-proxied checksum, extracts
only `workgate-windows-x86_64/workgate.exe`, and checks the requested version.
It publishes the verified executable to a unique versioned filename and updates
the **existing owned Task Scheduler action** to launch that file. The task's
original logon trigger is preserved; a one-time trigger schedules the next
start after the current Executor drains active commands and exits. Neither a
second supervisor nor a new service definition is required.

If updating the task or its stored metadata fails, the original executable is
retained and the update is marked failed. Source/Python-package installs remain
owned by their installer and are not self-mutated. The prior Windows image
remains on disk during a switch because the running process owns it; old
Workgate-generated runtime images are pruned best-effort on later updates.
A native Windows binary with a windowless service entry point is required for
end-to-end frozen update acceptance (#391).
