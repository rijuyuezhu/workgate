"""Native Windows apply_patch smoke using the upstream trusted-workspace model."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

if os.name != "nt":
    raise RuntimeError("This smoke requires native Windows Python")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from workgate.config.executor import resolve_executor_config  # noqa: E402
from workgate.config.settings import clear_settings_cache, get_settings  # noqa: E402
from workgate.executor.patch import apply_patch_execute  # noqa: E402
from workgate.executor.tool_session.store import ToolSessionStore  # noqa: E402
from workgate.persistence import FileStateStore, use_state_store  # noqa: E402


async def smoke() -> None:
    with tempfile.TemporaryDirectory(prefix="workgate-native-patch-") as tmp:
        root = Path(tmp)
        os.environ["WORKGATE_DEFAULT_WORKDIR"] = str(root)
        os.environ["WORKGATE_STATE_DIR"] = str(root / ".workgate")
        os.environ["WORKGATE_AGENT_BRIDGE_ENABLED"] = "false"
        clear_settings_cache()
        config = resolve_executor_config(get_settings())
        state = FileStateStore(lambda: config.state_dir)
        store = ToolSessionStore(
            state_store=state,
            default_workdir=config.default_workdir,
            max_session_snapshots=config.max_session_snapshots,
            max_session_snapshot_bytes=config.max_session_snapshot_bytes,
        )
        original = root / "original.txt"
        original.write_text("old\n", encoding="utf-8")
        obsolete = root / "obsolete.txt"
        obsolete.write_text("remove\n", encoding="utf-8")

        with use_state_store(state):
            session_id = "sess_native_windows_patch_01"
            store.create_session(session_id=session_id, workdir=root)
            result = await apply_patch_execute(
                config,
                store,
                """*** Begin Patch
*** Update File: original.txt
@@
-old
+updated
*** Add File: created.txt
+new file
*** Delete File: obsolete.txt
*** End Patch
""",
                ".",
                session_id,
            )
            assert result.ok and result.checked and result.applied, result
            assert original.read_text(encoding="utf-8") == "updated\n"
            assert (root / "created.txt").read_text(
                encoding="utf-8"
            ) == "new file\n"
            assert not obsolete.exists()

            bad = await apply_patch_execute(
                config,
                store,
                """diff --git a/original.txt b/original.txt
--- a/original.txt
+++ b/original.txt
@@ -1 +1 @@
-not-present
+bad
""",
                ".",
                session_id,
            )
            assert bad.checked is False and bad.applied is False
            assert original.read_text(encoding="utf-8") == "updated\n"

            unified = await apply_patch_execute(
                config,
                store,
                """diff --git a/original.txt b/original.txt
--- a/original.txt
+++ b/original.txt
@@ -1 +1 @@
-updated
+unified
""",
                ".",
                session_id,
            )
            assert unified.checked and unified.applied, unified
            assert original.read_text(encoding="utf-8") == "unified\n"

    print("Windows native upstream Git apply check/apply: PASS")


if __name__ == "__main__":
    asyncio.run(smoke())
