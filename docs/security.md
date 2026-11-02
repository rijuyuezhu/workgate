# Security

A Workgate Executor runs files, shells and other tools with the permissions of its operating-system account. Choose what computers and directories to expose before granting a client access.

## Protect Control

- Keep local Control on loopback. For remote connections, serve it through HTTPS and use OAuth.
- Keep the admin approval PIN, signing state and backups private.
- Approve only trusted MCP clients and revoke access when needed.

## Protect Executors

- Pair only devices and Control servers you trust.
- Run Executors as a regular user with the permissions required for your workspace.
- A session's default working directory is a starting location, **not a sandbox**. An Executor can access any path its OS account can access. Use an OS account, VM or other isolation if you need stronger boundaries.

## Protect integrations and files

- Keep integration credentials in managed secret storage rather than committed project files.
- Review the permissions requested by browser and desktop operations. Desktop tools interact with the actual Executor desktop.
- File download links are bearer links: anyone holding one can access its snapshot until its expiry or revocation. Prefer short-lived links for sensitive files.
- Audit logs can contain project data, paths and outputs. Protect them like workspace data.

For settings, see [Configuration](reference/configuration.md). For specific problems, see [Troubleshooting](troubleshooting.md).
