# Configuration reference

Workgate uses role-specific YAML configuration. The repository's [example config](https://github.com/rijuyuezhu/workgate/blob/main/config.example.yaml) is a starting point; the generated reference below shows the current settings and defaults.

## Find and edit settings

For an installed Workgate service, open **Control config** or the selected Executor's **Config** tab in the [WebUI](../guides/webui.md). The UI shows the saved values and defaults. Save changes and choose **Apply** when a restart is needed.

You can also edit the role's YAML file. On Linux the usual locations are:

~~~text
~/.config/workgate/config.yaml
~/.config/workgate/executor/config.yaml
~~~

For installed source services, use the actual selected config path shown by the service/CLI. Keep config, state and credential files private.

For most settings, precedence is:

~~~text
defaults < YAML < WORKGATE_* environment < CLI arguments
~~~

The CLI **--help** output lists supported overrides.

## Choose a working directory

Set **default_workdir** in the Executor configuration to a trusted project directory. It determines where relative workspace operations start; the Executor retains the permissions of its OS account.

## Before migrating a config

Record the actual current config and state paths and back up the role's private state. Reuse those paths when upgrading an existing paired Executor.

## All settings

<div class="generated-reference" data-reference-json="../generated/configuration.json">
Loading generated configuration reference...
</div>
