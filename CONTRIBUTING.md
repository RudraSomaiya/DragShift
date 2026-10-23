# Contributing to DragShift

Thanks for helping. This page has the commands, the rules every change must follow, and the Git workflow.

[`docs/PRD.md`](docs/PRD.md) is the spec. Before you change behaviour, read the requirement (FR/NFR) that covers it. If your change disagrees with the PRD, say so in the pull request and update the PRD in the same PR.

## Commands

```powershell
uv sync                          # install dependencies, including the dev tools
uv run dragshift                 # run with console logs; Ctrl+C quits
uv run dragshift --background    # run detached via pythonw
uv run dragshift ops <file>      # list the operations valid for a file
uv run pytest -q                 # tests
uv run ruff check . ; uv run ruff format .
```

## Rules

1. **Never report `MoveAction` on a drop.** Over an enabled petal, call `event.setDropAction(Qt.CopyAction)` and `event.accept()`. Everywhere else, call `event.ignore()`. Never call `acceptProposedAction()`. If the target reports a move, Explorer deletes the user's file (PRD FR-1.8).
2. **Keep the UI process lean.** Modules under `dragshift/ui` and `dragshift/platform`, and `gesture.py` and `app.py`, never import Pillow, fitz (PyMuPDF), pandas, numpy, pdf2docx or pypandoc. Heavy work runs in worker functions that `Op.func` names by dotted path (PRD §9.2, §9.4). The lean-import tests enforce this.
3. **Hook callbacks do O(1) work.** No COM, file I/O or logging inside them. Emit a queued Qt signal and return `CallNextHookEx`. Never swallow input.
4. **Never overwrite, move or delete a source file.** Write to a `*.part` file from `JobContext.output_path()` and let `JobManager` rename it.
5. **Keep to the interfaces in PRD §9.4.** Changing one is a PRD change: update the PRD in the same PR and say so in the description.
6. **Only use the dependencies listed in PRD §9.1.** If you need a new one, explain why in the PR.
7. **Stay offline.** The only network request allowed is the one-time favicon fetch for a Quick Launch site.

## Git workflow

`main` is protected. Every change goes through a pull request with one approval and a green CI run, and is squash-merged.

- **Branch:** `<type>/DS-<n>-<slug>`, for example `feat/DS-13-image-converters`. Types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`.
- **Commits:** Conventional Commits with the Jira key, for example `feat(engine): add HEIC to JPG [DS-13]`.
- **PR title:** `DS-13 feat(engine): image converters`. Keep one story per PR. In the description, say what changed and which FRs it covers, and paste the items from [`docs/manual-test-checklist.md`](docs/manual-test-checklist.md) that your change affects, ticked.

## Where things live

PRD §9.3 has the full layout. The modules you will touch most:

| Module | What it does |
|---|---|
| `gesture.py` | The gesture state machine |
| `platform/hooks.py` | Low-level mouse and keyboard hooks |
| `platform/shell.py` | Explorer COM: the selection and the folder in front |
| `ui/wheel.py`, `ui/glass.py` | The wheel and its glass rendering |
| `engine/registry.py` | Every operation (`Op`) the wheels offer |
| `engine/jobs.py` | Worker pool, output naming, undo |

## Style

- Ruff defaults, line length 100, and type hints on public functions.
- Keep modules small. Comment Win32 calls and name their constants. Don't comment obvious Python.
- Tests generate their fixtures at run time. Don't commit large binary samples.
