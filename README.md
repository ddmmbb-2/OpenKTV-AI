# OpenKTV-AI Current Feature Overview

OpenKTV-AI is not just a downloader. It is a complete local KTV management and playback system designed to organize a personal music/video library for home karaoke and local-network control.

Its core goals are:

- organize video resources into a playable local KTV library
- allow phones, PCs, and TVs to connect to the same system
- support queueing, playback, track switching, audio mode control, and lyrics synchronization
- auto-fill metadata and group songs by artist / album / title / genre
- support batch downloading, log tracking, and QR-based access from the admin panel

The current implementation is closer to a home KTV + LAN mobile control + local library management system than a simple YouTube downloader.

---

## 1. Current System Structure

### 1.1 Page Entry Points

The project currently includes the following pages:

- /player: playback display for projectors, TVs, or large screens; focused on video playback
- /remote: mobile control page for queueing, playback control, sorting, and lyrics viewing
- /combo: full-screen KTV control console with video playback area, queue panel, synced lyrics, and bottom control dock
- /admin: management dashboard for downloads, logs, metadata, and QR access

Relevant files:

- main.py
- templates/player.html
- templates/remote.html
- templates/combo.html
- templates/admin.html

### 1.2 Backend Architecture

The backend is built with:

- Flask
- Flask-SocketIO
- Python filesystem access
- local JSON metadata index

It is not a database-centric system. Instead, it is responsible for:

- serving web pages
- managing the song directory
- handling queue and playback state
- reading and saving lyrics
- updating metadata
- processing download and separation jobs

---

## 2. Real Feature Overview

### 2.0 Recent Behavior Updates

Recent updates in the current codebase include:

- iPhone Chrome fullscreen compatibility in `/combo` with native fullscreen attempts plus an in-page pseudo-fullscreen fallback
- unified queue-skip semantics so `cut`, `skip`, `stop`, and `next` can all trigger next-song behavior safely
- lyrics search now persists manually entered title / artist / album into song metadata immediately
- lyrics term loading now prefers saved metadata over filename parsing, preventing fallback to raw filename-based terms

### 2.1 Song Library Management

The system scans the `ktv_songs` directory for `.mp4` files and treats them as playable resources.

It currently does the following:

- reads all songs in the folder
- builds a song list
- displays metadata
- allows browsing by artist / album / title / genre
- supports selecting songs and adding them to the queue

### 2.2 Remote Queueing and Ordering

The remote page currently behaves like this:

- browser songs in grouped card views instead of a flat list
- default sorting by artist / album / title / genre
- clicking a group opens a song picker
- selecting a song directly adds it to the queue
- supports metadata autofill
- supports deleting songs while updating the local file and UI state
- supports filtering search terms

This is an important change from the earlier simple queue model:

- no longer just a flat song list
- evolved into grouped browsing + picker-based selection + one-tap queue insertion

### 2.3 Playback Control

Playback is driven by real-time socket events such as:

- add_to_queue
- request_play
- play_video
- control
- change_track
- seek_to
- song_ended

The `combo` page is the current primary control console. It includes:

- large video playback area
- bottom control bar for play, next, seek, fullscreen, and lyric toggle
- right-side queue panel
- right-side lyrics tools and synchronized lyric list
- lyrics match / search / save / delete workflow

The `player` page is more display-focused, while `combo` acts as the complete KTV operation dashboard.

Both `combo` and `player` support:

- play / pause
- skip to next song
- original / instrumental switching
- volume control
- key adjustment
- seeking to a specific time
- fullscreen mode

On iPhone browsers (including Chrome on iOS), `combo` also includes a pseudo-fullscreen fallback mode when native fullscreen is rejected by WebKit restrictions.

### 2.4 Lyrics Management

The lyrics system is a complete local lyrics workflow:

- reads local `.lrc` files
- searches for lyrics candidates
- lets the user select and save lyrics
- removes local lyrics
- updates the current highlighted line during playback
- renders synced lyric lists in `combo` and `remote`

The current implementation also updates lyrics by playback time in the remote page, so it is no longer only a static text display.

When searching lyrics in `combo`, manual title / artist / album inputs are now written back to metadata so later refreshes keep user-corrected values instead of reverting to filename-derived defaults.

### 2.5 Metadata and Cover Recognition

The current implementation supports:

- parsing title / artist / album from filenames
- inferring genre from text-based keywords
- supplementing artwork / artist image via Apple iTunes search
- determining whether metadata is complete
- filtering and displaying songs using metadata

This is one of the most important indexing layers of the project, because it directly affects how songs can be browsed and grouped.

### 2.6 Admin Panel

The admin page is no longer a simple download-only screen. It currently provides:

- download task entry
- batch queue management
- log output
- metadata status visibility
- QR code access links
- dark themed interface

QR links are generated based on the current host IP:

- /remote
- /combo
- /player

If the app is accessed via localhost / 127.0.0.1, it falls back to the current LAN IP so mobile devices can scan and open the site.

---

## 3. Actual File and Metadata Management

### 3.1 Directory Structure

The current structure is roughly:

```text
OpenKTV-AI/
├── main.py
├── README.md
├── templates/
│   ├── admin.html
│   ├── combo.html
│   ├── player.html
│   └── remote.html
├── ktv_songs/
│   ├── song_1.mp4
│   ├── song_2.mp4
│   ├── ...
│   └── _song_metadata.json
├── temp_processing/
│   └── temporary processing files
├── .venv/
│   └── Python virtual environment
└── ffmpeg/   （if configured）
```

### 3.2 Metadata File

Metadata is not stored in a database. Instead, it is tracked in a single JSON file:

```text
ktv_songs/_song_metadata.json
```

This store keeps index data for each song, including:

- filename
- title
- artist
- album
- genre
- artwork_url
- artist_image_url
- completed
- updated_at

Metadata can be updated by multiple flows, including lyrics search input from the `combo` page.

### 3.3 Autofill Flow

The metadata autofill process typically works in this order:

1. parse information from the filename
2. attempt to extract lyric-search keywords
3. infer title / artist / album using candidate search results
4. infer genre
5. look up artwork
6. write updated metadata back to the JSON store

The logic is centralized in `main.py`, and the key functions include:

- load_song_metadata_store()
- save_song_metadata_store()
- parse_filename_metadata()
- is_metadata_completed()
- normalize_song_metadata_entry()
- fetch_artwork_metadata()
- autofill_song_metadata()

### 3.4 Deletion Sync

When a song is deleted through the remote or admin interface, the app currently does the following:

- deletes the local MP4 file
- removes the matching metadata entry
- removes the song from the playback queue
- refreshes the list view
- re-renders the UI state

This prevents stale entries where the file is deleted but the UI still shows it.

---

## 4. Chrome Extension: One-Click Add Song

A ready-to-load Chrome extension is included in:

```text
chrome-extension/
```

### 4.1 What It Does

- click the extension icon on a song page (for example YouTube)
- send the current tab URL + page title to OpenKTV
- OpenKTV queues it into the same download pipeline as admin page jobs

### 4.2 Backend API Used

- endpoint: `POST /api/extension/add-song`
- payload:

```json
{
	"url": "https://www.youtube.com/watch?v=...",
	"title": "Page Title"
}
```

### 4.3 Install Steps (Chrome)

1. Open `chrome://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked**
4. Select folder: `OpenKTV-AI/chrome-extension`
5. Open extension **Options** and set your server URL (default: `http://127.0.0.1:5000`)

Example server URL on LAN:

```text
http://192.168.1.100:5000
```

### 4.4 Notes

- OpenKTV server must be running when clicking the extension.
- If server is unavailable, extension will show failure via action title/badge.

---

## 5. Package as EXE (Windows)

This repository now includes one-click packaging scripts:

- `build_exe.ps1`
- `build_exe.bat`

### 5.1 Quick Build

1. Open PowerShell in project root
2. Run:

```powershell
.\build_exe.ps1
```

Or double-click:

```text
build_exe.bat
```

### 5.2 Output

After build completes:

```text
dist/OpenKTV-AI/OpenKTV-AI.exe
```

Portable package (recommended for distribution):

```text
dist/OpenKTV-AI-portable.zip
```

The script also copies optional runtime files when present:

- `yt-dlp.exe`
- `ffmpeg/`
- `start_hidden.ps1`

### 5.3 Notes

- Uses `.venv/Scripts/python.exe` by default.
- Uses PyInstaller `--onedir` for better compatibility with templates and media tools.
- Copies common runtime DLLs (`vcruntime*.dll`, `msvcp140.dll`, `python*.dll`) into output folder.
- Target Windows PCs normally do not need Visual Studio or Python installed; just unzip and run `OpenKTV-AI.exe`.

## 4. Download and AI Separation Flow

### 4.1 Download Process

The download entry point is the admin page, which sends tasks through the background queue via the `start_download` event.

The typical flow is:

- paste or upload a link
- place it into the `download_queue`
- execute `try_start_next_download()` sequentially
- process the actual job in `run_download_job()`

### 4.2 AI Separation

The separation step uses Demucs:

```bash
python -m demucs --two-stems vocals -o output_dir input_path
```

This is used to:

- better separate vocals and accompaniment
- generate a KTV-friendly track layout
- allow the player to switch between original and instrumental modes

This part is an important backend pipeline stage and should be run in the project virtual environment to avoid issues such as `No module named demucs` when using the system Python.

---

## 5. Playback and Lyrics Synchronization Flow

### 5.1 Playback Flow

The player page receives backend events such as:

- play_video
- song_status
- command
- set_audio
- seek_to

Playback state is continuously broadcast from `combo` / `player`, and the remote page subscribes to `song_status` to update highlight position and lyrics sync according to the current playback time.

### 5.2 Lyrics Sync Mechanism

The current lyrics sync flow works like this:

- read the `.lrc` file
- parse time-based lyric entries
- determine the current line based on playback time
- update the current highlight
- scroll to that current line
- synchronize the lyric region across `combo` and `remote`

This is one of the most significant experience improvements in the current version.

---

## 6. Recommended Usage Patterns

### 6.1 Best Resource Sources

Although the original demo may have centered on YouTube, the current app is better suited to:

- local MP4 libraries
- personally owned / licensed video content
- authorized live-performance recordings
- publicly downloadable media that is allowed for local use
- songs processed and organized into a local KTV library

### 6.2 Recommended Workflow

Typical usage flow:

1. place the song files in `ktv_songs/`
2. open /remote
3. trigger Auto-fill metadata
4. browse songs by artist / album / genre
5. add songs to the queue and play
6. search and save lyrics in the `combo` page as needed
7. use the admin page for download and maintenance tasks

---

## 7. Runtime Requirements

### 7.1 Python Environment

It is recommended to use the project virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install flask flask-socketio yt-dlp demucs
python main.py
```

### 7.2 Access URLs

After startup, common entry points are:

- http://localhost:5000/remote
- http://localhost:5000/combo
- http://localhost:5000/admin
- http://localhost:5000/player

If used on a LAN, it is recommended to use the workstation local IP:

```text
http://192.168.x.x:5000/remote
```

### 7.3 Common Issues

#### 1) No module named demucs

Cause: the app was started with the system Python instead of the project `.venv`.

Fix:

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

#### 2) QR code does not work on mobile devices

Cause: the access URL is localhost or not on the same network segment.

Fix:

- use the QR code generated in the admin page
- or open the app using the LAN IP directly

#### 3) Lyrics are not syncing

Common reasons:

- `.lrc` file does not exist
- lyrics were never saved
- playback state was not properly broadcast

Fix:

- use Search Lyrics in the `combo` page
- or place a `.lrc` file in the local song directory

#### 4) iPhone Chrome fullscreen does not enter native fullscreen

Cause: iOS/WebKit may reject native fullscreen on non-video containers or in some runtime states.

Fix:

- use the fullscreen button in `combo`
- if native fullscreen is rejected, the page automatically falls back to in-page pseudo-fullscreen mode

---

## 8. Key Technical Judgement of the Current Version

Compared with the earlier version, the current implementation has changed significantly:

- remote page evolved from a flat list to group browsing + picker selection
- admin page updated to a dark QR-based management UI
- lyric sync evolved from static display to time-based synchronization
- metadata flow evolved from filename-only parsing to an index + autofill + artwork model
- playback and queue control now rely heavily on SocketIO real-time feedback
- the processing flow is more aligned with local library management + LAN access + automatic enhancements

In other words, the app is no longer just a simple download script; it is a more complete local KTV management system.

---

## 9. Copyright and Usage Notes

This project is intended for:

- personal entertainment
- home KTV use
- demo and self-use scenarios

Please do not use downloaded or processed content for:

- commercial screening
- unauthorized redistribution
- unlicensed public sharing

---

## 10. Current Project Summary

A more accurate description of OpenKTV-AI is:

“A local-song-library-first KTV system with LAN-based web interfaces, AI vocal separation, and automated lyrics/metadata enhancement for home karaoke playback.”

Its core value is centered on:

- unified local song library management
- multi-device coordination via local network
- metadata autofill
- smooth mobile remote control
- more polished playback and lyric experience

If you want, the next step can be to convert this documentation into one of these formats:

1. customer-facing product overview
2. developer architecture document
3. formal GitHub-ready release README

---

## 11. Release Notes

Detailed version history is maintained in [CHANGELOG.md](CHANGELOG.md).