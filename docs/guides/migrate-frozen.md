# Migrate a legacy frozen Executor to source + uv

This procedure is for an **already paired, personally owned Executor-only**
service installed from an older self-contained Workgate executable. It is
**not** a first-install recipe, nor a Control/Colocate or user-owned Git
checkout migration. Do not apply it to production without a maintenance
window, a tested rollback command and the owner's approval.

The normal [source installer](../getting-started/install-options.md) intentionally
refuses to overwrite a running or stopped native service. That refusal protects
the existing Executor ID, bearer profile and service ownership. Workgate does
**not** silently adopt frozen installations.

This procedure covers Linux systemd and native Windows Task Scheduler.
It does **not** establish a tested macOS LaunchAgent cutover. Legacy frozen
macOS Executors must retain their existing runtime until an explicit,
release-specific source migration is verified; do not apply Linux/Windows
service commands on macOS.

## Before changing the service

1. Record the **actual existing** CLI executable, Control URL, Executor
   configuration path, private `state_dir`, Executor ID, current service
   status, native service/task identity and service launch command. Do not
   infer the state directory from platform defaults if the existing installation
   overrides it.
2. Store a restricted backup of the Executor profile and its settings,
   including the service's generated `config.json` when it is the only copy.
   That generated config is deleted by `executor uninstall-service`. A JSON
   settings file is also valid YAML for `--config`. Treat backups as secrets
   and **never print or commit bearer credentials**.
3. Preserve the original executable and its associated runtime files for
   manual rollback. Check that the **original CLI** supports `executor stop`,
   `executor uninstall-service` and `executor install-service`; old releases
   with different commands need a release-specific migration plan.
4. Check that the Control still accepts the paired profile. If the Executor
   was revoked, or if the saved Control URL/identity differs, **stop**; do not
   approve a new pairing as part of migration. Finish any active jobs before
   shutting down the old service.

## Stage and validate the new source before touching the old service

On **Linux**, from a trusted checkout containing the published source
preparation scripts:

```sh
tag=v5.0.0a12
sha=ffdb695ba41bdaf3e76dd9c9b20dc228f2c7c775
root="$HOME/.local/opt/workgate"
sh deploy/source/prepare.sh --role executor --tag "$tag" \
  --expected-commit "$sha" --install-root "$root" --no-activate-launcher
new="$root/versions/$sha/.venv/bin/python"
"$new" -m workgate.main --version
```

On **native Windows PowerShell**, from the same trusted source checkout:

```powershell
$tag = 'v5.0.0a12'
$sha = 'ffdb695ba41bdaf3e76dd9c9b20dc228f2c7c775'
$root = Join-Path $env:LOCALAPPDATA 'Workgate/source'
& .\deploy\source\prepare.ps1 -Role executor -Tag $tag `
  -InstallRoot $root -NoActivateLauncher
$new = Join-Path $root "versions/$sha/.venv/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $new)) { throw 'Pinned source was not prepared' }
& $new -m workgate.main --version
```

The published tag resolves to the exact pinned checkout; verify the reported
version and interpreter path. The source preparer does **not** touch the native
service or activate the stable CLI. Any staging failure leaves the old service
intact. The native ARM64 preparer uses the verified dependency Wheel from the
matching published release; no manual Wheel SHA256 is required.

Using the *same* durable Executor configuration and Control URL as the old
service, run the new interpreter's **read-only-success path**:

```text
<new Python> -m workgate.main executor connect <Control URL> --config <saved YAML/JSON>
```

It must report **"Already paired as <same Executor ID>"**. An unexpected
pairing prompt or a different ID is a **stop condition**; do not approve
or write a replacement profile. Verify the saved config references the same
private state directory before proceeding.

## Explicit service cutover and rollback

Only after the above checks pass:

1. Stop the original Executor using its **original CLI** and explicit saved
   config, then call its `executor uninstall-service`. Verify its owned
   systemd unit or Windows Task Scheduler task is absent. The supported
   uninstall removes service configuration/metadata but **preserves the
   Executor profile**. Do not delete the profile, old executable or backup.
2. Start the source service using
   `<new Python> -m workgate.main executor install-service --config <saved config>`.
   Verify `executor status` reports `running` and `Runtime owner: source`,
   the original Executor ID is still registered, and Control can execute a
   real remote shell/file tool on that same Executor. Check its native
   service action refers to the pinned source Python, not a frozen executable.
3. Once verified, you may activate the staged versioned source CLI using
   the normal `prepare` command **without** `--no-activate-launcher`
   (Windows: without `-NoActivateLauncher`). Keep the old binary until
   rollback is no longer needed.

If source service installation or online/tool verification fails, stop
and uninstall any **new owned source** service first. Verify the original
task/unit is absent and that the paired profile and backup are intact. Then
reinstall/restart the original frozen service using its saved executable,
original configuration and supported `executor install-service` command.
If either task is foreign/unrecognized, **stop and investigate**; never force
task replacement or delete the profile to make a command succeed.

The above uses existing service lifecycle operations, not an automatic
transaction. It includes a short service interruption and cannot make
in-flight commands atomic. The durable profile is intentionally reused;
source and old binary artifacts are not deleted by this procedure.

**Evidence boundary:** native Linux and Windows tests establish that
Executor `uninstall-service` preserves pairing identity, and a disposable
Linux test confirms old `control-managed` runtime metadata can be removed
and replaced with `source` metadata without changing the profile. A full
live frozen-executable-to-source service cutover across every old release
and OS is **not yet certified**; it remains a release gate under
[issue #457](https://github.com/rijuyuezhu/workgate/issues/457).
