"""PyInstaller entry point for building the standalone appima AppImage."""

from appima.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
