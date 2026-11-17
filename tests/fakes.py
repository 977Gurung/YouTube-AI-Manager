"""Fake versions of every outside service, so the whole system can run in a sandbox.

FakeWeb is one local HTTP server. `install_requests_redirect(port)` rewrites every `requests` call
to an outside host into http://127.0.0.1:<port>/<host>/<path>, so the app's hard-coded URLs
(api.pexels.com, www.googleapis.com, trends.google.com, ...) all land here. Unknown hosts raise,
which proves nothing leaks to the real internet.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

FEED_HOSTS = {"feeds.feedburner.com", "www.bleepingcomputer.com", "krebsonsecurity.com", "www.cisa.gov",
              "www.theverge.com", "techcrunch.com", "feeds.local"}


# --------------------------------------------------------------------------- media fixtures
def make_media(d: Path) -> dict:
    d.mkdir(parents=True, exist_ok=True)
    out = {}
    def ff(*a):
        subprocess.run(["ffmpeg", "-v", "error", "-y", *a], check=True)
    for name, src in (("clip_city.mp4", "testsrc2=size=1280x720:rate=25:duration=4"),
                      ("clip_server.mp4", "testsrc=size=1920x1080:rate=30:duration=3"),
                      ("clip_portrait.mp4", "smptebars=size=720x1280:rate=30:duration=3")):
        p = d / name
        if not p.exists():
            ff("-f", "lavfi", "-i", src, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p))
        out[name] = p
    p = d / "music.mp3"
    if not p.exists():
        ff("-f", "lavfi", "-i", "sine=frequency=220:duration=6", "-ac", "2", str(p))
    out["music.mp3"] = p
    p = d / "speech.mp3"
    if not p.exists():
        ff("-f", "lavfi", "-i", "sine=frequency=400:duration=2", "-ac", "1", str(p))
    out["speech.mp3"] = p
    p = d / "broken.mp4"
    p.write_bytes(b"this is not a video")
    out["broken.mp4"] = p
    return out


def rss(titles, atom=False):
    items = "".join(f"<item><title>{t}</title><link>https://example.org/{i}</link></item>" for i, t in enumerate(titles))
    return f'<?xml version="1.0"?><rss version="2.0" xmlns:ht="https://trends.google.com/trending/rss"><channel><title>t</title>{items}</channel></rss>'.encode()


# --------------------------------------------------------------------------- fake LLM
class Brain:
    """Answers the app's prompts the way a well-behaved local model would."""

    def __init__(self):
        self.calls: list[str] = []
        self.bad_json = 0          # return this many invalid replies first
        self.short_too_long = False
        self.fail_http = 0         # return HTTP 500 this many times
        self.lock = threading.Lock()
        self.idea_runs = 0

    def reply(self, messages: list[dict]) -> str:
        system = messages[0]["content"]
        user = messages[1]["content"]
        last = messages[-1]["content"]
        with self.lock:
            self.calls.append(user[:80])
            if self.bad_json > 0:
                self.bad_json -= 1
                return "Sure! Here are some ideas: 1) foo 2) bar"
        if "Propose exactly" in user:
            n = int(re.search(r"Propose exactly (\d+)", user).group(1))
            self.idea_runs += 1
            if self.idea_runs > 1:   # later research runs bring genuinely new topics
                pool = ["zero-day browser exploits", "deepfake voice scams", "quantum-safe encryption", "AI coding assistants",
                        "smart TV privacy", "router firmware updates", "SIM swap attacks", "open-source LLMs", "EV charger hacking",
                        "satellite internet security", "AI in hospitals", "password manager breaches", "bug bounty programs",
                        "phishing kits as a service", "facial recognition laws", "humanoid robots", "data broker opt-outs"]
                k = self.idea_runs * 3
                return json.dumps({"ideas": [{"topic": f"What {pool[(k + j) % len(pool)]} mean for you in {2026 + self.idea_runs}",
                                              "format": "short", "score": 7} for j in range(min(n, 3))]})
            ideas = [
                {"topic": "How ransomware gangs pick their victims", "angle": "defender's view", "format": "short",
                 "hook": "They don't pick you at random.", "why": "BleepingComputer headline", "score": 8},
                {"topic": "What the new EU AI Act means for you", "angle": "plain-English guide", "format": "long",
                 "hook": "A law just changed your apps.", "why": "The Verge AI feed", "score": 7},
                {"topic": "How ransomware gangs pick their victims!", "format": "short", "score": 6},  # duplicate
                {"topic": "Passkeys explained in 60 seconds", "format": "podcast", "score": "nine"},  # bad format + score
                {"topic": "", "format": "short"},  # empty -> skipped
                {"topic": "Why AI chips are so expensive", "format": "short", "score": 6},
            ]
            return json.dumps({"ideas": ideas[:max(n, 4) + 2]})
        if "YouTube Shorts script" in user:
            if self.short_too_long and "too long" not in last:
                secs = [{"text": "word " * 60, "visual": "server room"} for _ in range(4)]
            else:
                secs = [{"text": "**Your password** is already leaked. [music]", "visual": "city traffic night"},
                        {"text": "Narrator: Attackers buy old breach lists for a few dollars and try them everywhere.", "visual": "hacker hoodie laptop!!"},
                        {"text": "That trick is called credential stuffing, and it works because we reuse passwords.", "visual": "server room lights"},
                        {"text": "Turn on a password manager and passkeys today. Did you know that? 🔐", "visual": "phone fingerprint unlock"}]
            return json.dumps({"title_options": ["Your Password Is Already Leaked", "Credential Stuffing Explained", ""],
                               "thumbnail_text": "YOU WERE BREACHED", "sections": secs + [{"bad": 1}, "junk"]})
        if "Plan a" in user:
            return json.dumps({"title_options": ["The EU AI Act, Explained"], "thumbnail_text": "NEW AI LAW",
                               "intro": {"text": "A law just changed the apps you use. Here is what it means.", "visual": "parliament building"},
                               "chapters": [{"title": "What the law covers", "key_points": ["risk tiers"]},
                                            {"title": "What changes for users", "key_points": ["labels"]},
                                            {"title": "What happens next", "key_points": ["timeline"]}],
                               "outro": {"text": "That's the AI Act in plain English. Subscribe for more.", "visual": "city skyline night"}})
        if "Write chapter" in user:
            return json.dumps({"sections": [{"text": "The law sorts AI systems by how much harm they could cause. " * 3, "visual": "data center"},
                                            {"text": "High risk systems need human oversight and clear records of how they work. " * 2, "visual": "office team meeting"}]})
        if "image generator" in system:
            secs = re.findall(r"^(\d+)\. needs (\d+) image", user, re.M)
            return json.dumps({"sections": [{"i": int(i), "prompts": [f"dark server room with blue light, shot {k}" for k in range(int(n))]} for i, n in secs]})
        if "SEO specialist" in system:
            return json.dumps({"title": "Your <Password> Is   Already Leaked - Here's Why", "description": "Attackers reuse breach lists.\nProtect yourself.",
                               "tags": ["credential stuffing", "password security", "passkeys", "cyber security", "x" * 60] + [f"tag {i}" for i in range(40)],
                               "hashtags": ["cybersecurity", "#infosec", "pass keys", "#AI"]})
        return json.dumps({"error": "unexpected prompt"})


# --------------------------------------------------------------------------- the server
class FakeWeb:
    def __init__(self, media: dict):
        self.media = media
        self.brain = Brain()
        self.calls: list[tuple[str, str, str]] = []  # (method, host, path)
        self.hooks: list[dict] = []
        self.yt_public: set[str] = set()
        self.pexels_status = 200
        self.feed_fail: set[str] = set()
        self.ollama_models = ["qwen2.5:7b-instruct"]
        fw = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _handle(self, method):
                parts = urlsplit(self.path)
                seg = parts.path.lstrip("/").split("/", 1)
                host, rest = seg[0], "/" + (seg[1] if len(seg) > 1 else "")
                q = {k: v[0] for k, v in parse_qs(parts.query).items()}
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                fw.calls.append((method, host, rest))
                status, ctype, data = fw.route(method, host, rest, q, body, dict(self.headers))
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, host: str, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}/{host}{path}"

    def stop(self):
        self.server.shutdown()

    # ---------------------------------------------------------------- routing
    def route(self, method, host, path, q, body, headers):
        J = lambda obj, s=200: (s, "application/json", json.dumps(obj).encode())
        if host == "ollama.local":
            if path == "/api/tags":
                return J({"models": [{"name": m} for m in self.ollama_models]})
            if path == "/api/generate":
                return J({"done": True})
            if path == "/api/chat":
                if self.brain.fail_http > 0:
                    self.brain.fail_http -= 1
                    return J({"error": "model loading"}, 500)
                req = json.loads(body)
                return J({"message": {"role": "assistant", "content": self.brain.reply(req["messages"])}})
        if host in FEED_HOSTS:
            if host in self.feed_fail:
                return 503, "text/plain", b"down"
            return 200, "application/rss+xml", rss([f"{host} story {i}: AI and security news" for i in range(8)])
        if host == "trends.google.com":
            return 200, "application/rss+xml", rss(["ai regulation", "football final", "data breach", "new phone launch"])
        if host == "www.reddit.com":
            return 200, "application/rss+xml", rss([f"r/{path.split('/')[2]} top post {i}" for i in range(5)])
        if host == "www.youtube.com" and path == "/feeds/videos.xml":
            entries = "".join(f"<entry><yt:videoId>comp{q.get('channel_id', 'x')[-3:]}{i}</yt:videoId><title>Competitor video {i}</title></entry>" for i in range(3))
            return 200, "application/atom+xml", f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">{entries}</feed>'.encode()
        if host == "www.googleapis.com":
            if not q.get("key"):
                return J({"error": {"code": 403, "message": "missing key"}}, 403)
            if path == "/youtube/v3/videos":
                if q.get("chart") == "mostPopular":
                    ids = [f"pop{i}" for i in range(int(q.get("maxResults", 5)))]
                else:
                    ids = q.get("id", "").split(",")
                items = [{"id": i, "snippet": {"title": f"Video {i}", "publishedAt": "2026-11-09T10:00:00Z", "channelTitle": "Chan"},
                          "statistics": {"viewCount": str(1000 + 7 * len(i)), "likeCount": "50", "commentCount": "4"},
                          "status": {"privacyStatus": "public" if i in self.yt_public or not i.startswith("vid") else "private"}}
                         for i in ids if i]
                return J({"items": items})
            if path == "/youtube/v3/search":
                return J({"items": [{"id": {"videoId": f"s{i}"}} for i in range(3)]})
        if host == "api.pexels.com":
            if self.pexels_status != 200:
                return J({"error": "Unauthorized"}, self.pexels_status)
            if headers.get("Authorization") != "pexels-test-key":
                return J({"error": "bad key"}, 401)
            portrait = q.get("orientation") == "portrait"
            return J({"videos": [{"id": 100 + i, "duration": 4, "url": f"https://pexels.com/v/{i}", "user": {"name": "Pex Author"},
                                  "video_files": [{"file_type": "video/mp4", "width": 720 if portrait else 1280, "height": 1280 if portrait else 720,
                                                   "link": f"https://files.local/{'clip_portrait' if portrait else 'clip_city'}.mp4?i={i}"},
                                                  {"file_type": "video/mp4", "width": 3840, "height": 2160, "link": "https://files.local/huge.mp4"}]}
                                 for i in range(3)]})
        if host == "pixabay.com" and path.startswith("/api/videos"):
            if q.get("key") != "pixabay-test-key":
                return 400, "text/plain", b"[ERROR 400] Invalid or missing API key"
            return J({"hits": [{"id": 500 + i, "duration": 3, "user": "pixa", "pageURL": f"https://pixabay.com/v/{i}",
                                "videos": {"medium": {"url": f"https://files.local/clip_server.mp4?p={i}", "width": 1920, "height": 1080}}}
                               for i in range(3)]})
        if host == "files.local":
            name = path.lstrip("/")
            if name == "huge.mp4":
                return 404, "text/plain", b"nope"
            p = self.media.get(name)
            return (200, "video/mp4", p.read_bytes()) if p else (404, "text/plain", b"missing")
        if host == "api.elevenlabs.io":
            if headers.get("xi-api-key") != "el-test-key":
                return J({"detail": "bad key"}, 401)
            return 200, "audio/mpeg", self.media["speech.mp3"].read_bytes()
        if host == "hooks.local":
            self.hooks.append({"path": path, "body": json.loads(body or b"{}")})
            return J({"ok": True})
        return 404, "text/plain", f"no fake for {host}{path}".encode()


def install_requests_redirect(port: int, allow_local: bool = True):
    """Patch requests so every outside URL goes to FakeWeb. Returns an undo function."""
    import requests

    original = requests.sessions.Session.request

    def request(self, method, url, *a, **kw):
        p = urlsplit(url)
        if p.hostname in ("127.0.0.1", "localhost") and allow_local:
            return original(self, method, url, *a, **kw)
        new = f"http://127.0.0.1:{port}/{p.hostname}{p.path}" + (f"?{p.query}" if p.query else "")
        return original(self, method, new, *a, **kw)

    requests.sessions.Session.request = request
    return lambda: setattr(requests.sessions.Session, "request", original)


# --------------------------------------------------------------------------- fake YouTube (googleapiclient)
class _Req:
    def __init__(self, result=None, error=None, chunks=1):
        self.result, self.error, self.chunks = result, error, chunks

    def execute(self):
        if self.error:
            raise self.error
        return self.result

    def next_chunk(self):
        if self.error:
            err, self.error = self.error, None if getattr(self, "transient", False) else self.error
            raise err
        self.chunks -= 1
        return (None, None) if self.chunks > 0 else (None, self.result)


def http_error(status: int, reason: str):
    import httplib2
    from googleapiclient.errors import HttpError
    resp = httplib2.Response({"status": status})
    resp.reason = reason
    return HttpError(resp, json.dumps({"error": {"errors": [{"reason": reason}], "message": reason}}).encode())


class FakeYouTube:
    def __init__(self):
        self.inserted: list[dict] = []
        self.thumbs: list[str] = []
        self.captions_sent: list[dict] = []
        self.fail_insert = None
        self.fail_thumb = None
        self.transient_500 = 0
        self.n = 0

    def videos(self):
        yt = self

        class V:
            def insert(self, part, body, media_body):
                yt.inserted.append(body)
                if yt.fail_insert:
                    return _Req(error=yt.fail_insert)
                yt.n += 1
                r = _Req(result={"id": f"vid{yt.n:03d}"}, chunks=2)
                if yt.transient_500:
                    yt.transient_500 -= 1
                    r.error = http_error(503, "backendError")
                    r.transient = True
                return r

            def list(self, part, id):
                return _Req(result={"items": [{"id": i, "statistics": {"viewCount": "42", "likeCount": "3", "commentCount": "1"},
                                               "status": {"privacyStatus": "public"}} for i in id.split(",")]})
        return V()

    def thumbnails(self):
        yt = self

        class T:
            def set(self, videoId, media_body):
                yt.thumbs.append(videoId)
                return _Req(error=yt.fail_thumb) if yt.fail_thumb else _Req(result={})
        return T()

    def captions(self):
        yt = self

        class C:
            def insert(self, part, body, media_body):
                yt.captions_sent.append(body)
                return _Req(result={"id": "cap1"})
        return C()

    def channels(self):
        class Ch:
            def list(self, part, mine):
                return _Req(result={"items": [{"id": "UCfakeChannel123", "snippet": {"title": "Demo Channel"}}]})
        return Ch()
