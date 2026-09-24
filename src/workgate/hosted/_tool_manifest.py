"""Generated hosted MCP manifest. Do not edit by hand."""

from __future__ import annotations

import json

LATEST_MCP_PROTOCOL_VERSION = '2025-11-25'
SUPPORTED_MCP_PROTOCOL_VERSIONS = frozenset(('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25'))

HOSTED_TOOL_MANIFEST: tuple[dict[str, object], ...] = tuple(
    json.loads(
        r'''
[
  {
    "annotations": {
      "destructiveHint": false,
      "idempotentHint": true,
      "openWorldHint": false,
      "readOnlyHint": true
    },
    "description": "List Skills in project/session, managed, then global priority order without loading instructions.",
    "inputSchema": {
      "properties": {
        "session_id": {
          "description": "Required shared session id. The bound executor adds <workdir>/.agents/skills as the highest-priority source.",
          "title": "Session Id",
          "type": "string"
        }
      },
      "required": [
        "session_id"
      ],
      "title": "list_agent_skillsArguments",
      "type": "object"
    },
    "name": "list_agent_skills",
    "outputSchema": {
      "description": "Discovered agent skills from ordered project, managed, and global roots.",
      "properties": {
        "skills": {
          "description": "Discovered skill metadata rows.",
          "items": {
            "additionalProperties": true,
            "type": "object"
          },
          "title": "Skills",
          "type": "array"
        },
        "sources": {
          "description": "Ordered Skill registry source rows.",
          "items": {
            "additionalProperties": {
              "type": "string"
            },
            "type": "object"
          },
          "title": "Sources",
          "type": "array"
        },
        "warnings": {
          "description": "Non-fatal skill discovery warnings.",
          "items": {
            "type": "string"
          },
          "title": "Warnings",
          "type": "array"
        }
      },
      "required": [
        "sources",
        "skills",
        "warnings"
      ],
      "title": "ListAgentSkillsOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": false,
      "idempotentHint": true,
      "openWorldHint": false,
      "readOnlyHint": true
    },
    "description": "Load one exact Skill from the executor-backed session registry used by list_agent_skills.",
    "inputSchema": {
      "properties": {
        "name": {
          "description": "Exact skill name returned by list_agent_skills.",
          "title": "Name",
          "type": "string"
        },
        "session_id": {
          "description": "Required shared session id. The bound executor adds <workdir>/.agents/skills as the highest-priority source.",
          "title": "Session Id",
          "type": "string"
        }
      },
      "required": [
        "name",
        "session_id"
      ],
      "title": "activate_agent_skillArguments",
      "type": "object"
    },
    "name": "activate_agent_skill",
    "outputSchema": {
      "description": "Loaded agent skill instructions.",
      "properties": {
        "bytes": {
          "description": "Original SKILL.md byte count.",
          "title": "Bytes",
          "type": "integer"
        },
        "content": {
          "description": "Markdown instruction content for the skill.",
          "title": "Content",
          "type": "string"
        },
        "description": {
          "description": "Skill summary description.",
          "title": "Description",
          "type": "string"
        },
        "entry_path": {
          "description": "Skill entry file path relative to the selected source config root.",
          "title": "Entry Path",
          "type": "string"
        },
        "name": {
          "description": "Activated skill name.",
          "title": "Name",
          "type": "string"
        },
        "related_files": {
          "description": "Paths relative to the skill directory, excluding SKILL.md.",
          "items": {
            "type": "string"
          },
          "title": "Related Files",
          "type": "array"
        },
        "source": {
          "description": "Selected Skill registry source.",
          "title": "Source",
          "type": "string"
        },
        "source_path": {
          "description": "Absolute selected Skill source root.",
          "title": "Source Path",
          "type": "string"
        }
      },
      "required": [
        "name",
        "source",
        "source_path",
        "entry_path",
        "description",
        "content",
        "bytes",
        "related_files"
      ],
      "title": "ActivateAgentSkillOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": false,
      "idempotentHint": true,
      "openWorldHint": false,
      "readOnlyHint": true
    },
    "description": "Read a bounded related file from the same selected Skill source; activate the Skill first.",
    "inputSchema": {
      "properties": {
        "name": {
          "description": "Exact skill name returned by list_agent_skills.",
          "title": "Name",
          "type": "string"
        },
        "path": {
          "description": "Canonical POSIX path relative to the selected skill directory. Use one of activate_agent_skill.related_files.",
          "title": "Path",
          "type": "string"
        },
        "session_id": {
          "description": "Required shared session id. The bound executor adds <workdir>/.agents/skills as the highest-priority source.",
          "title": "Session Id",
          "type": "string"
        }
      },
      "required": [
        "name",
        "path",
        "session_id"
      ],
      "title": "read_agent_skill_fileArguments",
      "type": "object"
    },
    "name": "read_agent_skill_file",
    "outputSchema": {
      "description": "One bounded related text file from an agent skill.",
      "properties": {
        "bytes": {
          "description": "Original file byte count.",
          "title": "Bytes",
          "type": "integer"
        },
        "content": {
          "description": "Normalized UTF-8 text content.",
          "title": "Content",
          "type": "string"
        },
        "name": {
          "description": "Selected skill name.",
          "title": "Name",
          "type": "string"
        },
        "path": {
          "description": "Path relative to the skill directory.",
          "title": "Path",
          "type": "string"
        },
        "source": {
          "description": "Selected Skill registry source.",
          "title": "Source",
          "type": "string"
        },
        "source_path": {
          "description": "Absolute selected Skill source root.",
          "title": "Source Path",
          "type": "string"
        }
      },
      "required": [
        "name",
        "source",
        "source_path",
        "path",
        "content",
        "bytes"
      ],
      "title": "ReadAgentSkillFileOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": false,
      "idempotentHint": true,
      "openWorldHint": false,
      "readOnlyHint": true
    },
    "description": "List files and directories under a session workdir path for quick inspection. Relative paths resolve inside the explicit agent/workspace session. The result reports whether entries were truncated by the requested limit or server cap. The bound executor applies its configured directory-entry limit.",
    "inputSchema": {
      "properties": {
        "max_entries": {
          "default": 500,
          "description": "Maximum number of entries to return before reporting truncation. Bounded by the server configuration.",
          "title": "Max Entries",
          "type": "integer"
        },
        "path": {
          "default": ".",
          "description": "Directory path to list. Relative paths resolve inside the agent/workspace session workdir.",
          "title": "Path",
          "type": "string"
        },
        "recursive": {
          "default": false,
          "description": "Whether to recurse into descendant directories. Required for deleting non-empty directories.",
          "title": "Recursive",
          "type": "boolean"
        },
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        }
      },
      "required": [
        "session_id"
      ],
      "title": "list_filesArguments",
      "type": "object"
    },
    "name": "list_files",
    "outputSchema": {
      "$defs": {
        "EntryInfo": {
          "description": "One file-system entry in a directory listing.",
          "properties": {
            "modified": {
              "description": "Last modification time as a Unix timestamp.",
              "title": "Modified",
              "type": "number"
            },
            "path": {
              "description": "Workspace-relative entry path.",
              "title": "Path",
              "type": "string"
            },
            "size": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "File size in bytes, or null for directories and other entries.",
              "title": "Size"
            },
            "target": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Raw symlink target for link entries, otherwise omitted.",
              "title": "Target"
            },
            "type": {
              "description": "Entry type: file, dir, or other.",
              "title": "Type",
              "type": "string"
            }
          },
          "required": [
            "path",
            "type",
            "modified"
          ],
          "title": "EntryInfo",
          "type": "object"
        }
      },
      "description": "Directory listing result.",
      "properties": {
        "count": {
          "description": "Number of entries returned in entries.",
          "title": "Count",
          "type": "integer"
        },
        "entries": {
          "description": "Returned directory entries.",
          "items": {
            "$ref": "#/$defs/EntryInfo"
          },
          "title": "Entries",
          "type": "array"
        },
        "is_truncated": {
          "description": "Whether more entries existed beyond the configured or requested limit.",
          "title": "Is Truncated",
          "type": "boolean"
        },
        "limit_count": {
          "description": "Maximum number of entries limited by the request or configuration.",
          "title": "Limit Count",
          "type": "integer"
        }
      },
      "required": [
        "limit_count",
        "count",
        "is_truncated",
        "entries"
      ],
      "title": "ListFilesOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": true,
      "idempotentHint": false,
      "openWorldHint": false,
      "readOnlyHint": false
    },
    "description": "Write a complete UTF-8 file inside an explicit agent/workspace session. Use only for new files or intentional whole-file replacement; do not use it for partial edits. For ordinary edits to existing files, use hashline_edit from copied read/search rows instead of rewriting the file. Use edit_lines only when you already have exact structured path/start/end/replacement data. Use bash only when a command-driven transformation is clearer. The bound executor applies its configured write limit.",
    "inputSchema": {
      "properties": {
        "content": {
          "description": "Complete UTF-8 text content to write to the target file.",
          "title": "Content",
          "type": "string"
        },
        "overwrite": {
          "default": true,
          "description": "Whether to replace an existing file. Set false to fail when the target already exists.",
          "title": "Overwrite",
          "type": "boolean"
        },
        "path": {
          "description": "Workspace-relative path, or an allowed absolute path, for the file or directory operation.",
          "title": "Path",
          "type": "string"
        },
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        }
      },
      "required": [
        "session_id",
        "path",
        "content"
      ],
      "title": "write_fileArguments",
      "type": "object"
    },
    "name": "write_file",
    "outputSchema": {
      "description": "File write result.",
      "properties": {
        "bytes": {
          "description": "Number of UTF-8 bytes written.",
          "title": "Bytes",
          "type": "integer"
        },
        "created": {
          "description": "Whether the file did not exist before this write.",
          "title": "Created",
          "type": "boolean"
        },
        "path": {
          "description": "Workspace-relative file path that was written.",
          "title": "Path",
          "type": "string"
        }
      },
      "required": [
        "path",
        "bytes",
        "created"
      ],
      "title": "WriteFileOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": true,
      "idempotentHint": false,
      "openWorldHint": false,
      "readOnlyHint": false
    },
    "description": "Low-level structured line edit for callers that already have exact path/start_line/end_line/replacement data. Do not use this as the normal model editing path from read/search output; use hashline_edit for copied `[path#snapshot_id]` plus `line:text` rows. If you do call edit_lines, pass the same session_id and the snapshot_id from the read/search result so stale files or unseen ranges are rejected. The range is inclusive, 1-based, and should cover only lines being changed; use an empty replacement to delete. The bound executor applies its configured write limit.",
    "inputSchema": {
      "properties": {
        "end_line": {
          "description": "1-based final original line to replace. The range is inclusive.",
          "title": "End Line",
          "type": "integer"
        },
        "path": {
          "description": "Workspace-relative path, or an allowed absolute path, for the file or directory operation.",
          "title": "Path",
          "type": "string"
        },
        "replacement": {
          "description": "Replacement text for the selected whole-line range. Use an empty string to delete the range.",
          "title": "Replacement",
          "type": "string"
        },
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        },
        "snapshot_id": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional snapshot_id returned by read or search; copy it exactly, do not invent it. When provided, the edit is rejected if the file changed or the line range was not shown.",
          "title": "Snapshot Id"
        },
        "start_line": {
          "description": "1-based first original line to replace. The range is inclusive.",
          "title": "Start Line",
          "type": "integer"
        }
      },
      "required": [
        "path",
        "start_line",
        "end_line",
        "replacement",
        "session_id"
      ],
      "title": "edit_linesArguments",
      "type": "object"
    },
    "name": "edit_lines",
    "outputSchema": {
      "$defs": {
        "LineRange": {
          "description": "Inclusive 1-based line range shown to the agent.",
          "properties": {
            "end": {
              "description": "Final visible 1-based line number.",
              "title": "End",
              "type": "integer"
            },
            "start": {
              "description": "First visible 1-based line number.",
              "title": "Start",
              "type": "integer"
            }
          },
          "required": [
            "start",
            "end"
          ],
          "title": "LineRange",
          "type": "object"
        },
        "ReadFileOutput": {
          "description": "UTF-8 text file content plus edit-grounding metadata.",
          "properties": {
            "bytes": {
              "description": "Total file size in bytes.",
              "title": "Bytes",
              "type": "integer"
            },
            "bytes_read": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Number of bytes read into the text response, when applicable.",
              "title": "Bytes Read"
            },
            "content": {
              "description": "Decoded UTF-8 text content. Prefer numbered_content/hashline output for grounded edits.",
              "title": "Content",
              "type": "string"
            },
            "end_line": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Final original 1-based line number returned across selected ranges, or null when no lines were returned.",
              "title": "End Line"
            },
            "file_sha256": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "SHA-256 digest of the complete file at the time it was read.",
              "title": "File Sha256"
            },
            "line_count": {
              "default": 0,
              "description": "Number of decoded text lines returned in lines and grounded numbered_content across all selected ranges.",
              "title": "Line Count",
              "type": "integer"
            },
            "lines": {
              "description": "Returned lines with original 1-based line numbers for precise follow-up edits.",
              "items": {
                "$ref": "#/$defs/ReadLine"
              },
              "title": "Lines",
              "type": "array"
            },
            "numbered_content": {
              "default": "",
              "description": "Grounded model-facing text: optional [path#snapshot_id] header plus 'line:text' rows for all selected ranges.",
              "title": "Numbered Content",
              "type": "string"
            },
            "path": {
              "description": "Workspace-relative file path that was read.",
              "title": "Path",
              "type": "string"
            },
            "seen_ranges": {
              "description": "Inclusive original line ranges that were actually shown and are eligible for grounded line edits.",
              "items": {
                "$ref": "#/$defs/LineRange"
              },
              "title": "Seen Ranges",
              "type": "array"
            },
            "session_id": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Explicit agent/workspace session that recorded this read, or null when no grounding snapshot was recorded.",
              "title": "Session Id"
            },
            "snapshot_id": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Opaque handle for this displayed file snapshot, used by line-based edit tools to reject stale edits.",
              "title": "Snapshot Id"
            },
            "start_line": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "First original 1-based line number returned across selected ranges, or null when no lines were returned.",
              "title": "Start Line"
            },
            "total_lines": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Total decoded text line count before optional line-range selection.",
              "title": "Total Lines"
            },
            "truncated": {
              "default": false,
              "description": "Whether text content was truncated to fit the read limit.",
              "title": "Truncated",
              "type": "boolean"
            },
            "truncated_bytes": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Number of file bytes omitted due to the read limit, when applicable.",
              "title": "Truncated Bytes"
            }
          },
          "required": [
            "path",
            "bytes",
            "content"
          ],
          "title": "ReadFileOutput",
          "type": "object"
        },
        "ReadLine": {
          "description": "One decoded line with its original file line number.",
          "properties": {
            "line": {
              "description": "Original 1-based line number in the file.",
              "title": "Line",
              "type": "integer"
            },
            "text": {
              "description": "Line text without its trailing newline.",
              "title": "Text",
              "type": "string"
            }
          },
          "required": [
            "line",
            "text"
          ],
          "title": "ReadLine",
          "type": "object"
        }
      },
      "description": "Grounded whole-line edit result.",
      "properties": {
        "context": {
          "$ref": "#/$defs/ReadFileOutput",
          "description": "Hashline post-edit context around the changed line range, including a fresh snapshot_id."
        },
        "diff": {
          "description": "Unified diff for the applied line edit.",
          "title": "Diff",
          "type": "string"
        },
        "end_line": {
          "description": "Original 1-based final line replaced by this edit.",
          "title": "End Line",
          "type": "integer"
        },
        "path": {
          "description": "Workspace-relative file path that was edited.",
          "title": "Path",
          "type": "string"
        },
        "replacement_line_count": {
          "description": "Number of replacement lines inserted for the selected range.",
          "title": "Replacement Line Count",
          "type": "integer"
        },
        "start_line": {
          "description": "Original 1-based first line replaced by this edit.",
          "title": "Start Line",
          "type": "integer"
        }
      },
      "required": [
        "path",
        "start_line",
        "end_line",
        "replacement_line_count",
        "diff",
        "context"
      ],
      "title": "EditLinesOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": true,
      "idempotentHint": false,
      "openWorldHint": false,
      "readOnlyHint": false
    },
    "description": "Default model-facing edit tool for existing UTF-8 files. Copy the `[path#snapshot_id]` header and relevant `line:text` rows from the latest read/search output; never invent snapshot ids/tags. Then provide the final new content as `+text` rows. Supported hunk forms: copied rows followed by `+replacement` rows; copied rows with no `+` rows to delete; `SWAP start[-end]:` followed by `+replacement` rows; and `INSERT [BEFORE|AFTER] line:` followed by `+inserted` rows. To apply multiple non-overlapping hunks, separate hunk bodies with a blank line under the same header or repeat a `[path#snapshot_id]` header for another section or file. Body rows are final content only: use `+` for blank lines, preserve indentation after `+`, and do not write `-old` rows or bare context lines. Keep hunks tight. Line numbers refer to the original displayed snapshot; stale files, wrong paths, overlapping hunks, or unseen ranges are rejected. After every edit, use the returned fresh hunk contexts or run read/search again before the next edit. The bound executor applies its configured write limit.",
    "inputSchema": {
      "properties": {
        "input": {
          "description": "Hashline edit text starting with [path#snapshot_id]. Copy the header/tag and displayed line:text rows from read/search; do not invent snapshot ids. Add + final-content rows, or use SWAP/INSERT directives. Separate multiple non-overlapping hunks with blank lines or repeated headers.",
          "title": "Input",
          "type": "string"
        },
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        }
      },
      "required": [
        "session_id",
        "input"
      ],
      "title": "hashline_editArguments",
      "type": "object"
    },
    "name": "hashline_edit",
    "outputSchema": {
      "$defs": {
        "HashlineEditHunkOutput": {
          "description": "One applied hashline edit hunk.",
          "properties": {
            "context": {
              "$ref": "#/$defs/ReadFileOutput",
              "description": "Hashline post-edit context around this hunk, including a fresh snapshot_id."
            },
            "end_line": {
              "description": "Original 1-based final line touched by this hunk.",
              "title": "End Line",
              "type": "integer"
            },
            "path": {
              "description": "Workspace-relative file path edited by this hunk.",
              "title": "Path",
              "type": "string"
            },
            "replacement_line_count": {
              "description": "Number of replacement lines inserted for this hunk.",
              "title": "Replacement Line Count",
              "type": "integer"
            },
            "start_line": {
              "description": "Original 1-based first line touched by this hunk.",
              "title": "Start Line",
              "type": "integer"
            }
          },
          "required": [
            "path",
            "start_line",
            "end_line",
            "replacement_line_count",
            "context"
          ],
          "title": "HashlineEditHunkOutput",
          "type": "object"
        },
        "LineRange": {
          "description": "Inclusive 1-based line range shown to the agent.",
          "properties": {
            "end": {
              "description": "Final visible 1-based line number.",
              "title": "End",
              "type": "integer"
            },
            "start": {
              "description": "First visible 1-based line number.",
              "title": "Start",
              "type": "integer"
            }
          },
          "required": [
            "start",
            "end"
          ],
          "title": "LineRange",
          "type": "object"
        },
        "ReadFileOutput": {
          "description": "UTF-8 text file content plus edit-grounding metadata.",
          "properties": {
            "bytes": {
              "description": "Total file size in bytes.",
              "title": "Bytes",
              "type": "integer"
            },
            "bytes_read": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Number of bytes read into the text response, when applicable.",
              "title": "Bytes Read"
            },
            "content": {
              "description": "Decoded UTF-8 text content. Prefer numbered_content/hashline output for grounded edits.",
              "title": "Content",
              "type": "string"
            },
            "end_line": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Final original 1-based line number returned across selected ranges, or null when no lines were returned.",
              "title": "End Line"
            },
            "file_sha256": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "SHA-256 digest of the complete file at the time it was read.",
              "title": "File Sha256"
            },
            "line_count": {
              "default": 0,
              "description": "Number of decoded text lines returned in lines and grounded numbered_content across all selected ranges.",
              "title": "Line Count",
              "type": "integer"
            },
            "lines": {
              "description": "Returned lines with original 1-based line numbers for precise follow-up edits.",
              "items": {
                "$ref": "#/$defs/ReadLine"
              },
              "title": "Lines",
              "type": "array"
            },
            "numbered_content": {
              "default": "",
              "description": "Grounded model-facing text: optional [path#snapshot_id] header plus 'line:text' rows for all selected ranges.",
              "title": "Numbered Content",
              "type": "string"
            },
            "path": {
              "description": "Workspace-relative file path that was read.",
              "title": "Path",
              "type": "string"
            },
            "seen_ranges": {
              "description": "Inclusive original line ranges that were actually shown and are eligible for grounded line edits.",
              "items": {
                "$ref": "#/$defs/LineRange"
              },
              "title": "Seen Ranges",
              "type": "array"
            },
            "session_id": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Explicit agent/workspace session that recorded this read, or null when no grounding snapshot was recorded.",
              "title": "Session Id"
            },
            "snapshot_id": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Opaque handle for this displayed file snapshot, used by line-based edit tools to reject stale edits.",
              "title": "Snapshot Id"
            },
            "start_line": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "First original 1-based line number returned across selected ranges, or null when no lines were returned.",
              "title": "Start Line"
            },
            "total_lines": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Total decoded text line count before optional line-range selection.",
              "title": "Total Lines"
            },
            "truncated": {
              "default": false,
              "description": "Whether text content was truncated to fit the read limit.",
              "title": "Truncated",
              "type": "boolean"
            },
            "truncated_bytes": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Number of file bytes omitted due to the read limit, when applicable.",
              "title": "Truncated Bytes"
            }
          },
          "required": [
            "path",
            "bytes",
            "content"
          ],
          "title": "ReadFileOutput",
          "type": "object"
        },
        "ReadLine": {
          "description": "One decoded line with its original file line number.",
          "properties": {
            "line": {
              "description": "Original 1-based line number in the file.",
              "title": "Line",
              "type": "integer"
            },
            "text": {
              "description": "Line text without its trailing newline.",
              "title": "Text",
              "type": "string"
            }
          },
          "required": [
            "line",
            "text"
          ],
          "title": "ReadLine",
          "type": "object"
        }
      },
      "description": "Grounded hashline edit result for one or more applied hunks.",
      "properties": {
        "context": {
          "$ref": "#/$defs/ReadFileOutput",
          "description": "Hashline post-edit context around the changed line range, including a fresh snapshot_id."
        },
        "diff": {
          "description": "Unified diff for the applied line edit.",
          "title": "Diff",
          "type": "string"
        },
        "end_line": {
          "description": "Original 1-based final line replaced by this edit.",
          "title": "End Line",
          "type": "integer"
        },
        "hunk_count": {
          "description": "Number of hashline hunks applied.",
          "title": "Hunk Count",
          "type": "integer"
        },
        "hunks": {
          "description": "Per-hunk edit summaries in original input order.",
          "items": {
            "$ref": "#/$defs/HashlineEditHunkOutput"
          },
          "title": "Hunks",
          "type": "array"
        },
        "path": {
          "description": "Workspace-relative file path that was edited.",
          "title": "Path",
          "type": "string"
        },
        "replacement_line_count": {
          "description": "Number of replacement lines inserted for the selected range.",
          "title": "Replacement Line Count",
          "type": "integer"
        },
        "start_line": {
          "description": "Original 1-based first line replaced by this edit.",
          "title": "Start Line",
          "type": "integer"
        }
      },
      "required": [
        "path",
        "start_line",
        "end_line",
        "replacement_line_count",
        "diff",
        "context",
        "hunk_count",
        "hunks"
      ],
      "title": "HashlineEditOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": true,
      "idempotentHint": false,
      "openWorldHint": false,
      "readOnlyHint": false
    },
    "description": "Delete a file or directory inside a session workdir.",
    "inputSchema": {
      "properties": {
        "path": {
          "description": "Workspace-relative path, or an allowed absolute path, for the file or directory operation.",
          "title": "Path",
          "type": "string"
        },
        "recursive": {
          "default": false,
          "description": "Whether to recurse into descendant directories. Required for deleting non-empty directories.",
          "title": "Recursive",
          "type": "boolean"
        },
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        }
      },
      "required": [
        "session_id",
        "path"
      ],
      "title": "delete_file_or_dirArguments",
      "type": "object"
    },
    "name": "delete_file_or_dir",
    "outputSchema": {
      "description": "File or directory deletion result.",
      "properties": {
        "deleted": {
          "description": "Deleted item type, usually file or directory.",
          "title": "Deleted",
          "type": "string"
        },
        "path": {
          "description": "Workspace-relative path that was deleted.",
          "title": "Path",
          "type": "string"
        }
      },
      "required": [
        "path",
        "deleted"
      ],
      "title": "DeleteFileOrDirOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": true,
      "idempotentHint": false,
      "openWorldHint": false,
      "readOnlyHint": false
    },
    "description": "Check and apply a standard unified diff or an apply_patch envelope inside an explicit agent/workspace session. Paths resolve relative to cwd within the session workdir; absolute envelope paths are accepted only when they stay inside cwd. The tool validates the entire envelope, runs `git apply --check`, and applies only after preflight succeeds. Prefer hashline_edit for ordinary grounded edits copied from read/search; use apply_patch for portable multi-file patches or compatibility with apply_patch envelopes. The bound executor applies its configured patch/write limit.",
    "inputSchema": {
      "properties": {
        "cwd": {
          "default": ".",
          "description": "Directory inside the explicit session workdir against which patch paths are resolved. Defaults to the session workdir.",
          "title": "Cwd",
          "type": "string"
        },
        "patch": {
          "description": "A standard unified diff or an apply_patch envelope beginning with '*** Begin Patch'. Paths resolve inside cwd and the explicit session workdir.",
          "title": "Patch",
          "type": "string"
        },
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        }
      },
      "required": [
        "session_id",
        "patch"
      ],
      "title": "apply_patchArguments",
      "type": "object"
    },
    "name": "apply_patch",
    "outputSchema": {
      "description": "Result of validating and applying a unified diff.",
      "properties": {
        "applied": {
          "description": "Whether the patch was applied to the worktree.",
          "title": "Applied",
          "type": "boolean"
        },
        "checked": {
          "description": "Whether the preflight git apply --check phase succeeded.",
          "title": "Checked",
          "type": "boolean"
        },
        "command": {
          "description": "Human-readable git apply command.",
          "title": "Command",
          "type": "string"
        },
        "cwd": {
          "description": "Resolved directory against which paths were applied.",
          "title": "Cwd",
          "type": "string"
        },
        "duration_ms": {
          "description": "Elapsed time for the reported git phase.",
          "title": "Duration Ms",
          "type": "integer"
        },
        "exit_code": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "description": "git apply exit code, or null when execution timed out.",
          "title": "Exit Code"
        },
        "ok": {
          "description": "Whether the final patch application succeeded.",
          "title": "Ok",
          "type": "boolean"
        },
        "patch_path": {
          "description": "Temporary normalized unified-diff file used by git apply.",
          "title": "Patch Path",
          "type": "string"
        },
        "stderr": {
          "default": "",
          "description": "Bounded standard error.",
          "title": "Stderr",
          "type": "string"
        },
        "stdout": {
          "default": "",
          "description": "Bounded standard output.",
          "title": "Stdout",
          "type": "string"
        },
        "timed_out": {
          "default": false,
          "description": "Whether git apply exceeded its timeout.",
          "title": "Timed Out",
          "type": "boolean"
        },
        "truncated": {
          "default": false,
          "description": "Whether captured output was truncated.",
          "title": "Truncated",
          "type": "boolean"
        }
      },
      "required": [
        "ok",
        "exit_code",
        "duration_ms",
        "cwd",
        "command",
        "patch_path",
        "checked",
        "applied"
      ],
      "title": "ApplyPatchOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": false,
      "idempotentHint": true,
      "openWorldHint": false,
      "readOnlyHint": true
    },
    "description": "Read one file or list one directory inside an explicit agent/workspace session with optional selector suffixes in the path. Use this for normal code context when you know one path, especially before hashline_edit. Use search for content discovery across files, tree_view/list_files/glob_search for path discovery, and connector fetch only when consuming an id from workspace_search. Put ranges in the path selector and preserve the hashline output for edits: `[path#snapshot_id]` plus `line:text` rows can be copied directly into hashline_edit. Use edit_lines only when you already have exact structured path/start/end/replacement data. Supported selectors: path:50, path:50-80, path:50+20, path:5-16,960-973, path:raw, path:50-80:raw, and path:5-16,960-973:raw. Comma-separated ranges apply only within the same file, not across multiple files; ranges must be ordered and non-overlapping; call read separately for each file. The executor enforces its configured per-file read limit.",
    "inputSchema": {
      "properties": {
        "path": {
          "description": "Single file or directory path with optional selector suffix. Examples: file.py, file.py:50, file.py:50-80, file.py:50+20, file.py:5-16,960-973, file.py:raw, file.py:50-80:raw, file.py:5-16,960-973:raw. Do not combine multiple files in one path; comma-separated ranges apply only within the same file.",
          "title": "Path",
          "type": "string"
        },
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        }
      },
      "required": [
        "session_id",
        "path"
      ],
      "title": "readArguments",
      "type": "object"
    },
    "name": "read",
    "outputSchema": {
      "$defs": {
        "EntryInfo": {
          "description": "One file-system entry in a directory listing.",
          "properties": {
            "modified": {
              "description": "Last modification time as a Unix timestamp.",
              "title": "Modified",
              "type": "number"
            },
            "path": {
              "description": "Workspace-relative entry path.",
              "title": "Path",
              "type": "string"
            },
            "size": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "File size in bytes, or null for directories and other entries.",
              "title": "Size"
            },
            "target": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Raw symlink target for link entries, otherwise omitted.",
              "title": "Target"
            },
            "type": {
              "description": "Entry type: file, dir, or other.",
              "title": "Type",
              "type": "string"
            }
          },
          "required": [
            "path",
            "type",
            "modified"
          ],
          "title": "EntryInfo",
          "type": "object"
        },
        "LineRange": {
          "description": "Inclusive 1-based line range shown to the agent.",
          "properties": {
            "end": {
              "description": "Final visible 1-based line number.",
              "title": "End",
              "type": "integer"
            },
            "start": {
              "description": "First visible 1-based line number.",
              "title": "Start",
              "type": "integer"
            }
          },
          "required": [
            "start",
            "end"
          ],
          "title": "LineRange",
          "type": "object"
        },
        "ListFilesOutput": {
          "description": "Directory listing result.",
          "properties": {
            "count": {
              "description": "Number of entries returned in entries.",
              "title": "Count",
              "type": "integer"
            },
            "entries": {
              "description": "Returned directory entries.",
              "items": {
                "$ref": "#/$defs/EntryInfo"
              },
              "title": "Entries",
              "type": "array"
            },
            "is_truncated": {
              "description": "Whether more entries existed beyond the configured or requested limit.",
              "title": "Is Truncated",
              "type": "boolean"
            },
            "limit_count": {
              "description": "Maximum number of entries limited by the request or configuration.",
              "title": "Limit Count",
              "type": "integer"
            }
          },
          "required": [
            "limit_count",
            "count",
            "is_truncated",
            "entries"
          ],
          "title": "ListFilesOutput",
          "type": "object"
        },
        "ReadFileMetadata": {
          "description": "File read metadata without duplicate file text.",
          "properties": {
            "bytes": {
              "description": "Total file size in bytes.",
              "title": "Bytes",
              "type": "integer"
            },
            "bytes_read": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Number of bytes read into the text response, when applicable.",
              "title": "Bytes Read"
            },
            "end_line": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Final original 1-based line number shown across selected ranges, or null when no lines were shown.",
              "title": "End Line"
            },
            "file_sha256": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "SHA-256 digest of the complete file at the time it was read.",
              "title": "File Sha256"
            },
            "line_count": {
              "default": 0,
              "description": "Number of decoded text lines shown across all selected ranges.",
              "title": "Line Count",
              "type": "integer"
            },
            "path": {
              "description": "Workspace-relative file path that was read.",
              "title": "Path",
              "type": "string"
            },
            "seen_ranges": {
              "description": "Inclusive original line ranges that were actually shown and are eligible for grounded edits.",
              "items": {
                "$ref": "#/$defs/LineRange"
              },
              "title": "Seen Ranges",
              "type": "array"
            },
            "session_id": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Explicit agent/workspace session that recorded this read, or null when no grounding snapshot was recorded.",
              "title": "Session Id"
            },
            "snapshot_id": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Opaque handle for this displayed file snapshot, used by edit tools to reject stale edits.",
              "title": "Snapshot Id"
            },
            "start_line": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "First original 1-based line number shown across selected ranges, or null when no lines were shown.",
              "title": "Start Line"
            },
            "total_lines": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Total decoded text line count before optional line-range selection.",
              "title": "Total Lines"
            },
            "truncated": {
              "default": false,
              "description": "Whether text content was truncated to fit the read limit.",
              "title": "Truncated",
              "type": "boolean"
            },
            "truncated_bytes": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Number of file bytes omitted due to the read limit, when applicable.",
              "title": "Truncated Bytes"
            }
          },
          "required": [
            "path",
            "bytes"
          ],
          "title": "ReadFileMetadata",
          "type": "object"
        }
      },
      "description": "Read result for files and directories.",
      "properties": {
        "content": {
          "description": "Model-facing content. File reads use hashline-style text unless raw is true; directories use a compact listing.",
          "title": "Content",
          "type": "string"
        },
        "directory": {
          "anyOf": [
            {
              "$ref": "#/$defs/ListFilesOutput"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Structured directory listing data when kind is directory."
        },
        "file": {
          "anyOf": [
            {
              "$ref": "#/$defs/ReadFileMetadata"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "File metadata without duplicate file text when kind is file."
        },
        "kind": {
          "description": "Type of target that was read.",
          "enum": [
            "file",
            "directory"
          ],
          "title": "Kind",
          "type": "string"
        },
        "path": {
          "description": "Workspace-relative target path.",
          "title": "Path",
          "type": "string"
        },
        "raw": {
          "default": false,
          "description": "Whether content omits model-facing line-number prefixes.",
          "title": "Raw",
          "type": "boolean"
        }
      },
      "required": [
        "kind",
        "path",
        "content"
      ],
      "title": "ReadOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": false,
      "idempotentHint": true,
      "openWorldHint": false,
      "readOnlyHint": true
    },
    "description": "Return a compact directory tree inside an explicit executor-backed agent/workspace session for high-level project orientation before targeted file reads. Use tree_view when you need structure, directories, and broad layout; use glob_search when you already know filename patterns; use search when you need content matches. Pass the shared session_id returned by session_start. cwd defaults to the session workdir; any relative cwd override resolves inside that session workdir on the bound executor. The bound executor applies its configured tree-entry limit.",
    "inputSchema": {
      "properties": {
        "cwd": {
          "default": ".",
          "description": "Directory path to render as a compact tree. Relative paths resolve inside the agent/workspace session workdir.",
          "title": "Cwd",
          "type": "string"
        },
        "depth": {
          "default": 3,
          "description": "Maximum directory nesting depth to include in the tree view.",
          "title": "Depth",
          "type": "integer"
        },
        "max_entries": {
          "default": 500,
          "description": "Maximum number of tree entries to return before reporting truncation. Bounded by server configuration.",
          "title": "Max Entries",
          "type": "integer"
        },
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        }
      },
      "required": [
        "session_id"
      ],
      "title": "tree_viewArguments",
      "type": "object"
    },
    "name": "tree_view",
    "outputSchema": {
      "description": "Compact directory tree result.",
      "properties": {
        "count": {
          "description": "Number of entries returned.",
          "title": "Count",
          "type": "integer"
        },
        "entries": {
          "description": "Indented tree entries relative to root.",
          "items": {
            "type": "string"
          },
          "title": "Entries",
          "type": "array"
        },
        "exists": {
          "description": "Whether the requested tree root exists.",
          "title": "Exists",
          "type": "boolean"
        },
        "is_directory": {
          "description": "Whether the requested root is a directory.",
          "title": "Is Directory",
          "type": "boolean"
        },
        "message": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional diagnostic message for missing or non-directory roots.",
          "title": "Message"
        },
        "nearest_existing_parent": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Nearest existing parent for a missing root, when available.",
          "title": "Nearest Existing Parent"
        },
        "nearest_parent_entries": {
          "anyOf": [
            {
              "items": {
                "type": "string"
              },
              "type": "array"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Entries in the nearest existing parent for a missing root.",
          "title": "Nearest Parent Entries"
        },
        "nearest_parent_entries_truncated": {
          "anyOf": [
            {
              "type": "boolean"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Whether nearest_parent_entries was truncated.",
          "title": "Nearest Parent Entries Truncated"
        },
        "root": {
          "description": "Resolved root path used for the tree view.",
          "title": "Root",
          "type": "string"
        },
        "truncated": {
          "description": "Whether additional entries were omitted due to limits.",
          "title": "Truncated",
          "type": "boolean"
        }
      },
      "required": [
        "root",
        "exists",
        "is_directory",
        "entries",
        "count",
        "truncated"
      ],
      "title": "TreeViewOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": false,
      "idempotentHint": true,
      "openWorldHint": false,
      "readOnlyHint": true
    },
    "description": "Find files by glob pattern inside an explicit executor-backed agent/workspace session when you know filename patterns and need matching paths, not file contents. Use glob_search for path discovery by pattern; use tree_view for directory shape; use search for content matches with edit-grounding metadata. Pass the shared session_id returned by session_start. cwd defaults to the session workdir; any relative cwd override resolves inside that session workdir on the bound executor. The bound executor applies its configured glob-result limit.",
    "inputSchema": {
      "properties": {
        "cwd": {
          "default": ".",
          "description": "Directory path that narrows the search root. Relative paths resolve inside the agent/workspace session workdir.",
          "title": "Cwd",
          "type": "string"
        },
        "max_results": {
          "default": 500,
          "description": "Maximum number of matching paths to return. Bounded by server configuration.",
          "title": "Max Results",
          "type": "integer"
        },
        "pattern": {
          "description": "Glob expression matched against workspace-relative paths and file names.",
          "title": "Pattern",
          "type": "string"
        },
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        }
      },
      "required": [
        "session_id",
        "pattern"
      ],
      "title": "glob_searchArguments",
      "type": "object"
    },
    "name": "glob_search",
    "outputSchema": {
      "description": "Glob file search result.",
      "properties": {
        "paths": {
          "description": "Workspace-relative paths matching the glob pattern.",
          "items": {
            "type": "string"
          },
          "title": "Paths",
          "type": "array"
        }
      },
      "required": [
        "paths"
      ],
      "title": "GlobSearchOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": false,
      "idempotentHint": true,
      "openWorldHint": false,
      "readOnlyHint": true
    },
    "description": "Search code content inside an explicit executor-backed agent/workspace session for matching lines. Use this built-in search for content discovery instead of shell grep/ripgrep when you need editable grounding, because displayed rows carry hashline grounding for hashline_edit. Use read when you already know the exact file/range, glob_search when you only need matching paths, and workspace_search/fetch when connector-compatible result shapes are useful; those tools still require the same session_id. pattern is text or regex depending on regex; paths scopes to files, directories, globs, or file line selectors such as `src/app.py:10-20,30-40`. gitignore defaults to true, so search respects .gitignore, .ignore, and related ignore rules; set gitignore=false to include ignored files. matches contains actual matched lines only. displayed_lines contains the shown editable rows and marks each row with kind=\"match\" or kind=\"context\"; numbered_content keeps the same rows in copyable `[path#snapshot_id]` plus `line:text` form that can be copied into hashline_edit. Use `skip` with the same pattern and paths to page through later actual matches when results are truncated or noisy. Use edit_lines only when you already have exact structured path/start/end/replacement data. The bound executor applies its configured search-result limit.",
    "inputSchema": {
      "properties": {
        "case_sensitive": {
          "default": true,
          "description": "Whether matching should be case-sensitive.",
          "title": "Case Sensitive",
          "type": "boolean"
        },
        "gitignore": {
          "default": true,
          "description": "Whether search should respect .gitignore, .ignore, and related ignore rules. Defaults to true; set false to include ignored files.",
          "title": "Gitignore",
          "type": "boolean"
        },
        "max_results": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional maximum number of matches to return. Omit to use the configured server limit.",
          "title": "Max Results"
        },
        "paths": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "items": {
                "type": "string"
              },
              "type": "array"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional file, directory, glob, line-scoped file selector such as src/app.py:10-20,30-40, or list of them that scopes the high-level search; omit to search the workspace root.",
          "title": "Paths"
        },
        "pattern": {
          "description": "Text or regular expression pattern to search for; prefer built-in search tools so matches carry grounding metadata.",
          "title": "Pattern",
          "type": "string"
        },
        "regex": {
          "default": true,
          "description": "Whether query is interpreted as a regular expression.",
          "title": "Regex",
          "type": "boolean"
        },
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        },
        "skip": {
          "default": 0,
          "description": "Number of earlier matches to skip before returning results. Use with the same pattern/paths to page through noisy searches.",
          "title": "Skip",
          "type": "integer"
        }
      },
      "required": [
        "session_id",
        "pattern"
      ],
      "title": "searchArguments",
      "type": "object"
    },
    "name": "search",
    "outputSchema": {
      "$defs": {
        "GrepDisplayLine": {
          "description": "One displayed search output line, either an actual match or context.",
          "properties": {
            "file_sha256": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "SHA-256 digest of the complete matched file when displayed.",
              "title": "File Sha256"
            },
            "kind": {
              "description": "Whether this displayed line is an actual match or surrounding context.",
              "enum": [
                "match",
                "context"
              ],
              "title": "Kind",
              "type": "string"
            },
            "line": {
              "description": "1-based displayed line number.",
              "title": "Line",
              "type": "integer"
            },
            "numbered_line": {
              "description": "Copyable 'line:text' row for this displayed line; use with the enclosing [path#snapshot_id] header from numbered_content.",
              "title": "Numbered Line",
              "type": "string"
            },
            "path": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "description": "Path containing the displayed line.",
              "title": "Path"
            },
            "seen_range": {
              "anyOf": [
                {
                  "$ref": "#/$defs/LineRange"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Inclusive original line range shown in this displayed context window."
            },
            "session_id": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Agent grounding session that recorded this displayed line.",
              "title": "Session Id"
            },
            "snapshot_id": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Snapshot handle for the displayed context window, usable with hashline_edit and edit_lines.",
              "title": "Snapshot Id"
            },
            "text": {
              "description": "Displayed line text without the trailing newline.",
              "title": "Text",
              "type": "string"
            }
          },
          "required": [
            "path",
            "line",
            "kind",
            "text",
            "numbered_line"
          ],
          "title": "GrepDisplayLine",
          "type": "object"
        },
        "GrepMatch": {
          "description": "One ripgrep match.",
          "properties": {
            "column": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "description": "1-based column number for the first match on the line.",
              "title": "Column"
            },
            "file_sha256": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "SHA-256 digest of the complete matched file when displayed.",
              "title": "File Sha256"
            },
            "line": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "description": "1-based line number containing the match.",
              "title": "Line"
            },
            "numbered_line": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Grounded match text with optional hashline header plus 'line:text' row.",
              "title": "Numbered Line"
            },
            "path": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "description": "Path containing the match, when reported by ripgrep.",
              "title": "Path"
            },
            "seen_range": {
              "anyOf": [
                {
                  "$ref": "#/$defs/LineRange"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Inclusive original line range shown for this match."
            },
            "session_id": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Agent grounding session that recorded this match line.",
              "title": "Session Id"
            },
            "snapshot_id": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Snapshot handle for the displayed match line, usable with hashline_edit and edit_lines.",
              "title": "Snapshot Id"
            },
            "text": {
              "description": "Matching line text without the trailing newline.",
              "title": "Text",
              "type": "string"
            }
          },
          "required": [
            "path",
            "line",
            "column",
            "text"
          ],
          "title": "GrepMatch",
          "type": "object"
        },
        "LineRange": {
          "description": "Inclusive 1-based line range shown to the agent.",
          "properties": {
            "end": {
              "description": "Final visible 1-based line number.",
              "title": "End",
              "type": "integer"
            },
            "start": {
              "description": "First visible 1-based line number.",
              "title": "Start",
              "type": "integer"
            }
          },
          "required": [
            "start",
            "end"
          ],
          "title": "LineRange",
          "type": "object"
        }
      },
      "description": "Ripgrep content search result.",
      "properties": {
        "context_radius": {
          "default": 0,
          "description": "Number of surrounding context lines requested around each returned match.",
          "title": "Context Radius",
          "type": "integer"
        },
        "count": {
          "description": "Number of matches returned.",
          "title": "Count",
          "type": "integer"
        },
        "displayed_count": {
          "default": 0,
          "description": "Number of displayed match and context lines returned.",
          "title": "Displayed Count",
          "type": "integer"
        },
        "displayed_lines": {
          "description": "Displayed hashline rows with kind='match' for actual matches and kind='context' for surrounding context.",
          "items": {
            "$ref": "#/$defs/GrepDisplayLine"
          },
          "title": "Displayed Lines",
          "type": "array"
        },
        "matches": {
          "description": "Returned ripgrep matches.",
          "items": {
            "$ref": "#/$defs/GrepMatch"
          },
          "title": "Matches",
          "type": "array"
        },
        "numbered_content": {
          "default": "",
          "description": "Grouped grounded hashline search snippets with [path#snapshot_id] headers and copyable line:text rows; use displayed_lines.kind to distinguish actual matches from context.",
          "title": "Numbered Content",
          "type": "string"
        },
        "ok": {
          "description": "Whether ripgrep completed successfully or with no matches.",
          "title": "Ok",
          "type": "boolean"
        },
        "skipped": {
          "default": 0,
          "description": "Number of earlier matches skipped before the returned page.",
          "title": "Skipped",
          "type": "integer"
        },
        "stderr": {
          "description": "Captured ripgrep stderr, after output limiting.",
          "title": "Stderr",
          "type": "string"
        },
        "truncated": {
          "description": "Whether results were truncated by match or output limits.",
          "title": "Truncated",
          "type": "boolean"
        }
      },
      "required": [
        "ok",
        "matches",
        "count",
        "truncated",
        "stderr"
      ],
      "title": "GrepSearchOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": false,
      "idempotentHint": true,
      "openWorldHint": false,
      "readOnlyHint": true
    },
    "description": "Scan text files under an explicit agent/workspace session for common secret-like strings before commit, push, release, or sharing logs. Results are heuristic and do not prove the workspace is secret-free. The bound executor applies its configured result limit.",
    "inputSchema": {
      "properties": {
        "cwd": {
          "default": ".",
          "description": "Directory to scan. Relative paths resolve inside the agent/workspace session workdir.",
          "title": "Cwd",
          "type": "string"
        },
        "glob": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional glob pattern to narrow which files are scanned.",
          "title": "Glob"
        },
        "max_results": {
          "default": 200,
          "description": "Maximum number of findings to return before reporting truncation. Bounded by server configuration.",
          "title": "Max Results",
          "type": "integer"
        },
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        }
      },
      "required": [
        "session_id"
      ],
      "title": "secret_scanArguments",
      "type": "object"
    },
    "name": "secret_scan",
    "outputSchema": {
      "$defs": {
        "SecretFinding": {
          "description": "One heuristic secret finding.",
          "properties": {
            "line": {
              "description": "1-based line number containing the finding.",
              "title": "Line",
              "type": "integer"
            },
            "path": {
              "description": "Workspace-relative file path containing the finding.",
              "title": "Path",
              "type": "string"
            },
            "type": {
              "description": "Heuristic pattern name that matched.",
              "title": "Type",
              "type": "string"
            }
          },
          "required": [
            "type",
            "path",
            "line"
          ],
          "title": "SecretFinding",
          "type": "object"
        }
      },
      "description": "Heuristic workspace secret-scan result.",
      "properties": {
        "findings": {
          "description": "Returned heuristic secret findings.",
          "items": {
            "$ref": "#/$defs/SecretFinding"
          },
          "title": "Findings",
          "type": "array"
        },
        "truncated": {
          "description": "Whether the finding list was truncated by the result limit.",
          "title": "Truncated",
          "type": "boolean"
        },
        "truncated_files": {
          "description": "Number of scanned files whose text was truncated before scanning.",
          "title": "Truncated Files",
          "type": "integer"
        }
      },
      "required": [
        "findings",
        "truncated",
        "truncated_files"
      ],
      "title": "SecretScanOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": false,
      "idempotentHint": false,
      "openWorldHint": true,
      "readOnlyHint": false
    },
    "description": "Start an explicit agent/workspace session on an executor and bind it to a required workdir. Omit executor_id only when exactly one trusted, non-revoked, protocol-compatible session-capable executor is currently online; otherwise pass the stable executor_id explicitly. The control plane allocates one opaque shared session_id and the executor stores the same id. Before calling, infer the most specific safe project workdir from the task. Pass the returned session_id to every machine-facing workspace tool.",
    "inputSchema": {
      "properties": {
        "executor_id": {
          "anyOf": [
            {
              "maxLength": 128,
              "pattern": "^exec_[A-Za-z0-9_-]{22,}$",
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional stable executor_id. Omit only when exactly one eligible executor is online.",
          "title": "Executor Id"
        },
        "label": {
          "anyOf": [
            {
              "maxLength": 80,
              "minLength": 1,
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional human-readable label for this agent session.",
          "title": "Label"
        },
        "workdir": {
          "description": "Working directory to bind to the session on the selected executor. Relative paths resolve against that executor's configured workspace root.",
          "title": "Workdir",
          "type": "string"
        }
      },
      "required": [
        "workdir"
      ],
      "title": "session_startArguments",
      "type": "object"
    },
    "name": "session_start",
    "outputSchema": {
      "$defs": {
        "GitSessionInfo": {
          "description": "Lightweight git orientation for a session workdir.",
          "properties": {
            "branch": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Current branch or short commit name, if available.",
              "title": "Branch"
            },
            "dirty": {
              "anyOf": [
                {
                  "type": "boolean"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Whether git reports uncommitted changes, if available.",
              "title": "Dirty"
            },
            "is_repo": {
              "description": "Whether the session workdir is inside a git repository.",
              "title": "Is Repo",
              "type": "boolean"
            },
            "root": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Git repository root, if available.",
              "title": "Root"
            }
          },
          "required": [
            "is_repo"
          ],
          "title": "GitSessionInfo",
          "type": "object"
        },
        "SessionCapabilitiesEnvironment": {
          "description": "Executor-local feature support relevant to choosing session tools.",
          "properties": {
            "conpty": {
              "description": "Whether Windows ConPTY is available.",
              "title": "Conpty",
              "type": "boolean"
            },
            "raw_pty": {
              "description": "Whether a raw persistent-terminal backend is available.",
              "title": "Raw Pty",
              "type": "boolean"
            }
          },
          "required": [
            "raw_pty",
            "conpty"
          ],
          "title": "SessionCapabilitiesEnvironment",
          "type": "object"
        },
        "SessionEnvironment": {
          "description": "Structured, bounded environment orientation for one session target.",
          "properties": {
            "capabilities": {
              "$ref": "#/$defs/SessionCapabilitiesEnvironment",
              "description": "Effective capabilities."
            },
            "policy": {
              "$ref": "#/$defs/SessionPolicyEnvironment",
              "description": "Safe effective policy and limits."
            },
            "runtime": {
              "$ref": "#/$defs/SessionRuntimeEnvironment",
              "description": "Runtime identity."
            },
            "tools": {
              "$ref": "#/$defs/SessionToolsEnvironment",
              "description": "Allowlisted tool probes."
            },
            "workspace": {
              "$ref": "#/$defs/SessionWorkspaceEnvironment",
              "description": "Workspace identity."
            }
          },
          "required": [
            "runtime",
            "workspace",
            "tools",
            "capabilities",
            "policy"
          ],
          "title": "SessionEnvironment",
          "type": "object"
        },
        "SessionPolicyEnvironment": {
          "description": "Safe executor-owned limits and modes that influence tool selection.",
          "properties": {
            "full_control": {
              "description": "Whether executor full-control path policy is active.",
              "title": "Full Control",
              "type": "boolean"
            },
            "max_concurrent_commands": {
              "description": "Concurrent executor command limit.",
              "title": "Max Concurrent Commands",
              "type": "integer"
            },
            "max_directory_entries": {
              "description": "Maximum directory entries returned by one listing.",
              "title": "Max Directory Entries",
              "type": "integer"
            },
            "max_file_read_bytes": {
              "description": "Maximum bytes read from one file.",
              "title": "Max File Read Bytes",
              "type": "integer"
            },
            "max_file_write_bytes": {
              "description": "Maximum bytes written to one file.",
              "title": "Max File Write Bytes",
              "type": "integer"
            },
            "max_glob_results": {
              "description": "Maximum glob-search result count.",
              "title": "Max Glob Results",
              "type": "integer"
            },
            "max_job_log_bytes": {
              "description": "Maximum durable log bytes retained for one shell job.",
              "title": "Max Job Log Bytes",
              "type": "integer"
            },
            "max_jobs": {
              "description": "Maximum retained tracked shell-job records.",
              "title": "Max Jobs",
              "type": "integer"
            },
            "max_output_bytes": {
              "description": "Maximum bounded command output bytes.",
              "title": "Max Output Bytes",
              "type": "integer"
            },
            "max_persistent_shells": {
              "description": "Persistent-shell session limit.",
              "title": "Max Persistent Shells",
              "type": "integer"
            },
            "max_search_results": {
              "description": "Maximum text-search result count.",
              "title": "Max Search Results",
              "type": "integer"
            },
            "max_session_snapshot_bytes": {
              "description": "Maximum grounding-snapshot metadata bytes per session.",
              "title": "Max Session Snapshot Bytes",
              "type": "integer"
            },
            "max_session_snapshots": {
              "description": "Maximum grounding snapshots retained per session.",
              "title": "Max Session Snapshots",
              "type": "integer"
            },
            "max_transfer_archive_entries": {
              "description": "Maximum entries accepted from one transfer archive.",
              "title": "Max Transfer Archive Entries",
              "type": "integer"
            },
            "max_transfer_unpacked_bytes": {
              "description": "Maximum unpacked regular-file bytes for one transfer.",
              "title": "Max Transfer Unpacked Bytes",
              "type": "integer"
            },
            "max_tree_entries": {
              "description": "Maximum tree-view entry count.",
              "title": "Max Tree Entries",
              "type": "integer"
            },
            "max_view_image_bytes": {
              "description": "Maximum image bytes accepted by view_image.",
              "title": "Max View Image Bytes",
              "type": "integer"
            },
            "shell_default_timeout_s": {
              "description": "Default bounded shell timeout in seconds.",
              "title": "Shell Default Timeout S",
              "type": "integer"
            },
            "shell_max_timeout_s": {
              "description": "Maximum bounded shell timeout in seconds.",
              "title": "Shell Max Timeout S",
              "type": "integer"
            }
          },
          "required": [
            "full_control",
            "shell_default_timeout_s",
            "shell_max_timeout_s",
            "max_output_bytes",
            "max_jobs",
            "max_job_log_bytes",
            "max_session_snapshots",
            "max_session_snapshot_bytes",
            "max_file_read_bytes",
            "max_file_write_bytes",
            "max_view_image_bytes",
            "max_search_results",
            "max_glob_results",
            "max_tree_entries",
            "max_directory_entries",
            "max_concurrent_commands",
            "max_persistent_shells",
            "max_transfer_archive_entries",
            "max_transfer_unpacked_bytes"
          ],
          "title": "SessionPolicyEnvironment",
          "type": "object"
        },
        "SessionRuntimeEnvironment": {
          "description": "Runtime identity safe to expose for tool selection.",
          "properties": {
            "architecture": {
              "description": "Normalized machine architecture.",
              "title": "Architecture",
              "type": "string"
            },
            "os": {
              "description": "Normalized operating-system family.",
              "title": "Os",
              "type": "string"
            },
            "package_version": {
              "description": "Installed workgate distribution version.",
              "title": "Package Version",
              "type": "string"
            },
            "process_bits": {
              "description": "Python process pointer width in bits.",
              "enum": [
                32,
                64
              ],
              "title": "Process Bits",
              "type": "integer"
            },
            "python_implementation": {
              "description": "Python implementation name, such as CPython.",
              "title": "Python Implementation",
              "type": "string"
            },
            "python_version": {
              "description": "Python language runtime version.",
              "title": "Python Version",
              "type": "string"
            },
            "release": {
              "description": "Bounded operating-system release string.",
              "title": "Release",
              "type": "string"
            },
            "runtime_kind": {
              "description": "Whether this process runs from source or a frozen executable.",
              "enum": [
                "source",
                "frozen"
              ],
              "title": "Runtime Kind",
              "type": "string"
            },
            "workgate_version": {
              "description": "Running workgate source version.",
              "title": "Workgate Version",
              "type": "string"
            }
          },
          "required": [
            "workgate_version",
            "package_version",
            "python_implementation",
            "python_version",
            "runtime_kind",
            "os",
            "release",
            "architecture",
            "process_bits"
          ],
          "title": "SessionRuntimeEnvironment",
          "type": "object"
        },
        "SessionToolProbe": {
          "description": "Bounded availability and version result for one allowlisted tool.",
          "properties": {
            "available": {
              "description": "Whether the tool can be selected.",
              "title": "Available",
              "type": "boolean"
            },
            "source": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Safe resolution source such as configured, system, or bundled.",
              "title": "Source"
            },
            "status": {
              "description": "Normalized probe status without raw exception text.",
              "enum": [
                "available",
                "missing",
                "timeout",
                "error",
                "unsupported"
              ],
              "title": "Status",
              "type": "string"
            },
            "version": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Extracted bounded version token, when available.",
              "title": "Version"
            }
          },
          "required": [
            "available",
            "status"
          ],
          "title": "SessionToolProbe",
          "type": "object"
        },
        "SessionToolsEnvironment": {
          "description": "Allowlisted executable probes owned by the session executor.",
          "properties": {
            "git": {
              "$ref": "#/$defs/SessionToolProbe",
              "description": "Git probe."
            },
            "ripgrep": {
              "$ref": "#/$defs/SessionToolProbe",
              "description": "ripgrep probe."
            },
            "shell": {
              "$ref": "#/$defs/SessionToolProbe",
              "description": "Configured shell probe."
            },
            "tmux": {
              "$ref": "#/$defs/SessionToolProbe",
              "description": "tmux backend probe."
            }
          },
          "required": [
            "shell",
            "git",
            "ripgrep",
            "tmux"
          ],
          "title": "SessionToolsEnvironment",
          "type": "object"
        },
        "SessionWorkspaceEnvironment": {
          "description": "Workspace orientation reported by the executor that owns the session.",
          "properties": {
            "workdir": {
              "description": "Canonical workdir on the execution target.",
              "title": "Workdir",
              "type": "string"
            },
            "workspace_root": {
              "description": "Canonical workspace root on the execution target.",
              "title": "Workspace Root",
              "type": "string"
            }
          },
          "required": [
            "workspace_root",
            "workdir"
          ],
          "title": "SessionWorkspaceEnvironment",
          "type": "object"
        }
      },
      "description": "Explicit agent/workspace session orientation.",
      "properties": {
        "created_at": {
          "description": "Unix timestamp when the session was created.",
          "title": "Created At",
          "type": "number"
        },
        "environment": {
          "$ref": "#/$defs/SessionEnvironment",
          "description": "Bounded runtime, workspace, tool, capability, and policy orientation."
        },
        "executor_id": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Stable executor id bound to this shared session.",
          "title": "Executor Id"
        },
        "git": {
          "$ref": "#/$defs/GitSessionInfo",
          "description": "Lightweight git orientation for the session workdir."
        },
        "instruction_files": {
          "description": "Workspace-relative project instruction files discovered near the session workdir.",
          "items": {
            "type": "string"
          },
          "title": "Instruction Files",
          "type": "array"
        },
        "label": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional human-readable session label.",
          "title": "Label"
        },
        "message": {
          "description": "Short model-facing instruction for using this session.",
          "title": "Message",
          "type": "string"
        },
        "session_id": {
          "description": "Opaque shared control/executor session id with at least 128 bits of randomness.",
          "title": "Session Id",
          "type": "string"
        },
        "updated_at": {
          "description": "Unix timestamp when the session was last touched.",
          "title": "Updated At",
          "type": "number"
        },
        "workdir": {
          "description": "Canonical workdir bound to this session.",
          "title": "Workdir",
          "type": "string"
        },
        "workspace_root": {
          "description": "Configured workspace root reported by the executor that owns this session.",
          "title": "Workspace Root",
          "type": "string"
        }
      },
      "required": [
        "session_id",
        "workdir",
        "created_at",
        "updated_at",
        "workspace_root",
        "git",
        "instruction_files",
        "environment",
        "message"
      ],
      "title": "SessionStartOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": false,
      "idempotentHint": false,
      "openWorldHint": true,
      "readOnlyHint": false
    },
    "description": "Change an existing executor-backed agent/workspace session to a new required workdir. Relative workdirs resolve against the executor's fixed workspace_root; old grounding snapshots are invalidated before the durable cwd changes. Use this when the user redirects you to a different project/subdirectory.",
    "inputSchema": {
      "properties": {
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        },
        "workdir": {
          "description": "Working directory to bind to the session on the selected executor. Relative paths resolve against that executor's configured workspace root.",
          "title": "Workdir",
          "type": "string"
        }
      },
      "required": [
        "session_id",
        "workdir"
      ],
      "title": "session_change_cwdArguments",
      "type": "object"
    },
    "name": "session_change_cwd",
    "outputSchema": {
      "$defs": {
        "GitSessionInfo": {
          "description": "Lightweight git orientation for a session workdir.",
          "properties": {
            "branch": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Current branch or short commit name, if available.",
              "title": "Branch"
            },
            "dirty": {
              "anyOf": [
                {
                  "type": "boolean"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Whether git reports uncommitted changes, if available.",
              "title": "Dirty"
            },
            "is_repo": {
              "description": "Whether the session workdir is inside a git repository.",
              "title": "Is Repo",
              "type": "boolean"
            },
            "root": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Git repository root, if available.",
              "title": "Root"
            }
          },
          "required": [
            "is_repo"
          ],
          "title": "GitSessionInfo",
          "type": "object"
        },
        "SessionCapabilitiesEnvironment": {
          "description": "Executor-local feature support relevant to choosing session tools.",
          "properties": {
            "conpty": {
              "description": "Whether Windows ConPTY is available.",
              "title": "Conpty",
              "type": "boolean"
            },
            "raw_pty": {
              "description": "Whether a raw persistent-terminal backend is available.",
              "title": "Raw Pty",
              "type": "boolean"
            }
          },
          "required": [
            "raw_pty",
            "conpty"
          ],
          "title": "SessionCapabilitiesEnvironment",
          "type": "object"
        },
        "SessionEnvironment": {
          "description": "Structured, bounded environment orientation for one session target.",
          "properties": {
            "capabilities": {
              "$ref": "#/$defs/SessionCapabilitiesEnvironment",
              "description": "Effective capabilities."
            },
            "policy": {
              "$ref": "#/$defs/SessionPolicyEnvironment",
              "description": "Safe effective policy and limits."
            },
            "runtime": {
              "$ref": "#/$defs/SessionRuntimeEnvironment",
              "description": "Runtime identity."
            },
            "tools": {
              "$ref": "#/$defs/SessionToolsEnvironment",
              "description": "Allowlisted tool probes."
            },
            "workspace": {
              "$ref": "#/$defs/SessionWorkspaceEnvironment",
              "description": "Workspace identity."
            }
          },
          "required": [
            "runtime",
            "workspace",
            "tools",
            "capabilities",
            "policy"
          ],
          "title": "SessionEnvironment",
          "type": "object"
        },
        "SessionPolicyEnvironment": {
          "description": "Safe executor-owned limits and modes that influence tool selection.",
          "properties": {
            "full_control": {
              "description": "Whether executor full-control path policy is active.",
              "title": "Full Control",
              "type": "boolean"
            },
            "max_concurrent_commands": {
              "description": "Concurrent executor command limit.",
              "title": "Max Concurrent Commands",
              "type": "integer"
            },
            "max_directory_entries": {
              "description": "Maximum directory entries returned by one listing.",
              "title": "Max Directory Entries",
              "type": "integer"
            },
            "max_file_read_bytes": {
              "description": "Maximum bytes read from one file.",
              "title": "Max File Read Bytes",
              "type": "integer"
            },
            "max_file_write_bytes": {
              "description": "Maximum bytes written to one file.",
              "title": "Max File Write Bytes",
              "type": "integer"
            },
            "max_glob_results": {
              "description": "Maximum glob-search result count.",
              "title": "Max Glob Results",
              "type": "integer"
            },
            "max_job_log_bytes": {
              "description": "Maximum durable log bytes retained for one shell job.",
              "title": "Max Job Log Bytes",
              "type": "integer"
            },
            "max_jobs": {
              "description": "Maximum retained tracked shell-job records.",
              "title": "Max Jobs",
              "type": "integer"
            },
            "max_output_bytes": {
              "description": "Maximum bounded command output bytes.",
              "title": "Max Output Bytes",
              "type": "integer"
            },
            "max_persistent_shells": {
              "description": "Persistent-shell session limit.",
              "title": "Max Persistent Shells",
              "type": "integer"
            },
            "max_search_results": {
              "description": "Maximum text-search result count.",
              "title": "Max Search Results",
              "type": "integer"
            },
            "max_session_snapshot_bytes": {
              "description": "Maximum grounding-snapshot metadata bytes per session.",
              "title": "Max Session Snapshot Bytes",
              "type": "integer"
            },
            "max_session_snapshots": {
              "description": "Maximum grounding snapshots retained per session.",
              "title": "Max Session Snapshots",
              "type": "integer"
            },
            "max_transfer_archive_entries": {
              "description": "Maximum entries accepted from one transfer archive.",
              "title": "Max Transfer Archive Entries",
              "type": "integer"
            },
            "max_transfer_unpacked_bytes": {
              "description": "Maximum unpacked regular-file bytes for one transfer.",
              "title": "Max Transfer Unpacked Bytes",
              "type": "integer"
            },
            "max_tree_entries": {
              "description": "Maximum tree-view entry count.",
              "title": "Max Tree Entries",
              "type": "integer"
            },
            "max_view_image_bytes": {
              "description": "Maximum image bytes accepted by view_image.",
              "title": "Max View Image Bytes",
              "type": "integer"
            },
            "shell_default_timeout_s": {
              "description": "Default bounded shell timeout in seconds.",
              "title": "Shell Default Timeout S",
              "type": "integer"
            },
            "shell_max_timeout_s": {
              "description": "Maximum bounded shell timeout in seconds.",
              "title": "Shell Max Timeout S",
              "type": "integer"
            }
          },
          "required": [
            "full_control",
            "shell_default_timeout_s",
            "shell_max_timeout_s",
            "max_output_bytes",
            "max_jobs",
            "max_job_log_bytes",
            "max_session_snapshots",
            "max_session_snapshot_bytes",
            "max_file_read_bytes",
            "max_file_write_bytes",
            "max_view_image_bytes",
            "max_search_results",
            "max_glob_results",
            "max_tree_entries",
            "max_directory_entries",
            "max_concurrent_commands",
            "max_persistent_shells",
            "max_transfer_archive_entries",
            "max_transfer_unpacked_bytes"
          ],
          "title": "SessionPolicyEnvironment",
          "type": "object"
        },
        "SessionRuntimeEnvironment": {
          "description": "Runtime identity safe to expose for tool selection.",
          "properties": {
            "architecture": {
              "description": "Normalized machine architecture.",
              "title": "Architecture",
              "type": "string"
            },
            "os": {
              "description": "Normalized operating-system family.",
              "title": "Os",
              "type": "string"
            },
            "package_version": {
              "description": "Installed workgate distribution version.",
              "title": "Package Version",
              "type": "string"
            },
            "process_bits": {
              "description": "Python process pointer width in bits.",
              "enum": [
                32,
                64
              ],
              "title": "Process Bits",
              "type": "integer"
            },
            "python_implementation": {
              "description": "Python implementation name, such as CPython.",
              "title": "Python Implementation",
              "type": "string"
            },
            "python_version": {
              "description": "Python language runtime version.",
              "title": "Python Version",
              "type": "string"
            },
            "release": {
              "description": "Bounded operating-system release string.",
              "title": "Release",
              "type": "string"
            },
            "runtime_kind": {
              "description": "Whether this process runs from source or a frozen executable.",
              "enum": [
                "source",
                "frozen"
              ],
              "title": "Runtime Kind",
              "type": "string"
            },
            "workgate_version": {
              "description": "Running workgate source version.",
              "title": "Workgate Version",
              "type": "string"
            }
          },
          "required": [
            "workgate_version",
            "package_version",
            "python_implementation",
            "python_version",
            "runtime_kind",
            "os",
            "release",
            "architecture",
            "process_bits"
          ],
          "title": "SessionRuntimeEnvironment",
          "type": "object"
        },
        "SessionToolProbe": {
          "description": "Bounded availability and version result for one allowlisted tool.",
          "properties": {
            "available": {
              "description": "Whether the tool can be selected.",
              "title": "Available",
              "type": "boolean"
            },
            "source": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Safe resolution source such as configured, system, or bundled.",
              "title": "Source"
            },
            "status": {
              "description": "Normalized probe status without raw exception text.",
              "enum": [
                "available",
                "missing",
                "timeout",
                "error",
                "unsupported"
              ],
              "title": "Status",
              "type": "string"
            },
            "version": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Extracted bounded version token, when available.",
              "title": "Version"
            }
          },
          "required": [
            "available",
            "status"
          ],
          "title": "SessionToolProbe",
          "type": "object"
        },
        "SessionToolsEnvironment": {
          "description": "Allowlisted executable probes owned by the session executor.",
          "properties": {
            "git": {
              "$ref": "#/$defs/SessionToolProbe",
              "description": "Git probe."
            },
            "ripgrep": {
              "$ref": "#/$defs/SessionToolProbe",
              "description": "ripgrep probe."
            },
            "shell": {
              "$ref": "#/$defs/SessionToolProbe",
              "description": "Configured shell probe."
            },
            "tmux": {
              "$ref": "#/$defs/SessionToolProbe",
              "description": "tmux backend probe."
            }
          },
          "required": [
            "shell",
            "git",
            "ripgrep",
            "tmux"
          ],
          "title": "SessionToolsEnvironment",
          "type": "object"
        },
        "SessionWorkspaceEnvironment": {
          "description": "Workspace orientation reported by the executor that owns the session.",
          "properties": {
            "workdir": {
              "description": "Canonical workdir on the execution target.",
              "title": "Workdir",
              "type": "string"
            },
            "workspace_root": {
              "description": "Canonical workspace root on the execution target.",
              "title": "Workspace Root",
              "type": "string"
            }
          },
          "required": [
            "workspace_root",
            "workdir"
          ],
          "title": "SessionWorkspaceEnvironment",
          "type": "object"
        }
      },
      "description": "Explicit agent/workspace session orientation.",
      "properties": {
        "created_at": {
          "description": "Unix timestamp when the session was created.",
          "title": "Created At",
          "type": "number"
        },
        "environment": {
          "$ref": "#/$defs/SessionEnvironment",
          "description": "Bounded runtime, workspace, tool, capability, and policy orientation."
        },
        "executor_id": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Stable executor id bound to this shared session.",
          "title": "Executor Id"
        },
        "git": {
          "$ref": "#/$defs/GitSessionInfo",
          "description": "Lightweight git orientation for the session workdir."
        },
        "instruction_files": {
          "description": "Workspace-relative project instruction files discovered near the session workdir.",
          "items": {
            "type": "string"
          },
          "title": "Instruction Files",
          "type": "array"
        },
        "label": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional human-readable session label.",
          "title": "Label"
        },
        "message": {
          "description": "Short model-facing instruction for using this session.",
          "title": "Message",
          "type": "string"
        },
        "session_id": {
          "description": "Opaque shared control/executor session id with at least 128 bits of randomness.",
          "title": "Session Id",
          "type": "string"
        },
        "updated_at": {
          "description": "Unix timestamp when the session was last touched.",
          "title": "Updated At",
          "type": "number"
        },
        "workdir": {
          "description": "Canonical workdir bound to this session.",
          "title": "Workdir",
          "type": "string"
        },
        "workspace_root": {
          "description": "Configured workspace root reported by the executor that owns this session.",
          "title": "Workspace Root",
          "type": "string"
        }
      },
      "required": [
        "session_id",
        "workdir",
        "created_at",
        "updated_at",
        "workspace_root",
        "git",
        "instruction_files",
        "environment",
        "message"
      ],
      "title": "SessionStartOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": true,
      "idempotentHint": false,
      "openWorldHint": true,
      "readOnlyHint": false
    },
    "description": "End one explicit executor-backed agent/workspace session. The control plane first persists desired termination, stops owned tracked jobs and persistent PTYs as required, and asks the bound executor to make the shared session absent. If the executor is permanently unreachable, force=true explicitly releases only the control binding and reports that executor cleanup was not confirmed. Use session_end when a task is complete so durable capacity is released without restarting the server. This is destructive for running work in that session but does not delete workspace files.",
    "inputSchema": {
      "properties": {
        "force": {
          "default": false,
          "description": "When the bound executor is permanently unreachable, release only the control binding without claiming executor-side cleanup. This may leave orphaned executor resources.",
          "title": "Force",
          "type": "boolean"
        },
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        }
      },
      "required": [
        "session_id"
      ],
      "title": "session_endArguments",
      "type": "object"
    },
    "name": "session_end",
    "outputSchema": {
      "description": "Result of ending one explicit agent/workspace session.",
      "properties": {
        "ended": {
          "description": "Whether durable session state was removed.",
          "title": "Ended",
          "type": "boolean"
        },
        "executor_id": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Stable executor id formerly bound to this shared session, when available.",
          "title": "Executor Id"
        },
        "force_released": {
          "default": false,
          "description": "Whether control released the binding without confirmed executor cleanup.",
          "title": "Force Released",
          "type": "boolean"
        },
        "session_id": {
          "description": "Ended agent/workspace session id.",
          "title": "Session Id",
          "type": "string"
        },
        "stopped_jobs": {
          "description": "Tracked job ids stopped before session removal.",
          "items": {
            "type": "string"
          },
          "title": "Stopped Jobs",
          "type": "array"
        },
        "stopped_shells": {
          "description": "Persistent shell ids stopped before session removal.",
          "items": {
            "type": "string"
          },
          "title": "Stopped Shells",
          "type": "array"
        }
      },
      "required": [
        "session_id",
        "ended"
      ],
      "title": "SessionEndOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": true,
      "idempotentHint": false,
      "openWorldHint": true,
      "readOnlyHint": false
    },
    "description": "Run terminal commands inside an explicit agent/workspace session for builds, tests, package managers, git inspection, one-off scripts, and other work that genuinely needs a shell. Pass the session_id returned by session_start. cwd defaults to the session workdir; any cwd override resolves inside that session workdir. Prefer specialized tools for file context and edits: use read/search/tree_view/glob_search/list_files for inspection, hashline_edit when editing copied read/search rows, edit_lines for structured snapshot-grounded precise edits, write_file only for new files or intentional whole-file replacements, and delete_file_or_dir only for intentional removals. Use bash when the task is a command, not when a structured tool can do the job more safely.\n\nDefault mode is bounded and returns captured stdout/stderr. Use run_python_code instead of bash when you want to execute an ad hoc Python snippet without manually writing a script file. Set async_=true for long-running non-interactive work; this returns a job_id owned by the same session_id and must be managed with the job companion. Set pty=true for executor-side interactive programs, REPLs, servers, or commands that need later input; this returns a shell_id for persistent-shell companion tools. Persistent-shell companion tools require both the owning session_id and the returned shell_id. Do not use shell_id with job. If both async_ and pty are true, PTY mode is used. Use env for multiline, quote-heavy, or caller-provided values instead of embedding them directly in the command. Omit timeout_s to use the bound executor default; the executor enforces its own maximum timeout.",
    "inputSchema": {
      "properties": {
        "async_": {
          "default": false,
          "description": "Whether to start long-running non-interactive work as a tracked background job owned by this session. Returns job_id for the job tool, not shell_id.",
          "title": "Async",
          "type": "boolean"
        },
        "command": {
          "description": "Shell command string to execute for terminal work such as tests, builds, package managers, git, or scripts.",
          "title": "Command",
          "type": "string"
        },
        "cwd": {
          "default": ".",
          "description": "Optional working directory for the command, resolved inside the agent/workspace session workdir. Omit or pass . to use the session workdir.",
          "title": "Cwd",
          "type": "string"
        },
        "env": {
          "anyOf": [
            {
              "additionalProperties": {
                "type": "string"
              },
              "type": "object"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional environment variables for multiline, quote-heavy, or caller-provided values; reference them from the command instead of embedding them directly.",
          "title": "Env"
        },
        "max_output_bytes": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional combined stdout/stderr byte budget for bounded command mode. Values above the configured server cap are clamped.",
          "title": "Max Output Bytes"
        },
        "name": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional name for the tracked async job or persistent PTY shell.",
          "title": "Name"
        },
        "pty": {
          "default": false,
          "description": "Whether to start the command in a persistent PTY shell for interactive programs, REPLs, servers, or commands needing later input. PTY mode returns shell_id for persistent-shell companion tools, not job_id.",
          "title": "Pty",
          "type": "boolean"
        },
        "purpose": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional short purpose explaining why this tool call is being made. Maximum 500 characters.",
          "title": "Purpose"
        },
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        },
        "timeout_s": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional timeout in seconds for bounded command mode. For long-running work, prefer async_=true and manage the returned job_id with job.",
          "title": "Timeout S"
        }
      },
      "required": [
        "session_id",
        "command"
      ],
      "title": "bashArguments",
      "type": "object"
    },
    "name": "bash",
    "outputSchema": {
      "description": "Result returned by the bash shell execution tool.",
      "properties": {
        "command": {
          "description": "Shell command submitted by the caller.",
          "title": "Command",
          "type": "string"
        },
        "cwd": {
          "description": "Resolved working directory used for this shell execution.",
          "title": "Cwd",
          "type": "string"
        },
        "mode": {
          "description": "Execution mode selected by the bash tool.",
          "enum": [
            "command",
            "job",
            "pty"
          ],
          "title": "Mode",
          "type": "string"
        },
        "result": {
          "additionalProperties": true,
          "description": "Structured result from the selected shell mode: bounded command output, async job metadata with owning agent session_id and job_id, or PTY metadata with shell_id for persistent-shell companion tools.",
          "title": "Result",
          "type": "object"
        }
      },
      "required": [
        "mode",
        "command",
        "cwd",
        "result"
      ],
      "title": "ShellExecutionOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": true,
      "idempotentHint": false,
      "openWorldHint": true,
      "readOnlyHint": false
    },
    "description": "Write Python code to a temporary file and execute it inside an explicit agent/workspace session. Pass the session_id returned by session_start. This is a convenience wrapper over bash that runs `python3 <temporary-script>` and supports the same cwd, timeout_s, max_output_bytes, env, async_, pty, and name controls. Use it for quick Python calculations, project-aware scripts, or structured file analysis where Python is clearer than a shell pipeline. Use bash instead when you already have a concrete terminal command; use read/search/hashline_edit/edit_lines/write_file when the task is file inspection or editing rather than script execution.\n\ncwd defaults to the session workdir; any cwd override resolves inside that session workdir. Default mode is bounded and returns captured stdout/stderr under result. Set async_=true for a non-interactive background job owned by the same session_id and managed with job. Set pty=true for executor-side Python processes that need an interactive terminal, returning shell_id for persistent-shell companion tools. Omit timeout_s to use the bound executor default; the executor enforces its own maximum timeout.",
    "inputSchema": {
      "properties": {
        "async_": {
          "default": false,
          "description": "Whether to start long-running non-interactive work as a tracked background job owned by this session. Returns job_id for the job tool, not shell_id.",
          "title": "Async",
          "type": "boolean"
        },
        "code": {
          "description": "Complete Python source code to write to a temporary script and execute through the shell execution surface.",
          "title": "Code",
          "type": "string"
        },
        "cwd": {
          "default": ".",
          "description": "Optional working directory for the command, resolved inside the agent/workspace session workdir. Omit or pass . to use the session workdir.",
          "title": "Cwd",
          "type": "string"
        },
        "env": {
          "anyOf": [
            {
              "additionalProperties": {
                "type": "string"
              },
              "type": "object"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional environment variables for multiline, quote-heavy, or caller-provided values; reference them from the command instead of embedding them directly.",
          "title": "Env"
        },
        "max_output_bytes": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional combined stdout/stderr byte budget for bounded command mode. Values above the configured server cap are clamped.",
          "title": "Max Output Bytes"
        },
        "name": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional name for the tracked async job or persistent PTY shell.",
          "title": "Name"
        },
        "pty": {
          "default": false,
          "description": "Whether to start the command in a persistent PTY shell for interactive programs, REPLs, servers, or commands needing later input. PTY mode returns shell_id for persistent-shell companion tools, not job_id.",
          "title": "Pty",
          "type": "boolean"
        },
        "purpose": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional short purpose explaining why this tool call is being made. Maximum 500 characters.",
          "title": "Purpose"
        },
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        },
        "timeout_s": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional timeout in seconds for bounded command mode. For long-running work, prefer async_=true and manage the returned job_id with job.",
          "title": "Timeout S"
        }
      },
      "required": [
        "session_id",
        "code"
      ],
      "title": "run_python_codeArguments",
      "type": "object"
    },
    "name": "run_python_code",
    "outputSchema": {
      "description": "Result of writing Python code to a temporary file and executing it through shell modes.",
      "properties": {
        "command": {
          "description": "Generated shell command used to run the temporary Python script.",
          "title": "Command",
          "type": "string"
        },
        "cwd": {
          "description": "Resolved working directory used for this Python execution.",
          "title": "Cwd",
          "type": "string"
        },
        "mode": {
          "description": "Execution mode selected for the generated Python script.",
          "enum": [
            "command",
            "job",
            "pty"
          ],
          "title": "Mode",
          "type": "string"
        },
        "result": {
          "additionalProperties": true,
          "description": "Structured result from the selected shell mode: bounded command output, async job metadata with owning agent session_id and job_id, or PTY metadata with shell_id for persistent-shell companion tools.",
          "title": "Result",
          "type": "object"
        },
        "script_path": {
          "description": "Path to the temporary Python script that was executed.",
          "title": "Script Path",
          "type": "string"
        }
      },
      "required": [
        "mode",
        "command",
        "cwd",
        "result",
        "script_path"
      ],
      "title": "RunPythonCodeOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": true,
      "idempotentHint": false,
      "openWorldHint": true,
      "readOnlyHint": false
    },
    "description": "Send input to an existing persistent shell created by bash(pty=true). Pass the owning session_id and shell_id returned by bash PTY mode or list_persistent_shells. Use this only for interactive or manually managed shells that need later input, such as REPLs, prompts, or development servers. Jobs started with bash(async_=true) are non-interactive background jobs; use the job companion for those instead. Set enter=false only when intentionally sending partial input without a newline.",
    "inputSchema": {
      "properties": {
        "enter": {
          "default": true,
          "description": "Whether to send Enter after the input text.",
          "title": "Enter",
          "type": "boolean"
        },
        "input_text": {
          "description": "Text to send to the persistent shell.",
          "title": "Input Text",
          "type": "string"
        },
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        },
        "shell_id": {
          "description": "Persistent shell_id returned by bash(pty=true) or list_persistent_shells. This is not the agent/workspace session_id.",
          "title": "Shell Id",
          "type": "string"
        }
      },
      "required": [
        "session_id",
        "shell_id",
        "input_text"
      ],
      "title": "send_persistent_shell_inputArguments",
      "type": "object"
    },
    "name": "send_persistent_shell_input",
    "outputSchema": {
      "description": "Result of sending input to a persistent shell.",
      "properties": {
        "enter": {
          "description": "Whether an Enter key was sent after the input text.",
          "title": "Enter",
          "type": "boolean"
        },
        "sent_bytes": {
          "description": "Number of UTF-8 bytes sent to the persistent shell.",
          "title": "Sent Bytes",
          "type": "integer"
        },
        "shell_id": {
          "description": "Persistent shell that received input.",
          "title": "Shell Id",
          "type": "string"
        }
      },
      "required": [
        "shell_id",
        "sent_bytes",
        "enter"
      ],
      "title": "SendPersistentShellInputOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": false,
      "idempotentHint": false,
      "openWorldHint": true,
      "readOnlyHint": false
    },
    "description": "Resize a persistent PTY shell created by bash(pty=true). Pass the owning session_id and shell_id returned by bash PTY mode or list_persistent_shells. Supply the visible terminal width and height so terminal UIs and interactive full-screen programs can track the client viewport.",
    "inputSchema": {
      "properties": {
        "cols": {
          "description": "Persistent terminal width in character columns (20-1600).",
          "maximum": 1600,
          "minimum": 20,
          "title": "Cols",
          "type": "integer"
        },
        "rows": {
          "description": "Persistent terminal height in character rows (3-500).",
          "maximum": 500,
          "minimum": 3,
          "title": "Rows",
          "type": "integer"
        },
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        },
        "shell_id": {
          "description": "Persistent shell_id returned by bash(pty=true) or list_persistent_shells. This is not the agent/workspace session_id.",
          "title": "Shell Id",
          "type": "string"
        }
      },
      "required": [
        "session_id",
        "shell_id",
        "cols",
        "rows"
      ],
      "title": "resize_persistent_shellArguments",
      "type": "object"
    },
    "name": "resize_persistent_shell",
    "outputSchema": {
      "description": "Result of resizing a persistent terminal.",
      "properties": {
        "backend": {
          "description": "Persistent-terminal backend that handled the resize request.",
          "title": "Backend",
          "type": "string"
        },
        "cols": {
          "description": "Applied terminal width in character columns.",
          "title": "Cols",
          "type": "integer"
        },
        "resized": {
          "description": "Whether the backend applied the requested size.",
          "title": "Resized",
          "type": "boolean"
        },
        "rows": {
          "description": "Applied terminal height in character rows.",
          "title": "Rows",
          "type": "integer"
        },
        "shell_id": {
          "description": "Persistent shell that was resized.",
          "title": "Shell Id",
          "type": "string"
        }
      },
      "required": [
        "shell_id",
        "cols",
        "rows",
        "resized",
        "backend"
      ],
      "title": "ResizePersistentShellOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": false,
      "idempotentHint": true,
      "openWorldHint": false,
      "readOnlyHint": true
    },
    "description": "Read recent output from a persistent shell created by bash(pty=true). Pass the owning session_id and shell_id returned by bash PTY mode or list_persistent_shells. Use after send_persistent_shell_input to inspect an interactive or manually managed shell without blocking. For tracked non-interactive jobs from bash(async_=true), use job(poll=[...]) because it works from job_id and refreshes job status. lines defaults to 200 and controls how many recent terminal lines are returned; increase it only when needed for context.",
    "inputSchema": {
      "properties": {
        "lines": {
          "default": 200,
          "description": "Number of recent terminal lines to capture from the persistent shell.",
          "title": "Lines",
          "type": "integer"
        },
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        },
        "shell_id": {
          "description": "Persistent shell_id returned by bash(pty=true) or list_persistent_shells. This is not the agent/workspace session_id.",
          "title": "Shell Id",
          "type": "string"
        }
      },
      "required": [
        "session_id",
        "shell_id"
      ],
      "title": "read_persistent_shell_outputArguments",
      "type": "object"
    },
    "name": "read_persistent_shell_output",
    "outputSchema": {
      "additionalProperties": true,
      "description": "Recent output captured from a persistent shell.",
      "properties": {
        "backend": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Persistent-shell backend that produced the output.",
          "title": "Backend"
        },
        "lines": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Requested number of recent terminal lines, when returned by the implementation.",
          "title": "Lines"
        },
        "output": {
          "default": "",
          "description": "Captured recent terminal output from the shell.",
          "title": "Output",
          "type": "string"
        },
        "shell_id": {
          "description": "Persistent shell that was read.",
          "title": "Shell Id",
          "type": "string"
        }
      },
      "required": [
        "shell_id"
      ],
      "title": "ReadPersistentShellOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": true,
      "idempotentHint": false,
      "openWorldHint": true,
      "readOnlyHint": false
    },
    "description": "Terminate a persistent shell created by bash(pty=true). Pass the owning session_id and shell_id returned by bash PTY mode or list_persistent_shells. Use for manually managed shells such as servers, watches, REPLs, or stuck interactive commands. For tracked non-interactive jobs from bash(async_=true), use job(cancel=[...]) so the job record is updated. This is destructive for that shell process but does not delete files.",
    "inputSchema": {
      "properties": {
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        },
        "shell_id": {
          "description": "Persistent shell_id returned by bash(pty=true) or list_persistent_shells. This is not the agent/workspace session_id.",
          "title": "Shell Id",
          "type": "string"
        }
      },
      "required": [
        "session_id",
        "shell_id"
      ],
      "title": "kill_persistent_shellArguments",
      "type": "object"
    },
    "name": "kill_persistent_shell",
    "outputSchema": {
      "additionalProperties": true,
      "description": "Result of terminating a persistent shell.",
      "properties": {
        "backend": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Persistent-shell backend that handled termination.",
          "title": "Backend"
        },
        "killed": {
          "anyOf": [
            {
              "type": "boolean"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Whether the persistent shell was killed successfully.",
          "title": "Killed"
        },
        "shell_id": {
          "description": "Persistent shell targeted for termination.",
          "title": "Shell Id",
          "type": "string"
        },
        "stderr": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Captured backend error text from the kill operation, when available.",
          "title": "Stderr"
        }
      },
      "required": [
        "shell_id"
      ],
      "title": "KillPersistentShellOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": false,
      "idempotentHint": true,
      "openWorldHint": false,
      "readOnlyHint": true
    },
    "description": "List active persistent shells owned by the explicit session_id. Use this when you need the shell_id before reading, sending input, or killing a manually managed shell. Returned shell_id values are persistent-shell handles scoped by the owning session. Async bash jobs are not listed here; use job(session_id, list_jobs=true) to inspect bash(async_=true) background jobs.",
    "inputSchema": {
      "properties": {
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        }
      },
      "required": [
        "session_id"
      ],
      "title": "list_persistent_shellsArguments",
      "type": "object"
    },
    "name": "list_persistent_shells",
    "outputSchema": {
      "$defs": {
        "PersistentShellInfo": {
          "additionalProperties": true,
          "description": "One persistent shell entry.",
          "properties": {
            "backend": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Persistent-shell backend that owns this session.",
              "title": "Backend"
            },
            "command": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Initial or current command, when known.",
              "title": "Command"
            },
            "cwd": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Current or initial working directory, when known.",
              "title": "Cwd"
            },
            "name": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "Optional human-readable shell label.",
              "title": "Name"
            },
            "shell_id": {
              "description": "Persistent shell identifier.",
              "title": "Shell Id",
              "type": "string"
            }
          },
          "required": [
            "shell_id"
          ],
          "title": "PersistentShellInfo",
          "type": "object"
        }
      },
      "additionalProperties": true,
      "description": "Active persistent shells.",
      "properties": {
        "shells": {
          "description": "Active persistent shells with at least shell_id and optional implementation-specific metadata.",
          "items": {
            "$ref": "#/$defs/PersistentShellInfo"
          },
          "title": "Shells",
          "type": "array"
        }
      },
      "required": [
        "shells"
      ],
      "title": "ListPersistentShellsOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": false,
      "idempotentHint": true,
      "openWorldHint": false,
      "readOnlyHint": true
    },
    "description": "Search text files on the executor bound to session_id and return connector-compatible result cards. This broad read-only search uses that executor's configured workspace root; call fetch with the same session_id for a returned result id.",
    "inputSchema": {
      "properties": {
        "query": {
          "description": "Case-insensitive literal text query used by connector-style workspace search.",
          "title": "Query",
          "type": "string"
        },
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        }
      },
      "required": [
        "session_id",
        "query"
      ],
      "title": "workspace_searchArguments",
      "type": "object"
    },
    "name": "workspace_search",
    "outputSchema": {
      "$defs": {
        "SearchResult": {
          "description": "One connector-compatible search result card.",
          "properties": {
            "id": {
              "description": "Workspace result identifier passed to fetch.",
              "title": "Id",
              "type": "string"
            },
            "title": {
              "description": "Human-readable result title.",
              "title": "Title",
              "type": "string"
            },
            "url": {
              "description": "Connector URL for the result card.",
              "title": "Url",
              "type": "string"
            }
          },
          "required": [
            "id",
            "title",
            "url"
          ],
          "title": "SearchResult",
          "type": "object"
        }
      },
      "description": "Connector-compatible search response payload.",
      "properties": {
        "results": {
          "description": "Connector-compatible result cards.",
          "items": {
            "$ref": "#/$defs/SearchResult"
          },
          "title": "Results",
          "type": "array"
        }
      },
      "required": [
        "results"
      ],
      "title": "SearchOutput",
      "type": "object"
    }
  },
  {
    "annotations": {
      "destructiveHint": false,
      "idempotentHint": true,
      "openWorldHint": false,
      "readOnlyHint": true
    },
    "description": "Fetch one UTF-8 workspace text file from the executor bound to session_id. The id should normally come from workspace_search on the same session. For coding-agent work, prefer read because it returns grounding metadata for safe edits.",
    "inputSchema": {
      "properties": {
        "id": {
          "description": "Workspace result id returned by connector-style search, normally a relative file path.",
          "title": "Id",
          "type": "string"
        },
        "session_id": {
          "description": "Opaque shared agent/workspace session_id returned by session_start.",
          "maxLength": 128,
          "minLength": 8,
          "pattern": "^(?:sess_[A-Za-z0-9_-]{22,}|[A-Za-z0-9]{8})$",
          "title": "Session Id",
          "type": "string"
        }
      },
      "required": [
        "session_id",
        "id"
      ],
      "title": "fetchArguments",
      "type": "object"
    },
    "name": "fetch",
    "outputSchema": {
      "description": "Connector-compatible fetched document payload.",
      "properties": {
        "id": {
          "description": "Fetched workspace result identifier.",
          "title": "Id",
          "type": "string"
        },
        "metadata": {
          "anyOf": [
            {
              "additionalProperties": true,
              "type": "object"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional connector metadata for the fetched document.",
          "title": "Metadata"
        },
        "text": {
          "description": "Fetched document text content.",
          "title": "Text",
          "type": "string"
        },
        "title": {
          "description": "Human-readable fetched document title.",
          "title": "Title",
          "type": "string"
        },
        "url": {
          "description": "Connector URL for the fetched document.",
          "title": "Url",
          "type": "string"
        }
      },
      "required": [
        "id",
        "title",
        "text",
        "url"
      ],
      "title": "FetchOutput",
      "type": "object"
    }
  }
]
'''
    )
)
