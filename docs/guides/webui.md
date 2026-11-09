# WebUI

Open **/ui** on your Control server. For a local installation:

[http://127.0.0.1:8765/ui](http://127.0.0.1:8765/ui)

Sign in to see the controls available to your account.

## What you can do

| Area | Use it for |
| --- | --- |
| **Dashboard** | Check Control and connected Executors |
| **Executors** | Pair computers, inspect connectivity and edit settings |
| **Tasks and Sessions** | Review work, task progress, plans and activity |
| **Files** | Browse and manage files on a selected Executor |
| **Terminals** | Work with persistent shells |
| **Audit** | Review tool and administration activity |

The available panels depend on permissions and the selected Executor's capabilities.

## Download a file

Open **Files**, select an online Executor and a regular file, then select **Download**. The browser saves the original bytes without loading the whole file into the WebUI. A one-time download link expires after 60 seconds; request another download if the link has expired. Upload and text editing remain separate actions.

## Change settings

Open an Executor's **Config** tab or **Control config**, select the settings to edit, and save them. Use **Apply** when the interface offers a restart action for the saved configuration.

For settings and defaults, consult the [Configuration reference](../reference/configuration.md).

## Use a desktop

With an active session on a graphical Executor, choose **Open GUI** from that session. Choose a window, capture a frame and enable manual input if needed.

GUI tools require the host OS's desktop permissions. See [Executors](executors.md).

## Troubleshoot

- Missing Executor tools: check the selected session and Executor connection.
- Terminal disconnected: check the Executor status, then reattach.
- Unauthorized action: sign in with an account that has the required permissions.

See [Troubleshooting](../troubleshooting.md).
