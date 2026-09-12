# Configuration reference

This page is rendered from [`generated/configuration.json`](generated/configuration.json), which is generated from the application settings registry.

Complete copy-editable examples are committed at the repository root as `.env.example` and `config.example.yaml`.

Settings resolve in this order:

```text
defaults < config file < WORKGATE_* environment variables < CLI arguments
```

The config file is selected as `--config PATH`, then `WORKGATE_CONFIG`, then the platform default config file when it exists. On Linux the default is `${XDG_CONFIG_HOME:-~/.config}/workgate/config.yaml`. A missing default config file is normal and is not created implicitly. Executor commands use the same settings loader, so machine policy such as `workspace_root`, path restrictions, and executable choices must be configured on the executor host rather than assumed from control.

YAML config files use flat setting names such as `auth_mode` and `workspace_root`. Nested groups are not read by the application settings loader. Filesystem paths stored in YAML are persistent configuration: after `~` and environment-variable expansion they must be absolute. Relative YAML paths such as `./project` or `../state` are rejected. Relative path overrides from environment variables and CLI arguments remain invocation-oriented and resolve against the directory Workgate was started from.

`workspace_root` is the user-content filesystem boundary, not an application-data directory. If it is omitted, Workgate uses the process invocation CWD. Changing a session or shell CWD inside that workspace does not change the boundary. Long-running services should set their launcher working directory or configure `workspace_root` explicitly rather than relying on an incidental daemon CWD.

## Application-owned paths

Workgate keeps its own files separate from the workspace. Linux defaults are:

| Lifetime | Default | Examples |
| --- | --- | --- |
| config | `${XDG_CONFIG_HOME:-~/.config}/workgate` | `config.yaml`, Agent Bridge manifest and managed Skills |
| state | `${XDG_STATE_HOME:-~/.local/state}/workgate` | control/executor session state, audit history, jobs, OAuth material, Agent Bridge credentials, executor profile |
| data | `${XDG_DATA_HOME:-~/.local/share}/workgate` | persistent application data kept separate from runtime state |
| cache | `${XDG_CACHE_HOME:-~/.cache}/workgate` | regenerable native UI materialization |
| runtime/temp | `$XDG_RUNTIME_DIR/workgate` when suitable, otherwise a private per-user temp root | disposable scratch files |

Only absolute XDG base-directory values are honored; invalid relative values use the documented fallback. macOS and Windows use native per-user application locations. These categories remain independently owned lifetime namespaces even on platforms whose native base directories overlap.

`audit_log_path` and the private Agent Bridge credential directory are derived from `state_dir` as `audit_log/audit.jsonl` and `agent_auth`; they are not standalone settings. Agent Bridge declarative configuration instead follows the platform config namespace.

### Migration from pre-5.0-alpha layouts

Workgate does not automatically move arbitrary state from `/workspace/.workgate` or other old roots. An existing deployment can temporarily keep its durable controller state by setting `WORKGATE_STATE_DIR` to the old absolute path, then migrate intentionally. Agent Bridge configuration that previously lived under `state_dir/agent_config` should be moved separately to the platform config namespace; preserve private permissions because legacy manifests may contain literal `env` or `headers` values.

<div class="generated-reference" data-reference-json="../generated/configuration.json">
Loading generated configuration reference...
</div>
