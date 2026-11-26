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

Provider profile storage and sampling are adapted from the final upstream sources:

| Workgate file | Upstream source at the pin | Git blob |
| --- | --- | --- |
| `provider_profiles.py` | `src/local_shell_mcp/webui/provider_profiles.py` | `f4cc2abde8895eb47e8abbca99b1a59f7e582cc3` |
| `sampling.py` | `src/local_shell_mcp/webui/sampling.py` | `74350d3276bf72f704c3aa9b6ba8b5738387a335` |
| `tests/hosted_chat/test_provider_profiles.py` | `tests/test_webui_provider_profiles.py` (repository cases) | `3feef623b70da2f6a2de1fd4e1e366763684e5d5` |
| `tests/hosted_chat/test_sampling.py` | `tests/test_webui_sampling.py` (validation/merge cases) | `5bcc7e81ae65aaf0449f9976c8cf483123b9e9bc` |

The repository retains the final channel names, protocol aliases, redacted catalog DTO, reference guards, and model sampling validation. Explicit Control-owned database and cipher instances replace global HTTP-route dependencies. API keys are encrypted and bound to channel, provider and endpoint. A deleted channel name cannot be reused while retained history references it. A private HTTP endpoint requires explicit opt-in; resolved-address validation is required in the transport unit before any requests. Unsaved built-in defaults carry no ambient credentials. Automatic discovery and account OAuth are not exposed by this private unit; their final source integrations and route/transport acceptance remain pending. Generation sampling snapshot tests follow the generation store import.


Model discovery and refresh are adapted from the final upstream catalog service:

| Workgate file | Upstream source at the pin | Git blob |
| --- | --- | --- |
| `model_discovery.py` | `src/local_shell_mcp/webui/model_discovery.py` | `145ea6abf8113cee559ac71ca5a9ffe9c80b3701` |
| `tests/hosted_chat/test_model_discovery.py` | `tests/test_webui_provider_profiles.py` (discovery cases) | `3feef623b70da2f6a2de1fd4e1e366763684e5d5` |

The catalog preserves source protocol pagination, filtering, context-window
overrides, bounded requests, failure retention and opt-in refresh. Workgate
uses the same endpoint validation and pinned transport for both profile
configuration and requests; saved API keys are decrypted only for their
bound endpoint. Automatic save probes outside the SQLite write transaction,
then compares the exact saved snapshot before committing the new profile.
Refresh also compares the prior row, preventing stale replies from overwriting
edits or restoring deleted profiles. Account authorization remains a separate
unit. The refresh coroutine is not started by importing these modules.


Model-facing tool and media projection uses these final upstream sources:

| Workgate file | Upstream source at the pin | Git blob |
| --- | --- | --- |
| `mcp_content.py` | `src/local_shell_mcp/webui/mcp_content.py` | `524dd4396577ca6734e6c5da10bedf61a416fc67` |
| `mcp_projection.py` | `src/local_shell_mcp/webui/mcp_projection.py` | `b17c27ca8c6516c93720c5aaf8a176b3d010555f` |
| `provider_multimodal.py` | `src/local_shell_mcp/webui/provider_multimodal.py` | `33ce92d0dee596ce8a16ed8e06c21a9dd035ec4a` |
| `result_projection.py` | `src/local_shell_mcp/webui/approval_runtime.py` (UTF-16 truncation) | `e12100e03427bd3e6272352b10c80496767640d1` |
| `tests/hosted_chat/test_provider_projection.py` | `tests/test_webui_message_parity.py` (MCP cases) | `f3f229bfca58601f2823ee666dfe0209d686b136` |
| same | `tests/test_webui_pr355_edge_coverage.py` (projection cases) | `1f20e8d244165912a1384440aa6217a7226dcc73` |
| same | `tests/test_webui_steer_media.py` (native media cases) | `e9876cc4923caa39b5bfd36075faa00b7c5b9dc5` |
| `tests/fixtures/hosted_chat/mcp_content_vectors.json` | `tests/fixtures/librechat/mcp_content_vectors.json` | `89ca4369f1d6a2d9a05c4d42271483684e70c823` |
| `tests/fixtures/hosted_chat/approval_runtime_reference.cjs` | `tests/fixtures/librechat/approval_runtime_reference.cjs` | `34587d8e224e7cf9b3d09933d73e322b665d9c3f` |

The original source oracle and its license/source metadata remain intact.
Projection recognizes the native `call_agent_mcp_tool` result, including
structured content, without interpreting ordinary tool JSON as MCP media.
It retains raw redacted results and projects complete batches at their
original positions. Remote image URLs and active UI HTML are not projected;
native MCP Apps authorization and rendering remain separate. Inline image/PDF
parts use the same bounded canonical base64 validator. The model-facing
result budget remains bounded even when history is reloaded. Actual attachment
ownership, durable generation, protocol serialization and app rendering
are integrated in their respective units; importing these helpers does
not start a service or send a request.
