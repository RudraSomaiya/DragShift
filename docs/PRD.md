# DragShift — Product Requirements Document (PRD)

| Field | Value |
|---|---|
| Product | **DragShift**: drag a file, tap a key, and convert or act on it from a glass wheel. For Windows. |
| Status | **Approved for Sprint 1** |
| Version | 1.0 (19 Oct 2026) |
| Team | 2 developers: **Dev A (Platform)** and **Dev B (UI + Engine)**. Both use Claude Code. |
| Sprint | **Sprint 1**: Day 1 = Tue 20 Oct 2026 → Day 7 = Mon 26 Oct 2026 |
| License | **AGPL-3.0** (required because the project uses PyMuPDF) |
| Repo | `github.com/RudraSomaiya/DragShift` · Jira project key **`DS`** |
| Inspiration | [Tangerine for Mac](https://tangerineformac.com/) (a clean-room re-implementation. We copy no code or assets.) |

> **How to use this document (humans and AI agents):**
> - This is the single source of truth for *what* we build and *how it must behave*.
> - Every requirement has an ID: **FR-x** for functional requirements, **NFR-x** for non-functional ones, and **DS-x** for stories.
> - When you implement a story, read its row in §12 and the FRs it references, and keep to the interfaces in §9.
> - If code and this PRD disagree, raise it in the PR. Do not silently diverge.
> - "MUST", "SHOULD", and "MAY" are used as in RFC 2119.

---

## Table of contents
1. [Problem & vision](#1-problem--vision)
2. [Goals, non-goals, success metrics](#2-goals-non-goals-success-metrics)
3. [Users & key scenarios](#3-users--key-scenarios)
4. [Glossary](#4-glossary)
5. [Functional requirements](#5-functional-requirements)
6. [Conversion & tool matrix](#6-conversion--tool-matrix)
7. [Non-functional requirements](#7-non-functional-requirements)
8. [UI / UX specification](#8-ui--ux-specification)
9. [Architecture & technical design](#9-architecture--technical-design)
10. [Data & configuration](#10-data--configuration)
11. [Testing strategy](#11-testing-strategy)
12. [Delivery plan (Sprint 1)](#12-delivery-plan-sprint-1)
13. [Engineering workflow](#13-engineering-workflow)
14. [Risks & mitigations](#14-risks--mitigations)
15. [References](#15-references)

---

## 1. Problem & vision

**Problem.** Converting or tweaking a file on Windows is slow. You open an app or a website, upload the file, pick a format, download the result, and move it back. Online converters also send private files to unknown servers.

**What inspired us.** Tangerine for Mac turns an ordinary drag into a conversion. You start dragging a file, tap a modifier key, and a radial wheel appears around the cursor. You drop the file on a segment, and the converted file appears next to the original. Everything runs locally.

**Vision.** DragShift brings this gesture to Windows as a free, open-source, lightweight background app. It also adds **Quick Launch wheels**: tap Shift or Ctrl on the desktop to open your favourite apps or websites.

---

## 2. Goals, non-goals, success metrics

### 2.1 Goals (Sprint 1 / v0.1.0)
- **G1:** Drag + **Shift** → **Convert** wheel. Drag + **Ctrl** → **Tools** wheel. Drag + **Alt** → **File actions** wheel.
- **G2:** Local conversions for images, PDF, documents, spreadsheets, audio, video, subtitles, and archives (§6).
- **G3:** **Quick Launch:** tap Shift → apps wheel; tap Ctrl → websites wheel. Works only on the desktop or in Explorer with nothing selected.
- **G4:** A "Frost" glass / liquid-glass UI with light and dark mode that follows the Windows accent colour. No orange theme.
- **G5:** Start from the command line (`uv run dragshift`), with optional autostart. No `.exe` required.
- **G6:** Low idle RAM (NFR-1).
- **G7:** Smart ordering, Undo + job history, and size presets across all compress tools.

### 2.2 Non-goals (v0.1.0)
- macOS or Linux support.
- An installer or `.exe` packaging.
- Cloud processing, accounts, telemetry, or auto-update.
- Dragging from browsers or virtual files (e.g. Outlook attachments without a real path). DragShift handles real file-system paths only.
- Any feature not listed in this PRD. Backlog ideas live in Jira, not here.

### 2.3 Success metrics (checked at the sprint review)
| ID | Metric | Target |
|---|---|---|
| M1 | Idle private memory of the UI process after 2 min idle | **≤ 60 MB**, with **0** worker processes |
| M2 | Time from modifier key-down to the wheel being visible | **≤ 120 ms** (p95, on a dev machine) |
| M3 | Source files lost or moved by a drop | **0**. Hard requirement, see FR-1.8. |
| M4 | Image → image conversion of a 12 MP photo | ≤ 2 s, excluding worker cold start |
| M5 | Manual test checklist (§11.2) | 100 % pass on both devs' machines |
| M6 | False triggers of Quick Launch during one day of normal use | 0 |

---

## 3. Users & key scenarios
**Primary user:** a Windows power user, student, or developer who converts screenshots, PDFs, and media often and keeps many files on the desktop or in Explorer.

| # | Scenario | Expected result |
|---|---|---|
| S1 | Drag `Page 001.pdf` from the desktop, tap **Shift**, drop on **DOCX** | `Page 001.docx` appears next to the PDF. A toast offers Open / Show in folder / Undo. |
| S2 | Drag `photo.heic`, tap **Shift**, drop on **JPG** | `photo.jpg` is created. The original is untouched. |
| S3 | Drag a screenshot, tap **Ctrl**, drop on **Add BG** | The Add BG dialog opens. The user tunes it and clicks Save; `screenshot BG.png` is created. |
| S4 | Drag a 200 MB video, tap **Ctrl**, drop on **Compress → Discord (10 MB)** | `video Compressed.mp4` ≤ 10 MB. A progress toast is shown while it runs. |
| S5 | Drag 3 PDFs, tap **Ctrl**, drop on **Merge** | `Merged.pdf` is created in the folder of the first file. |
| S6 | Drag a folder, tap **Alt**, drop on **Copy path** | The path is on the clipboard and a toast confirms it. |
| S7 | Click an empty spot on the desktop, tap **Shift** | The apps wheel opens at the cursor. Clicking "VS Code" launches it. |
| S8 | In Explorer with nothing selected, tap **Ctrl** | The websites wheel opens. Clicking "GitHub" opens it in the default browser. |
| S9 | Tap Shift while typing in Word, playing a game, or renaming a file | **Nothing happens.** |
| S10 | Converted the wrong file | Click **Undo** on the toast or in tray → History. The output goes to the Recycle Bin. |

---

## 4. Glossary
| Term | Meaning |
|---|---|
| **Wheel** | A circular glass popup centred on the cursor, made of petals around a hub. |
| **Petal** | One selectable segment of the wheel, with a label and an optional icon. |
| **Hub** | The centre of the wheel. It shows a file thumbnail and the hovered petal's label. It is *not* a drop target. |
| **More… ring** | An outer ring of extra petals. It opens when the user hovers the "More…" petal. |
| **Convert / Tools / Actions wheel** | The 3 drag wheels. Default modifiers are Shift / Ctrl / Alt. |
| **Apps / Sites wheel** | The Quick Launch wheels, opened by *tapping* Shift / Ctrl with no drag. |
| **Drag candidate** | The left mouse button (LMB) is held and the mouse moved past the system drag threshold. |
| **Tap** | A modifier key goes down and up within 300 ms, with no other key or mouse button in between. |
| **Op** | A registered operation: a conversion, tool, or action (§9.4). |
| **Job** | One execution of an Op on one or more files, run in a worker process. |
| **Gate** | A context check that must pass before a wheel may open. |
| **Worker** | A short-lived Python process that runs heavy conversions. |

---

## 5. Functional requirements

### 5.1 Drag gesture → wheel (FR-1)
| ID | Requirement | Priority |
|---|---|---|
| FR-1.1 | DragShift MUST detect a **drag candidate** globally: LMB down, then cursor movement beyond `SM_CXDRAG`/`SM_CYDRAG`. It uses low-level mouse hooks (`WH_MOUSE_LL`) and ignores injected events (`LLMHF_INJECTED`). | MUST |
| FR-1.2 | When a mapped modifier gets a **key-down** during a drag candidate, DragShift MUST confirm it is a real file drag. Either **(a)** a visible window of class `SysDragImage` exists, **or (b)** the root window under the cursor at LMB-down had class `CabinetWClass`, `Progman`, or `WorkerW`. If neither holds, DragShift does nothing. A modifier that was already held *before* the drag started does **not** open a wheel. | MUST |
| FR-1.3 | On confirmation, the wheel MUST open centred on the cursor and clamped fully inside the current monitor's work area. Default mapping: Shift → Convert, Ctrl → Tools, Alt → Actions. The mapping is configurable (FR-8). | MUST |
| FR-1.4 | The wheel window MUST NOT take focus (`WA_ShowWithoutActivating`, `Qt.Tool`, frameless, always on top). Explorer must stay the active window so the OLE drag continues. | MUST |
| FR-1.5 | The wheel SHOULD render its petals at once, pre-filled from the Explorer selection (Shell COM `SelectedItems`, §9.6). The actual dropped paths come from `dragEnterEvent`/`dropEvent` (`mimeData().urls()`), and those are authoritative. If they differ from the pre-fill, the wheel re-renders. | SHOULD |
| FR-1.6 | Pressing a **different** mapped modifier while a wheel is open MUST swap to that wheel with a 120 ms cross-fade. Releasing the modifier MUST NOT close the wheel. | MUST |
| FR-1.7 | Hovering a petal while dragging MUST highlight it (§8.3), and the hub MUST show that petal's label. | MUST |
| FR-1.8 | **Drop safety (critical).** On drop over an *enabled petal*, DragShift MUST call `event.setDropAction(Qt.CopyAction)` and then `event.accept()`. It MUST NEVER report `MoveAction`; otherwise Explorer deletes the source when Shift is held. A drop on the hub, a gap, a disabled petal, or outside the disc MUST call `event.ignore()`. | MUST |
| FR-1.9 | The wheel MUST close when: a drop happens (anywhere); LMB is released outside the wheel; Esc is pressed (Esc also cancels the OLE drag); or 30 s pass with no drag events (a safety timeout). | MUST |
| FR-1.10 | If the dropped data has no local file paths (e.g. text or browser content), the hub MUST show "Unsupported" and every petal is disabled. | MUST |
| FR-1.11 | If the dropped files are of mixed kinds, the wheel MUST show only the Ops valid for **all** of them (intersection). If that set is empty, show only the Actions wheel's petals plus "Unsupported mix" in the hub for Convert/Tools. | MUST |
| FR-1.12 | The user MUST be able to pause DragShift from the tray ("Pause gestures"). While paused, no wheel opens. | MUST |

### 5.2 Wheel behaviour (FR-2)
| ID | Requirement | Priority |
|---|---|---|
| FR-2.1 | A wheel shows **2–8 petals**. With more than 8 valid Ops, it shows 7 plus a **More…** petal. Hovering More… for 400 ms (or dragging onto it) opens an outer ring holding the remaining Ops, up to 16. | MUST |
| FR-2.2 | **Smart ordering:** petals are ordered by (1) the user's pinned order from Settings, then (2) usage count for this input kind (descending), then (3) the default order in §6. Usage counts are stored in `history.json` (§10.2). | MUST |
| FR-2.3 | Petal positions are fixed clockwise starting at 12 o'clock, so muscle memory is stable within a session. Re-ordering is applied only when a wheel opens. | MUST |
| FR-2.4 | Ops that are unavailable (e.g. Office → PDF without Office or LibreOffice) MUST NOT appear. | MUST |
| FR-2.5 | Ops that need a dialog (Crop, Add BG, custom compress) open that dialog after the drop. The dialog is the only DragShift window that takes focus. | MUST |
| FR-2.6 | Keyboard inside an open wheel: digits `1`–`8` pick petals in clockwise order, and Esc closes the wheel. During a drag, Explorer owns the keyboard, so this applies to Quick Launch wheels. | MUST |

### 5.3 Quick Launch wheels (FR-3)
| ID | Requirement | Priority |
|---|---|---|
| FR-3.1 | A **tap** of the Apps modifier (default Shift) opens the **Apps wheel**. A tap of the Sites modifier (default Ctrl) opens the **Sites wheel**. Tap = key-down then key-up within **300 ms**, with no other key, mouse button, or wheel scroll in between, and not an auto-repeat. A **double-tap** mode (two taps within 350 ms) can be chosen in Settings. | MUST |
| FR-3.2 | **Gates.** All must pass, or nothing happens: **G-a** the foreground window's root class is `Progman`, `WorkerW`, or `CabinetWClass`; **G-b** `SHQueryUserNotificationState` is not `QUNS_RUNNING_D3D_FULL_SCREEN`, `QUNS_PRESENTATION_MODE`, or `QUNS_BUSY`; **G-c** the focused control is not a text input (the `GetGUIThreadInfo().hwndFocus` class is not `Edit`, `RichEdit*`, or `Windows.UI.Input.InputSite.WindowClass`); **G-d** no items are selected in the foreground Explorer view or on the desktop; **G-e** the foreground process is not in the user's blocklist; **G-f** gestures are not paused. | MUST |
| FR-3.3 | The Apps wheel has up to **8 slots**. A slot can be an `.exe`, an `.lnk`, a folder, or a Store/UWP app (`shell:AppsFolder\<AUMID>`). Launch with `os.startfile` (or `explorer.exe shell:AppsFolder\…` for AUMIDs). Icons come from `QFileIconProvider`. | MUST |
| FR-3.4 | The Sites wheel has up to **8 slots** of `{label, url}`. Open with the default browser (`os.startfile(url)`). The favicon is fetched **once** when a site is added (`https://<host>/favicon.ico`, falling back to a letter monogram) and cached in `%APPDATA%\DragShift\icons\`. No network access happens at any other time. | MUST |
| FR-3.5 | Quick Launch wheels open centred on the cursor (clamped to the work area). Select by clicking a petal or pressing `1`–`8`. Close with Esc, a click outside, or 10 s of inactivity. These wheels **may** take focus so they receive keys. | MUST |
| FR-3.6 | Empty slots render as "+ Add". Clicking one opens Settings → Launcher. | SHOULD |

### 5.4 Convert, Tools, and Actions (FR-4)
| ID | Requirement | Priority |
|---|---|---|
| FR-4.1 | Each Op in §6 MUST be implemented as a registered Op (§9.4) and be reachable from its wheel. | MUST |
| FR-4.2 | Multi-file drops: Ops with `multi="each"` run once per file. Ops with `multi="all"` (Merge, Images → single PDF, Zip) run once over all the files. | MUST |
| FR-4.3 | **Size presets** MUST be available in **every compress tool** (image, PDF, video, audio): **Email (25 MB)**, **WhatsApp (16 MB)**, **Discord (10 MB)**, **Small (≈ 50 % size)**, and **Custom…** (opens a dialog with a target MB or a quality value). Presets show as an outer ring when the user hovers "Compress", so a single drop is enough. | MUST |
| FR-4.4 | When a target size cannot be reached (e.g. a 2 h video into 10 MB at an acceptable bitrate), the job MUST still write the best effort, and the toast says "Couldn't reach 10 MB — got 14.2 MB". | MUST |
| FR-4.5 | Long jobs (> 1 s) show a progress toast with a Cancel button. Cancel kills the worker or subprocess and deletes the partial output. | MUST |

### 5.5 Output, toasts, undo, history (FR-5)
| ID | Requirement | Priority |
|---|---|---|
| FR-5.1 | Outputs are written **next to the source**. Converts are named `<stem>.<ext>`. Tools are named `<stem> <Label>.<ext>` (e.g. `Page 001 Cropped.jpg`, `clip Compressed.mp4`, `shot BG.png`). Multi-page rasterisation writes to a folder `<stem> pages\<stem> 001.png`. | MUST |
| FR-5.2 | Name collisions append ` (2)`, ` (3)`, …. DragShift MUST NEVER overwrite an existing file. | MUST |
| FR-5.3 | If the source folder is not writable, the output goes to `%USERPROFILE%\Downloads` and the toast says so. | MUST |
| FR-5.4 | The source file MUST never be modified, moved, or deleted, by any Op except **Move to** (an Actions-wheel Op that the user chooses explicitly). | MUST |
| FR-5.5 | After a successful job, a glass toast (bottom-right, 6 s) shows the output name and the buttons **Open**, **Show in folder**, and **Undo**. On failure, a toast shows a one-line reason and a **Details** button that copies the error and log path. | MUST |
| FR-5.6 | **Undo** sends the job's outputs to the Recycle Bin (`send2trash`). For Move/Copy-to actions, Undo reverses the move or removes the copy. Undo is available from the toast and from History. | MUST |
| FR-5.7 | **History**: the tray menu lists the last 20 jobs, each with Open / Show / Undo. It is stored in `history.json` (§10.2), which also stores the usage counts for smart ordering. | MUST |

### 5.6 App lifecycle, tray, settings (FR-6 … FR-8)
| ID | Requirement | Priority |
|---|---|---|
| FR-6.1 | `uv run dragshift` starts the app in the foreground with console logs. | MUST |
| FR-6.2 | `uv run dragshift --background` relaunches itself detached with `pythonw.exe` (no console) and exits. | MUST |
| FR-6.3 | Only a single instance may run (named mutex `Local\DragShift.SingleInstance`). A second launch tells the first to open Settings, then exits. | MUST |
| FR-6.4 | CLI sub-commands (for tests and scripting): `dragshift convert <paths…> --to <fmt>`, `dragshift tool <op-id> <paths…> [--preset discord]`, `dragshift ops <path>` (lists the valid Ops). They run headless and print the output paths. | MUST |
| FR-6.5 | Logs go to `%LOCALAPPDATA%\DragShift\logs\dragshift.log` (rotating, 1 MB × 3). | MUST |
| FR-7.1 | Tray icon menu: **Settings…**, **History ▸**, **Pause gestures** (toggle), **Open log folder**, **Quit**. | MUST |
| FR-7.2 | The **Autostart** toggle writes or removes `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\DragShift` = `"<venv>\Scripts\pythonw.exe" -m dragshift`. | MUST |
| FR-8.1 | **Settings window** tabs: **Keys** (modifier per wheel, tap vs double-tap); **Wheels** (pin and reorder petals per input kind; reset usage stats); **Launcher** (8 app slots, 8 site slots; add by dropping a file or shortcut, by browsing, or by picking from the Start apps list; add a site by label + URL); **Appearance** (System/Light/Dark; accent: Windows accent or a custom colour; reduced motion); **Output** (next to source / fixed folder; JPEG/WebP quality default 85; PDF raster DPI default 300); **General** (autostart, trim memory when idle, Quick Launch on/off, app blocklist). | MUST |
| FR-8.2 | Settings save immediately to `config.json` (§10.1) and apply live, without a restart. | MUST |

---

## 6. Conversion & tool matrix
Engines run **only in worker processes** (§9.2). The **Engine** column is the implementation contract.

### 6.1 Convert wheel (Shift)
| Input kind (extensions) | Targets (default order) | Engine |
|---|---|---|
| **Image**: jpg jpeg png webp heic heif avif tiff tif bmp gif ico, **svg (input only)** | JPG PNG WEBP HEIC AVIF PDF TIFF BMP ICO | Pillow + pillow-heif. AVIF: Pillow if it supports AVIF, else ffmpeg. SVG: rasterise with QtSvg at 2× in the worker. ICO: sizes 16–256. JPG output flattens alpha on white. |
| **PDF** | DOCX PNG JPG TXT MD PPTX | PyMuPDF (render at the config DPI, default 300), pdf2docx, pymupdf4llm (MD), python-pptx (one page image per slide) |
| **Document**: docx md txt html htm rtf odt epub | DOCX PDF* MD TXT HTML EPUB ODT RTF | pandoc via `pypandoc-binary`. *PDF only via Office/LibreOffice (stretch DS-33). |
| **Slides**: ppt pptx odp | PDF* PNG* JPG* | *Stretch DS-33: Office COM or LibreOffice → PDF → PyMuPDF raster |
| **Spreadsheet**: xlsx xls ods csv tsv | XLSX CSV TSV ODS PDF* | pandas + openpyxl + odfpy + xlrd (one CSV per sheet when there are several sheets). *PDF stretch DS-33. |
| **Audio**: mp3 m4a wav flac ogg opus aiff aif wma aac | MP3 M4A WAV FLAC OGG OPUS AIFF | ffmpeg (`imageio-ffmpeg` binary; ffmpeg on PATH is preferred if present) |
| **Video**: mp4 mov mkv webm avi wmv m4v | MP4 MOV MKV WEBM GIF MP3 AVI | ffmpeg. GIF: two-pass palette at fps 15 and width ≤ 640. MP3: audio track. |
| **Subtitle**: srt vtt ass ssa | SRT VTT ASS TXT | pysubs2 |
| **Archive**: zip tar gz tgz rar 7z | Extract here, ZIP, TAR.GZ | stdlib `zipfile`/`tarfile`. RAR/7z extraction via Windows `tar.exe` (bsdtar/libarchive). |
| **Folder** | ZIP | stdlib `zipfile` |
| Multiple images | **+ "Merge to PDF"** (`multi="all"`) | Pillow |

A target equal to the source format is hidden (e.g. no "PNG" petal for a PNG).

### 6.2 Tools wheel (Ctrl)
| Input kind | Tools (default order) | Notes |
|---|---|---|
| Image | **Compress ▸ presets**, **Crop** (dialog), **Add BG** (dialog), Resize ▸ (50 %, 1080p, 4K, Custom…), Rotate ▸ (90° ⟳, 90° ⟲, 180°, Flip H, Flip V), Strip metadata | Compress keeps the format; for the size presets it may switch PNG → WebP only when the user enables "allow format change". Strip metadata removes EXIF/GPS/XMP. |
| PDF | **Compress ▸ presets**, Merge (multi), Split (one file per page), Extract pages… (dialog: page ranges), Rotate ▸, Strip metadata | PyMuPDF: `garbage=4, deflate=True`, and images downsampled to reach the size target. |
| Video | **Compress ▸ presets**, Mute, Extract audio (M4A), To GIF, Resize ▸ (720p, 1080p) | Compress = two-pass H.264 at a target bitrate computed from the duration and the target size, falling back to CRF. |
| Audio | **Compress ▸ presets**, Normalize (EBU R128 `loudnorm`), Bitrate ▸ (96/128/192/320 kbps) | |
| Any files | Zip (multi) | Also available on the Actions wheel |
| **Stretch** (only if pulled in) | Redact (DS-36), Annotate (DS-37), Trim audio/video (DS-38), Edit adjustments (DS-39) | |

### 6.3 Actions wheel (Alt) — any files or folders, mixed kinds allowed
| Action | Behaviour | Runs in |
|---|---|---|
| Copy path | Copies the full path(s) to the clipboard, newline-separated | UI |
| Copy name | Copies the file name(s) | UI |
| Open terminal here (petal: Terminal) | `wt.exe -d "<folder>"` if available, else `powershell.exe` started in the folder (Windows PowerShell 5.1 has no `-WorkingDirectory`). A dragged folder opens in itself, a file in its folder | UI |
| Copy to ▸ / Move to ▸ | Pinned folders (1–4, set in Settings); the ring shows the folder names. A name already taken gets ` (2)` (FR-5.2). Neither goes to Downloads (FR-5.3): the user chose the folder. A move across drives copies, then deletes the original once the copy is complete. Undo removes the copy or moves the item back (FR-5.6) | Worker |
| Zip | `<stem>.zip` for one item, `Archive.zip` for several, next to the first | Worker |
| Copy SHA-256 | Hash of the file(s), copied as `hash  name` lines | Worker |
| **Stretch:** Float (DS-35) | An always-on-top mini shelf; you can drag the file back out with `QDrag` | UI |

---

## 7. Non-functional requirements
| ID | Requirement |
|---|---|
| **NFR-1 Memory** | UI process idle private bytes ≤ **60 MB** after 2 min idle. No worker processes while idle. The UI process MUST NOT import Pillow, PyMuPDF, pandas, numpy, pdf2docx, or pypandoc (this is enforced by a test, §11). |
| **NFR-2 Latency** | Modifier key-down → wheel visible ≤ 120 ms (p95). Hover highlight ≤ 16 ms per frame (60 fps). |
| **NFR-3 Hook safety** | Low-level hook callbacks MUST return in < 1 ms: no COM, no I/O, no logging inside callbacks. Events go to the Qt thread through a queued signal. If Windows removes a hook (timeout), it is re-installed on the next tick (a 5 s watchdog). |
| **NFR-4 Data safety** | Never overwrite, move, or delete source files (FR-1.8, FR-5.2, FR-5.4). Write outputs to `*.part` and then rename atomically. |
| **NFR-5 Privacy** | 100 % offline. The only network call is the one-time favicon fetch (FR-3.4). No telemetry. |
| **NFR-6 Compatibility** | Primary target: Windows 11 23H2+. Minimum: Windows 10 22H2 (glass falls back, see §8.6). x64. Per-monitor DPI v2 (Qt default), multi-monitor, 100–200 % scaling. |
| **NFR-7 Privileges** | Runs non-elevated. If the drag source is an elevated window, Windows UIPI blocks the drop; this is documented and not worked around. |
| **NFR-8 Accessibility** | Respect "Animation effects" off (`SPI_GETCLIENTAREAANIMATION`). Text contrast ≥ 4.5:1 on glass (§8.2 tint values are chosen for this). Every wheel can be closed with Esc. |
| **NFR-9 Robustness** | Any Op failure shows a toast and is logged. It never crashes the UI process. If a worker crashes, the pool is recreated. |
| **NFR-10 Code quality** | `ruff check` + `ruff format` clean. Type hints on public functions. CI green before merge. |

---

## 8. UI / UX specification — "Frost" glass

### 8.1 Visual concept
Neutral, cool, frosted liquid glass that takes its colour from what is behind it. The only strong colour is the **accent**, which defaults to the user's Windows accent colour. No orange theme.

### 8.2 Design tokens (`ui/theme.py`)
| Token | Light | Dark |
|---|---|---|
| `glass.tint` | `rgba(255,255,255,0.22)` | `rgba(16,20,28,0.38)` |
| `glass.petal` | `rgba(255,255,255,0.30)` | `rgba(255,255,255,0.08)` |
| `glass.stroke` | `rgba(255,255,255,0.45)` | `rgba(255,255,255,0.14)` |
| `glass.highlight` (top-left inner rim) | `rgba(255,255,255,0.60)` → 0 | `rgba(255,255,255,0.28)` → 0 |
| `text.primary` | `#0B1220` | `#F2F5FA` |
| `text.secondary` | `rgba(11,18,32,0.62)` | `rgba(242,245,250,0.62)` |
| `accent` | Windows accent (DWM `AccentColor`, see §9.6); fallback **Glacier** `#4C8DFF` | same |
| `accent.gradient` | `#4C8DFF → #8B6CFF` (fallback only) | same |
| `petal.hover` | `accent @ 55 %` + specular sheen | `accent @ 65 %` + sheen |
| `shadow` | `0 12px 32px rgba(0,0,0,0.30)` | `0 12px 32px rgba(0,0,0,0.50)` |
| Font | Segoe UI Variable (fallback Segoe UI). Petal labels 11 px semibold, ALL CAPS, letter-spacing 0.4 px. Dialog body 13 px. | |
| Radius | Petal corners 18 px. Dialog 12 px. Buttons 8 px. Segmented control 6 px. | |

The theme follows `HKCU\…\Themes\Personalize\AppsUseLightTheme` unless it is overridden in Settings.

### 8.3 Wheel anatomy (sizes at 100 % scale)
- **Window:** 380 × 380 px, frameless, translucent background. The visible disc has a **300 px** diameter, and the margin holds the painted shadow.
- **Disc:** a glass circle, with a 1 px `glass.stroke` outer stroke.
- **Petals:** N ∈ [2, 8] annular sectors between radius 58 and 142, with a **6 px gap** between petals and 18 px corner rounding. Each has a label, plus an optional 16 px line icon above the label (Tools, Actions, and Launcher wheels).
- **Hub:** 96 px glass circle. It shows a 40 px thumbnail of the first dragged file (shell thumbnail via `QFileIconProvider`) and, below it, the hovered petal's label in a pill (as in the Tangerine reel, where the hub reads "TIFF"). With several files, a count badge appears ("3").
- **Hover:** the petal fills with `petal.hover`, scales to 1.04 about the wheel centre, and gets a radial specular sheen. The transition is 90 ms.
- **More… ring:** outer annulus between radius 150 and 210. The window grows to 460 px while it is open.
- **Disabled petal:** 35 % opacity, with no hover.
- **Petal order:** clockwise from 12 o'clock (FR-2.3).

### 8.4 Liquid-glass rendering recipe (`ui/glass.py`, Qt only, no numpy)
The desktop behind a wheel is effectively static while the wheel is open, so we render glass once per open:
1. **Snapshot:** before showing the window, `BitBlt` the target rect from the screen DC with `SRCCOPY` **without `CAPTUREBLT`**. This leaves out layered windows, so the OLE drag image is not baked in. Convert to `QImage`.
2. **Blur:** scale down to 1/8 with `SmoothTransformation`, then scale back up. Repeat twice, which looks like a ~20 px Gaussian. Increase saturation ×1.3 (cheap HSL pass at 1/8 size).
3. **Refraction rim:** inside the outer 18 px band of the disc, draw the blurred snapshot **magnified ×1.06 about the centre**, clipped to the annulus. This creates the lensing look of curved glass.
4. **Tint:** fill the disc with `glass.tint`, and fill petals with `glass.petal`.
5. **Specular:** a 1 px inner highlight arc from 10 to 2 o'clock (`glass.highlight` fading to transparent), plus the 1 px outer stroke.
6. **Shadow:** a pre-blurred circular shadow sprite (generated once per theme) drawn under the disc.
7. Cache the composed background as a `QPixmap`. Hover and animation layers paint on top, so the snapshot is not recomputed.

### 8.5 Motion
| Event | Animation |
|---|---|
| Wheel open | scale 0.85 → 1.0 and opacity 0 → 1, **140 ms** `OutBack(1.2)`; petals fade in staggered by 12 ms |
| Wheel close | opacity → 0, 90 ms `InCubic` |
| Wheel swap (FR-1.6) | 120 ms cross-fade |
| More… ring | radial expand 160 ms `OutCubic` |
| Toast | slide-up 12 px + fade, 160 ms; auto-hide after 6 s; pauses while hovered |
| Reduced motion | all durations become 0 (instant) |

### 8.6 Dialogs (Crop, Add BG, Custom compress, Extract pages, Settings)
- **Base (`dialogs/base_glass.py`):**
  - `GlassDialog(title, theme)`; every dialog is built as `Dialog(paths, theme)` (App passes `theme.resolve(appearance)`) and has `set_theme()` so it follows Windows light/dark while open.
  - Frameless window with a custom 40 px title bar (centred title, ✕ at left, the way the Tangerine dialogs look).
  - Backdrop: `DwmSetWindowAttribute(DWMWA_SYSTEMBACKDROP_TYPE=38, DWMSBT_TRANSIENTWINDOW=3)` (Acrylic), `DWMWA_USE_IMMERSIVE_DARK_MODE=20` per theme, and `DWMWA_WINDOW_CORNER_PREFERENCE=33 → DWMWCP_ROUND`. The window needs `WA_TranslucentBackground` plus `DwmExtendFrameIntoClientArea(-1)`.
  - **Fallback** (Windows 10, or the attribute fails): an opaque panel `#F3F5F9` (light) / `#1B1F27` (dark) at 96 % alpha.
- **Controls:**
  - Segmented control: pill group; the active segment is filled with the accent.
  - Sliders: accent fill, with a px value readout at the right.
  - Primary button: accent fill with white text.
  - Secondary button: glass petal style.
- **Crop dialog** (reel image 5):
  - Title "Crop Image".
  - Image preview fitted to 560 × 560 max, with a crop rectangle that has 8 handles and a rule-of-thirds overlay while dragging.
  - Aspect ratio segmented control: Free · 1:1 · 16:9 · 9:16 · 4:3 · 3:4.
  - W and H numeric fields in px (editable; they follow the ratio).
  - Footer: **Reset** (left) and **Apply** (right, primary).
  - Output: `<stem> Cropped.<ext>`.
- **Add BG dialog** (reel image 2):
  - Title "Add BG".
  - Live preview of the image on its background.
  - Background swatches, row 1: 8 gradients (Glacier, Aurora, Mint, Dusk, Mono, Peach, Ocean, Candy) + "Image…" (pick a file).
  - Background swatches, row 2: 8 solids + a custom colour picker.
  - **Padding** slider (0–400 px, default 96), **Corners** slider (0–200 px, default 24), **Shadow** slider (0–100, default 24).
  - **Ratio** segmented control: Auto · 1:1 · 16:9 · 4:5.
  - Footer: **Reset**, and **Save with Background** (primary).
  - Output: `<stem> BG.png`.
  - The preview is rendered with QPainter in the UI process, at most 800 px. The final full-resolution render runs in the worker with Pillow, using the same parameters.
- **Custom compress dialog:** target MB *or* quality slider, plus an estimate line.
- **Extract pages dialog:** text field for a page range (`1-3,7`) and a page count hint.

### 8.7 Toasts
- 360 × 72 px glass card, bottom-right, 16 px above the taskbar, stacking upward (max 3).
- Contents: icon, output file name (ellipsised in the middle), a sub-line ("Converted to PNG · 1.2 MB"), and buttons **Open · Show · Undo**.
- Progress variant: an indeterminate or determinate bar plus **Cancel**.

### 8.8 Settings window
- 760 × 520 px, base glass dialog, with a left nav listing the 6 tabs (FR-8.1).
- **Launcher tab:** shows the Apps and Sites wheels as live mini-wheels. Drop a file onto a slot, or click a slot to edit it; drag between slots to reorder.

---

## 9. Architecture & technical design

### 9.1 Tech stack
| Layer | Choice | Why |
|---|---|---|
| Language | **Python 3.12** | The team knows it. It has the richest local conversion libraries, and Claude Code is strong in it. |
| Env / packaging | **uv**, `pyproject.toml` with a src layout, `uv.lock` committed | Setup is `uv sync` and run is `uv run dragshift`. No `.exe`. |
| UI | **PySide6-Essentials** ≥ 6.8: Qt **Widgets** + QPainter (not QML) | Qt implements `IDropTarget` for us. It supports translucent frameless windows and is lighter than QML. QtSvg is included. |
| Win32 | **ctypes** (hooks, DWM, BitBlt, user32) + **pywin32** (Shell COM) | No compiled extensions. |
| Engines (worker only) | Pillow ≥ 11, pillow-heif, PyMuPDF, pdf2docx, pymupdf4llm, python-pptx, pypandoc-binary, imageio-ffmpeg, pandas, openpyxl, odfpy, xlrd, pysubs2, send2trash | Everything is pip-installable and offline. |
| Dev | ruff, pytest, pytest-qt, pre-commit, GitHub Actions `windows-latest` | |

Dependency groups in `pyproject.toml`: the main `dependencies` hold everything above. `[dependency-groups] dev` holds ruff, pytest, pytest-qt, and pre-commit.

`send2trash` is the one engine-row library the UI process uses: Undo (FR-5.6) calls it on the Qt thread, so Undo never waits for a worker pool to start. It is a thin wrapper over the shell's `IFileOperation`, imported only when Undo runs (DS-18).

### 9.2 Process model
```
dragshift  (UI process — always running, lean)
 ├─ HookThread (ctypes)  WH_MOUSE_LL + WH_KEYBOARD_LL + own GetMessage loop
 │     └─ pushes RawInput events → Qt queued signal (O(1), no COM/IO)
 ├─ Qt main thread
 │     ├─ GestureController (state machine §9.5) ── gates (platform/context_guard.py)
 │     ├─ WheelWindow / LauncherWheel / dialogs / toasts / tray / settings
 │     └─ JobManager ──────────────► ProcessPoolExecutor(max_workers=2, mp_context="spawn")
 │                                    • created lazily on the first job
 │                                    • shut down after 60 s with no jobs (→ 0 workers)
 │                                    • workers import engines lazily per Op
 │                                    • ffmpeg / pandoc / tar.exe run as subprocesses
 └─ Never imports heavy engine libs (NFR-1)
```
Progress from workers comes back through a `multiprocessing.Manager().Queue()`, or by polling a small progress file. `jobs.py` hides this choice.

### 9.3 Repository layout
```
pyproject.toml  uv.lock  README.md  LICENSE  CLAUDE.md  CONTRIBUTING.md  .pre-commit-config.yaml  .gitignore
.github/ workflows/ci.yml  CODEOWNERS  ISSUE_TEMPLATE/
docs/ PRD.md  architecture.md  manual-test-checklist.md  jira-import.csv
src/dragshift/
  __init__.py  __main__.py            # argparse CLI: (default) run | --background | convert | tool | ops
  app.py                              # QApplication bootstrap, wiring, single instance, tray
  config.py                           # dataclass schema + load/save/migrate (§10.1)
  history.py                          # jobs history + usage counts (§10.2)
  gesture.py                          # GestureController state machine (§9.5)
  platform/
    hooks.py          # HookThread: LL mouse/keyboard hooks → signals
    drag_detect.py    # SysDragImage + window-class checks (FR-1.2)
    shell.py          # Explorer COM: foreground view, SelectedItems, folder path, desktop view
    context_guard.py  # Quick Launch gates G-a..G-f (FR-3.2)
    screen.py         # BitBlt snapshot (no CAPTUREBLT), monitor work areas
    dwm.py            # backdrop, dark mode, corners, accent colour, light/dark detection
    autostart.py      # HKCU Run key
    files.py          # open a file; show it selected in Explorer (toast Open / Show)
    single_instance.py
  ui/
    theme.py  glass.py  wheel.py  wheel_geometry.py  wheel_model.py  launcher_wheel.py  toast.py  tray.py
    job_toasts.py     # job events → progress / result / error toasts and their buttons
    dialogs/ base_glass.py  crop.py  add_bg.py  compress.py  pages.py  settings.py
  engine/
    kinds.py          # Kind enum + extension → Kind detection
    registry.py       # Op dataclass, @op decorator, ops_for()
    jobs.py           # JobManager, JobContext, naming, .part writes, undo tokens
    presets.py        # size presets (FR-4.3)
    variants.py       # @ring_op: Resize ▸ / Rotate ▸ / Bitrate ▸ outer rings and their params (§6.2)
    converters/ images.py  pdf.py  docs.py  media.py  sheets.py  subtitles.py  archives.py  office.py(stretch)
    tools/      image.py  pdf.py  video.py  audio.py
    actions/    files.py
  launcher/ apps.py  sites.py
tests/ conftest.py  fixtures.py(generated samples)  engine/  ui/  test_naming.py  test_ui_imports.py
```

### 9.4 Core interfaces (the contract between Dev A and Dev B; agree on Day 1, story DS-6)
```python
# engine/kinds.py
class Kind(StrEnum):
    IMAGE = "image"; PDF = "pdf"; DOCUMENT = "document"; SLIDES = "slides"; SHEET = "sheet"
    AUDIO = "audio"; VIDEO = "video"; SUBTITLE = "subtitle"; ARCHIVE = "archive"; FOLDER = "folder"; OTHER = "other"
def kind_of(path: Path) -> Kind: ...

# engine/registry.py
Wheel = Literal["convert", "tools", "actions"]
@dataclass(frozen=True)
class Op:
    id: str                      # stable, e.g. "convert.image.png", "tools.image.compress", "actions.copy_path"
    wheel: Wheel
    label: str                   # petal label, e.g. "PNG", "Compress"
    kinds: frozenset[Kind]       # input kinds accepted
    func: str                    # dotted path "dragshift.engine.converters.images:to_png" (imported lazily in the worker)
    multi: Literal["each", "all"] = "each"
    min_files: int = 1
    icon: str | None = None      # name of an icon in ui/icons
    dialog: str | None = None    # "crop" | "add_bg" | "compress" | "pages" — UI opens it, passes params
    children: tuple[str, ...] = ()   # sub-ops shown in the outer ring (e.g. compress presets)
    where: Literal["worker", "ui"] = "worker"
    available: Callable[[], bool] = lambda: True   # e.g. office installed
    hide_if_same_ext: str | None = None            # hide the "PNG" petal for .png input

def register(op: Op) -> Op: ...
def ops_for(paths: Sequence[Path], wheel: Wheel) -> list[Op]: ...     # intersection + availability + default order;
    # an Op listed in another's children is left out (it is in that Op's ring), and so is an Op whose children all are
def children_for(op: Op, paths: Sequence[Path]) -> list[Op]: ...     # op's outer ring: its valid, available children
def get(op_id: str) -> Op: ...

# Worker-side function signature (every converter/tool):
def run(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]: ...

# engine/jobs.py
class JobContext:
    def output_path(self, src: Path, *, ext: str, label: str | None = None, folder: Path | None = None) -> Path: ...
        # applies FR-5.1..5.3 naming, returns a *.part path; JobManager renames on success
    def out_dir(self, src: Path) -> Path: ...
        # Settings > Output > one folder (FR-8.1) if set, else src's folder; also for "<stem> pages" folders
    def progress(self, fraction: float, text: str = "") -> None: ...
    def cancelled(self) -> bool: ...
    def redirect(self, path: Path) -> Path: ...
        # FR-5.3: the same name in Downloads; any op writing outside output_path calls it on PermissionError
    def reserve(self, base: Path) -> Path: ...
        # the first free base, base (2), … as an empty *.part, in that folder or not at all (Copy to, Move to)
    def copy_text(self, text: str) -> None: ...
        # put text on the clipboard when the job finishes; the UI process does it (Copy path, SHA-256)
    def note(self, text: str) -> None: ...
        # a line for a successful job's toast, e.g. "Couldn't reach 10 MB — got 14.2 MB" (FR-4.4)
    final: list[Path]                 # outputs the op renamed itself; JobManager leaves their names alone
    moves: list[tuple[Path, Path]]    # (from, to) per item Move to moved, for Undo

@dataclass
class JobResult:
    op_id: str; inputs: list[Path]; outputs: list[Path]; ok: bool; message: str; undo: dict
    cancelled: bool = False   # cancelled jobs get no toast and no history entry
    copied: str = ""          # text for the clipboard; the App sets it on the Qt thread
    # message: the failure, or a note on a successful job (it went to Downloads, FR-5.3)

def apply_undo(undo: dict) -> list[Path]: ...   # FR-5.6: reverses JobResult.undo; returns outputs kept because they changed

class JobManager(QObject):
    progressed = Signal(str, float, str)   # job_id, fraction, text
    finished   = Signal(str, object)       # job_id, JobResult
    def submit(self, op_id: str, paths: list[Path], params: dict | None = None) -> str: ...   # returns job_id
        # params["output_folder"] (OUTPUT_FOLDER) goes to JobContext.out_dir, not to the op
    def cancel(self, job_id: str) -> None: ...

# history.py — the one place a job is undone, from its toast or the tray (FR-5.6, FR-5.7)
class History(QObject):
    changed = Signal()
    entries: tuple[HistoryEntry, ...]           # newest first, at most 20
    usage: Mapping[str, Mapping[str, int]]      # input kind -> op id -> successful runs (FR-2.2)
    def record(self, job_id: str, result: JobResult) -> None: ...
    def undo(self, job_id: str) -> list[Path]: ...   # once per job; KeyError after that
    def reset_usage(self) -> None: ...               # Settings > Wheels (FR-8.1); the jobs stay
```
DS-18 moved Undo from `JobManager.undo(job_id)` to `History.undo(job_id)`. History outlives the session and knows whether a job was already undone, so a job can be undone only once, whether from its toast or from the tray.
`ui/wheel.py` exposes `WheelWindow.open(kind: Wheel, at: QPoint, prefill: list[Path])`, `.swap(kind)`, `.close()`, and the signal `dropped(op_id: str, paths: list[Path])`. It also exposes `closed()` (emitted after any close, including a drop anywhere, FR-1.9, so `gesture.py` can go back to IDLE), `set_ordering(pinned, usage)` (FR-2.2: config pins and `history.json` usage counts, applied the next time a wheel opens), and `set_appearance(appearance)` (§8.2, NFR-8: the config's `appearance` section, resolved against the Windows settings each time a wheel opens). Wheel geometry and the petal model live in `ui/wheel_geometry.py` and `ui/wheel_model.py`.
`gesture.py` connects `dropped` → (open a dialog if `op.dialog`) → `JobManager.submit`.

### 9.5 Gesture state machine (`gesture.py`)
```
IDLE ──LMB down──► PRESSED(pos, root_class) ──move > SM_CXDRAG──► DRAGGING
PRESSED/DRAGGING ──LMB up──► IDLE
DRAGGING ──mapped modifier key-DOWN & is_file_drag()──► WHEEL_OPEN(wheel)
WHEEL_OPEN ──other mapped modifier down──► WHEEL_OPEN(other)   (swap)
WHEEL_OPEN ──drop on petal──► JOB_SUBMITTED ──► IDLE
WHEEL_OPEN ──LMB up / Esc / 30 s timeout / drop elsewhere──► IDLE (close)

IDLE ──modifier down (no other keys/buttons held)──► TAP_PENDING(key, t0)
TAP_PENDING ──any other key/button/wheel, or 300 ms elapsed──► IDLE
TAP_PENDING ──same key up < 300 ms──► (double-tap mode? wait ≤ 350 ms for 2nd tap) ──► gates_pass()? ──► LAUNCHER_OPEN
LAUNCHER_OPEN ──click / 1-8 / Esc / outside click / 10 s──► IDLE
```
`is_file_drag()` = FR-1.2. `gates_pass()` = FR-3.2, evaluated on the Qt thread, never inside the hook.

### 9.6 Windows integration notes (for implementers)
- **Hooks:**
  - `SetWindowsHookExW(WH_MOUSE_LL=14 / WH_KEYBOARD_LL=13, proc, GetModuleHandleW(None), 0)` on a dedicated thread that runs `GetMessageW`.
  - Keep a module-level reference to the `CFUNCTYPE` callback, or it is garbage-collected.
  - Always `CallNextHookEx`. **Never swallow events.**
- **Modifier VKs:** Shift `0x10/0xA0/0xA1`, Ctrl `0x11/0xA2/0xA3`, Alt `0x12/0xA4/0xA5` (Alt arrives as `WM_SYSKEYDOWN`).
- **SysDragImage:** `FindWindowW("SysDragImage", None)` + `IsWindowVisible`.
- **Root class:** `GetAncestor(WindowFromPoint(pt), GA_ROOT)` → `GetClassNameW`.
- **Explorer selection:**
  - `win32com.client.Dispatch("Shell.Application").Windows()`; match `w.HWND == foreground_root`; read `w.Document.SelectedItems().Count` and `w.Document.Folder.Self.Path`.
  - Desktop: `IShellWindows.FindWindowSW(CSIDL_DESKTOP, …, SWC_DESKTOP=8, …, SWFO_NEEDDISPATCH=1)`.
  - Call these on the Qt thread with COM initialised (`pythoncom.CoInitialize`).
- **Full-screen check:** `shell32.SHQueryUserNotificationState(byref(state))`.
- **Focused control:** `GetGUIThreadInfo(GetWindowThreadProcessId(fg), byref(info))` → `info.hwndFocus` → class name.
- **Wheel window flags:** `Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus`, with `WA_TranslucentBackground`, `WA_ShowWithoutActivating`, and `setAcceptDrops(True)`.
- **Drop:**
  - `dragMoveEvent` and `dropEvent` accept **only** over enabled petals (`event.acceptProposedAction()` is **forbidden**; use `setDropAction(Qt.CopyAction); accept()`), and `ignore()` everywhere else.
  - `dragEnterEvent` **always** accepts, with `CopyAction` only. Windows sends later moves and the drop only to a target that accepted the enter, and the wheel opens with the cursor on the hub, so an ignored first enter leaves the wheel dead. Qt follows the enter at once with a move, so the effect reported to Explorer is still "none" over the hub (checked against the window's real OLE `IDropTarget`, DS-9).
- **Accent colour:** `HKCU\Software\Microsoft\Windows\DWM\AccentColor` (ABGR), else `DwmGetColorizationColor`. The API can return a darker shade than the accent the user picked (#5E0074 for #680081, measured in DS-12), so it is only the fallback.
- **Store apps list (Launcher picker):** `powershell -NoProfile -Command "Get-StartApps | ConvertTo-Json"`, run in a worker.

---

## 10. Data & configuration

### 10.1 `%APPDATA%\DragShift\config.json` (schema v1)
```json
{
  "version": 1,
  "keys": { "convert": "shift", "tools": "ctrl", "actions": "alt",
            "launch_apps": "shift", "launch_sites": "ctrl", "tap_mode": "single" },
  "wheels": { "pinned": { "image": ["convert.image.png", "convert.image.jpg"] } },
  "launcher": {
    "enabled": true,
    "apps":  [ { "label": "VS Code", "target": "C:\\Users\\me\\AppData\\Local\\Programs\\Microsoft VS Code\\Code.exe" } ],
    "sites": [ { "label": "GitHub", "url": "https://github.com" } ]
  },
  "appearance": { "theme": "system", "accent": "system", "reduced_motion": "system" },
  "output": { "location": "next_to_source", "fixed_folder": null, "jpeg_quality": 85, "pdf_dpi": 300,
              "allow_format_change_for_size": false },
  "folders": { "pinned": [] },
  "general": { "autostart": false, "trim_memory_when_idle": true, "paused": false, "blocklist": [] }
}
```
- Loaded into frozen dataclasses with defaults.
- Unknown keys are preserved.
- An invalid file is backed up to `config.bad.json` and replaced with defaults.
- `version` drives migrations.
- `appearance`: `theme` is `system` | `light` | `dark`; `accent` is `system` or `#RRGGBB`; `reduced_motion` is `system` | `on` | `off`. `system` follows Windows (§8.2, NFR-8).

### 10.2 `%APPDATA%\DragShift\history.json`
```json
{ "jobs":  [ { "id": "…", "op": "convert.image.png", "inputs": ["…"], "outputs": ["…"], "ok": true,
               "message": "", "undo": {"type": "trash", "paths": ["…"], "stamps": [[1234, 1729504931000000000]], "dirs": ["…"]},
               "ts": "2026-10-21T10:02:11", "undone": false } ],
  "usage": { "image": { "convert.image.png": 12, "tools.image.compress": 4 } } }
```
It keeps at most 20 jobs (FIFO), newest first, and writes atomically. Cancelled jobs are not kept. `undo.stamps` holds `[size, mtime_ns]` per path, taken when the job finished. Undo keeps any output whose stamp no longer matches, because it is no longer the file the job wrote. `undo.dirs` (optional) lists the output folders the job itself made, such as `<stem> pages`, but not parents created on the way, such as a missing Downloads. Undo removes them once they are empty. Undo refuses outputs on network and removable drives: there is no Recycle Bin there, and the shell would delete them for good without asking. A Move to job has `{"type": "move", "moves": [[from, to], …], "stamps": […]}` instead: Undo moves each item back, unless it changed since or its old place is taken again. `undone` is set after an Undo, so a job is undone only once. `usage` counts successful jobs whose inputs all share one kind (the wheel's ordering key). An unreadable file is moved to `history.bad.json`.

---

## 11. Testing strategy

### 11.1 Automated (CI on `windows-latest`, required to merge)
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest -q`.
- **Engine round-trips:** fixtures are generated at test time:
  - Pillow-drawn PNG/JPG/HEIC.
  - A PyMuPDF-built 3-page PDF.
  - A pandoc-built DOCX/MD.
  - An ffmpeg `lavfi` 2 s test tone and video.
  - CSV/XLSX.
  - SRT.
  - ZIP.

  Each Op asserts that the output exists, opens, has the expected format/page count/duration, and that **the source bytes are unchanged** (hash before and after).
- **Naming:** collision ` (2)`, label suffixes, the read-only folder → Downloads fallback (monkeypatched).
- **Registry:** every extension in §6 maps to a Kind; every Kind has ≥ 2 convert Ops; `ops_for` intersection logic; hide-same-ext.
- **Presets:** the target-bitrate maths; best-effort reporting (FR-4.4).
- **UI (pytest-qt, offscreen):** wheel geometry (petal angles, hit-test at the centre of each petal, gaps and hub return `None`), More… ring, smart ordering, theme tokens.
- **`test_ui_imports.py`:** import `dragshift.app` in a subprocess and assert that `PIL`, `fitz`, `pandas`, `numpy`, `pdf2docx`, and `pypandoc` are **not** in `sys.modules` (NFR-1).
- **Gesture:** the state machine is unit-tested with synthetic event sequences (no real hooks).

### 11.2 Manual checklist (`docs/manual-test-checklist.md`; tick the relevant items in every PR)
1. Desktop: drag JPG → Shift → drop on PNG → the PNG exists **and the JPG is still in place** (not moved).
2. Explorer window: the same, then with 3 files selected → 3 outputs.
3. Ctrl wheel → Compress → Discord preset on a video → ≤ 10 MB or a best-effort message.
4. Alt wheel → Copy path on a folder.
5. Swap Shift → Ctrl → Alt mid-drag; Esc cancels; dropping on the hub or outside does nothing.
6. Drag text from a browser + Shift → "Unsupported" (or no wheel).
7. Tap Shift on an empty desktop → Apps wheel; tap Ctrl in an empty Explorer → Sites wheel.
8. Tap Shift with a file selected, while renaming, in Notepad, and in a full-screen game → **nothing**.
9. Second monitor + 150 % scaling: the wheel is centred and clamped.
10. Light ↔ dark switch in Windows → the wheel and dialogs follow.
11. Undo from the toast and from History.
12. Autostart on → reboot → running. `--background` leaves no console.
13. Idle 2 min → private bytes ≤ 60 MB (`Get-Process pythonw | Select-Object Id,PM,WS`), and no worker processes.

---

## 12. Delivery plan (Sprint 1)

**Ownership:**
- **Dev A — Platform:** hooks, gesture, Explorer/COM, Quick Launch, file actions, media/sheets/archives, tray, settings, lifecycle.
- **Dev B — UI + Engine:** wheel, glass, theme, registry/jobs, image/PDF/docs converters, tools, dialogs, memory.

Each person assigns themselves A or B in Jira.

**Points:** 1 pt ≈ 1–2 focused hours with Claude Code. Committed: A ≈ 46 pts, B ≈ 45 pts, shared 5 pts.

**Mid-sprint gate (end of Day 4):** if a dev's remaining committed points exceed their remaining capacity, cut work in this order: DS-23 extras (video resize, audio bitrate), then DS-21 hashing, then DS-27 W/H fields. Stretch stories start **only** when all of a dev's committed stories are In Review or Done.

### 12.1 Stories (committed)
| ID | Day | Owner | Pts | Epic | Story | Acceptance criteria (summary) | FRs |
|---|---|---|---|---|---|---|---|
| DS-1 | 0 | A | 1 | E1 Foundation | Planning docs: PRD, CLAUDE.md, Jira import | PRD merged, CLAUDE.md present, CSV imports cleanly | — |
| DS-2 | 1 | A | 2 | E1 | Repo scaffold | uv project with src layout; `uv run dragshift --help` works; ruff + pytest + pre-commit configured; LICENSE AGPL-3.0; `.gitignore` | FR-6.1 |
| DS-3 | 1 | A | 2 | E1 | CI + PR template + CODEOWNERS | GitHub Actions on `windows-latest` runs ruff and pytest; PR template with checklist; CODEOWNERS requests the other dev | NFR-10 |
| DS-4 | 1 | A | 5 | E2 Gesture | **Spike:** hooks + drag detection + COPY-only drop window | Throwaway script: logs drag candidate + modifier; shows a test drop window; dropping a file with Shift held leaves the **source in place**; notes on `SysDragImage` reliability and DragEnter timing in the PR | FR-1.1, 1.2, 1.8 |
| DS-5 | 1 | B | 3 | E2 | **Spike:** glass wheel prototype + RAM baseline | Static wheel rendered with the §8.4 recipe over a snapshot; screenshot in the PR; idle RAM of a bare PySide6 app recorded | §8.4, NFR-1 |
| DS-6 | 1 | B | 3 | E3 Conversions | Engine contract: kinds, registry, JobManager skeleton | §9.4 interfaces implemented; one sample Op (PNG→JPG) runs end-to-end through the worker pool; pool shuts down after 60 s idle; tests | FR-4.1, 4.2 |
| DS-7 | 2 | A | 3 | E2 | HookThread → Qt bridge | LL hooks on their own thread; queued signals; injected events ignored; watchdog re-install; < 1 ms callbacks | FR-1.1, NFR-3 |
| DS-8 | 2 | A | 2 | E7 App shell | Config module | §10.1 schema, load/save/migrate, bad-file backup, live-reload signal; tests | FR-8.2 |
| DS-9 | 2 | B | 5 | E2 | Wheel widget | 2–8 petals, geometry and hit-test, hover, hub label and thumbnail, More… ring, disabled state, drop handling per FR-1.8, `dropped` signal; pytest-qt tests | FR-1.7–1.10, 2.1–2.3 |
| DS-10 | 3 | A | 5 | E2 | Gesture state machine | §9.5 drag branch; `is_file_drag`; opens/swaps/closes the wheel; Explorer selection pre-fill; pause; unit tests with synthetic events | FR-1.2–1.6, 1.9, 1.12 |
| DS-11 | 3 | A | 2 | E2 | Drop safety, Esc, LMB-up, timeout | Manual checklist items 1, 5, 6 pass; never MoveAction | FR-1.8, 1.9 |
| DS-12 | 3 | B | 5 | E2 | Glass renderer + theme | §8.2 tokens, §8.4 recipe, light/dark, Windows accent, open/close/swap motion, reduced motion | §8, NFR-8 |
| DS-13 | 3 | B | 3 | E3 | Image converters | §6.1 image row incl. HEIC, AVIF, SVG input, ICO, Merge to PDF; round-trip tests | §6.1 |
| DS-14 | 4 | A | 3 | E3 | Media converters (ffmpeg) | §6.1 audio and video rows; progress parsing from `-progress pipe:1`; cancel kills the process | §6.1, FR-4.5 |
| DS-15 | 4 | A | 5 | E6 Quick Launch | Tap detection + context gates | Tap and double-tap; gates G-a…G-f; manual checklist item 8 passes | FR-3.1, 3.2 |
| DS-16 | 4 | B | 3 | E3 | PDF converters | DOCX, PNG/JPG at the config DPI into a pages folder, TXT, MD, PPTX | §6.1 |
| DS-17 | 4 | B | 2 | E3 | Document converters (pandoc) | §6.1 document row (except PDF) | §6.1 |
| DS-18 | 4 | B | 3 | E3 | Output naming, toasts, undo, history, usage counts | FR-5.1–5.7; `.part` + atomic rename; history.json; usage counts feed smart ordering | FR-5, FR-2.2 |
| DS-19 | 5 | A | 3 | E6 | Apps + Sites wheels | Launcher wheel UI that reuses the Wheel widget; launch exe/lnk/folder/AUMID/URL; favicon cache; 1–8 keys | FR-3.3–3.6 |
| DS-20 | 5 | A | 3 | E3 | Sheets, subtitles, archives converters | §6.1 rows; RAR/7z via `tar.exe`; folder → ZIP | §6.1 |
| DS-21 | 5 | A | 3 | E5 File actions | Actions wheel | §6.3 actions incl. pinned folders, undo for Move/Copy-to | §6.3, FR-5.6 |
| DS-22 | 5 | B | 3 | E4 Tools | Compress + size presets | FR-4.3/4.4 for image, PDF, video, audio; preset outer ring; Custom dialog hook | FR-4.3, 4.4 |
| DS-23 | 5 | B | 3 | E4 | Other tools | Image resize/rotate/strip metadata; PDF merge/split/extract/rotate/strip; video mute/extract audio/GIF/resize; audio normalize/bitrate | §6.2 |
| DS-24 | 5 | B | 2 | E4 | Acrylic dialog base | §8.6 base, Win10 fallback, title bar, controls styled | §8.6 |
| DS-25 | 6 | A | 5 | E7 | Tray + Settings window | FR-7.1, FR-8.1 all tabs, live apply, Launcher editor | FR-7.1, 8.1 |
| DS-26 | 6 | A | 2 | E7 | Autostart, single instance, CLI, background | FR-6.2–6.5, FR-7.2 | FR-6, 7.2 |
| DS-27 | 6 | B | 3 | E4 | Crop dialog | §8.6 Crop spec | §8.6 |
| DS-28 | 6 | B | 5 | E4 | Add BG dialog | §8.6 Add BG spec; preview in UI, final render in worker | §8.6 |
| DS-29 | 6 | B | 2 | E7 | Idle-RAM pass | `test_ui_imports` passes; working-set trim option; numbers recorded; M1 met | NFR-1 |
| DS-30 | 7 | Both | 2 | E8 Release | README, docs, demo GIFs | README with install/run/usage GIFs; `docs/architecture.md`; manual checklist up to date | — |
| DS-31 | 7 | Both | 3 | E8 | Bug bash | Full §11.2 checklist on both machines; bugs filed and fixed or deferred | M5 |
| DS-32 | 7 | A | 1 | E8 | Release v0.1.0 | Tag `v0.1.0`, GitHub release notes, sprint review + retro notes in Jira | — |

### 12.2 Stretch (only after the Day 4 gate)
| ID | Owner | Pts | Story |
|---|---|---|---|
| DS-33 | B | 3 | Office/LibreOffice → PDF (docs, slides, sheets) + slides → PNG/JPG |
| DS-34 | A | 3 | Keyboard alternative: a global hotkey (default `Ctrl+Alt+Space`) opens the Convert wheel for the Explorer selection; arrows, Enter, Tab, Esc |
| DS-35 | A | 3 | Float shelf action |
| DS-36 | B | 5 | Redact (PDF text search + image box redaction) |
| DS-37 | B | 5 | Annotate (arrows, boxes, text on images) |
| DS-38 | A | 3 | Trim dialog for audio/video |
| DS-39 | B | 3 | Edit adjustments (brightness/contrast/saturation) |

### 12.3 Day-by-day view
| Day | Dev A | Dev B |
|---|---|---|
| 0 (Mon 19) | DS-1 | — |
| 1 (Tue 20) | DS-2, DS-3, DS-4 | DS-5, DS-6 |
| 2 (Wed 21) | DS-7, DS-8 | DS-9 |
| 3 (Thu 22) | DS-10, DS-11 | DS-12, DS-13 |
| 4 (Fri 23) | DS-14, DS-15 · **scope gate** | DS-16, DS-17, DS-18 |
| 5 (Sat 24) | DS-19, DS-20, DS-21 | DS-22, DS-23, DS-24 |
| 6 (Sun 25) | DS-25, DS-26 | DS-27, DS-28, DS-29 |
| 7 (Mon 26) | DS-30, DS-31, DS-32 | DS-30, DS-31 |

**Dependencies:**
- DS-6 → DS-13/14/16/17/20/22/23.
- DS-9 → DS-10, DS-19.
- DS-7 → DS-10, DS-15.
- DS-8 → DS-15, DS-19, DS-25.
- DS-18 → DS-21.
- DS-24 → DS-25, DS-27, DS-28.

Until a dependency merges, work against its interface in §9.4 (stub it).

---

## 13. Engineering workflow
- **Branches:** `main` is protected (PR + 1 approval + CI green; squash merge only). Work happens in short-lived branches named `<type>/DS-<n>-<slug>`, e.g. `feat/DS-13-image-converters`. Types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `spike`.
- **Commits:** Conventional Commits that include the Jira key, e.g. `feat(engine): add HEIC to JPG [DS-13]`.
- **PR title:** `DS-13 feat(engine): image converters`. The description covers: what changed, FRs covered, screenshots/GIFs for UI work, manual checklist items ticked, and RAM numbers when relevant.
- **Reviews:** the other dev reviews within 4 working hours. Run Claude Code `/code-review` before requesting a human review.
- **Definition of Done:** acceptance criteria met · tests added/updated · CI green · 1 approval · relevant manual checklist items ticked · docs/PRD updated if behaviour changed · Jira ticket in Done (automatic on merge).
- **Rules for AI agents (Claude Code):**
  - Read `CLAUDE.md` and the story's PRD rows first.
  - Never push to `main`.
  - Keep each PR to one story.
  - Do not add dependencies not listed in §9.1 without noting it in the PR.
  - Never import engine libraries in UI modules.

---

## 14. Risks & mitigations
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `SysDragImage` is missing for some drag sources | Med | Med | Fallback gate (b) on Explorer window classes; DS-4 spike measures this on Day 1 |
| `DragEnter` only fires after the next mouse move | High | Low | Pre-fill from the Explorer selection (FR-1.5) |
| Explorer deletes the source on a Shift-drop | Med | **Critical** | FR-1.8 CopyAction only; manual checklist item 1 in every gesture PR |
| LL hook removed by a Windows timeout | Low | High | No work in callbacks; watchdog re-install (NFR-3) |
| Quick Launch false triggers | Med | Med | Strict gates; double-tap mode; blocklist |
| Idle RAM above 60 MB | Med | Med | Import discipline test; worker pool shutdown; working-set trim |
| AVIF unsupported by the installed Pillow | Med | Low | ffmpeg fallback |
| pandoc/ffmpeg binaries missing | Low | Med | Bundled through `pypandoc-binary` / `imageio-ffmpeg` wheels |
| Week-long timeline slips | Med | Med | Day 4 scope gate; stretch list kept separate; small PRs |

---

## 15. References
- Tangerine for Mac: https://tangerineformac.com/
- MacGeneration review (Oct 2026): https://www.macg.co/logiciels/2026/10/tangerine-un-convertisseur-de-fichiers-toujours-portee-de-main-sur-mac-311381
- Liquid glass via displacement maps (LogRocket): https://blog.logrocket.com/how-create-liquid-glass-effects-css-and-svg
- Building Glass for the Web (Aave): https://aave.com/design/building-glass-for-the-web
- DWM system backdrop (Mica/Acrylic) in Win32 apps: https://windowslatest.com/2022/06/02/windows-11-22h2-is-bringing-mica-acrylic-design-to-more-win32-desktop-apps/amp
- win32mica (Python Mica/Acrylic reference): https://pypi.org/project/win32mica/1.9
- Detecting system-wide drags via `SysDragImage` (Microsoft Q&A): https://learn.microsoft.com/en-us/answers/a/2020151
