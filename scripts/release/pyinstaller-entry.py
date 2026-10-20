"""One-file entrypoint; only Windows needs a private screenshot child path."""

import sys

if __name__ == "__main__":
    if sys.platform == "win32" and sys.argv[1:2] == ["--_windows-version-file"]:
        from pathlib import Path

        from workgate import __version__

        if len(sys.argv) != 3:
            raise SystemExit(2)
        Path(sys.argv[2]).write_text(
            f"workgate {__version__}\n", encoding="ascii"
        )
    elif sys.platform == "win32" and sys.argv[1:2] == ["--_windows-capture"]:
        from workgate.executor.gui.windows_capture import main as capture_main

        capture_main(sys.argv[2:])
    else:
        from workgate.main import main

        # PyInstaller --windowed has no stdout/stderr. Managed Windows service
        # errors belong in the existing Executor log, not a silent GUI process.
        if sys.platform == "win32" and sys.argv[1:4] == [
            "executor",
            "run",
            "--managed-service",
        ]:
            import contextlib
            import traceback
            from pathlib import Path

            args = sys.argv[1:]
            if "--config" not in args or args.index("--config") + 1 >= len(
                args
            ):
                raise SystemExit(2)
            log = Path(args[args.index("--config") + 1]).with_name(
                "executor.log"
            )
            with (
                log.open("a", encoding="utf-8", buffering=1) as output,
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(output),
            ):
                try:
                    main(args)
                except Exception:
                    traceback.print_exc()
                    raise
        else:
            main(sys.argv[1:])
