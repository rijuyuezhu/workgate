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

## Keep Tasks organized

In **Tasks and Sessions**, select a Task and open **Sessions → New Session in this Task** to continue work on another Executor without creating a separate Task. A Task can have several execution Sessions.

When the final execution Session is finished, use **End Session & Complete Task** to close both. Use **End Session** alone when the Task will continue later. Completing a Task requires its Plan steps to be completed or skipped, and the combined action checks again that no Sessions remain active.

Use the **Needs review** filter for Tasks that have retained Sessions but none still running. Review their Plan and choose **Complete** or **Resume**; these Tasks stay available until you explicitly finish them.

## Transfer files

Open **Files**, select an online Executor and a regular file, then select **Download**. The browser saves the original bytes without loading the whole file into the WebUI. A one-time download link expires after 60 seconds; request another download if the link has expired. For uploads, select **Upload** to send large files in chunks; use **Cancel upload** to stop before the destination is published. Existing files are not overwritten. Text editing remains a separate action.

## Change settings

Open an Executor's **Config** tab or **Control config**, select the settings to edit, and save them. Use **Apply** when the interface offers a restart action for the saved configuration.

For settings and defaults, consult the [Configuration reference](../reference/configuration.md).

## Use a desktop

With an active session on a graphical Executor, choose **Open GUI**, then select a window. Use **Start live** for a bounded JPEG Desktop stream; **Stop live** ends it. The browser pauses the stream while its tab is hidden. Wayland captures refresh on request or after explicit input to avoid repeatedly stealing focus; **Capture frame** also remains available for one-shot observation. To send manual input, explicitly choose **Enter manual mode**; each action needs a fresh, short-lived observation.

GUI tools require the host OS's desktop permissions. See [Executors](executors.md).

## Troubleshoot

- Missing Executor tools: check the selected session and Executor connection.
- Terminal disconnected: check the Executor status, then reattach.
- Unauthorized action: sign in with an account that has the required permissions.

See [Troubleshooting](../troubleshooting.md).
