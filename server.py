#!/usr/bin/env python3
"""Loopback-only static server and session-scoped YouTube media proxy."""

import ipaddress
import json
import mimetypes
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parent
PORT = 5173
SESSION_TTL = 30 * 60
PREPARATION_TIMEOUT = 90
LOCAL_EXTRACTOR = ROOT / ".tools" / "yt-dlp"
# Long recordings contain thousands of signed segment URLs, even without media data.
MANIFEST_LIMIT = 16 * 1024 * 1024
SESSIONS = {}
LOCK = threading.RLock()
EXTRACTIONS = threading.BoundedSemaphore(2)
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{11}")


class PlaybackError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def validate_source(url):
    parts = urlsplit(url)
    host = parts.hostname or ""
    if (parts.scheme != "https" or parts.username or parts.password
            or parts.port not in (None, 443) or not host.endswith(".googlevideo.com")):
        raise PlaybackError(502, "INVALID_SOURCE", "Sumber streaming tidak diizinkan.")
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise PlaybackError(502, "INVALID_SOURCE", "Alamat sumber streaming tidak diizinkan.")
    return url


class SourceRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_source(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_source(url, headers, byte_range=None, timeout=20):
    validate_source(url)
    request_headers = {k: v for k, v in headers.items()
                       if k.lower() in ("user-agent", "referer", "accept-language")}
    request_headers["Accept-Encoding"] = "identity"
    if byte_range:
        request_headers["Range"] = byte_range
    # Do not inherit a system proxy that could redirect these requests to a private service.
    return build_opener(ProxyHandler({}), SourceRedirect()).open(
        Request(url, headers=request_headers), timeout=timeout)


def read_manifest(response):
    data = response.read(MANIFEST_LIMIT + 1)
    if len(data) > MANIFEST_LIMIT or not data.lstrip().startswith(b"#EXTM3U"):
        raise PlaybackError(415, "UNSUPPORTED_FORMAT", "Playlist HLS tidak tersedia atau tidak didukung.")
    return data.decode("utf-8-sig")


def filter_master(text):
    lines, output, variants = text.splitlines(), [], 0
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("#EXT-X-STREAM-INF:"):
            size = re.search(r"RESOLUTION=\d+x(\d+)", line)
            if index + 1 >= len(lines) or lines[index + 1].startswith("#"):
                raise PlaybackError(415, "UNSUPPORTED_FORMAT", "Playlist HLS tidak lengkap.")
            if size and int(size[1]) <= 720 and 'avc1.' in line:
                output.extend((line, lines[index + 1]))
                variants += 1
            index += 2
            continue
        if not (line.startswith("#EXT-X-I-FRAME-STREAM-INF:")
                or (line.startswith("#EXT-X-MEDIA:") and "TYPE=SUBTITLES" in line)):
            output.append(line)
        index += 1
    if not variants:
        raise PlaybackError(415, "UNSUPPORTED_FORMAT", "Tidak ada HLS H.264 hingga 720p yang dapat dipakai.")
    return "\n".join(output) + "\n"


def resource_url(session, source):
    parts = urlsplit(source)
    if parts.scheme != "https" or not (parts.hostname or "").endswith(".googlevideo.com"):
        raise PlaybackError(502, "INVALID_SOURCE", "Playlist berisi sumber yang tidak diizinkan.")
    with LOCK:
        token = session["urls"].get(source)
        if token is None:
            if len(session["resources"]) >= 20000:
                raise PlaybackError(502, "PLAYLIST_TOO_LARGE", "Playlist terlalu besar untuk sesi ini.")
            token = secrets.token_hex(12)
            session["urls"][source] = token
            session["resources"][token] = source
    return f'/api/playback/{session["id"]}/resource/{token}'


def rewrite_manifest(text, base_url, session):
    output = []
    for line in text.splitlines():
        if line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")) and "METHOD=NONE" not in line:
            raise PlaybackError(415, "UNSUPPORTED_FORMAT", "Stream terenkripsi tidak didukung oleh fallback.")
        if line.startswith("#"):
            line = re.sub(r'URI="([^"]+)"',
                          lambda match: 'URI="' + resource_url(session, urljoin(base_url, match[1])) + '"', line)
        elif line.strip():
            line = resource_url(session, urljoin(base_url, line.strip()))
        output.append(line)
    return "\n".join(output) + "\n"


def prune_sessions():
    now = time.monotonic()
    with LOCK:
        for key in list(SESSIONS):
            if now - SESSIONS[key]["accessed"] > SESSION_TTL:
                del SESSIONS[key]


def preparation_time_left(deadline, maximum):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PlaybackError(504, "EXTRACTION_TIMEOUT", "Persiapan stream melewati batas 90 detik. Coba lagi.")
    return min(remaining, maximum)


def prepare_source(info, deadline):
    formats = info.get("formats", [])
    hls = [item for item in formats if item.get("manifest_url") and not item.get("has_drm")
           and item.get("protocol", "").startswith("m3u8")]
    mp4 = sorted([item for item in formats
                  if item.get("url") and not item.get("has_drm") and item.get("protocol") == "https"
                  and item.get("ext") == "mp4" and 0 < (item.get("height") or 0) <= 720
                  and (item.get("vcodec") or "").startswith("avc1")
                  and (item.get("acodec") or "").startswith("mp4a")],
                 key=lambda item: (item["height"], item.get("tbr") or 0), reverse=True)
    candidates = ([hls[0]] if hls else []) + mp4
    if not candidates:
        raise PlaybackError(415, "UNSUPPORTED_FORMAT", "Video ini tidak menyediakan HLS atau MP4 dengan audio yang dapat diputar.")
    for index, item in enumerate(candidates):
        headers = item.get("http_headers", info.get("http_headers", {}))
        session = {"id": secrets.token_hex(16), "accessed": time.monotonic(),
                   "headers": headers, "resources": {}, "urls": {}}
        is_hls = item in hls
        source = item["manifest_url"] if is_hls else item["url"]
        try:
            with open_source(source, headers, None if is_hls else "bytes=0-1023",
                             timeout=preparation_time_left(deadline, 10)) as response:
                if is_hls:
                    master = filter_master(read_manifest(response))
                    session["manifest"] = rewrite_manifest(master, response.url, session)
                else:
                    if response.status != 206 or response.headers.get_content_type() != "video/mp4":
                        raise PlaybackError(415, "UNSUPPORTED_FORMAT", "Sumber MP4 tidak mendukung streaming dengan seek.")
                    probe = response.read(1024)
                    if len(probe) < 8 or probe[4:8] != b"ftyp":
                        raise PlaybackError(415, "UNSUPPORTED_FORMAT", "Sumber tidak berisi video MP4 yang valid.")
                    session["mediaUrl"] = resource_url(session, response.url)
            preparation_time_left(deadline, 10)
            return session
        except HTTPError as error:
            error.close()
            if index == len(candidates) - 1:
                raise
        except PlaybackError as error:
            if error.code != "UNSUPPORTED_FORMAT" or index == len(candidates) - 1:
                raise


def create_session(video_id):
    extractor = ([sys.executable, str(LOCAL_EXTRACTOR)] if LOCAL_EXTRACTOR.is_file()
                 else [shutil.which("yt-dlp")])
    if not extractor[0] or not shutil.which("node"):
        raise PlaybackError(503, "MISSING_DEPENDENCY", "Fallback memerlukan yt-dlp dan Node.js di komputer server.")
    if not EXTRACTIONS.acquire(blocking=False):
        raise PlaybackError(503, "BUSY", "Pemutar alternatif sedang menyiapkan video lain. Coba lagi sebentar.")
    deadline = time.monotonic() + PREPARATION_TIMEOUT
    try:
        for attempt in range(2):
            try:
                result = subprocess.run(extractor + [
                    "--ignore-config", "--no-cache-dir", "--no-playlist", "--skip-download",
                    "--dump-single-json", "--js-runtimes", "node", "--socket-timeout", "10",
                    "--retries", "0", "--extractor-retries", "0", "--",
                    f"https://www.youtube.com/watch?v={video_id}",
                ], capture_output=True, text=True, timeout=preparation_time_left(deadline, 35))
                if result.returncode:
                    reason = result.stderr.lower()
                    if any(message in reason for message in ("private video", "video has been removed", "video is unavailable", "video is not available")):
                        raise PlaybackError(404, "VIDEO_UNAVAILABLE", "Video tidak tersedia untuk pemutar alternatif atau memerlukan akses khusus.")
                    if any(message in reason for message in ("sign in", "age-restricted", "not available in your country")):
                        raise PlaybackError(403, "VIDEO_RESTRICTED", "YouTube membatasi akses video ini. Pemutar lokal tidak menggunakan akun YouTube.")
                    if attempt == 0:
                        continue
                    raise PlaybackError(502, "EXTRACTION_FAILED", "Stream gagal diambil setelah dua percobaan. Periksa koneksi dan versi yt-dlp.")
                try:
                    info = json.loads(result.stdout)
                    if not isinstance(info, dict) or not isinstance(info.get("formats"), list) or any(not isinstance(item, dict) for item in info["formats"]):
                        raise ValueError()
                except (ValueError, TypeError):
                    raise PlaybackError(502, "EXTRACTION_FAILED", "Respons extractor tidak dapat dibaca.") from None
                session = prepare_source(info, deadline)
                break
            except subprocess.TimeoutExpired:
                if attempt:
                    raise PlaybackError(504, "EXTRACTION_TIMEOUT", "Pengambilan stream terlalu lama setelah dua percobaan. Coba lagi.") from None
            except HTTPError as error:
                if attempt or error.code not in (403, 410, 429, 500, 502, 503, 504):
                    raise
            except (TimeoutError, URLError):
                if attempt:
                    raise PlaybackError(502, "SOURCE_UNAVAILABLE", "Sumber streaming tidak dapat dihubungi setelah dua percobaan. Periksa koneksi.") from None
            except OSError:
                raise PlaybackError(503, "MISSING_DEPENDENCY", "yt-dlp atau Node.js tidak dapat dijalankan.") from None
    finally:
        EXTRACTIONS.release()
    with LOCK:
        prune_sessions()
        if len(SESSIONS) >= 16:
            raise PlaybackError(503, "BUSY", "Terlalu banyak sesi pemutar. Tutup pemutar lain lalu coba lagi.")
        SESSIONS[session["id"]] = session
    return session


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Resource IDs are local tokens; never log signed upstream URLs.
        print(f"{self.address_string()} {format % args}", flush=True)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def json_response(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def check_request(self):
        host = self.headers.get("Host", "")
        if host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            raise PlaybackError(403, "INVALID_HOST", "Alamat server tidak diizinkan.")
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{host}":
            raise PlaybackError(403, "INVALID_ORIGIN", "Permintaan harus berasal dari app lokal ini.")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise PlaybackError(403, "INVALID_ORIGIN", "Permintaan lintas situs tidak diizinkan.")

    def dispatch(self):
        sent = False
        try:
            self.check_request()
            path = urlsplit(self.path).path
            prune_sessions()
            if self.command == "POST" and path == "/api/playback":
                if self.headers.get_content_type() != "application/json":
                    raise PlaybackError(400, "INVALID_INPUT", "Gunakan body JSON berisi videoId.")
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 256 or self.headers.get("Transfer-Encoding"):
                        raise ValueError()
                    data = json.loads(self.rfile.read(size))
                    video_id = data.get("videoId") if isinstance(data, dict) else None
                    if not isinstance(video_id, str) or not ID_PATTERN.fullmatch(video_id):
                        raise ValueError()
                except (ValueError, UnicodeError):
                    raise PlaybackError(400, "INVALID_INPUT", "videoId harus berupa ID YouTube 11 karakter.") from None
                session = create_session(video_id)
                try:
                    source = ({"mediaUrl": session["mediaUrl"]} if "mediaUrl" in session else
                              {"manifestUrl": f'/api/playback/{session["id"]}/manifest.m3u8'})
                    self.json_response(201, {"sessionId": session["id"], **source})
                except (BrokenPipeError, ConnectionResetError):
                    with LOCK:
                        SESSIONS.pop(session["id"], None)
                return
            route = re.fullmatch(r"/api/playback/([a-f0-9]{32})(?:/(manifest\.m3u8|resource/([a-f0-9]{24})))?", path)
            if route:
                session_id, resource, token = route.groups()
                if self.command == "DELETE" and resource is None:
                    with LOCK:
                        SESSIONS.pop(session_id, None)
                    self.json_response(200, {"deleted": True})
                    return
                with LOCK:
                    session = SESSIONS.get(session_id)
                    if session is None:
                        raise PlaybackError(410, "SESSION_EXPIRED", "Sesi pemutar kedaluwarsa. Menyiapkan stream baru diperlukan.")
                    session["accessed"] = time.monotonic()
                if self.command not in ("GET", "HEAD") or not resource:
                    raise PlaybackError(404, "NOT_FOUND", "Endpoint tidak ditemukan.")
                if resource == "manifest.m3u8":
                    if "manifest" not in session:
                        raise PlaybackError(404, "NOT_FOUND", "Sesi ini tidak memakai playlist HLS.")
                    body = session["manifest"].encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(body)
                    return
                with LOCK:
                    source = session["resources"].get(token)
                if source is None:
                    raise PlaybackError(404, "NOT_FOUND", "Segmen tidak ditemukan.")
                byte_range = self.headers.get("Range")
                if byte_range and not re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", byte_range):
                    raise PlaybackError(416, "INVALID_RANGE", "Rentang byte tidak didukung.")
                with open_source(source, session["headers"], byte_range) as response:
                    media_type = response.headers.get_content_type()
                    if "mpegurl" in media_type or urlsplit(source).path.endswith(".m3u8"):
                        body = rewrite_manifest(read_manifest(response), response.url, session).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        if self.command != "HEAD":
                            self.wfile.write(body)
                    else:
                        self.send_response(response.status)
                        for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                            if response.headers.get(name):
                                self.send_header(name, response.headers[name])
                        self.end_headers()
                        sent = True
                        if self.command != "HEAD":
                            while chunk := response.read(64 * 1024):
                                with LOCK:
                                    if SESSIONS.get(session_id) is not session:
                                        break
                                    session["accessed"] = time.monotonic()
                                self.wfile.write(chunk)
                    return
            if self.command not in ("GET", "HEAD"):
                raise PlaybackError(404, "NOT_FOUND", "Endpoint tidak ditemukan.")
            decoded = unquote(path)
            file = (ROOT / ("index.html" if decoded == "/" else decoded.lstrip("/"))).resolve()
            allowed = file in [ROOT / name for name in ("index.html", "app.js", "styles.css")]
            allowed = allowed or file.is_relative_to(ROOT / "assets")
            if not allowed or not file.is_file():
                raise PlaybackError(404, "NOT_FOUND", "File tidak ditemukan.")
            body = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(file)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except HTTPError as error:
            status = error.code if error.code in (403, 410, 416) else 502
            if not sent:
                if status == 416:
                    self.send_response(416)
                    self.send_header("Content-Length", "0")
                    if error.headers.get("Content-Range"):
                        self.send_header("Content-Range", error.headers["Content-Range"])
                    self.end_headers()
                else:
                    self.json_response(status, {"code": "SOURCE_REJECTED", "message": "Sumber streaming menolak akses. Coba menyiapkan sesi baru."})
            error.close()
        except PlaybackError as error:
            if not sent:
                self.json_response(error.status, {"code": error.code, "message": error.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (TimeoutError, URLError, OSError, ValueError):
            if not sent:
                self.json_response(502, {"code": "SOURCE_UNAVAILABLE", "message": "Sumber streaming tidak dapat dihubungi. Periksa koneksi lalu coba lagi."})
        finally:
            self.close_connection = True

    do_GET = do_HEAD = do_POST = do_DELETE = dispatch


if __name__ == "__main__":
    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(110)
            return connection, address

    try:
        with Server(("127.0.0.1", PORT), Handler) as server:
            print(f"Ruang Putar: http://127.0.0.1:{PORT}", flush=True)
            if (not LOCAL_EXTRACTOR.is_file() and not shutil.which("yt-dlp")) or not shutil.which("node"):
                print("Fallback belum tersedia: pasang yt-dlp dan Node.js. Player resmi tetap dapat digunakan.", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except OSError as error:
        raise SystemExit(f"Server tidak dapat dimulai: {error}. Hentikan server lama pada port {PORT}; port tidak diganti otomatis.")
