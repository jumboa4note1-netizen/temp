#!/usr/bin/env python3
"""
Fahh MIDI · headless automation helper
======================================

Drive the Fahh MIDI web app from Python with zero manual interaction:
upload a MIDI file, tweak ANY setting, export / import settings files,
and capture lossless RGBA frames streamed directly into ffmpeg for
single-pass high-quality MP4 encoding (1920x1200 @ 60fps default).
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import shutil
import subprocess
import time
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

try:
    from playwright.sync_api import Browser, Page, sync_playwright
except ImportError:  # pragma: no cover
    sys.exit("Playwright is required:  pip install playwright && python -m playwright install chromium")

HERE = Path(__file__).resolve().parent
_FORWARD_PROGRESS_JS = """
window.addEventListener('fahhmidi-export-progress', e => { try { window.__fahhProgress(e.detail) } catch (_) {} });
"""


def _default_dist_dir() -> Path:
    built = HERE.parent / "dist"
    if (built / "index.html").exists():
        return built
    raise RuntimeError("No built app in ../dist/index.html. Run `npm run build` first.")


def _default_url() -> str:
    env = os.environ.get("FAHH_MIDI_URL")
    if env:
        return env
    return _default_dist_dir().joinpath("index.html").as_uri()


class FahhMidiError(RuntimeError):
    pass


def verify_mp4(path: str | Path, expected_width: int, expected_height: int, min_frames: int = 1) -> dict[str, Any]:
    """Validate an encoded MP4 via ffprobe and return its stream metadata."""
    p = Path(path)
    if not p.exists() or p.stat().st_size < 4096:
        raise FahhMidiError(f"Output MP4 is missing or too small: {p}")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise FahhMidiError("ffprobe is required to verify MP4 integrity.")
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,pix_fmt,width,height,nb_frames,avg_frame_rate",
        "-show_entries",
        "format=duration,size",
        "-of",
        "json",
        str(p),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise FahhMidiError(f"ffprobe failed for {p}: {res.stderr.strip()}")
    info = json.loads(res.stdout or "{}")
    streams = info.get("streams") or []
    fmt = info.get("format") or {}
    if not streams:
        raise FahhMidiError(f"No video stream present in {p}")
    st = streams[0]
    w = int(st.get("width") or 0)
    h = int(st.get("height") or 0)
    nb_frames = int(st.get("nb_frames") or 0)
    duration = float(fmt.get("duration") or 0.0)
    codec = str(st.get("codec_name") or "")
    if codec != "h264":
        raise FahhMidiError(f"Unexpected video codec '{codec}' in {p}")
    if w != expected_width or h != expected_height:
        raise FahhMidiError(f"Dimension mismatch in {p}: expected {expected_width}x{expected_height}, got {w}x{h}")
    if duration <= 0.05:
        raise FahhMidiError(f"Invalid duration {duration}s in {p}")
    if nb_frames and nb_frames < min_frames:
        raise FahhMidiError(f"Truncated frame count in {p}: {nb_frames} < {min_frames}")
    return {
        "codec": codec,
        "pix_fmt": st.get("pix_fmt"),
        "width": w,
        "height": h,
        "nb_frames": nb_frames,
        "duration": duration,
        "size_bytes": int(fmt.get("size") or p.stat().st_size),
    }


class _PipeServer:
    """Single-origin localhost HTTP server that serves dist/ and pipes raw RGBA frames straight into ffmpeg stdin."""

    def __init__(self, dist_dir: Optional[Path] = None) -> None:
        self.dist_dir = dist_dir
        self._stdin_lock = threading.Lock()
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._expected_frame_bytes = 0
        self._frames_written = 0
        self._pipe_error: Optional[str] = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # silence per-frame access logs
                return

            def _send_cors(self) -> None:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")

            def do_OPTIONS(self) -> None:
                self.send_response(HTTPStatus.NO_CONTENT)
                self._send_cors()
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:
                if outer.dist_dir is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                req_path = urlparse(self.path).path.lstrip("/") or "index.html"
                target = (outer.dist_dir / req_path).resolve()
                if not str(target).startswith(str(outer.dist_dir.resolve())) or not target.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                data = target.read_bytes()
                ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                self.send_response(HTTPStatus.OK)
                self._send_cors()
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path != "/frame":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0:
                    self.send_error(HTTPStatus.BAD_REQUEST, "Empty frame body")
                    return
                body = self.rfile.read(length)
                if len(body) != outer._expected_frame_bytes:
                    outer._pipe_error = f"Expected {outer._expected_frame_bytes} RGBA bytes, got {len(body)}"
                    self.send_error(HTTPStatus.BAD_REQUEST, outer._pipe_error)
                    return
                with outer._stdin_lock:
                    proc = outer._proc
                    if proc is None or proc.stdin is None:
                        self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "No active ffmpeg pipe")
                        return
                    try:
                        proc.stdin.write(body)
                        outer._frames_written += 1
                    except Exception as exc:
                        outer._pipe_error = str(exc)
                        self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, outer._pipe_error)
                        return
                resp = b"ok"
                self.send_response(HTTPStatus.OK)
                self._send_cors()
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def begin_pipe(self, proc: subprocess.Popen[bytes], width: int, height: int) -> None:
        with self._stdin_lock:
            self._proc = proc
            self._expected_frame_bytes = width * height * 4
            self._frames_written = 0
            self._pipe_error = None

    def end_pipe(self) -> tuple[int, Optional[str]]:
        with self._stdin_lock:
            proc = self._proc
            self._proc = None
            written = self._frames_written
            err = self._pipe_error
            if proc and proc.stdin:
                try:
                    proc.stdin.flush()
                    proc.stdin.close()
                except Exception:
                    pass
            return written, err

    def close(self) -> None:
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass


class FahhMidi:
    """A running, scriptable Fahh MIDI page with lossless RGBA->ffmpeg piping."""

    def __init__(
        self,
        url: Optional[str] = None,
        *,
        headless: bool = True,
        channel: Optional[str] = None,
        viewport: tuple[int, int] = (1920, 1200),
        startup_settings: Optional[dict | str | Path] = None,
        on_progress: Optional[Callable[[float, int, int], None]] = None,
        timeout_s: float = 120,
    ):
        self.explicit_url = url or os.environ.get("FAHH_MIDI_URL")
        self.headless = headless
        self.channel = channel
        self.viewport = viewport
        self.startup_settings = startup_settings
        self.on_progress = on_progress
        self.timeout_ms = int(timeout_s * 1000)
        self._pw = None
        self._browser: Optional[Browser] = None
        self.page: Optional[Page] = None
        self._pipe_server: Optional[_PipeServer] = None
        self._prog_frame = 0
        self._prog_total = 0

    def __enter__(self) -> "FahhMidi":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> "FahhMidi":
        dist_dir: Optional[Path] = None
        if not self.explicit_url:
            dist_dir = _default_dist_dir()
        self._pipe_server = _PipeServer(dist_dir=dist_dir)
        target_url = self.explicit_url or f"{self._pipe_server.base_url}/index.html"

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            channel=self.channel,
            args=["--autoplay-policy=no-user-gesture-required", "--disable-dev-shm-usage"],
        )
        ctx = self._browser.new_context(viewport={"width": self.viewport[0], "height": self.viewport[1]}, accept_downloads=True)
        self.page = ctx.new_page()
        self.page.set_default_timeout(self.timeout_ms)
        self.page.add_init_script("window.FAHH_MIDI_AUTOMATION = true;")
        if self.startup_settings is not None:
            cfg = self._read_settings(self.startup_settings)
            self.page.add_init_script(f"window.FAHH_MIDI_CONFIG = {json.dumps(cfg)};")

        def _progress(d: dict) -> None:
            self._prog_frame = int(d.get("frame", 0))
            self._prog_total = int(d.get("total", 0))
            if self.on_progress:
                self.on_progress(float(d.get("pct", 0)), int(d.get("frame", 0)), int(d.get("total", 0)))

        self.page.expose_function("__fahhProgress", _progress)
        self.page.add_init_script(_FORWARD_PROGRESS_JS)
        self.page.goto(target_url)
        self.page.wait_for_function("window.FahhMIDI && window.FahhMIDI.isReady()", timeout=self.timeout_ms)
        return self

    def close(self) -> None:
        for obj in (self._browser, self._pw, self._pipe_server):
            try:
                if obj:
                    obj.close() if hasattr(obj, "close") else obj.stop()
            except Exception:
                pass
        self._browser = self._pw = self.page = self._pipe_server = None

    # ------------------------------------------------------------------ plumbing
    def js(self, expression: str, arg: Any = None) -> Any:
        assert self.page, "call start() or use `with FahhMidi() as fm:`"
        try:
            return self.page.evaluate(expression, arg) if arg is not None else self.page.evaluate(expression)
        except Exception as e:
            msg = str(e).split("\n")[0].replace("Page.evaluate: Error: ", "")
            raise FahhMidiError(msg) from None

    def api(self, method: str, *args: Any) -> Any:
        return self.js("([m, a]) => window.FahhMIDI[m](...a)", [method, list(args)])

    @staticmethod
    def _read_settings(src: dict | str | Path) -> Any:
        if isinstance(src, dict):
            return src
        p = Path(str(src))
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return json.loads(str(src))

    # ------------------------------------------------------------------ MIDI input
    def load_midi(self, path: str | Path) -> dict:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(path)
        data = base64.b64encode(path.read_bytes()).decode()
        return self.api("loadMidiBase64", data, path.name)["song"]

    def load_midi_url(self, url: str) -> dict:
        return self.api("loadMidiUrl", url)["song"]

    def load_builtin(self, song_id: str) -> dict:
        return self.api("loadBuiltInSong", song_id)

    def builtin_songs(self) -> list[dict]:
        return self.api("listBuiltInSongs")

    def song_info(self) -> dict:
        return self.api("getSongInfo")

    def set_track_hand(self, track: int, hand: str) -> None:
        self.api("setTrackHand", track, hand)

    # ------------------------------------------------------------------ key & tempo
    def get_key_estimate(self, time_s: Optional[float] = None) -> dict:
        return self.api("getKeyEstimate", *([time_s] if time_s is not None else []))

    def get_tempo_summary(self, time_s: Optional[float] = None) -> dict:
        return self.api("getTempoSummary", *([time_s] if time_s is not None else []))

    # ------------------------------------------------------------------ settings
    def list_settings(self) -> list[dict]:
        return self.api("listSettings")

    def get_settings(self) -> dict:
        return self.api("getSettings")

    def get(self, path: str) -> Any:
        return self.api("get", path)

    def set(self, path: str, value: Any) -> Any:
        return self.api("set", path, value)

    def update(self, settings: dict) -> dict:
        return self.api("setSettings", settings)

    def load_settings(self, source: dict | str | Path) -> dict:
        res = self.api("importSettings", self._read_settings(source))
        if res["rejected"]:
            print(f"[fahh-midi] ignored {len(res['rejected'])} unknown/invalid settings: {res['rejected'][:3]}", file=sys.stderr)
        return res

    def save_settings(self, path: str | Path, only_changed: bool = False) -> Path:
        text = self.api("exportSettings", {"onlyChanged": only_changed})
        p = Path(path)
        p.write_text(text, encoding="utf-8")
        return p

    def reset(self, path: Optional[str] = None) -> None:
        self.api("resetSettings", *([path] if path else []))

    def presets(self) -> list[dict]:
        return self.api("listPresets")

    def apply_preset(self, preset_id: str) -> None:
        self.api("applyPreset", preset_id)

    # ------------------------------------------------------------------ pictures
    def render_frame(self, out: str | Path, *, frame: Optional[int] = None, time: Optional[float] = None, width: Optional[int] = None, height: Optional[int] = None, fmt: str = "png") -> Path:
        opts: dict[str, Any] = {"type": f"image/{fmt}"}
        if frame is not None:
            opts["frame"] = frame
        if time is not None:
            opts["time"] = time
        if width:
            opts["width"] = width
        if height:
            opts["height"] = height
        b64 = self.api("renderFrameBase64", opts)
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(base64.b64decode(b64))
        return p

    # ------------------------------------------------------------------ video (lossless raw RGBA pipe -> single-pass ffmpeg)
    def render_mp4_lossless(
        self,
        out: str | Path,
        *,
        fps: int = 60,
        width: int = 1920,
        height: int = 1200,
        crf: int = 10,
        preset: str = "slow",
        lead_in: Optional[float] = None,
        tail_out: Optional[float] = None,
        start_frame: Optional[int] = None,
        end_frame: Optional[int] = None,
        manifest_path: Optional[str | Path] = None,
        write_manifest: bool = True,
        log_interval_s: Optional[float] = None,
        log_label: str = "render",
    ) -> dict[str, Any]:
        """Stream lossless raw RGBA frames straight into single-pass high-quality H.264 MP4 via ffmpeg."""
        assert self._pipe_server is not None
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise FahhMidiError("ffmpeg binary not found in PATH.")

        w = max(320, int(width)) & ~1
        h = max(240, int(height)) & ~1
        fps_i = max(1, int(fps))

        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = out_path.with_name(f".{out_path.stem}.tmp.{os.getpid()}.mp4")
        if tmp_path.exists():
            tmp_path.unlink()

        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgba",
            "-s",
            f"{w}x{h}",
            "-r",
            str(fps_i),
            "-i",
            "pipe:0",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            str(crf),
            "-tune",
            "animation",
            "-pix_fmt",
            "yuv420p",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
            "-movflags",
            "+faststart",
            str(tmp_path),
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        self._pipe_server.begin_pipe(proc, w, h)
        if log_interval_s is None:
            log_interval_s = float(os.environ.get("FAHH_LOG_INTERVAL_S", "300"))
        stop_evt = threading.Event()
        mon = threading.Thread(
            target=self._monitor, args=(stop_evt, proc, log_interval_s, log_label, fps_i, w, h), daemon=True
        )
        mon.start()

        opts: dict[str, Any] = {
            "endpoint": f"{self._pipe_server.base_url}/frame",
            "width": w,
            "height": h,
            "fps": fps_i,
        }
        if lead_in is not None:
            opts["leadInSeconds"] = lead_in
        if tail_out is not None:
            opts["tailOutSeconds"] = tail_out
        if start_frame is not None:
            opts["startFrame"] = int(start_frame)
        if end_frame is not None:
            opts["endFrame"] = int(end_frame)

        try:
            res = self.api("streamFramesToLocalServer", opts)
        except Exception:
            stop_evt.set()
            self._pipe_server.end_pipe()
            proc.kill()
            proc.wait()
            if tmp_path.exists():
                tmp_path.unlink()
            raise

        stop_evt.set()
        written, pipe_err = self._pipe_server.end_pipe()
        print(f"[{log_label}] frames streamed: {written}; waiting for ffmpeg to finalize...", flush=True)
        stderr_bytes = proc.stderr.read() if proc.stderr else b""
        ret = proc.wait()
        if pipe_err:
            if tmp_path.exists():
                tmp_path.unlink()
            raise FahhMidiError(f"Frame pipe error: {pipe_err}")
        if ret != 0:
            if tmp_path.exists():
                tmp_path.unlink()
            raise FahhMidiError(f"ffmpeg exited with {ret}: {stderr_bytes.decode(errors='replace').strip()}")

        expected_frames = int(res["renderedFrames"])
        probe = verify_mp4(tmp_path, w, h, min_frames=expected_frames)
        tmp_path.replace(out_path)

        manifest = res["manifest"]
        if write_manifest:
            mp = Path(manifest_path) if manifest_path else out_path.with_suffix(".mp4.sync.json")
            mp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return {"manifest": manifest, "probe": probe, "framesWritten": written}


    def _monitor(self, stop: threading.Event, proc: Any, interval: float, label: str, fps: int, w: int, h: int) -> None:
        """Heartbeat: prints frames captured, fps (overall + last interval), ETA, health, every `interval` seconds."""
        srv = self._pipe_server
        t0 = time.monotonic()
        last_t, last_n = t0, 0
        wait = min(30.0, interval)  # first line early so you know it is alive
        while not stop.wait(wait):
            wait = interval
            now = time.monotonic()
            n = srv._frames_written if srv else 0
            total = self._prog_total
            avg = n / max(1e-6, now - t0)
            cur = (n - last_n) / max(1e-6, now - last_t)
            last_t, last_n = now, n
            if total:
                pct = 100.0 * n / total
                eta = (total - n) / avg if avg > 0 else float("inf")
                prog = f"{n}/{total} frames ({pct:.1f}%)"
                eta_s = f"ETA {eta / 60:.1f} min" if eta != float("inf") else "ETA ?"
            else:
                prog, eta_s = f"{n} frames", "ETA ?"
            try:
                load = os.getloadavg()[0]
                mem = next((int(l.split()[1]) // 1024 for l in open("/proc/meminfo") if l.startswith("MemAvailable")), -1)
            except Exception:
                load, mem = -1.0, -1
            ff = "alive" if proc.poll() is None else f"EXITED({proc.returncode})"
            warn = "  !! NO NEW FRAMES since last log (stalled?)" if cur == 0 else ""
            print(
                f"[{label}] t+{(now - t0) / 60:.1f}m | {prog} | avg {avg:.2f} fps, last-interval {cur:.2f} fps "
                f"| video {n / fps:.0f}s @ {w}x{h} | {eta_s} | ffmpeg {ff} | load {load:.1f} | mem free {mem} MB{warn}",
                flush=True,
            )

    def render_mp4(self, out: str | Path, *, fps: int = 60, width: int = 1920, height: int = 1200, lead_in: Optional[float] = None, tail_out: Optional[float] = None, manifest_path: Optional[str | Path] = None) -> dict:
        return self.render_mp4_lossless(
            out,
            fps=fps,
            width=width,
            height=height,
            lead_in=lead_in,
            tail_out=tail_out,
            manifest_path=manifest_path,
        )["manifest"]


# ---------------------------------------------------------------------- CLI
def _parse_kv(items: Iterable[str]) -> dict:
    out: dict[str, Any] = {}
    for it in items:
        if "=" not in it:
            raise SystemExit(f"--set expects path=value, got {it!r}")
        k, v = it.split("=", 1)
        try:
            out[k.strip()] = json.loads(v)
        except json.JSONDecodeError:
            out[k.strip()] = v
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Render a MIDI file with Fahh MIDI (lossless single-pass 16:10 MP4).")
    ap.add_argument("midi", nargs="?", help=".mid / .midi file to load")
    ap.add_argument("-o", "--out", help="output .mp4")
    ap.add_argument("--url", help="app URL (default: serves ../dist on localhost)")
    ap.add_argument("--settings", help="settings .json")
    ap.add_argument("--preset", help="theme preset id")
    ap.add_argument("--set", action="append", default=[], metavar="PATH=VALUE", help="override one setting")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1200)
    ap.add_argument("--crf", type=int, default=10)
    ap.add_argument("--preset-x264", default="slow")
    ap.add_argument("--frame", type=int, help="render a single PNG of this video frame instead of an MP4")
    ap.add_argument("--save-settings", help="write the final settings used to this file")
    ap.add_argument("--channel", help="browser channel, e.g. chrome / msedge")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--list-settings", action="store_true", help="print every setting and exit")
    ap.add_argument("--list-presets", action="store_true")
    a = ap.parse_args(argv)

    def progress(pct: float, frame: int, total: int) -> None:
        sys.stderr.write(f"\r[fahh-midi] rendering {pct:5.1f}%  ({frame}/{total})")
        sys.stderr.flush()

    with FahhMidi(a.url, headless=not a.headed, channel=a.channel, on_progress=progress) as fm:
        if a.list_settings:
            for s in fm.list_settings():
                rng = f"[{s['min']}..{s['max']}]" if s["min"] is not None else (str(s["options"]) if s["options"] else "")
                print(f"{s['path']:42} {s['type']:8} default={json.dumps(s['default'])} {rng}")
            return 0
        if a.list_presets:
            for p in fm.presets():
                print(p["id"], "-", p["label"])
            return 0
        if a.midi:
            info = fm.load_midi(a.midi)
            tempo = fm.get_tempo_summary(1.0)
            key = fm.get_key_estimate()
            print(f"[fahh-midi] loaded {info['displayTitle']} ({info['displaySub']}): {info['noteCount']} notes, {info['durationSeconds']:.1f}s | {key['global']['name']} | ~{tempo['initialBpm']:.0f} BPM")
        if a.settings:
            fm.load_settings(a.settings)
        if a.preset:
            fm.apply_preset(a.preset)
        if a.set:
            res = fm.update(_parse_kv(a.set))
            if res["rejected"]:
                print("[fahh-midi] rejected:", res["rejected"], file=sys.stderr)
        if a.save_settings:
            print("[fahh-midi] settings ->", fm.save_settings(a.save_settings))
        if a.out and a.frame is not None:
            print("[fahh-midi] frame ->", fm.render_frame(a.out, frame=a.frame, width=a.width, height=a.height))
        elif a.out:
            out = fm.render_mp4_lossless(a.out, fps=a.fps, width=a.width, height=a.height, crf=a.crf, preset=a.preset_x264)
            sys.stderr.write("\n")
            v = out["manifest"]["video"]
            p = out["probe"]
            print(f"[fahh-midi] {a.out}: {v['width']}x{v['height']} @ {v['fps']}fps, {p['nb_frames']} frames, {p['size_bytes'] / (1024 * 1024):.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
