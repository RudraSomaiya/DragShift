# DragShift

DragShift lets you drag a file and tap a key to convert it, compress it, crop it, and more. Everything runs locally, on Windows.

It is an open-source Windows take on [Tangerine for Mac](https://tangerineformac.com/):

| Gesture | Wheel |
|---|---|
| Drag a file + **Shift** | **Convert:** PNG, JPG, WEBP, PDF, DOCX, MP4, MP3… |
| Drag a file + **Ctrl** | **Tools:** compress (Email / WhatsApp / Discord presets), crop, add background, resize… |
| Drag a file + **Alt** | **Actions:** copy path, open terminal here, move/copy to pinned folders, zip… |
| Tap **Shift** on an empty desktop | **Quick Launch:** your favourite apps |
| Tap **Ctrl** on an empty desktop | **Quick Launch:** your favourite websites |

> 🚧 Under active development (Sprint 1 → v0.1.0). See [`docs/PRD.md`](docs/PRD.md) for the full spec.

## Quick start (once v0.1.0 lands)
```powershell
uv sync
uv run dragshift              # run with logs
uv run dragshift --background # run quietly in the tray
```

## Contributing
Read [`CLAUDE.md`](CLAUDE.md) and [`docs/PRD.md`](docs/PRD.md) §13. `main` is protected, so all work goes through PRs.

## License
AGPL-3.0
