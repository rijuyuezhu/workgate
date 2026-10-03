# Configuration reference

The primary copy-editable example is `config.example.yaml` at the repository
root. `.env.example` exists for deployments that intentionally use environment
overrides; YAML is the normal durable setup.

## Loading settings

Precedence is:

```text
defaults < config file < WORKGATE_* environment variables < CLI arguments
```

The config file is selected in this order:

1. `--config PATH`;
2. `WORKGATE_CONFIG`;
3. the role-specific platform default, when that file exists.

On Linux the defaults are:

```text
~/.config/workgate/config.yaml           # control/general commands
~/.config/workgate/executor/config.yaml  # executor connect/run
```

Control accepts control/shared settings; executor accepts executor/shared
settings. `workgate standalone` may read one user-facing file, then resolves
separate child configuration before launching control and executor. A role's
`--help` is authoritative for its CLI overrides.

YAML uses flat setting names such as `auth_mode` and `default_workdir`.
Persistent filesystem paths in YAML must resolve to absolute paths. Relative
environment or CLI path overrides are invocation-oriented and resolve from the
directory Workgate was started in.

`default_workdir` is the executor's initial relative-path anchor, not a
filesystem boundary. Long-running executors should set it explicitly when they
want a stable default independent of the service manager's working directory.

## Application-owned paths

Workgate keeps application data separate from executor workspaces. Linux
defaults are:

| Lifetime | Default | Examples |
| --- | --- | --- |
| config | `${XDG_CONFIG_HOME:-~/.config}/workgate` | control `config.yaml` and `agent/`; executor `executor/config.yaml` and `executor/agent/` |
| state | `${XDG_STATE_HOME:-~/.local/state}/workgate` | control state; executor defaults to `executor-runtime/` |
| data | `${XDG_DATA_HOME:-~/.local/share}/workgate` | control-owned persistent payload/data |
| cache | `${XDG_CACHE_HOME:-~/.cache}/workgate` | regenerable native UI materialization |
| runtime/temp | `$XDG_RUNTIME_DIR/workgate` when suitable, otherwise a private per-user temp root | disposable scratch files |

Only absolute XDG base-directory values are honored. macOS and Windows use
native per-user application locations.

`audit_log_path` and the private Agent Bridge credential directory are derived
from each role's `state_dir`. The executor profile lives under
`<executor state_dir>/executor/profile.json`.

## Migration from pre-5.0-alpha layouts

Workgate does not automatically migrate old state roots. If an existing
deployment must keep an old control or executor state directory, configure that
absolute `state_dir` explicitly before upgrading. In particular, the executor's
new private default does not copy a previous paired profile; preserve the old
executor state root explicitly or pair again.

Move legacy Agent Bridge configuration separately into the appropriate
role-private config namespace and keep credential-bearing files private.

<div class="generated-reference" data-reference-json="../generated/configuration.json">
Loading generated configuration reference...
</div>
