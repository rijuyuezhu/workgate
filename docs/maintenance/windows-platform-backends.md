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

## Native Windows functional checks

- scripts/validation/windows-stdlib-smoke.py: Windows paths, file locks,
  credentials, Skills and WebUI file CRUD.
- scripts/validation/windows-transfer-smoke.py: resume in a separate Windows
  process, checksums and directory archive transfers.
- scripts/validation/windows-patch-smoke.py: multi-file git apply, unified
  diffs, and failed preflight.

Linux/macOS tests cover the shared implementation.
