# Taskbar reservation extension

The Python aifuel application remains MIT licensed. This separate Windhawk
extension and its adapted taskbar helpers are GPL-3.0-only; the corresponding
source and full license are included alongside the portable runtime in `Source/`.
The application communicates with the extension through window properties.

- `windhawk_taskbar_helpers.h`: adapted from Michael Maltsev's (m417z)
  [taskbar-start-button-position](https://github.com/ramensoftware/windhawk-mods/blob/main/mods/taskbar-start-button-position.wh.cpp).
  Only primary x64 taskbar lookup, symbol resolution, and UI-thread dispatch
  helpers are used. Unknown object layouts and null objects are rejected.
- [Windhawk 1.7.3](https://github.com/ramensoftware/windhawk/tree/v1.7.3):
  GPL-3.0, by Michael Maltsev and contributors. The build downloads its signed
  official offline installer, verifies SHA-256, and extracts portable binaries.
  No Windhawk service, startup entry, or global registry configuration is installed.
- The compiler's libc++/libunwind runtime and MinGW notices are shipped in
  `LLVM-LICENSE.TXT` and `MinGW/`. Microsoft symbol-reader binaries come from the
  official Windhawk distribution and retain their embedded notices.

Build on Windows x64 with Python: `python native/build_runtime.py`.
The builder uses the official Windhawk compiler. The runtime contains neither
the compiler nor the Windhawk editor. Source code is never fetched or compiled
when launching the aifuel application.

Only the primary horizontal Windows 11 taskbar is supported. The extension uses
Windows symbols and validates the taskbar host layout before modifying it.
Taskbar customizations made through another Windhawk installation need manual
integration; aifuel does not stop or overwrite that installation.
