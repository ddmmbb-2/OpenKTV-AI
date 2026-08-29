# Changelog

All notable changes to this project are documented in this file.

## v1.1.1 - 2026-08-21

### Added
- Song Ordering (Sort by Artist) now supports artist-group rename from the UI.
- Added batch metadata API: `POST /api/metadata/artist-batch`.

### Changed
- Updated application version to `1.1.1`.
- Added release-notes entry point in `README.md`.

### Fixed
- Fixed Flask route collision for `artist-batch` by forwarding from `/api/metadata/<filename>`.
- Fixed GUI variable shadowing issue where Tk app instance could override Flask app reference.
- Confirmed Demucs can run on CUDA when using CUDA-enabled PyTorch environment.

## v1.1.0 - 2026-08-21

### Added
- Admin Demucs model setting (`htdemucs` default, `mdx_q`, `auto`) with persisted backend API.
- Improved extension queue handling and idempotency behavior for repeated add-song requests.
- Added local-library path management and rescan UX improvements.

### Changed
- Default Demucs model switched to `htdemucs`.
- Extension and admin integration refinements for stability.

### Fixed
- Reduced duplicate queue/download triggers from extension retries.
- Improved startup/open behavior and extension feedback flow.
