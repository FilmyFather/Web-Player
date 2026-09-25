"""
Standalone streaming helper for the FilmyDesiFlix player.

Run this next to (or as part of) your Telegram bot's web server. It does the
one thing a browser cannot do by itself: read a real MKV with ffmpeg/ffprobe
and hand the player a real audio track, a real embedded subtitle, or a real
re-encoded resolution.

Usage:
    pip install aiohttp
    python3 transcode_server.py            # listens on 0.0.0.0:8766

The player is told about this server's base URL and adds ?src=<original
media URL> to every call, so ONE instance of this server can serve every
file your bot links to — you don't need to change it per video.

Endpoints (all take ?src=<direct link to the .mkv/.mp4>):
  GET /api/tracks?src=...              -> JSON list of audio + subtitle streams
  GET /api/resolutions?src=...         -> JSON list of real quality options
  GET /audio/<index>?src=...&ss=<sec>  -> that audio stream only, as AAC/ADTS
  GET /subs/<index>?src=...            -> that subtitle stream, as WebVTT text
  GET /transcode/<height>?src=...&ss=<sec>&audio=<index> -> that resolution, as fMP4

Nothing here is a fake label: every field returned by /api/tracks and
/api/resolutions comes from ffprobe actually reading the file, and every
byte returned by the other three routes comes out of a live ffmpeg process
actually doing that work.

SECURITY: by default this will fetch and transcode ANY http(s) URL a caller
gives it — that's an open proxy. Before putting this on the public internet,
set ALLOWED_SRC_HOSTS to a comma-separated list of the hostnames your bot
actually serves files from, e.g.:
    ALLOWED_SRC_HOSTS=f2l3.reaperzclub.dpdns.org python3 transcode_server.py
Every transcode is also a real, CPU-heavy ffmpeg process — put this behind
some concurrency/rate limit (e.g. nginx, or a semaphore in front of it) if
many people can hit it at once.
"""
import asyncio, json, os, time, urllib.parse
from aiohttp import web

DEFAULT_VCODEC = os.environ.get("DEFAULT_VCODEC", "h264")   # override only for local testing

FFPROBE = "ffprobe"
FFMPEG = "ffmpeg"
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
}
_sub_cache = {}          # src+index -> (vtt_text, timestamp)
_tracks_cache = {}       # src -> (json_list, timestamp)
CACHE_TTL = 3600


def cors(resp):
    for k, v in CORS.items():
        resp.headers[k] = v
    return resp


ALLOWED_SRC_HOSTS = [h.strip().lower() for h in os.environ.get("ALLOWED_SRC_HOSTS", "").split(",") if h.strip()]

def get_src(request):
    src = request.query.get("src", "")
    if not src or not (src.startswith("http://") or src.startswith("https://")):
        raise web.HTTPBadRequest(text="missing or invalid ?src=")
    if ALLOWED_SRC_HOSTS:
        host = urllib.parse.urlparse(src).hostname or ""
        if host.lower() not in ALLOWED_SRC_HOSTS:
            raise web.HTTPForbidden(text="src host not allowed")
    return src


async def ffprobe_json(src):
    proc = await asyncio.create_subprocess_exec(
        FFPROBE, "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", src,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=25)
    if proc.returncode != 0:
        raise web.HTTPBadGateway(text="ffprobe failed: " + err.decode(errors="ignore")[:400])
    return json.loads(out.decode(errors="ignore"))


async def handle_options(request):
    return cors(web.Response())


async def handle_tracks(request):
    src = get_src(request)
    cached = _tracks_cache.get(src)
    if cached and time.time() - cached[1] < CACHE_TTL:
        return cors(web.json_response(cached[0]))
    data = await ffprobe_json(src)
    streams = data.get("streams", [])
    out = []
    for s in streams:
        ctype = s.get("codec_type")
        if ctype not in ("audio", "subtitle"):
            continue
        out.append({
            "index": s.get("index"),
            "codec_type": ctype,
            "codec_name": s.get("codec_name", ""),
            "tags": s.get("tags", {}) or {},
        })
    _tracks_cache[src] = (out, time.time())
    return cors(web.json_response(out))


async def handle_resolutions(request):
    src = get_src(request)
    data = await ffprobe_json(src)
    vstream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    height = int(vstream.get("height", 0)) if vstream else 0
    ladder = [h for h in (2160, 1440, 1080, 720, 480, 360, 240, 144) if h < height]
    options = [{"label": f"Original ({height}p)", "height": 0}]
    for h in ladder:
        options.append({"label": f"{h}p", "height": h})
    return cors(web.json_response(options))


async def stream_process(request, args, content_type):
    """Run ffmpeg, stream stdout to the client, kill it cleanly if the
    client disconnects (mid-scrub reselects happen constantly)."""
    resp = web.StreamResponse(status=200, headers={"Content-Type": content_type, **CORS})
    await resp.prepare(request)
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            await resp.write(chunk)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return resp


DEFAULT_ACODEC = os.environ.get("DEFAULT_ACODEC", "aac")   # override only for local testing

async def handle_audio(request):
    src = get_src(request)
    index = int(request.match_info["index"])
    ss = float(request.query.get("ss", "0") or 0)
    acodec = request.query.get("acodec", DEFAULT_ACODEC)
    if acodec == "opus":
        args = ["-loglevel", "error", "-ss", str(max(0, ss)), "-i", src,
                "-map", f"0:{index}", "-vn", "-c:a", "libopus", "-b:a", "128k",
                "-f", "webm", "pipe:1"]
        return await stream_process(request, args, "audio/webm")
    args = ["-loglevel", "error", "-ss", str(max(0, ss)), "-i", src,
            "-map", f"0:{index}", "-vn", "-c:a", "aac", "-b:a", "160k",
            "-f", "adts", "pipe:1"]
    return await stream_process(request, args, "audio/aac")


async def handle_subs(request):
    src = get_src(request)
    index = int(request.match_info["index"])
    key = src + "#" + str(index)
    cached = _sub_cache.get(key)
    if cached and time.time() - cached[1] < CACHE_TTL:
        return cors(web.Response(text=cached[0], content_type="text/vtt"))
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-loglevel", "error", "-i", src, "-map", f"0:{index}",
        "-f", "webvtt", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=40)
    if proc.returncode != 0:
        raise web.HTTPBadGateway(text="subtitle extraction failed: " + err.decode(errors="ignore")[:400])
    text = out.decode("utf-8", errors="ignore")
    _sub_cache[key] = (text, time.time())
    return cors(web.Response(text=text, content_type="text/vtt"))


async def handle_transcode(request):
    src = get_src(request)
    height = int(request.match_info["height"])
    ss = float(request.query.get("ss", "0") or 0)
    audio_idx = request.query.get("audio")
    vcodec = request.query.get("vcodec", DEFAULT_VCODEC)
    args = ["-loglevel", "error", "-ss", str(max(0, ss)), "-i", src, "-map", "0:v:0"]
    if audio_idx is not None:
        args += ["-map", f"0:{audio_idx}"]
    else:
        args += ["-map", "0:a:0?"]
    args += ["-vf", f"scale=-2:{height}"]
    if vcodec == "vp9":
        args += ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32", "-deadline", "realtime", "-cpu-used", "8",
                  "-c:a", "libopus", "-b:a", "128k", "-f", "webm", "pipe:1"]
        return await stream_process(request, args, "video/webm")
    args += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]
    return await stream_process(request, args, "video/mp4")


app = web.Application()
app.router.add_route("OPTIONS", "/{tail:.*}", handle_options)
app.router.add_get("/api/tracks", handle_tracks)
app.router.add_get("/api/resolutions", handle_resolutions)
app.router.add_get("/audio/{index}", handle_audio)
app.router.add_get("/subs/{index}", handle_subs)
app.router.add_get("/transcode/{height}", handle_transcode)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8766)
