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

## Shell support (in progress, #384)

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
recovery and service-level lifecycle still require validation before #385
can close.

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
- scripts/validation/windows-colocate-job-smoke.py: launcher/role/grandchild
  containment on normal and forced colocate termination.

Linux/macOS tests cover the shared implementation.
