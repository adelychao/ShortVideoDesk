# Windows 首次启动自动配置 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task by task. Steps use checkbox syntax. Keep the existing non-workbench launchers compatible. Automated tests must not be added or run unless the user asks.

**Goal:** Make double-clicking `start-local.cmd` install missing Windows tools, prepare the project-local Python and frontend dependencies, and start the workbench with clear Chinese progress and recovery messages.

**Architecture:** Keep `start-local.cmd` as the one-click entry and delegate setup plus launch to `scripts/start_local.ps1`, so the same PowerShell process can refresh PATH after winget installs and pass it to the child processes. Let the PowerShell helper install required tools and prepare the project environment; extend `launcher.py` with opt-in requirement-file and skip-install arguments while preserving its current defaults for the standalone downloader.

**Tech Stack:** Windows CMD, Windows PowerShell, winget, Python 3.11, uv, FastAPI/Vite launcher, npm.

## Global Constraints

- The local workbench uses Python 3.11; the standalone downloader keeps its existing Python 3.10+ behavior.
- Install missing Windows tools with exact winget IDs: `Python.Python.3.11`, `astral-sh.uv`, `OpenJS.NodeJS.LTS`, `Git.Git`, and optional `Gyan.FFmpeg`.
- Do not silently accept package EULAs; allow native winget or installer prompts.
- Install Python packages into project `.venv`; do not install application packages globally.
- Preserve `requirements.txt` as the default dependency file for the standalone downloader.
- FFmpeg remains optional: warn on failure and continue startup.
- Print Chinese progress/errors for setup, and do not delete project data.
- Do not add or run automated tests unless the user asks; perform static syntax and diff checks only.

---

### Task 1: Define the workbench dependency profile and launcher options

**Files:**
- Create: `requirements-workbench.txt`
- Modify: `launcher.py`

**Interfaces:**
- `requirements-workbench.txt` includes `requirements.txt`; optional local ASR dependencies remain installable from the existing settings flow.
- Add launcher options `--requirements-file PATH` (default `requirements.txt`) and `--skip-dependency-install` (default false).
- Pass both options through `main()` → `_launch()` → `_prepare_virtual_environment()`.

- [ ] Add the workbench dependency file with this exact line:

```text
-r requirements.txt
```

- [ ] Extend `_prepare_virtual_environment()` to accept a `requirements_file: Path` and `skip_dependency_install: bool`. Resolve the requested file under the project root. Always validate that the `.venv` Python is supported and that the requirements file exists. When `skip_dependency_install` is false, retain the existing pip installation behavior using the selected file. When true, return the validated environment without running pip; the PowerShell bootstrap has already installed the file and checks its marker before launch.
- [ ] Add `--requirements-file` and `--skip-dependency-install` to `main()`, preserve `requirements.txt` and false as defaults, and pass the parsed values to `_launch()`.
- [ ] Keep existing `start.bat`, `start.command`, and `start.sh` invocations unchanged so they use the same default file and installation behavior.
- [ ] Inspect the updated `main()` and `_launch()` call chain to confirm all new parameters have a default-compatible value.

**Static verification:** `python -m py_compile launcher.py`; inspect `launcher.py --help` output if the local Python environment is available. Do not run tests.

### Task 2: Build the idempotent Windows bootstrap and startup flow

**Files:**
- Create: `scripts/start_local.ps1`
- Create: `scripts/project_lock_guard.py`
- Modify: `.gitignore`

**Interfaces:**
- Script parameters: accept remaining string arguments and pass them to `launcher.py` after the fixed workbench arguments.
- External tools: `winget install --id <exact-id> --exact --source winget --accept-source-agreements`; do not pass `--accept-package-agreements`.
- Workbench launch command: `.venv\Scripts\python.exe launcher.py --port 8767 --asgi-app anonymous_poc:app --frontend-dev --requirements-file requirements-workbench.txt --skip-dependency-install` plus user-supplied arguments.
- Local markers: store the SHA-256 of `requirements-workbench.txt` in `.venv\.shortvideodesk-requirements.sha256` and the SHA-256 of `frontend\package-lock.json` in `frontend\node_modules\.shortvideodesk-lock.sha256` after successful installs.

- [ ] Resolve the project root from `$PSScriptRoot`, print each setup phase in Chinese, and define a checked-process helper that reports the phase, executable, and nonzero exit code before stopping.
- [ ] Acquire `.shortvideodesk-bootstrap.lock` before checking/installing Windows tools and rechecking local dependency markers; keep it through preparation to serialize concurrent bootstrap processes. Before any project-local changes to `.venv` / `frontend\node_modules`, start a small Python guard that acquires the existing `data/runtime/project.lock` via `ProjectLock`. Hold it for all local environment mutations, then release it immediately before invoking `launcher.py`; this coordinates with `start.bat` and other launchers. Release both locks in `finally` and add the bootstrap lock file to `.gitignore`.
- [ ] Add a package table for the five IDs in Global Constraints. Probe Python with `py -3.11`, uv with `uv --version`, Node/npm with `node --version` and `npm --version`, Git with `git --version`, and FFmpeg with both `ffmpeg -version` and `ffprobe -version`.
- [ ] If a required tool is missing and winget is unavailable, stop with Chinese steps to install Windows App Installer/winget or manually install the missing named tool. If winget is present, install only missing required packages with exact IDs, merge machine/user PATH entries after the incoming process PATH to preserve its existing priority, then probe again. If Node must be installed or upgraded because the currently selected Node is below 20, make the newly installed compatible Node executable take precedence for this launch; preserve any already-selected compatible version-manager Node. If FFmpeg is missing, attempt `Gyan.FFmpeg`; on failure print a warning and continue.
- [ ] Verify Python 3.11 and Node 20+ after installation. Do not continue if either is unsupported. Verify `npm` and Git are callable before preparing packages.
- [ ] If `.venv` is missing, create it with `uv venv --seed --python 3.11 .venv`. If an existing `.venv` uses a different Python minor version, move it to `.venv.backup-<yyyyMMdd-HHmmss>` before creating a fresh environment; add `.venv.backup-*/` to `.gitignore` so the backup is not shown as project content.
- [ ] Compute the requirements marker from `requirements-workbench.txt` and every recursively included local `-r` / `--requirement` file, resolving includes relative to their declaring files. Stop with a Chinese error if an included file is missing or the include graph cycles. If the composite hash differs or importing any required workbench package from `requirements.txt` fails, run `uv pip install --system-certs --python .venv\Scripts\python.exe -r requirements-workbench.txt`. Keep optional `faster-whisper` and OpenCC out of the startup check; their existing on-demand install flow remains available. Write the composite marker only after a successful install.
- [ ] Before installing, check whether the local venv contains the `onnxruntime-gpu` distribution. If it does, expand the profile's local `-r` / `--requirement` includes, remove only CPU `onnxruntime` package entries, install from the filtered temporary manifest, and delete the temporary manifest on every exit path. This preserves the installer's existing GPU runtime behavior while still using uv's system certificate support.
- [ ] Run `.venv\Scripts\python.exe -m playwright install chromium` after Python dependencies are ready. If it fails, stop with a Chinese message naming the command and network requirement.
- [ ] Hash `frontend\package-lock.json`; if the Vite entry point is missing or the marker differs, run `npm --prefix frontend ci`. Write the marker only after success.
- [ ] If FFmpeg is still missing, print that media probing/audio extraction/processing may be unavailable. Set `PYTHONUTF8=1` and `OMD_FRONTEND_DEV=1`, then run the launcher from the local venv while forwarding command-line arguments.
- [ ] Add a PowerShell syntax-only check using `[System.Management.Automation.Language.Parser]::ParseFile(...)` and fix all parse errors. Do not execute the bootstrap against the current machine or run tests.

### Task 3: Make the batch entry beginner-friendly and update setup documentation

**Files:**
- Modify: `start-local.cmd`
- Modify: `README.md`
- Modify: `.gitignore` (from Task 2)

**Interfaces:**
- `start-local.cmd` changes to the project directory, invokes `scripts\start_local.ps1` with `-NoLogo -NoProfile -ExecutionPolicy Bypass`, forwards `%*`, preserves the PowerShell exit code, and pauses on failure.
- README's local workbench section directs a new Windows user to double-click `start-local.cmd`; separate manual environment commands are removed.

- [ ] Replace the immediate `.venv` failure in `start-local.cmd` with the PowerShell bootstrap invocation. Keep output in the same console, pause after a nonzero result, and return the original exit code.
- [ ] Rewrite the README local workbench setup steps for first launch and later launches. State that first launch downloads packages and Playwright Chromium, requires network access and winget when a required Windows tool is missing, and may show native license/elevation prompts.
- [ ] Explain that FFmpeg is optional for launching but required for some media features; point users to the Chinese bootstrap messages if installation fails. Clarify that Playwright-managed Chromium is installed automatically and that local ASR dependencies are installed on demand from settings.
- [ ] Keep the independent downloader section and its existing platform commands unchanged.
- [ ] Inspect the final diff, run `git diff --check`, parse the PowerShell script without executing it, and inspect batch/README paths for typos. Do not add or run automated tests.

## Plan self-review

- Covers the design's startup entry, exact winget packages, same-process PATH refresh, local Python/browser/npm setup, repeat-launch markers, Chinese recovery paths, optional FFmpeg, requirements mismatch, and README update.
- Serializes bootstrap mutations and rechecks markers while holding the lock; hashes nested requirement manifests so shared dependency changes trigger installation.
- Preserves downloader defaults by making the launcher options opt-in and leaves its other platform entrypoints unchanged.
- Static verification is limited to syntax/diff inspection because the current instructions prohibit adding or running tests unless requested.
