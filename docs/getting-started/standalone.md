# Standalone local mode

Standalone is the simplest one-machine Workgate deployment. One supervisor
starts a **control** process and an **executor** process and connects them over
the normal loopback executor protocol.

It works offline after Workgate and the machine-local tools you need are installed.

## Configure and start

For normal use, create `~/.config/workgate/config.yaml`:

```yaml
default_workdir: /absolute/path/to/project
```

Then run:

```bash
workgate standalone
```

The default control origin is `http://127.0.0.1:8765`; MCP is served at
`http://127.0.0.1:8765/mcp`.

For a one-off configuration file:

```bash
workgate standalone --config ./standalone.yaml
```

See [Configuration](../reference/configuration.md) for the settings accepted by standalone.

## First launch

The supervisor establishes a normal long-lived executor identity. Later launches
reuse the saved executor profile rather than pairing again.

If `oauth_admin_pin` is not configured, standalone creates one and prints the
path to its private file rather than the secret itself.

## State and recovery

Standalone keeps control and executor state separate. Given a user-facing
`state_dir` of `STATE`, the role roots live under:

```text
STATE/standalone/
  control/
  executor/
  run.lock
```

A child-process crash is restarted without changing executor identity. A control
restart may interrupt an in-flight ordinary call; the executor reconnects with
its saved credential and reports its current resources.

If control trust and the executor profile no longer match, restore the matching
state or intentionally reset/re-pair. Do not manufacture credentials by hand.

## Using the local executor

Standalone normally has one eligible executor, so sessions usually do not need
an explicit `executor_id`. They are still executor-backed: if the executor is
offline, machine-facing tools are unavailable.

For public or multi-machine setups, see [Choose a deployment](deployment.md).
For the ownership and failure contracts, see
[Control/executor architecture](../architecture/control-executor.md).
