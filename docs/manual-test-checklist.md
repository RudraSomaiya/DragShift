# Manual test checklist

Source: PRD §11.2. In every PR, tick the items your change affects and paste them into the PR description. For the bug bash (DS-31), run all of them on both machines.

- [ ] 1. Desktop: drag a JPG, press **Shift**, drop on **PNG**. The PNG exists, **and the JPG is still in place (not moved)**.
- [ ] 2. Explorer window: same as item 1. Then repeat with 3 files selected; you should get 3 outputs.
- [ ] 3. **Ctrl** wheel: Compress → **Discord** preset on a video. The output is ≤ 10 MB, or the toast shows a best-effort message.
- [ ] 4. **Alt** wheel: Copy path on a folder. The clipboard holds the path.
- [ ] 5. Swap Shift → Ctrl → Alt during one drag. **Esc** cancels. Dropping on the hub or outside the wheel does nothing.
- [ ] 6. Drag text from a browser and press Shift. You see "Unsupported" (or no wheel).
- [ ] 7. Tap **Shift** on an empty desktop: the Apps wheel opens. Tap **Ctrl** in an empty Explorer window: the Sites wheel opens.
- [ ] 8. Tap Shift with a file selected, while renaming a file, in Notepad, and in a full-screen game. **Nothing happens** in any of these.
- [ ] 9. On a second monitor at 150 % scaling, the wheel is centred on the cursor and stays inside the screen.
- [ ] 10. Switch Windows between light and dark. The wheel and dialogs follow.
- [ ] 11. Undo works from the toast and from tray → History.
- [ ] 12. Turn autostart on and reboot: DragShift is running. `--background` leaves no console window.
- [ ] 13. Leave it idle for 2 min. Private bytes are ≤ 60 MB (`Get-Process pythonw | Select-Object Id,PM,WS`), and no worker processes are running.
