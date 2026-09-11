# Example prompts

These prompts emphasize explicit workspaces, inspection before editing, and verification after changes. Adapt paths and commands to your project.

## Inspect a repository

```text
Use workgate in /workspace/project. Inspect the repository tree, instruction files, and Git status. Summarize the main entry points and do not change files yet.
```

## Make a focused change

```text
Use workgate in /workspace/project. Create a new branch, inspect the relevant code and tests, make the smallest requested change, run focused validation, show the final diff, and report anything not verified.
```

## Pair an executor

```text
Help me pair a Workgate executor named gpu1 with my control service. Give me the `workgate executor connect CONTROL_URL --name gpu1` command, then explain where to approve the pairing request in the Executors UI.
```

## Inspect an executor

```text
Use workgate on executor gpu1 in /home/me/project. Start a shared session there, inspect the repository and environment, run git status, and report what is available before editing.
```

## Run a test on an executor

```text
Use workgate on executor gpu1. Start or reuse the appropriate shared session, find the relevant test for the requested change, run it as a bounded or background job as appropriate, and summarize the result.
```

## Copy an artifact

```text
Use workgate to copy results/report.json from the gpu1 session into reports/latest.json in my local project, then verify the destination.
```

## Inspect a generated image

```text
Use workgate in the project. Run the command that generates the plot, open the resulting image, describe any visual anomaly, and relate it to the generating code.
```

See [Common workflows](common-workflows.md) for the underlying workflow and [Tool reference](../reference/tools.md) for exact contracts.
