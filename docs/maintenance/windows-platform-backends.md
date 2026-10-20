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
- scripts/validation/windows-frozen-bootstrap-smoke.py: on disposable
  `windows-2022` CI, serve the actual newly built release ZIP through the
  standard Control bootstrap HTTP routes, run the real downloaded PowerShell
  script with `-Persist`, approve device-code pairing, verify the windowless
  Task Scheduler Executor reaches Control, then reject a tampered checksum
  without stopping the existing service. The frozen CLI itself must uninstall
  the Task Scheduler service; the E2E then verifies process/log cleanup and
  removes its versioned runtime directory. This test runs only on disposable
  GitHub Windows runners and uses a local archive because the version is unreleased.
- scripts/validation/windows-pairing-approval-smoke.py: native
  `executor connect` device-code approval and denial against disposable
  loopback Controls in both `auth_mode: none` and **OAuth-protected** modes.
  Tests real signed `executor:use` owner bearer approval, 401 for missing,
  invalid or wrong-audience tokens, 403 for insufficient `shell:read` scope,
  and no persisted credential after an explicit denial. Localhost bypass is
  disabled even in the isolated OAuth test. The bearer is issued directly by
  the test using temporary shared signing state; this covers authorization
  enforcement, **not** the interactive browser OAuth consent/code exchange.
  Cross-physical-host interoperability remains a separate #392 requirement.
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
action limits and snapshot cleanup. Source Python invokes the isolated capture
module; frozen Windows re-enters the bundled executable through a private,
timeout-bounded capture entry point (verified in Windows binary smoke).

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
`workgate-windows-x86_64.zip` archive containing both
`workgate-windows-x86_64/workgate.exe` (interactive CLI) and
`workgate-windows-x86_64/workgatew.exe` (windowless Executor). The bootstrap
extracts exactly these two entries with bounded decompression, verifies both
versions, and installs into a separate versioned directory rather than replacing
an in-use executable. Native Windows build, PE subsystem and disposable Task
Scheduler startup checks are required by #391 CI; production release still
requires full end-to-end acceptance under #392. Persistent fresh-machine
bootstrap installs a per-user Task Scheduler service without administrator
elevation, WSL, or Bash.

For global Agent Skills discovery, Windows defaults to
`%APPDATA%\agents\skills` rather than a POSIX `~/.config` path. An absolute
`XDG_CONFIG_HOME` override remains supported, consistent with the existing
source priority (project > managed > global).

### Control-managed frozen runtime updates (#390)

Windows Executors cannot replace their own mapped `.exe` in place. A
control-managed Windows update validates the Control-proxied checksum, extracts
only `workgate-windows-x86_64/workgatew.exe`, and checks the requested version
using its private output-file probe (the windowless executable has no stdout).
It publishes the verified executable to a unique versioned filename and updates
the **existing owned Task Scheduler action** to launch that file. The task's
original logon trigger is preserved; a short, bounded one-time trigger retry
window schedules the next start after the current Executor drains active
commands and exits. The window tolerates delayed shutdown without keeping a
permanent periodic task. Explicit Executor `stop` and `restart` cancel the pending
upgrade trigger, so a manual stop cannot be undone by a delayed timer. Neither a
second supervisor nor a new service definition is required.

If updating the task or its stored metadata fails, the original executable is
retained and the update is marked failed. Source/Python-package installs remain
owned by their installer and are not self-mutated. The prior Windows image
remains on disk during a switch because the running process owns it; old
Workgate-generated runtime images are retained until an explicit cleanup so an
otherwise idle service or another owned task never loses its referenced binary.
When a frozen Windows Task Scheduler service stops, the Scheduler may leave
behind the PyInstaller one-file child holding the service log open. The shared
task stop path reaps only processes matching the verified owned task's exact
windowless executable path and managed-service command arguments.

Windows frozen Task Scheduler services use the bundled windowless entry point.
CI also builds an **acceptance-only synthetic older** windowless PE with version
`5.0.0a3.dev0` in `scripts/validation/build-windows-older-frozen.ps1` while
preserving the tracked source. Against the current locally built Control,
`windows-frozen-bootstrap-smoke.py` installs a disposable service, switches it
to this older PE, then checks its **automatic policy-driven upgrade** to the
current independently built version, real Task Scheduler scheduled restart,
re-authentication and failure recovery. The synthetic PE reuses this checkout's
source except for the version string; it is **not** the historical v5.0.0a3
release, and cannot substitute for testing upgrade across real historical
release artifacts. The CI test never publishes this synthetic build.
Complete real-release cross-version and two-host OAuth acceptance remain gated
by #392.

## Windows x86-64 native CI and release (#391)

The `windows-2022` x64 GitHub Actions runner uses Python 3.14 to run native
ConPTY, Win32 Job Object, file-lock, Scheduler and GUI contract tests. The
Windows PyInstaller build is a native PowerShell invocation of the existing
one-file CLI entrypoint. CI runs the same frozen executable CLI/HTTP/WebUI
smoke as Linux/macOS. No WSL/Bash installation is required for Windows.

Release archives use `workgate-windows-x86_64.zip`, containing
`workgate-windows-x86_64/workgate.exe` and
`workgate-windows-x86_64/workgatew.exe` alongside README, LICENSE and
QUICKSTART.txt. The matching SHA-256 checksum follows the existing release
proxy format and is verified by Control bootstrap and managed updates.
Existing four Linux/macOS release targets remain required.
The Windows release contains a console CLI (`workgate.exe`) and a windowless
Task Scheduler runtime (`workgatew.exe`) built from the same entry point. The
service installer checks for the windowless sibling before writing its service
config, and CI registers a disposable real Task Scheduler task to verify the
built binary's PE subsystem and process start. The two binaries must remain
adjacent for local persistent Executor installation.
Control-managed updates switch the windowless Executor image referenced by
Task Scheduler; they do **not** overwrite the interactive `workgate.exe` while
it is installed. The CLI can therefore report its original package version
while the managed Executor is running a newer version. Executor service status
uses the recorded Task Scheduler executable to avoid reporting a healthy
versioned Executor as stale. Full cross-version pairing and upgrade E2E are
tracked in #392.

The frozen Windows GUI screenshot worker re-enters the existing bundled
`workgate.exe` through a private `--_windows-capture` subcommand. Source builds
continue to use the Python module. Both paths use the same bounded per-window
PrintWindow implementation and child-process timeout; no machine-wide screen
capture fallback is added.
