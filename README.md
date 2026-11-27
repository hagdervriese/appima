# appima

A clean, minimalist **AppImage & Portable App Manager** for Linux.

`appima` gives portable Linux apps the native desktop polish you expect—without locking you into centralized stores, heavy daemons, or container runtimes. Your apps stay plain files in a folder you own (default: `~/Apps`), while `appima` takes care of verification, system integration, and background update checks.

## Philosophy

- **Manager, not a launcher:** Your desktop environment (KDE Plasma, GNOME, XFCE, etc.) already has a great application launcher. `appima` integrates apps into it and stays out of your way.
- **No lock-in:** Managed apps live transparently in `~/Apps` (configurable). Even without `appima`, they remain ordinary runnable files.
- **Calm, uncluttered UI:** Native Qt 6 interface that inherits your KDE Breeze light/dark theme—every action has one clear home.
- **Auditable core:** Zero third-party dependencies in the core engine and CLI (pure Python 3 standard library). Only `PySide6` is needed for the GUI.

## Features (v0.2)

- **AppImages & Portable Archives:** Manage both single-file `.AppImage` binaries and compressed portable bundles (`.tar.gz`, `.tgz`, `.tar.xz`, `.txz`, `.tar.bz2`, `.zip`)—including Electron apps (like Antigravity) and JetBrains/Android Studio tarballs (`product-info.json`), plus adopting existing app folders already in `~/Apps`.
- **Install from anywhere:** GitHub Releases, direct download URLs, or local files/folders.
- **Checksum verification:** Automatically detects MD5, SHA-256, SHA-512, or SHA-1 hashes, and auto-discovers companion checksum files (`SHA256SUMS`, `*.sha256`, `checksums.txt`) on GitHub Releases.
- **Safe desktop integration & duplicate guard:** Extracts metadata & icons into `~/.local/share/applications/`, checks for existing similar desktop entries (requiring a unique launcher name if you install multiple versions side-by-side), and lets you customize icons or archive launch binaries in Details.
- **Safe updates, auto-restart & rollback:** Tracks installed versions, updates in place with automatic cleanup of old files, saves backups for one-click rollback, and automatically restarts after self-updates.
- **Background update checks:** Optional `systemd --user` timer (Off / Every 6 hours / Daily / Weekly) with native desktop notifications—zero idle RAM usage.
- **Per-app launch customization:** Optional extra launch arguments and environment variables (e.g. `--no-sandbox` or Wayland flags).

## Roadmap

- **v0.3:** GitLab Releases provider, `.zsync` delta updates, and release notes viewer.

## Quick Start

### Option 1: Standalone AppImage *(Recommended — zero dependencies, ideal for KDE Linux & immutable distros)*

1. Download `appima-<version>-x86_64.AppImage` (and optionally `SHA256SUMS`) from the [GitHub Releases](https://github.com/hagdervriese/appima/releases) page.
2. Make it executable and launch it:
   ```bash
   chmod +x appima-*-x86_64.AppImage
   ./appima-*-x86_64.AppImage
   ```
3. Click **"Integrate appima"** on the welcome banner (or run `./appima-*-x86_64.AppImage integrate-self`). `appima` will place itself into `~/Apps`, create its own desktop launcher entry, and check GitHub for updates to itself just like any other managed app.

### Option 2: Run from Source

Requires Python 3.10+, `squashfs-tools`, and `PySide6` (for the GUI):

```bash
# Ubuntu / Kubuntu / Debian
sudo apt install python3-pyside6.qtwidgets python3-pyside6.qtgui python3-pyside6.qtcore squashfs-tools libnotify-bin

# Launch the Qt 6 GUI
python3 -m appima

# Or use the CLI
python3 -m appima list
python3 -m appima install https://github.com/owner/repo
python3 -m appima install /path/to/MyApp.AppImage --checksum <md5-or-sha256>
python3 -m appima check --notify
python3 -m appima update --all
python3 -m appima config --apps-dir ~/Apps --interval daily
```

## Project Structure

```text
appima/
├── appima/
│   ├── core/          # Pure stdlib: config, SQLite DB, checksums, downloader, XDG integration, manager
│   ├── formats/       # Pluggable package formats (AppImage now; Portable Archives planned)
│   ├── providers/     # Pluggable update sources (GitHub Releases, Direct URL, Local File)
│   ├── service/       # systemd --user timer management & desktop notifications
│   ├── cli/           # Command-line interface & headless update runner
│   └── gui/           # Minimalist native Qt 6 (PySide6) interface
└── tests/             # Isolated unit & integration tests
```

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

Licensed under the **GNU General Public License v3.0 (GPL-3.0)**. See [LICENSE](LICENSE) for details.
