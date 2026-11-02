# Source installation and upgrades

For a fresh installation, start with [Install Workgate](../../docs/getting-started/install-options.md). This directory contains the source installer and tools for managing an existing pinned installation.

Workgate runs three roles:

- **Control** provides the MCP endpoint and WebUI.
- **Executor** runs tools on a workspace machine.
- **Colocate** runs both roles together.

## Install from a checkout

If you already have a repository checkout and want to run its installer, use the appropriate entrypoint. Choose an official published tag:

~~~sh
sh deploy/source/install.sh --role colocate --tag vRELEASE --name workstation
~~~

On native Windows:

~~~powershell
& .\deploy\source\install.ps1 -Role colocate -Tag vRELEASE -Name workstation
~~~

For an Executor-only installation, select the Executor role, provide the Control URL and approve pairing in the WebUI. Control-only installations use the Control role. See the installer script's help for optional settings.

## Upgrade a managed source installation

**Before changing an existing service**, record its current version and config paths, back up its private configuration and state, and make sure any active work can be interrupted.

1. **Prepare** the published target release alongside the current one.
2. **Switch** the existing managed service to the prepared version.
3. **Check** that the service is running and a real operation reaches Control.
4. **Keep** the previous prepared release so you can switch back if needed.

From a checkout containing the deployment scripts, on Linux:

~~~sh
sh deploy/source/prepare.sh --role executor --tag vRELEASE --no-activate-launcher
sh deploy/source/switch.sh --role executor --tag vRELEASE \
  --install-root "$HOME/.local/opt/workgate" \
  --executor-config "$HOME/.config/workgate/executor/config.yaml"
~~~

For Linux Control, use the Control role and its existing Control YAML path; for Colocate, supply both existing role config paths. Run the script with --help for exact options.

On native Windows:

~~~powershell
& .\deploy\source\prepare.ps1 -Role executor -Tag vRELEASE -NoActivateLauncher
& .\deploy\source\switch.ps1 -Role executor -Tag vRELEASE -ExecutorConfig "C:\path\to\existing\executor.yaml"
~~~

Use -Role control or -Role colocate and the corresponding existing configuration paths when updating those services.

The switch uses the existing paired identity and service definition. Check online status and run a small workspace command after switching; a running operating-system service by itself only confirms the process is active.

## Roll back

Prepare or retain the previous published release, then run the same switch command with the previous tag. Verify the previous version and Control connectivity after the switch. If native service restoration fails, inspect the role's logs and service manager before trying another cutover.

Do not delete the saved Executor profile or replace a paired service with a new first-time installation.

## Older installations

- **Older self-contained executables:** follow the [migration guide](../../docs/guides/migrate-frozen.md) for an identity-preserving move to source.
- **Older unversioned Windows Executor source tasks:** a verified existing paired task can use the Windows switch script's -AdoptLegacyExecutor option, with the *current* saved Executor config file. Preserve the old runtime for rollback.
- **Existing managed Linux Colocate without an install receipt:** the Linux installer offers --adopt-existing-colocate with the current release identity and both existing YAML files.

For migrations, preserve the original service command, configuration, Executor ID, and a rollback path. Use the script's help and source for the exact eligibility checks.

## Windows ARM64

Ordinary published installs use the verified native dependency wheel from the selected Workgate release. Run the normal installer command in [Install Workgate](../../docs/getting-started/install-options.md).

When preparing a development build without a matching wheel, a native toolchain is needed: ARM64 MSVC, Rust, and a maintained ARM64 OpenSSL SDK. The source preparer validates these prerequisites and reports missing components. See [Windows development](../../docs/maintenance/windows-platform-backends.md).

## If something fails

| Symptom | First check |
| --- | --- |
| Preparation fails | Published tag, Git/uv availability, missing OS tools or dependencies |
| Existing service prevents installation | Use the upgrade/switch workflow with its existing configuration |
| Service runs but Executor is offline | Executor status and logs; confirm Control URL and saved pairing |
| Windows cutover cannot verify the current task | Task Scheduler owner/action and the current service config; keep the old task intact |
| New version cannot start | Service logs, config paths, and the retained previous release for rollback |

See [Troubleshooting](../../docs/troubleshooting.md), [Configuration](../../docs/reference/configuration.md) and each CLI command's --help for details.
