# CHANGELOG

## Unreleased

* Add opt-in `DELETE_LOCKED` / `--delete-locked`, disabled by default and effective only with `DELETE_AFTER_SYNC` / `--delete-after-sync`.
* Preserve HTML sync and deletion with one XML metadata lookup per eligible sync, cached for all candidates. Use the exact unambiguous XML path and size; skip deletion when metadata or the local copy cannot be verified.
* Send one fully encoded delete request per eligible file without path guessing or destructive retries, and pace camera HTTP requests at least one second apart, including failures.
* Add standard-library tests using synthetic files and mocked camera responses for deletion flags, download failures, HTML metadata resolution, exact delete requests, dry-run safety, and request pacing.
* Always defer the newest video capture during Wi-Fi sync while recording is active or unknown. Guard all camera channels, parking, and RO files from one complete listing snapshot before user filters; reassess on the next run without changing camera recording state.
* Include timestamp-skewed companion lenses within the existing 10-second/3-sequence pairing limits. Read recording state once per nonempty snapshot, scan all five HTML directories before selection, and pace camera requests at least one second apart even after failures.

## 1.6.1 (2026-10-03)

* Camera stacking pairs each front file with the nearest interior and rear file (within 10 seconds and 3 sequence numbers) instead of requiring the same timestamp and F/I/R order, so parking clips numbered I, F, R and snapshots stamped a few seconds apart are stacked too.

## 1.6 (2026-10-02)

* Several machines can stack the same library at once: each group is claimed with a `.stacklock` file, and `STACK_ORDER` / `--stack-order oldest` lets a second worker start from the other end.
* Add NVIDIA (`nvenc`), Intel QSV (`qsv`), AMD (`amf`) and Apple (`videotoolbox`) encoders, picked automatically when VAAPI is not available.
* `STACK_WORKDIR` / `--stack-workdir` copies inputs to a local folder before encoding, for workers reading the NAS over the network.

## 1.5.4 (2026-10-02)

* VAAPI stacking uses constant QP (`STACK_QP`, default 26, ~50 Mbps); the Intel low-power encoder ignores bitrate targets at 3840x3240.

## 1.5.3 (2026-10-02)

* Set VAAPI rate control to VBR explicitly so `STACK_BITRATE` is honoured (the driver otherwise used constant QP at ~300 Mbps).

## 1.5.2 (2026-10-02)

* Use the low-power (VDENC) H.264 VAAPI encoder; Intel Gen12 iGPUs reject 3840x3240 frames on the default encoder.

## 1.5.1 (2026-10-02)

* Run camera stacking even when the dashcam is unreachable and Wi-Fi sync fails.

## 1.5 (2026-10-02)

* Add camera stacking via `STACK_CAMERAS` / `--stack-cameras`: combine separate front/interior/rear (`F`/`I`/`R`) recordings into one 3840x3240 video matching the camera's stacked layout.
* Hardware encoding on Intel iGPUs via VAAPI (`/dev/dri`), with CPU fallback. Docker image now installs the Intel media driver on amd64.
* Stacked originals are kept under `_separate/` or deleted, and recorded in `.viofosync-stacked` so they are not synced or imported again.

## 1.4.1 (2026-04-30)

* Try Viofo delete-after-sync requests with multiple camera path formats so HTML-mode paths can be deleted on more camera firmwares.

## 1.4 (2026-04-29)

* Add Viofo photo (`.jpg` / `.jpeg`) sync and local import support, including the `/DCIM/Photo` HTML directory.
* Keep GPS extraction and chunk merging video-only.
* Add `DESTINATION` environment support for faster same-volume local imports.
* When both `IMPORT_SOURCE` and `ADDRESS` are set, fall back to Wi-Fi sync when the import source has no Viofo media files.

## 1.3 (2026-04-29)

* Add local import mode via `IMPORT_SOURCE` / `--import-source` for organizing recordings from a mounted SD card, SSD, or copied folder without Wi-Fi sync.
* Add optional `MOVE_IMPORTED` / `--move-imported` to remove local import source files after destination verification.
* Add optional chunk merging via `MERGE_CHUNKS` / `--merge-chunks` using `ffmpeg`, with configurable merge gap, output directory, and source cleanup.
* Merge only normal driving recordings (`F`/`R`), leave parking recordings (`PF`/`PR`) as individual files, respect grouping boundaries, and default chunk merging to a strict 2 second continuity gap.
* Harden delete-after-sync so camera deletion only runs when the local file size was verified.
* Fix `RUN_ONCE` container exit handling after successful one-shot runs.
* Clean up Docker runtime configuration, boolean environment parsing, README, stale helper files, and disk-usage enforcement.

## 1.2 (2026-04-28)

* Add `DELETE_AFTER_SYNC` / `--delete-after-sync`: optionally delete each file from the camera immediately after it has been successfully downloaded and the local copy verified. Read-only/locked files (`/RO/` folder or attr=33) are always skipped. Deletion is safe under `--dry-run`.

## 1.1

* Make download attempts configurable via `DOWNLOAD_ATTEMPTS` / `--download-attempts`

## 1.0 (2024-09-18)

* initial release
