"""One-file entrypoint; only Windows needs a private screenshot child path."""

import sys

if __name__ == "__main__":
    if sys.platform == "win32" and sys.argv[1:2] == ["--_windows-capture"]:
        from workgate.executor.gui.windows_capture import main as capture_main

        capture_main(sys.argv[2:])
    else:
        from workgate.main import main

        main(sys.argv[1:])
