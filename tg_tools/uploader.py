import os
import random
import re
import subprocess
import tempfile
import logging
from pyrogram import Client
from pyrogram.types import InputMediaPhoto, InputMediaVideo
from .compressor import compress_to_volumes, cleanup_volumes
from .config import SESSION_PATH, ensure_session_dir


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".heic"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v"}
PROFILE_EXTS = {".jpg", ".jpeg", ".png"}

BATCH_SIZE = 10

THUMB_MAX_BYTES = 200 * 1024
THUMB_MAX_DIM = 320

_PART_SUFFIX_RE = re.compile(r"\.(?:part\d+|\d{3,4}|00x)$", re.IGNORECASE)


# --- Logging de Pyrogram: silenciar ruido de red, exponer estado ---
_NET_ERROR_FLAG = {"active": False}


class _NetworkErrorFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        if ("Unable to connect" in msg or "Connection failed" in msg
                or "socket.send()" in msg or "Connection timed out" in msg
                or "Network is unreachable" in msg):
            _NET_ERROR_FLAG["active"] = True
            return False
        return True


def silence_pyrogram_network():
    for name in ("pyrogram", "pyrogram.session", "pyrogram.connection",
                 "pyrogram.session.session", "pyrogram.connection.connection",
                 "pyrogram.client"):
        lg = logging.getLogger(name)
        lg.addFilter(_NetworkErrorFilter())
        lg.propagate = False
        lg.handlers = [logging.NullHandler()]
        lg.setLevel(logging.CRITICAL)


def _short_name(name, max_len=40):
    return name if len(name) <= max_len else name[:max_len - 3] + "..."


def human_size(n):
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            if u == "B":
                return f"{int(f)} {u}"
            return f"{f:.2f} {u}"
        f /= 1024


def bar(pct):
    filled = int(pct / 5)
    if filled > 20:
        filled = 20
    return "[" + "*" * filled + "-" * (20 - filled) + "]"


def get_video_duration(path):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return int(float(out))
    except Exception:
        return 0


def get_audio_metadata(path):
    duration = 0
    performer = None
    title = None
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:format_tags=artist,title",
             "-of", "default=noprint_wrappers=1", path],
            stderr=subprocess.DEVNULL,
        ).decode().strip().splitlines()
        for line in out:
            if line.startswith("duration="):
                try:
                    duration = int(float(line.split("=", 1)[1]))
                except ValueError:
                    pass
            elif line.startswith("TAG:artist="):
                performer = line.split("=", 1)[1] or None
            elif line.startswith("TAG:title="):
                title = line.split("=", 1)[1] or None
    except Exception:
        pass
    return duration, performer, title


def get_random_thumb(path):
    try:
        fd, thumb = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        t = random.uniform(0, 5)
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(t), "-i", path, "-vframes", "1",
             "-vf", "scale=320:-1", thumb],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        return thumb
    except Exception:
        return None


def _thumb_dimensions(path):
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(path) as img:
            return img.size
    except Exception:
        return None


def _thumb_is_valid(path):
    try:
        if os.path.getsize(path) > THUMB_MAX_BYTES:
            return False
    except OSError:
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".jpg", ".jpeg"):
        return False
    dims = _thumb_dimensions(path)
    if dims is None:
        return False
    w, h = dims
    if w > THUMB_MAX_DIM or h > THUMB_MAX_DIM:
        return False
    return True


def _make_thumb_compatible(path):
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        fd, out = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((THUMB_MAX_DIM, THUMB_MAX_DIM), Image.Resampling.LANCZOS)
            img.save(out, format="JPEG", quality=80)
        if os.path.getsize(out) > THUMB_MAX_BYTES:
            with Image.open(out) as img:
                img = img.convert("RGB")
                for quality in (60, 40, 20, 10):
                    img.save(out, format="JPEG", quality=quality)
                    if os.path.getsize(out) <= THUMB_MAX_BYTES:
                        break
        if os.path.getsize(out) > THUMB_MAX_BYTES:
            try:
                os.remove(out)
            except OSError:
                pass
            return None
        return out
    except Exception:
        return None


def prepare_thumb(path):
    if not path or not os.path.isfile(path):
        return None, False
    if _thumb_is_valid(path):
        return path, False
    new_path = _make_thumb_compatible(path)
    if new_path is None:
        return None, False
    return new_path, True


def sorted_files(folder):
    entries = sorted(os.listdir(folder))
    return [
        os.path.join(folder, e) for e in entries
        if os.path.isfile(os.path.join(folder, e))
    ]


def default_limit(premium=False):
    return 3995 * 1024 * 1024 if premium else 1995 * 1024 * 1024


def file_kind(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    return "document"


def _stem_for_sidecar(path):
    base = os.path.basename(path)
    root, _ = os.path.splitext(base)
    while _PART_SUFFIX_RE.match(os.path.splitext(root)[1]):
        root, _ = os.path.splitext(root)
    return root


def _find_sidecar_image(path):
    folder = os.path.dirname(os.path.abspath(path)) or "."
    stem = _stem_for_sidecar(path)
    try:
        entries = os.listdir(folder)
    except OSError:
        return None
    abs_target = os.path.abspath(path)
    for entry in entries:
        full = os.path.join(folder, entry)
        if not os.path.isfile(full):
            continue
        if os.path.abspath(full) == abs_target:
            continue
        e_root, e_ext = os.path.splitext(entry)
        if e_ext.lower() not in IMAGE_EXTS:
            continue
        if e_root == stem:
            return full
    return None


class TelegramUploader:
    def __init__(self, api_id, api_hash, session_name=None,
                 premium=False, zip_option=None, thumb_path=None):
        if session_name is None:
            session_name = SESSION_PATH
        ensure_session_dir()
        self.client = Client(
            session_name,
            api_id=api_id,
            api_hash=api_hash,
            no_updates=True,
        )
        self.premium = premium
        self.limit = default_limit(premium)
        self.zip_option = zip_option
        self.thumb_path = thumb_path
        self.errors = []

    def start(self):
        silence_pyrogram_network()
        self.client.start()

    def stop(self):
        try:
            self.client.stop()
        except Exception:
            pass

    def _progress(self, current, total):
        pct = (current / total) * 100 if total else 0
        suffix = " [net retry]" if _NET_ERROR_FLAG["active"] else ""
        line = f"{bar(pct)} {human_size(current)}/{human_size(total)} {pct:5.1f}%{suffix}"
        print("\r" + line + " " * 4, end="", flush=True)

    def _done(self, total):
        line = f"{bar(100)} {human_size(total)}/{human_size(total)} 100.0%"
        print("\r" + line + " " * 4, flush=True)

    def _resolve_thumb(self, path, mode=None, sidecar_source=None):
        if mode in ("image", "video"):
            return None, False

        if self.thumb_path and os.path.isfile(self.thumb_path):
            return prepare_thumb(self.thumb_path)

        search_in = sidecar_source or path
        sidecar = _find_sidecar_image(search_in)
        if sidecar:
            print(f"Sidecar thumb: {os.path.basename(sidecar)} -> {os.path.basename(path)}")
            return prepare_thumb(sidecar)

        return None, False

    def _send_one(self, path, chat_id, mode, caption, sidecar_source=None):
        total = os.path.getsize(path)
        print(_short_name(caption))
        thumb_to_send, thumb_is_temp = self._resolve_thumb(path, mode, sidecar_source)
        try:
            if mode == "video":
                duration = get_video_duration(path)
                thumb = get_random_thumb(path)
                try:
                    self.client.send_video(
                        chat_id=chat_id,
                        video=path,
                        duration=duration,
                        thumb=thumb,
                        caption=caption,
                        supports_streaming=True,
                        progress=self._progress,
                    )
                finally:
                    if thumb and os.path.exists(thumb):
                        try:
                            os.remove(thumb)
                        except OSError:
                            pass
            elif mode == "image":
                self.client.send_photo(
                    chat_id=chat_id,
                    photo=path,
                    caption=caption,
                    progress=self._progress,
                )
            elif mode == "audio":
                duration, performer, title = get_audio_metadata(path)
                kwargs = {
                    "chat_id": chat_id,
                    "audio": path,
                    "caption": caption,
                    "duration": duration,
                    "performer": performer,
                    "title": title,
                    "progress": self._progress,
                }
                if thumb_to_send:
                    kwargs["thumb"] = thumb_to_send
                self.client.send_audio(**kwargs)
            else:
                kwargs = {
                    "chat_id": chat_id,
                    "document": path,
                    "caption": caption,
                    "progress": self._progress,
                }
                if thumb_to_send:
                    kwargs["thumb"] = thumb_to_send
                self.client.send_document(**kwargs)
        finally:
            if thumb_is_temp and thumb_to_send and os.path.exists(thumb_to_send):
                try:
                    os.remove(thumb_to_send)
                except OSError:
                    pass
        self._done(total)

    def _send_media_batch(self, paths, chat_id, caption=None):
        total = sum(os.path.getsize(p) for p in paths if os.path.exists(p))
        group_caption = caption if caption else f"Batch ({len(paths)} items)"
        print(_short_name(group_caption))
        media = []
        for i, p in enumerate(paths):
            kind = file_kind(p)
            cap = group_caption if i == 0 else None
            if kind == "video":
                duration = get_video_duration(p)
                media.append(InputMediaVideo(media=p, caption=cap, duration=duration, supports_streaming=True))
            else:
                media.append(InputMediaPhoto(media=p, caption=cap))
        self.client.send_media_group(chat_id=chat_id, media=media)
        self._done(total)

    def _should_compress(self, path):
        if self.zip_option is not None:
            return True
        return os.path.getsize(path) > self.limit

    def _compress(self, path):
        if self.zip_option is not None:
            volume, level = self.zip_option
        else:
            volume = self.limit
            level = 0
        return compress_to_volumes(path, volume, level)

    def send_file(self, path, chat_id=None, mode="document"):
        if chat_id is None:
            chat_id = "me"
        name = os.path.basename(path)
        if self._should_compress(path):
            try:
                parts = self._compress(path)
            except Exception as e:
                self.errors.append((path, str(e)))
                print(f"Error compressing {path}: {e}")
                return
            try:
                for part in parts:
                    part_name = os.path.basename(part)
                    try:
                        self._send_one(
                            part, chat_id, "document", part_name,
                            sidecar_source=path,
                        )
                    except Exception as e:
                        self.errors.append((part, str(e)))
                        print(f"Error sending {part}: {e}")
            finally:
                cleanup_volumes(parts)
        else:
            try:
                self._send_one(path, chat_id, mode, name)
            except Exception as e:
                self.errors.append((path, str(e)))
                print(f"Error sending {path}: {e}")

    def _send_paths(self, paths, chat_id, default_mode, media_batch):
        if media_batch:
            batchable = [p for p in paths if file_kind(p) in ("image", "video")]
            others = [p for p in paths if file_kind(p) not in ("image", "video")]
            for i in range(0, len(batchable), BATCH_SIZE):
                chunk = batchable[i:i + BATCH_SIZE]
                try:
                    self._send_media_batch(chunk, chat_id)
                except Exception as e:
                    self.errors.append((f"batch {i // BATCH_SIZE + 1}", str(e)))
                    print(f"Error sending batch: {e}")
            for p in others:
                self.send_file(p, chat_id=chat_id, mode=default_mode)
            return
        for p in paths:
            self.send_file(p, chat_id=chat_id, mode=default_mode)

    def send_folder(self, folder, chat_id=None, mode="document", media_batch=False):
        if not os.path.isdir(folder):
            raise NotADirectoryError(folder)
        if self.zip_option is not None or self._folder_needs_compress(folder):
            try:
                parts = self._compress(folder)
            except Exception as e:
                self.errors.append((folder, str(e)))
                print(f"Error compressing {folder}: {e}")
                return
            try:
                for part in parts:
                    part_name = os.path.basename(part)
                    try:
                        self._send_one(
                            part, chat_id, "document", part_name,
                            sidecar_source=folder,
                        )
                    except Exception as e:
                        self.errors.append((part, str(e)))
                        print(f"Error sending {part}: {e}")
            finally:
                cleanup_volumes(parts)
            return
        paths = sorted_files(folder)
        self._send_paths(paths, chat_id, mode, media_batch)

    def send_files(self, paths, chat_id=None, mode="document", media_batch=False):
        valid = [p for p in paths if os.path.isfile(p)]
        missing = [p for p in paths if not os.path.isfile(p)]
        for m in missing:
            self.errors.append((m, "file does not exist"))
            print(f"Error: file does not exist: {m}")
        if valid:
            self._send_paths(valid, chat_id, mode, media_batch)

    def set_profile_photo(self, path):
        self.client.set_profile_photo(photo=path)

    def set_profile_photos(self, paths, delay=0.0):
        import time
        valid = [p for p in paths if os.path.isfile(p)]
        missing = [p for p in paths if not os.path.isfile(p)]
        for m in missing:
            self.errors.append((m, "file does not exist"))
            print(f"Error: file does not exist: {m}")
        for p in valid:
            try:
                self.set_profile_photo(p)
                print(f"Profile photo set: {os.path.basename(p)}")
            except Exception as e:
                self.errors.append((p, str(e)))
                print(f"Error setting profile photo {p}: {e}")
            if delay > 0:
                time.sleep(delay)

    def _folder_needs_compress(self, folder):
        for f in sorted_files(folder):
            if os.path.getsize(f) > self.limit:
                return True
        return False
