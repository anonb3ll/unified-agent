- **PDF extraction works again under a macOS framework Python.** Homebrew's
  python@3.x re-execs `bin/python3.x` into
  `Resources/Python.app/Contents/MacOS/Python`. The `web_fetch` PDF worker's
  `sandbox-exec` profile granted exec only for `sys.executable` and its
  realpath, so on the Mac hub every extraction exited 1 (`posix_spawn ...
  Python.app`) and PDFs fell back to base64. The profile now also grants that
  one binary when it exists under `sys.base_prefix`.
