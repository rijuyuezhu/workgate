# Configuration reference

This page is rendered from [`generated/configuration.json`](generated/configuration.json), which is generated from the application settings registry.

Complete copy-editable examples are committed at the repository root as `.env.example` and `config.example.yaml`.

Settings resolve in this order:

```text
defaults < config file < WORKGATE_* environment variables < CLI arguments
```

The config file is selected as `--config PATH`, then `WORKGATE_CONFIG`, then the role-specific platform default when it exists. On Linux, control/general commands default to `${XDG_CONFIG_HOME:-~/.config}/workgate/config.yaml`, while `workgate executor connect/run` default to `${XDG_CONFIG_HOME:-~/.config}/workgate/executor/config.yaml`. A missing default config file is normal and is not created implicitly. Executor commands use the same settings loader but admit only executor/shared settings, so machine policy such as `workspace_root`, path restrictions, and executable choices must be configured on the executor host rather than assumed from control. `workgate standalone` may accept one convenient user-facing config, but it resolves that input into separate private control/executor child configs before either child starts. The generated registry below describes the shared settings schema; a role's `--help` is authoritative for its CLI overrides, and `workgate control` deliberately omits executor-only machine-policy flags.

YAML config files use flat setting names such as `auth_mode` and `workspace_root`. Nested groups are not read by the application settings loader. Filesystem paths stored in YAML are persistent configuration: after `~` and environment-variable expansion they must be absolute. Relative YAML paths such as `./project` or `../state` are rejected. Relative path overrides from environment variables and CLI arguments remain invocation-oriented and resolve against the directory Workgate was started from.

`workspace_root` is the executor's user-content filesystem boundary, not an application-data directory. If it is omitted, the executor uses its process invocation CWD. Changing a session or shell CWD inside that workspace does not change the boundary. Long-running executors should set an explicit `workspace_root` rather than relying on an incidental service-manager CWD.

## Application-owned paths

Workgate keeps its own files separate from the workspace. Linux defaults are:

| Lifetime | Default | Examples |
| --- | --- | --- |
| config | `${XDG_CONFIG_HOME:-~/.config}/workgate` | control `config.yaml` and `agent/`; executor `executor/config.yaml` and `executor/agent/` |
| state | `${XDG_STATE_HOME:-~/.local/state}/workgate` | control durable state; executor defaults to the private `executor-runtime/` subtree |
| data | `${XDG_DATA_HOME:-~/.local/share}/workgate` | control-owned persistent application data kept separate from runtime state |
| cache | `${XDG_CACHE_HOME:-~/.cache}/workgate` | regenerable native UI materialization |
| runtime/temp | `$XDG_RUNTIME_DIR/workgate` when suitable, otherwise a private per-user temp root | disposable scratch files |

Only absolute XDG base-directory values are honored; invalid relative values use the documented fallback. macOS and Windows use native per-user application locations. These categories remain independently owned lifetime namespaces even on platforms whose native base directories overlap.

`audit_log_path` and the private Agent Bridge credential directory are derived from each role's `state_dir` as `audit_log/audit.jsonl` and `agent_auth`; they are not standalone settings. By default, control uses the platform Workgate state root while executor commands use its `executor-runtime/` subtree. The executor profile therefore lives at `<executor state_dir>/executor/profile.json`. Agent Bridge declarative configuration follows the role-private config namespaces shown above.

### Migration from pre-5.0-alpha layouts

Workgate does not automatically move arbitrary state from `/workspace/.workgate` or other old roots. An existing deployment can temporarily keep its durable control state by setting `WORKGATE_STATE_DIR` to the old absolute path, then migrate intentionally. Executor commands now have a separate implicit default state root; Workgate deliberately does not copy an existing executor profile or bearer out of the old shared default. To keep an existing paired profile during migration, explicitly configure that executor's previous `state_dir`; otherwise pair the executor again under the new private default. Agent Bridge configuration that previously lived under `state_dir/agent_config` should be moved separately to the appropriate role-private platform config namespace; preserve private permissions because legacy manifests may contain literal `env` or `headers` values.

<div class="generated-reference" data-reference-json="../generated/configuration.json">
Loading generated configuration reference...
</div>
