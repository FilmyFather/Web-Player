# Real audio-track / quality / embedded-subtitle switching

Two files:

- **player_enhanced.html** — the player page (same one as before, updated)
- **transcode_server.py** — a small helper server that does the actual ffmpeg work

## Why a second file is needed

A browser cannot pick an audio track, re-encode a resolution, or read an
embedded subtitle out of an MKV by itself — there is no JavaScript API for
any of that. Something has to run **ffmpeg** on the real file and hand the
browser back a real stream. `transcode_server.py` is that something. It has
four endpoints, and every one of them is doing the real thing, not faking a
label:

| Endpoint | What it actually does |
|---|---|
| `/api/tracks?src=` | Runs `ffprobe` on the file, returns the real audio + subtitle streams |
| `/api/resolutions?src=` | Reads the real source height, returns quality options below it |
| `/audio/<index>?src=&ss=` | Runs `ffmpeg` to pull out *that* audio stream only, from position `ss`, as AAC |
| `/subs/<index>?src=` | Runs `ffmpeg` to pull the embedded subtitle out as WebVTT text |
| `/transcode/<height>?src=&ss=` | Runs `ffmpeg` to genuinely re-encode the video at that resolution, from position `ss` |

## Running it

```bash
pip install aiohttp
python3 transcode_server.py
```

It listens on port 8766. Run it anywhere that can reach your video files and
that your player's page can reach in turn (same VPS as your bot is simplest).

**Before you expose it publicly**, set an allowlist so it can't be used as an
open proxy to fetch/transcode arbitrary URLs:

```bash
ALLOWED_SRC_HOSTS=yourbotdomain.com python3 transcode_server.py
```

Each transcode is a real, CPU-heavy ffmpeg process. Put it behind a
concurrency limit (nginx, a semaphore, whatever you already use) if a lot of
people can hit it at once — it will not scale to hundreds of simultaneous
quality-switches on a small VPS.

## Wiring it into the player

The player looks for the helper's address in one place:

```html
<video ... data-api="__STREAM_API__">
```

Whatever fills `__MEDIA_URL__` and `__FILE_NAME__` today should also fill
`__STREAM_API__` with the helper server's base URL, e.g.
`https://your-vps.example.com:8766`. That's it — one more find-and-replace,
same mechanism you already have.

If `__STREAM_API__` is left blank (old bots that don't know about it), the
player works exactly as before: Audio Track and Quality stay hidden, and the
subtitle button falls back to "load a file yourself." Nothing breaks.

For testing by hand without touching your bot, you can also open the page
with `?api=http://your-server:8766` in the URL.

## What you get in the player's ⋮ menu

- **Audio Track** — only appears when a file actually has more than one
  audio track. Picking one truly silences the embedded track and plays the
  separately-extracted one; switching, seeking, play/pause, and the mute
  button all stay in sync with it.
- **Quality** — only appears when the source is taller than 144p. Picking
  one re-encodes from your current position onward at that height; the
  progress bar keeps showing the *real* total length and your real position
  throughout (a transcoded stream restarts its own internal clock at 0, so
  the player tracks the offset itself rather than trusting the video tag's
  raw clock).
- **Subtitles** — now offers the file's actual embedded subtitle tracks
  first, with "load a file yourself" underneath as before.

## Honest limitations

- Volume boost above 100% only works on the embedded track's own audio
  (via the existing Web Audio path); a separately-fetched audio track is
  capped at 100%, since it isn't running through that graph.
- The progress bar's buffered-ahead indicator is hidden while a
  quality-switched stream is playing — it's a live one-shot pipe, not a
  seekable file, so "buffered ahead" doesn't mean anything for it.
- `transcode_server.py` re-encodes with H.264/AAC by default because that
  decodes on essentially every phone. If you ever need to point it at a
  browser that truly can't do H.264, `&vcodec=vp9`/`&acodec=opus` exist as
  per-request overrides, but you shouldn't need them for normal use.
