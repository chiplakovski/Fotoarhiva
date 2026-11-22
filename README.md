# Fotoarhiva 2.5

**Download the installer:** [Fotoarhiva-Setup-2.5.0.exe](https://github.com/chiplakovski/fotoarhiva/raw/main/Fotoarhiva-Setup-2.5.0.exe)

Fotoarhiva is a Windows app that gathers all the photos on your computer in one place,
organizes them by year and month and recognizes what is in them.
It opens in Chrome as its own window. Everything runs on your computer.

## Installation

1. Download `Fotoarhiva-Setup-2.5.0.exe` and run it.
   If Windows shows "Windows protected your PC": **More info → Run anyway**.
2. Keep **"Recognition models"** ticked in the setup. They are downloaded during installation
   (~3.5 GB, a few minutes) so the app is complete.
3. At the end Fotoarhiva opens with a welcome guide. **Nothing is recognized until you finish it:**
   - tick the folders with photos (it finds Pictures, Downloads, Desktop, OneDrive, D: drive… by itself),
   - use "Add another folder or drive" for more folders,
   - choose whether photos are **copied** (recommended) or **moved**,
   - click **Start**. Only then do importing and recognition begin.

There is a **Fotoarhiva** icon on the Desktop and in the Start menu.

Upgrading from a Macedonian version (2.4 or older) converts the library automatically:
folder names (e.g. "07 - July", "Videos", "Screenshots", "Trash"), tags, sources and place names become English.
No photos are lost.

## Adding photos

- **Add photos** button at the top right:
  - **Choose photos…**: from the computer, a phone connected by cable, or USB,
  - **Import a whole folder…**: imports all photos from a folder once,
  - **Watch a folder automatically…**: new photos in that folder are added on their own.
- Or **drag** photos or folders from Explorer into the window.
- Watched folders are managed in **Settings** (the gear at the top right).

## Recognition

### Photo describer: built-in Qwen3-VL (recommended)

Fotoarhiva includes the **Qwen3-VL 4B vision-language model**. It "looks" at every photo like a person,
writes a description and adds accurate tags (for example "stainless steel machines in a factory").

- Free, and **everything runs on your computer**. Photos never leave it.
- Uses the graphics card (NVIDIA, AMD or Intel, 6 GB is enough) through DirectML.
  If the card can't run it, it switches to the processor on its own (slower) and remembers that.
- Change it in **Settings → Photo describer**: built-in Qwen3-VL, Claude (cloud) or Off.

### Quick recognition (search and similar photos)

SigLIP 2 / CLIP is used for free-text search ("dog on the beach") and "Similar photos":

| Quick recognition | Accuracy | Size |
|---|---|---|
| Standard (OpenAI CLIP) | basic | ~155 MB |
| Advanced (Google SigLIP 2) | good, many languages | ~400 MB |
| Large (SigLIP 2 Large) | very good | ~900 MB |
| Maximum (SigLIP 2 So400m) | best, slow | ~1.5 GB |

A tag is given only when the category is clearly visible. If nothing fits, the photo gets no tag rather than a wrong one.

### Claude (cloud, optional)

Settings → **Claude** → enter an API key from console.anthropic.com → choose a model → "Describe all photos" or "Only new photos".
It shows the estimated cost before starting. Only a smaller copy (at most 1024 px) is sent. The originals stay on the computer.

| Model | About per 1000 photos |
|---|---|
| Claude Opus 5.5 (most accurate) | $10 |
| Claude Sonnet 5.5 | $5 |
| Claude Haiku 5.5 | $0.30 |

### Fix a wrong group

If a group is wrong (for example "Museum" contains photos of stainless steel machines):
open the group → **"Not right? Fix it"** → write what the photos really are (e.g. "Stainless steel machines")
and optionally a short description. The photos move to the new group, the wrong tag never comes back,
the new one becomes your own category and similar photos are fixed automatically.
If only some photos are wrong, select them and click **"Not this"**.

### It learns from you

- **Tag by hand:** tag one photo (e.g. "Stainless steel machines") and the app also tags many similar photos.
  The more examples, the better. Remove a wrong tag with ×, it won't come back.
- **My categories:** in Settings add a name and a short description. Photos are re-evaluated right away.

## Smart search

Type freely in the search box: "dog on the beach", "birthday cake", "snow in the mountains".
The best matches are shown first.

## My places: Home, Work…

In **Settings → My places**, or in a photo's details ("set as Home, Work…"), set your own places
with a name and radius. Photos taken there get that name, appear under "My places" and are used for event names.
The place is chosen by searching an address (OpenStreetMap) or from a photo's GPS.

## What it recognizes

| What | How |
|---|---|
| Date | From the photo, from the file name (`IMG-20230605-WA0012`, `Screenshot_2026…`, `FB_IMG_…`), from the video file |
| Place | From the photo's GPS, offline: Ohrid, Skopje, Thessaloniki, Athens… |
| Content | 120+ categories (nature, places, home, work and industry, people, events, sport, food, animals, things, documents), your own categories and learned tags |
| Source | Camera, WhatsApp, Viber, Facebook / Messenger, Instagram, Telegram, Snapchat, edited photos, internet |
| Type | Photos, videos, screenshots |
| Events | Photos from the same period and place become an event automatically, e.g. "Ohrid · Jul 14–16, 2024" |
| Duplicates | Identical photos are skipped and listed under "Duplicates" |

## Photo details

Favorite, rotate, open in another program, show in folder, delete, change the date (the photo moves to the right month),
tags, albums, histogram, map, **similar photos** and **history** (the original folder it came from, copied/moved/imported,
when it was added, and other copies; "Open the original folder" opens it in Explorer). Click the photo to zoom.

## Where the photos are

```
Pictures\Fotoarhiva\
  2024\
    07 - July\
      IMG_4000.JPG
      Videos\
      Screenshots\
  Trash\          ← "deleted" photos, until you delete them permanently
  .fotoarhiva\    ← index and thumbnails (hidden folder, don't delete it)
```

## Settings

The gear at the top right: watched folders, library location, copy/move, how often to check,
recognition models, photo describer, Claude, my categories, my places, Desktop icon,
**start with Windows** (runs quietly in the background) and **quit the app**.

## Uninstall

- Start menu → **Fotoarhiva → Uninstall Fotoarhiva**, or
- Settings → Apps → **Fotoarhiva** → Uninstall, or
- in the app: Settings → **Uninstall Fotoarhiva**.

Uninstalling removes the program. Your photos and the organized library are **not** deleted.
At the end it asks whether to also delete the settings and the recognition models.

## If something doesn't work

- **The app doesn't open:** Start menu → Fotoarhiva → **Fotoarhiva (safe mode)**.
  It opens without recognition. Change the describer in Settings, then start it normally.
- If the graphics card can't run the model, Fotoarhiva switches to the processor and remembers it
  (to try the card again, delete `qwen3-vl-4b\gpu_failed.txt` in `%LOCALAPPDATA%\Fotoarhiva\models`).
- The app log is `%LOCALAPPDATA%\Fotoarhiva\fotoarhiva.log`.

## Privacy and safety

- The app is only reachable from your own computer (127.0.0.1) and doesn't accept commands from other web pages.
- In "copy" mode the originals are never changed.
- Rotating doesn't change the original file.
- Permanent deletion from the disk only happens in Trash, after confirmation.
