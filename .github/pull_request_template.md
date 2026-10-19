<!-- PR title: `DS-13 feat(engine): image converters`. One story per PR. -->

## Story
DS-<n>: <!-- title -->

## What changed
<!-- A few bullets. Say why, not only what. -->

## FRs / NFRs covered
<!-- e.g. FR-1.2, FR-1.8, NFR-10 -->

## Screenshots / GIFs
<!-- Required for UI work. Write "n/a" otherwise. -->

## RAM numbers
<!-- Required when the change could affect idle memory (NFR-1). Write "n/a" otherwise. -->

## Manual checklist
Tick the items this change affects. Steps are in [`docs/manual-test-checklist.md`](../blob/main/docs/manual-test-checklist.md). Every gesture PR must tick item 1.

- [ ] 1. Shift-drop on PNG: output exists and the source file is still in place
- [ ] 2. Explorer window, 3 files selected: 3 outputs
- [ ] 3. Ctrl wheel: Compress → Discord preset
- [ ] 4. Alt wheel: Copy path on a folder
- [ ] 5. Swap modifiers mid-drag; Esc cancels; hub and outside do nothing
- [ ] 6. Drag text from a browser: "Unsupported" or no wheel
- [ ] 7. Tap Shift / Ctrl on empty desktop or Explorer: Quick Launch wheels
- [ ] 8. Tap Shift while renaming, in Notepad, in a full-screen game: nothing happens
- [ ] 9. Second monitor at 150 %: wheel centred and on screen
- [ ] 10. Light/dark switch: wheel and dialogs follow
- [ ] 11. Undo from the toast and from tray → History
- [ ] 12. Autostart + reboot; `--background` leaves no console window
- [ ] 13. Idle 2 min: private bytes ≤ 60 MB, no worker processes
- [ ] None apply (docs, CI or tooling only)

## Definition of Done
- [ ] Acceptance criteria in PRD §12 are met
- [ ] Tests added or updated
- [ ] CI is green
- [ ] `/code-review` run before asking for review
- [ ] Docs and PRD updated if behaviour changed

## Rules check
- [ ] No source file is overwritten, moved, or deleted (CLAUDE.md rule 4)
- [ ] UI modules import no engine libraries (rule 2)
- [ ] PRD §9.4 interfaces unchanged, or the PRD is updated in this PR and described below (rule 5)
- [ ] No new dependencies, or each one is justified below (rule 6)

<!-- Interface or PRD changes, new dependencies and why: -->
