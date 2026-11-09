# Third-party libraries and corresponding source

Cloud Cluster uses unmodified Qt/PySide6 6.12.0 libraries under LGPL-3.0 and
rclone v1.76.0 under MIT. The DLL adds one RC entry point for rclone's directory
rename operations. Its source is `scripts/rclone-rename-dir.go` in the repository
and `licenses/rclone-rename-dir.go` in the distribution. The Python wrapper is
vendored with documented changes.
The application itself does not select a new license through this notice.

Exact upstream sources and build instructions are available without charge:

- Qt 6.12.0 (all Qt modules): https://download.qt.io/official_releases/qt/6.12/6.12.0/single/
- Qt base source: https://github.com/qt/qtbase/tree/v6.12.0
- PySide6 and Shiboken 6.12.0: https://github.com/qt/qtforpython/tree/v6.12.0
- Qt/PySide build instructions: https://doc.qt.io/qtforpython-6/building_from_source/index.html
- rclone v1.76.0: https://github.com/rclone/rclone/tree/e982ff36668f5130d94b4f70409babb3d964df42
- Vendored Python wrapper: https://github.com/rclone/rclone/tree/be8d6044eacb843b74d335db0c5fbe09222031fe/librclone
- CPython 3.15.2: https://www.python.org/downloads/source/

The onefile distribution extracts Qt DLLs and PySide6/Shiboken extension modules
as separate files into a unique `%TEMP%\onefile_*` directory while running.
It uses Nuitka's `--onefile-no-dll` mode, leaving a runnable CloudCluster.exe in
that directory. To use modified, ABI-compatible libraries, copy the entire
extracted directory to another writable folder while the application is running,
close the application, replace the libraries in the copy, and run the copied
CloudCluster.exe. The copied executable can also use `--data-dir` to select an
existing application data folder. No restriction is imposed on reverse
engineering needed to debug such modifications. LGPL-3.0-only.txt and
GPL-3.0-only.txt contain the applicable license texts inside the payload.
Open Help (도움말) → Open-source licenses (오픈소스 라이선스) to read the notices,
license texts, source links, and library replacement instructions without an
external licenses folder. Source links open in the system browser.

The build collects license/notice files for the Go modules actually linked into
librclone.dll under licenses/go-modules. Its modules.txt and rclone-build-info.json
record the versions, source commit, compiler versions, and DLL checksum. These
files are included in the onefile payload before it is packaged.

Redistributors must retain these notices, license texts, and equivalent access to
the exact corresponding library sources alongside the binary download. Verify
this access and the terms for the actual delivery method before public release.
