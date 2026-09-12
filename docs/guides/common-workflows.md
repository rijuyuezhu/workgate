# Common workflows

The safest workflow is: choose a workspace, inspect it, make a small change, and verify the result. Exact argument schemas live in the generated [Tool reference](../reference/tools.md).

## Start with an explicit session

Ask the client to start a session in the project directory before doing any work:

```text
Use workgate in /path/to/project. Start a session, inspect the returned workspace and Git state, and read any instruction files before changing anything.
```

Keep using the returned `session_id`. Use `session_change_cwd` when the task moves to another directory instead of starting unrelated operations from an assumed path.

## Inspect before editing

Use directory and search tools to locate the smallest relevant area. Then read
the exact files you plan to change.

```text
Find where the HTTP timeout is configured, show me the relevant files and tests, and propose a minimal change. Do not edit yet.
```

Useful tools include `tree_view`, `list_files`, `glob_search`, `search`, `read`,
and `view_image`. Call `read` once per targeted file; selectors can retrieve
multiple ordered ranges from the same file. Prefer targeted reads over dumping
an entire repository.

## Edit with current file context

Choose the editing tool that matches the change:

- `hashline_edit` for precise text changes based on recently read hashline rows;
- `edit_lines` for an exact, structured line-range replacement;
- `apply_patch` when you already have a portable unified diff;
- `write_file` for a new file or a deliberate full replacement.

After editing, reread the changed area or inspect `git diff` before running validation.

```text
Apply the smallest fix, show the diff, then run the focused tests. Do not make unrelated formatting changes.
```

## Run commands and tests

Use `bash` for bounded, non-interactive commands such as formatting, tests, builds, or Git inspection.

```text
Run the narrowest relevant test first. If it passes, run the project's normal validation command and summarize any failures.
```

For a long-running non-interactive command, set `async_=true` and manage the returned job with `job`. Use `pty=true` when the executor-bound session genuinely needs an interactive terminal, server process, or REPL; persistent shells remain owned by that same shared `session_id`.

## Copy between sessions

Use `session_copy` to move files or directories between two existing executor-backed sessions. Start both sessions first and name the source and destination clearly; they may be bound to the same executor or different executors.

```text
Copy artifacts/report.json from the build session on gpu1 into reports/latest.json in my workstation session, then verify the destination file.
```

The service chooses the supported transfer method. Users normally do not need to select a transport.

## Work on another executor

Pair or select the intended executor, then start a session with its stable executor id and workdir:

```text
Start a session in /home/me/project on executor gpu1, inspect the repository, and run git status without editing files.
```

The normal file, search, shell, job, Todo, and Audit tools all route through the executor bound to that shared session. See [Executors](remote-workers.md) for pairing, reconnect, and trust management.

## Review activity

Use the Human UI Audit panel or `audit_tail` to review recent tool activity. Request a full retained payload only for a specific entry and only when the client has the required scope.

Audit data can contain project content and command output even after redaction. Treat it as private. See [Audit log](audit-log.md).

## Finish cleanly

Before reporting completion:

1. inspect the final diff or generated output;
2. run the relevant formatter, type checker, tests, or build;
3. report what changed and what was not verified;
4. leave unrelated files untouched.

For contributor-specific validation and architecture rules, see [Development](../development.md).
