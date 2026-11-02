# Migrate an older Executor

Use this when an Executor is already paired but runs an older self-contained Workgate executable. The goal is to move to the managed source installer while keeping its identity and saved credentials.

Plan a maintenance window; commands can interrupt active work.

## 1. Record and back up the existing Executor

Before changing the service, save these details privately:

- Control URL, Executor ID and current service/task status.
- The exact current Executor config and state directory, including its paired profile.
- The current executable and native service startup command.
- A protected backup of the configuration, state and old executable for rollback.

Keep the original credentials and service files intact. Treat the profile as a secret.

## 2. Prepare the target source release

Follow [Prepare a release](https://github.com/rijuyuezhu/workgate/blob/main/deploy/source/README.md#upgrade-a-managed-source-installation) to stage the target release without changing the running service.

Before cutover, verify the prepared runtime sees the **same saved Executor profile**. If it asks for unexpected new pairing, check the configured state directory before continuing.

## 3. Move the service

Stop and uninstall the **original owned service** using its original Workgate CLI, keeping the private state and backup. Use the new prepared source runtime's **executor install-service** with the *same existing config*. Verify the native service, Executor ID and an actual file or shell operation through Control.

Once verified, activate the staged source CLI. Keep the original executable until a rollback is no longer needed.

If the new service cannot start or connect, remove only its owned service and restore the original version and service command from your backup.

This is a manual migration for an existing paired installation. For managed source-to-source upgrades, use the [source upgrade guide](https://github.com/rijuyuezhu/workgate/blob/main/deploy/source/README.md). If the old service cannot be identified or safely stopped, investigate its native service binding before proceeding.
