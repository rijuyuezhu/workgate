# Common workflows

Choose a computer and a project directory. Workgate runs workspace operations in a **Session** on that Executor.

## Inspect a project

> Start a Workgate session in my project directory, read the project's instructions, and show the Git status.

## Edit and test

> Find the implementation of this feature, make the smallest useful change, and run its relevant tests.

Review the diff and test results before committing.

## Run a longer job

> Start the build as a Workgate Job and show its status and recent output.

Use persistent shells for interactive programs or long-running terminals.

## Automate a browser

> Open the project dashboard in a Workgate browser session, inspect its visible controls, and report what you find.

Browser sessions run on the selected Executor.

## Copy between workspaces

> Copy the generated report from the session on my build machine into the reports directory in my workstation session.

Choose the source and destination Sessions explicitly.

## Share a file

> Create a short-lived download link for the report from this Workgate session.

Workgate creates a snapshot for the link. Treat the link as a secret: anyone with it can download the file until its access expires or is revoked.

## Keep task progress

> Create a task for this work, record the current plan and update the completed steps as you go.

Tasks can outlive their individual execution sessions. Use the WebUI to inspect progress and Audit history.

For exact tool arguments, see the generated [Tools reference](../reference/tools.md).
