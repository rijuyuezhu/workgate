# Hosted Chat source provenance

Source: `fwerkor/local-shell-mcp` v6.0.3, commit `f0ecf8f8039345f24d0de4402983766787f4574e`. The upstream MIT license is retained in `LICENSE.upstream`.

| Workgate file | Upstream source at the pin | Git blob |
| --- | --- | --- |
| `database.py` | `src/local_shell_mcp/sqlite_store.py` | `584b13ce57787ca617924c4e7f1f166d0c3becde` |
| `tests/hosted_chat/test_database.py` | `tests/test_sqlite_store.py` (Chat-relevant cases) | `29352f9fd435e64aa8328418398340c1c76cdfe8` |
| `storage.py` | `src/local_shell_mcp/webui/storage.py` | `46eebf1cad3f7a70c9dc872a1d9c8d6c680674b6` |
| `attachment_history.py` (canonical file DTO) | `src/local_shell_mcp/webui/attachments.py` (read projection) | `da80834bb588dc3950225f0674d8e2cba56a0751` |
| `attachment_history.py` (history links and events) | `src/local_shell_mcp/webui/tool_attachments.py` (read projection) | `20838698ea30fb3f6b4439ae37169841412eabac` |
| `tests/hosted_chat/test_storage.py` | `tests/test_webui_storage.py` | `08a3c5eff8a5481423ca49ef1d811e637da878eb` |

The database imports the final Hosted Chat table definitions, FTS projection, indexes and wakeup triggers. Workgate uses a dedicated private Chat database, not upstream's native state tables or migration code. New installations start with the final schema rather than replaying historical migrations. Native Workgate persistence, authorization, audit and service lifecycle are separate. No Chat runtime is started by these modules.

The initial differential schema check compared the exact pinned upstream DDL against the initialized Workgate database: 41 Chat table definitions unchanged; excluded upstream `state`, `state_append_chunks`, `state_append_lengths`, `legacy_imports`, and the generic `workspaces` table (native Workgate workspace state remains authoritative). SQLite application ID and schema version are Workgate-owned.

The repository retains final React DTOs, pagination, bookmark filters and preference merging. Adaptations enforce owner-scoped project and agent references within write transactions, consistent history snapshots, literal wildcard searches and bounded cursor decoding. The canonical attachment history projection joins persisted messages to their owning runs, ignores deleted files and retains trusted memory events. Attachment mutation routes and the generation runner are not imported in this unit; their source will be integrated with the corresponding lifecycle and permission contracts.
