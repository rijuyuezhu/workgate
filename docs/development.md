# Development

Workgate is a Python 3.14 application with an MCP Control plane and machine-side Executors. This guide is for contributors and coding agents.

## Where to start

- **Control** in src/workgate/control: clients, authorization, sessions, tasks and coordination.
- **Executor** in src/workgate/executor: filesystem, shell, jobs, browser and desktop operations.
- **Shared contracts** in src/workgate/protocol and schemas: messages crossing the Control/Executor boundary.
- **Tool registry** in src/workgate/tools/registry: public tools and their definitions.
- **WebUI** in src/workgate/ui/static and ui-terminal: JavaScript page controllers, React/TypeScript views, SVAR Files and xterm terminal assets.
- **Installation and validation** in deploy and scripts: deployment commands and platform checks.

Follow current call sites and tests for precise behavior. This overview is a map, not a second specification.

## Development principles

1. **Make the simplest change that solves the problem (KISS).** Reuse an existing mechanism rather than adding a new abstraction, state store, protocol, background worker or compatibility layer. Keep fixes focused and features cohesive.
2. **Honor ownership boundaries.** Control owns authorization and coordination; Executors own machine operations. Shared contracts should not depend on Executor internals. Extend the declarative tool registry for ordinary tools.
3. **Treat source code and tests as authoritative.** Check actual behavior before designing a fix. Avoid duplicating implementation rules, schemas, CLI flags or parameter defaults in prose.
4. **Use the repository's existing languages and tooling.** Core and tests: Python 3.14, using modern syntax directly (no postponed-annotation compatibility import). WebUI controllers: JavaScript/ES modules; React views: TypeScript/TSX. Deployment scripts: Shell and PowerShell where appropriate. Keep cross-platform behavior in shared Python code unless a native backend is needed.
5. **Verify changes in layers.** Run targeted tests, then related suites and static checks. For platform-specific code, use native platform tests. Inspect the full diff again for correctness, scope, duplicated logic and unnecessary mechanisms (Cold + KISS review). Fix substantive findings before merge.
6. **Protect deployed services and user data.** Test changes in isolated state and services. Preserve saved configuration, credentials and pairing when upgrading. Do not deploy, restart running services, release, or merge without the applicable approval.

## Work on the project

Install Git and uv:

~~~sh
git clone https://github.com/rijuyuezhu/workgate.git
cd workgate
uv sync --locked --group dev --group docs
uv run workgate --help
~~~

For an example local Control and Executor setup, see [Run from a development checkout](getting-started/colocate.md#run-from-a-development-checkout).

After making a change, run the tests and checks relevant to it:

~~~sh
uv run pytest -q tests/
uv run ruff check src tests deploy
uv run ruff format --check src tests deploy
uv run pyright
uv run pre-commit run --all-files
~~~

For Windows-native, browser and other OS-specific behavior, use the relevant tests and scripts under the repository's tests and scripts/validation directories.

For WebUI changes, JavaScript controllers under `src/workgate/ui/static` coordinate authenticated API calls and page navigation; React/TypeScript views under `ui-terminal/src` handle presentation and component-local state. The generated static assets include the SVAR Files view and xterm. Build them from source rather than editing bundles directly:

~~~sh
cd ui-terminal
npm ci
npm run build
npm run check
~~~

Commit any changed generated assets under `src/workgate/ui/static`, then run the relevant WebUI and browser tests.

## Documentation principles

**Docs help readers accomplish tasks; they are not changelogs, internal design transcripts or mirrors of code.**

- **Start with the reader.** Quickstart and installation should offer the shortest supported, copyable path and explain the next action. Keep one canonical installation workflow.
- **Organize by intent.** User guides say how to use features; operations pages say what to check and do when installing, updating or troubleshooting; development pages explain project boundaries and how to contribute.
- **Use progressive disclosure.** Put the common action first. Link to troubleshooting, security, source or generated reference only when the reader needs it.
- **Write plainly.** Avoid repetitive negative, defensive or historical claims, PR/Issue references and test-acceptance narratives in user-facing pages. Keep brief warnings when omitting them could cause data loss, exposure or a wrong operation.
- **Keep code authoritative.** Describe capabilities and workflows at a useful level. Rely on the live CLI help, generated [Configuration](reference/configuration.md) and [Tools](reference/tools.md) references for exact options and contracts. Keep investigation history in Issues and PRs.
- **Review as a newcomer.** Follow Home → Quickstart → Connect → First task; confirm the copyable examples, cross-page links and where a confused reader goes next.

Preview locally:

~~~sh
uv run --group docs mkdocs serve
~~~

Check the site and generated references before committing:

~~~sh
uv run --group docs mkdocs build --strict
uv run python scripts/validation/check-doc-reference-assets.py --site-dir site
~~~
