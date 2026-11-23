# Fotoarhiva 2.9

**Download the installer:** [Fotoarhiva-Setup-2.9.0.exe](https://github.com/chiplakovski/fotoarhiva/raw/main/Fotoarhiva-Setup-2.9.0.exe)

Fotoarhiva is a Windows app that gathers all the photos on your computer in one place,
organizes them by year and month and recognizes what is in them.
It opens in Chrome as its own window. Everything runs on your computer.

## Installation

1. Download `Fotoarhiva-Setup-2.9.0.exe` and run it.
   If Windows shows "Windows protected your PC": **More info → Run anyway** (see below).
2. Keep **"Recognition models"** ticked in the setup. They are downloaded during installation
   (~3.5 GB, a few minutes) so the app is complete.
3. At the end Fotoarhiva opens with a welcome guide. **Nothing is recognized until you finish it:**
   - tick the folders with photos (it finds Pictures, Downloads, Desktop, OneDrive, D: drive… by itself),
   - use "Add another folder or drive" for more folders,
   - choose whether photos are **copied** (recommended) or **moved**,
   - click **Start**. Only then do importing and recognition begin.

There is a **Fotoarhiva** icon on the Desktop and in the Start menu.

### Updating

Download the new installer and run it over the existing installation. It recognizes the installed version
and shows **"Update Fotoarhiva"**: the folder and the models are kept, nothing is downloaded again, and your photos,
albums, tags, settings and login stay. A running Fotoarhiva is closed for the update. After the update the app shows
what's new once.

### Sign-in

Fotoarhiva asks for a username and password when it opens. The default login is:

- **Username:** `admin`
- **Password:** `0409979`

Change it in **Settings → Sign-in** (you need the current password). The window stays signed in for 30 days,
or until you click **Sign out**. Changing the password signs out every other window.

### "Windows protected your PC"

Windows shows this for every program that is not signed with a paid code-signing certificate and is
not yet known to Microsoft. Fotoarhiva is not signed, so click **More info → Run anyway**.
The warning only goes away for good once the installer is signed (for example with Microsoft Trusted Signing)
or installed from the Microsoft Store.

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

## Years, months and days

So a big library doesn't become a long wall of dates, use the switch at the top: **Years · Months · All photos**.

- **Years**: one card per year with a few photos, the count and the main places. Click a year →
- its **months** as cards → click a month → its photos, grouped **by day** ("Saturday, Jul 14"). **← Back** goes up again.
- **Months**: all months, grouped under their year.
- **All photos**: every photo, grouped by month, as before.

A library with more than 300 photos over 3 or more years opens in **Years** by default. Your choice is remembered.
The filter chips at the top (places, tags) count only the year or month you are in.

**Events** are grouped by year (5 per year, **Show all** for the rest) and have clearer names: the place,
the main subject, the weekday for a one-day event or "3 days". The left menu shows only the latest three.
Less-used menu sections (Source, Years) start collapsed. Click a section title to open or close it.

## People

Fotoarhiva finds the faces in your photos in the background, a few photos per second.

1. Open a photo. Under **People** you see the faces in it (the largest first).
2. Type a name under a face, e.g. **Bozica**, and press Enter.
3. Fotoarhiva compares that face with every face in your library and puts all photos of the same person under
   **People → Bozica** in the left menu. New photos are recognized automatically too.
4. A face recognized automatically says *recognized*. If it is wrong, click **×**: the name is removed and never
   guessed again for that face. In the person's view you can also select photos and click **Not this**.
5. Name the same person in a few more photos (different ages, light, glasses) and even more photos are found.
6. In the person's view: **Save as album**, **Rename** or **Forget this person**. You can also search by the name.

Face recognition runs only on your computer with two small open models from the OpenCV model zoo
(YuNet for finding faces, SFace for recognizing them, ~39 MB). It can be turned off in **Settings → People**.
For things that are not faces (a car, a house, a dog), use **+ tag** in the photo's details: similar photos get the tag
automatically.

## Videos

- Videos are organized like photos (in a `Videos` folder of each month) and show their **duration** on the thumbnail.
- **Every format plays** in the app. MP4, MOV and WebM play directly. MKV, AVI, MTS, 3GP and videos the browser
  can't decode (for example some iPhone HEVC videos) are converted the first time you open them. The copy is stored
  in `.fotoarhiva\video`, and the original is never changed.
- The **place** is read from the phone's GPS in the video, and the capture date from the video itself.
- Videos are **recognized and described** like photos (from a frame of the video), so you can search them.
- Details show the duration, size and format (e.g. H264 · AAC).
- Videos added by older versions are updated automatically in the background.

## Duplicates

Open **Duplicates** in the left menu (under Other). There are two lists:

1. **The same photo in more than one file**: copies inside the library that are not byte-identical,
   e.g. resized, recompressed by WhatsApp or Viber, slightly brighter or saved twice. In each group the largest copy is
   marked **Best**. Click the copies you don't need, or **Select all extra copies**, then **Move to Trash**.
   At least one copy of each photo always stays. **Not duplicates** hides a group for good.
2. **Skipped files outside the library**: files that were not imported because exactly the same file is already in
   Fotoarhiva (e.g. the same photo in Downloads and in Pictures). Select them, or **Select all**, and **Delete**
   to free space. On Windows they go to the Recycle Bin. A file is only deleted when its copy is still in
   the library (not in Trash) and the file hasn't changed since it was compared.

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

## Closing the app

When you close the Fotoarhiva window, the app quits completely after a few seconds. Python and the
recognition models no longer run and no memory is used. Work that was not finished (importing, recognition)
continues the next time you open it.

If you want Fotoarhiva to keep watching your folders and recognizing photos after the window is closed,
turn on **Settings → When the window is closed → Keep working in the background**.
When **Start with Windows** is on, the copy started with Windows always runs in the background.

The photo describer (Qwen3-VL) is unloaded from memory after 5 minutes without work and starts again by itself
when new photos arrive.

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
