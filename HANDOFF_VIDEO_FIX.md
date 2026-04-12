# Video Capture Fix — Handoff Document

## What Was Done

### Root Causes Found
1. **Manual recording** used `-c copy -movflags frag_keyframe+empty_moov` (fragmented MP4 with empty moov atom — editors see 0/0 frames)
2. **Detection recording** had no `-g` flag (sparse keyframes, hard to seek/edit)
3. **Timelapse recording** missing `-pix_fmt yuv420p`, `-g`, `-movflags +faststart`
4. **Trim endpoint** used `-c copy` which inherited broken metadata from source
5. **transit_analyzer.py line 391** had dead `cv2.VideoWriter()` never assigned to a variable
6. **B-frames** (discovered during testing): libx264 default produces ~70% B-frames. HTML5 video seeking in Chromium/Electron cannot reliably land on B-frames, causing the viewer to only reach I+P frames (the "72 frame limit")

### Code Changes Made (4 files)

**src/telescope_routes.py:**
- Manual recording (~line 1942): replaced `-c copy -movflags frag_keyframe+empty_moov` with full libx264 re-encode: `-c:v libx264 -preset fast -crf 23 -bf 0 -pix_fmt yuv420p -g 30 -an -movflags +faststart`
- Timelapse recording (~line 1915): added `-bf 0 -pix_fmt yuv420p -g 1 -an -movflags +faststart`
- Recording Popen: changed `stderr=subprocess.DEVNULL` to `stderr=subprocess.PIPE` (existing stop handler already reads it)
- Trim endpoint (~line 2967): replaced `-c copy` with full libx264 re-encode, moved `-ss`/`-to` after `-i` for frame-accurate trim, timeout 120s -> 300s
- **NEW** export endpoint: `POST /telescope/files/export` — re-encodes any video as clean MP4
- **NEW** route registration for `/telescope/files/export`
- Rename companion list: added `_analyzed.mp4` suffix

**src/transit_detector.py:**
- Detection recording (~line 1800): added `-g 30 -bf 0`
- Changed `stderr=subprocess.DEVNULL` to `stderr=subprocess.PIPE`
- Added stderr reading/logging after `proc.wait()`

**src/transit_analyzer.py:**
- Removed dead code at lines 386-391 (orphaned `path.with_name()` and unassigned `cv2.VideoWriter()`)
- `_reencode_h264()` (~line 157): added `-bf 0 -g 30`

**static/telescope.js:**
- Added "Export MP4" button to viewer action bar (video files only)
- Added `viewerExportMp4()` function
- Fixed export success refresh call to `refreshFiles()`
- Added `scheduleRefreshFiles()` retry refresh helper for photo/recording/timelapse/detection events
- Reworked five-panel frame extraction from "extract all frames at load" to lightweight on-demand extraction around current frame (reduces viewer flashing/load spikes)

## What's Verified Working
- `-bf 0` flag works with the installed ffmpeg (tested, produces 0 B-frames)
- All Python files compile clean
- JS syntax is valid
- Encoding fix confirmed on pre-`-bf 0` recording: vid_20260412_085043.mp4 has 7 keyframes, nb_frames=202 (was N/A before), proper codec/pixfmt
- Flask API `/telescope/files` correctly returns new files (43 files, newest first)

## What's NOT Yet Verified
- **No recording has been made with `-bf 0` in effect** — the app hasn't been restarted since that change
- **"New recording doesn't appear in filmstrip/grid"** — user reported this BEFORE `-bf 0` was added. Unclear if:
  - The filmstrip just didn't auto-refresh (timing issue with single-shot refresh)
  - Or there's a deeper UI bug
  - The backend API DOES return the files correctly, so it's a frontend rendering or refresh issue
- **Flashing on video load** — mitigated by on-demand frame extraction; still needs real-device verification for regressions

## Open Questions
1. Will multi-pass refresh reliably show new recordings in filmstrip/grid on all recording paths?
2. Does `-bf 0` fully solve the "72 frame limit"? (Theory is solid — 72 = exact P-frame count — but not yet tested with a new recording)
3. Is any viewer flashing still visible after the on-demand extractor change?

## Key Files
- `src/telescope_routes.py` — manual/timelapse recording, trim, export, rename, file listing
- `src/transit_detector.py` — detection recording (ffmpeg MJPEG pipe)
- `src/transit_analyzer.py` — post-capture analysis, annotated video re-encode
- `static/telescope.js` — all frontend: filmstrip, grid, viewer, scrubber, export button
- `electron/main.js` — app shell, Flask launcher (not modified)

## How to Test
1. Restart app: `cd electron && npm start`
2. Make a manual recording (10s)
3. Check filmstrip refreshes and shows the file
4. Run: `ffprobe -v error -show_frames -show_entries frame=pict_type -of csv <newfile.mp4> | sort | uniq -c`
   - Should show 0 B-frames (only I and P)
5. Open in viewer — should play ALL frames without the 72-frame limit
6. Try trim, export, rename
7. Files are at: `/Users/Tom/zipcatcher/static/captures/2026/04/`

## Safe to Commit
Yes — `git commit` and `git push` will NOT trigger a release. The release workflow only fires on `v*` tags.
