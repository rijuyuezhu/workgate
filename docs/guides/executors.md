# Executors

An **Executor** runs Workgate tools on a computer you choose. Each Executor connects to Control and uses that computer's files, commands and installed tools.

## Add a computer

1. [Install Workgate](../getting-started/install-options.md) on the new machine with the **Executor** role and your Control HTTPS URL.
2. Approve the pairing request in the Control WebUI under **Executors**.
3. Start a Workgate session on that Executor to work in its project directory.

If Workgate was installed from a development checkout, use the pairing procedure in [Colocate from source](../getting-started/colocate.md#run-from-a-development-checkout), substituting your Control HTTPS URL.

A new source installation sets up the native user service. For an existing manually paired checkout:

~~~sh
workgate executor install-service
workgate executor status
~~~

Inspect recent logs with **workgate executor logs**.

## Use another Executor

In the WebUI, choose an online Executor when starting a session. From an MCP client, name the computer and workspace:

> Start a Workgate session on my GPU machine in my project directory and show the Git status.

Each session uses the selected Executor's local filesystem and operating-system permissions.

## Manage a connection

Use the **Executors** page to check connectivity, review capabilities or revoke a device. On the machine itself, **workgate executor status** and **workgate executor logs** help diagnose startup or reconnection problems.

After a temporary connection outage, the Executor reconnects with its saved pairing. If the owner revoked the device, pair it again.

## Use local capabilities

- **Files and shell:** on the selected Executor.
- **Browser:** uses Chromium installed on that Executor.
- **Desktop:** requires a graphical session and OS accessibility/input permissions.
- **Skills and MCP integrations:** see [Skills and MCP integrations](agent-bridge.md).
- **File sharing and copying:** see [Common workflows](common-workflows.md).

On Niri, optional compositor-native keyboard and pointer settings can be enabled in the Executor configuration with the required host tools. See the [Configuration reference](../reference/configuration.md).

## Upgrade or migrate

For an existing managed source installation, use the [source upgrade instructions](https://github.com/rijuyuezhu/workgate/blob/main/deploy/source/README.md). To move an older executable installation, see [Migration](migrate-frozen.md). Keep the saved Executor identity and configuration when upgrading.

For errors, see [Troubleshooting](../troubleshooting.md).
