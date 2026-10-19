# CLAUDE.md — DragShift

DragShift is a Windows background app. The user drags a file and taps Shift, Ctrl, or Alt, and a glass wheel appears around the cursor. Dropping the file on a petal converts it or runs a tool or action on it. Tapping Shift or Ctrl on an empty desktop opens Quick Launch wheels for apps and websites. The stack is Python 3.12, uv, and PySide6 (Qt Widgets). It runs on Windows only. License: AGPL-3.0.

**`docs/PRD.md` is the source of truth.** Before working on a story `DS-<n>`, read its row in PRD §12 and every FR/NFR it references. If the code you are about to write disagrees with the PRD, stop and say so.

## Commands
```powershell
uv sync                          # install deps (incl. dev group)
uv run dragshift                 # run with console logs
uv run dragshift --background    # run detached via pythonw
uv run dragshift ops <file>      # list ops valid for a file
uv run pytest -q                 # tests
uv run ruff check . ; uv run ruff format .
```

## Hard rules
1. **Never report `MoveAction` on a drop.** Use `event.setDropAction(Qt.CopyAction); event.accept()` over enabled petals, and `event.ignore()` everywhere else. Never use `acceptProposedAction()`. If you get this wrong, Explorer deletes the user's file (PRD FR-1.8).
2. **The UI process must stay lean.** Modules under `dragshift/ui`, `dragshift/platform`, `gesture.py`, and `app.py` must never import Pillow, fitz (PyMuPDF), pandas, numpy, pdf2docx, or pypandoc. Heavy work runs in worker functions referenced by dotted path in `Op.func` (PRD §9.2, §9.4). `tests/test_ui_imports.py` enforces this.
3. **Hook callbacks do O(1) work.** No COM, I/O, or logging inside them. Emit a queued Qt signal and return `CallNextHookEx`. Never swallow input.
4. **Never overwrite, move, or delete source files.** Write `*.part` files through `JobContext.output_path()` and let JobManager rename them.
5. **Keep to the interfaces in PRD §9.4.** Changing one is a PRD change: update the PRD in the same PR and mention it in the description.
6. **Only use the dependencies listed in PRD §9.1.** To add a new one, justify it in the PR.
7. **Stay offline.** The only network call allowed is the one-time favicon fetch.

## Git workflow (main is protected)
- Branch: `<type>/DS-<n>-<slug>`, e.g. `feat/DS-13-image-converters`. Never commit to `main`.
- Commit: Conventional Commits that include the key, e.g. `feat(engine): add HEIC to JPG [DS-13]`.
- PR title: `DS-13 feat(engine): image converters`. One story per PR. Fill in the PR template, including the manual checklist items from `docs/manual-test-checklist.md`.
- Run `/code-review` before asking the teammate for review.

## Layout
See PRD §9.3. Key modules:
- `gesture.py`: state machine
- `platform/hooks.py`: LL hooks
- `platform/shell.py`: Explorer COM
- `ui/wheel.py`, `ui/glass.py`: the wheel and its glass rendering
- `engine/registry.py`: Ops
- `engine/jobs.py`: worker pool, naming, undo

## Style
- Ruff defaults, line length 100, type hints on public functions.
- Small modules. Comment the Win32 calls (with constants named), not obvious Python.
- Tests: generate fixtures at test time. Never commit large binary samples.
