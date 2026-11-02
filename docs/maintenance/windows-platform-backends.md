# Windows development

Workgate supports native Windows executors and uses shared Python code where practical. This page points developers to platform-specific parts of the code and their tests.

## Relevant components

- Files, Git and transfers use the shared filesystem and protocol implementations.
- Commands use native PowerShell and Python subprocess behavior.
- Persistent interactive terminals use ConPTY through pywinpty.
- Managed Control, Colocate and Executor services use the current user's Task Scheduler.
- Native desktop automation uses Windows UI Automation and the interactive desktop.
- Process containment uses Windows Job Objects where descendant cleanup is needed.

Follow [Executor source](https://github.com/rijuyuezhu/workgate/tree/main/src/workgate/executor) and [Windows platform modules](https://github.com/rijuyuezhu/workgate/tree/main/src/workgate/platform) for implementation details.

## Develop and test

Run Python 3.14 on native Windows. Use the normal [development checks](../development.md) for shared code, plus Windows-native tests for service startup, shell behavior, terminal ownership, GUI access and transfers.

Look under the project's **scripts/validation** directory for native smoke checks. Run the check corresponding to the behavior you changed and verify the real native service or subprocess when relevant. The CI workflow identifies the required Windows test jobs.

## Common problems

| Problem | Check |
| --- | --- |
| PowerShell command fails | Shell executable, quoting, current workdir and exit code |
| Interactive terminal unavailable | Logged-in user session and pywinpty/ConPTY support |
| Service does not start | Task Scheduler task status, action, account and Workgate service logs |
| GUI tools unavailable | Logged-in unlocked interactive desktop and application permissions |
| Windows ARM64 dependency build fails | Matching release wheel, or ARM64 MSVC, Rust and OpenSSL SDK in development builds |

For supported installation and upgrade commands, see [Install Workgate](../getting-started/install-options.md) and the [source upgrade guide](https://github.com/rijuyuezhu/workgate/blob/main/deploy/source/README.md). Avoid duplicating OS-level implementation details here; source and native tests define the behavior.
