from pyrogram import Client, filters
from pyrogram.types import Message, InputMediaPhoto, InputMediaVideo
import asyncio
import glob
import os
import re
import shlex
import subprocess
import traceback
from urllib.parse import urlparse
from .config import ensure_credentials, SESSION_PATH, ensure_session_dir
from .compressor import parse_zip_option, compress_to_volumes, cleanup_volumes
from .uploader import (
    get_video_duration,
    get_audio_metadata,
    get_random_thumb,
    prepare_thumb,
    file_kind,
    sorted_files,
    default_limit,
    BATCH_SIZE,
    _find_sidecar_image,
    silence_pyrogram_network,
    _short_name,
    _NET_ERROR_FLAG,
)

API_ID, API_HASH = ensure_credentials()
ensure_session_dir()

app = Client(
    SESSION_PATH,
    api_id=API_ID,
    api_hash=API_HASH,
)

PARTIAL_SUFFIXES = {
    ".temp": True,
}


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


def parse_telegram_url(url):
    try:
        parsed = urlparse(url)
        if parsed.netloc != "t.me":
            return None, None
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) < 2:
            return None, None
        if path_parts[0] == "c":
            if len(path_parts) >= 3:
                chat_id = int(path_parts[1])
                if chat_id > 0:
                    chat_id = int(f"-100{chat_id}")
                message_id = int(path_parts[2])
                return chat_id, message_id
        else:
            username = path_parts[0]
            message_id = int(path_parts[1])
            return f"@{username}", message_id
    except Exception:
        return None, None


def sanitize_filename(name):
    name = os.path.basename(name)
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    return name or "file"


def get_media_filename(msg):
    if msg.document and msg.document.file_name:
        return msg.document.file_name
    if msg.video and msg.video.file_name:
        return msg.video.file_name
    if msg.audio and msg.audio.file_name:
        return msg.audio.file_name
    if msg.photo:
        return f"photo_{msg.id}.jpg"
    if msg.video:
        return f"video_{msg.id}.mp4"
    if msg.audio:
        return f"audio_{msg.id}.mp3"
    if msg.voice:
        return f"voice_{msg.id}.ogg"
    if msg.sticker:
        ext = ".webp"
        if msg.sticker.is_animated:
            ext = ".tgs"
        elif msg.sticker.is_video:
            ext = ".webm"
        return f"sticker_{msg.id}{ext}"
    return f"file_{msg.id}"


def _raw_args(message, command_name):
    text = message.text or message.caption or ""
    prefix = "." + command_name
    if not text.startswith(prefix):
        return None
    rest = text[len(prefix):]
    if rest and not rest[0].isspace():
        return None
    return rest.strip()


async def resolve_source(message, command_name):
    args = _raw_args(message, command_name)
    if args is None:
        return None, None, None

    source_chat_id = None
    msg_id = None
    custom_name = None

    if args.startswith("https://t.me/"):
        source_chat_id, msg_id = parse_telegram_url(args)
        if not source_chat_id or not msg_id:
            return None, None, None
    elif args and message.reply_to_message:
        source_chat_id = message.chat.id
        msg_id = message.reply_to_message.id
        custom_name = sanitize_filename(args)
    elif not args and message.reply_to_message:
        source_chat_id = message.chat.id
        msg_id = message.reply_to_message.id
    elif args and not message.reply_to_message:
        parts = args.split()
        if len(parts) == 1:
            source_chat_id = message.chat.id
            try:
                msg_id = int(parts[0])
            except ValueError:
                print(f"Usage: {command_name} (reply), {command_name} name.ext (reply), {command_name} https://t.me/user/123, {command_name} @user 123 or {command_name} -100123 456")
                return None, None, None
        elif len(parts) == 2:
            chat_input = parts[0]
            try:
                msg_id = int(parts[1])
            except ValueError:
                return None, None, None
            if chat_input.startswith("@"):
                source_chat_id = chat_input
            else:
                try:
                    source_chat_id = int(chat_input)
                except ValueError:
                    try:
                        chat = await message._client.get_chat(chat_input)
                        source_chat_id = chat.id
                    except Exception:
                        return None, None, None
        else:
            print(f"Usage: {command_name} (reply), {command_name} name.ext (reply), {command_name} https://t.me/user/123, {command_name} @user 123 or {command_name} -100123 456")
            return None, None, None
    else:
        print(f"Usage: {command_name} (reply), {command_name} name.ext (reply), {command_name} https://t.me/user/123, {command_name} @user 123 or {command_name} -100123 456")
        return None, None, None

    return source_chat_id, msg_id, custom_name


async def download_single(client, target_msg, custom_name=None):
    if custom_name:
        filename = custom_name
    else:
        filename = sanitize_filename(get_media_filename(target_msg))

    file_path = os.path.join(os.getcwd(), filename)
    last_edit = {"t": 0.0}

    print(_short_name(filename))

    async def progress(current, total):
        now = asyncio.get_event_loop().time()
        if now - last_edit["t"] < 2.0 and current < total:
            return
        last_edit["t"] = now
        pct = (current / total) * 100 if total else 0
        suffix = " [net retry]" if _NET_ERROR_FLAG["active"] else ""
        line = f"{bar(pct)} {human_size(current)}/{human_size(total)} {pct:5.1f}%{suffix}"
        print("\r" + line + " " * 4, end="", flush=True)

    file_path = await target_msg.download(
        file_name=file_path,
        progress=progress,
    )
    size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
    final = f"{bar(100)} {human_size(size)}/{human_size(size)} 100.0%"
    print("\r" + final + " " * 4, flush=True)
    print(f"Saved: {file_path}")


def _remote_media_size(msg):
    for attr in ("document", "video", "audio", "voice", "video_note", "animation"):
        m = getattr(msg, attr, None)
        if m and getattr(m, "file_size", None):
            return m.file_size
    if getattr(msg, "photo", None):
        sizes = [s.file_size for s in msg.photo.sizes if getattr(s, "file_size", None)]
        return max(sizes) if sizes else None
    if getattr(msg, "sticker", None):
        return getattr(msg.sticker, "file_size", None)
    return None


def _find_partial(target_path, remote_size):
    candidates = []
    if os.path.isfile(target_path):
        try:
            sz = os.path.getsize(target_path)
            if remote_size is None or sz <= remote_size:
                candidates.append((target_path, False, sz))
        except OSError:
            pass
    for suf, is_temp in PARTIAL_SUFFIXES.items():
        if not suf:
            continue
        candidate = target_path + suf
        if not os.path.isfile(candidate):
            continue
        try:
            sz = os.path.getsize(candidate)
        except OSError:
            continue
        if remote_size is not None and sz > remote_size:
            continue
        candidates.append((candidate, is_temp, sz))
    if not candidates:
        return None, False, 0
    candidates.sort(key=lambda x: (x[1], -x[2]))
    p, is_temp, sz = candidates[0]
    return p, is_temp, sz


def _final_name_for(partial_path, target_path):
    if partial_path == target_path:
        return target_path
    for suf, is_temp in PARTIAL_SUFFIXES.items():
        if not suf or not is_temp:
            continue
        if partial_path.endswith(suf):
            stripped = partial_path[:-len(suf)]
            if stripped:
                return stripped
    return target_path


async def download_resume_single(client, target_msg, custom_name=None):
    if custom_name:
        filename = custom_name
    else:
        filename = sanitize_filename(get_media_filename(target_msg))

    target_path = os.path.join(os.getcwd(), filename)
    remote_size = _remote_media_size(target_msg)
    if not remote_size:
        await download_single(client, target_msg, custom_name)
        return

    print(_short_name(filename))

    chunk_size = 1024 * 1024
    written = 0
    offset = 0

    partial_path, is_temp, local_size = _find_partial(target_path, remote_size)

    if partial_path is None:
        partial_path = target_path
        is_temp = False
        local_size = 0
        print(f"New download: {os.path.basename(partial_path)}")
    elif partial_path == target_path and local_size == remote_size:
        print(f"Already complete: {target_path}")
        print(f"Saved: {target_path}")
        return
    else:
        label = "temp" if is_temp else "existing"
        print(
            f"Found {label}: {os.path.basename(partial_path)} "
            f"({human_size(local_size)}/{human_size(remote_size)})"
        )
        chunks_done = local_size // chunk_size
        resume_chunk = max(0, chunks_done - 1)
        truncate_to = resume_chunk * chunk_size
        if truncate_to < local_size:
            with open(partial_path, "r+b") as f:
                f.truncate(truncate_to)
        offset = resume_chunk
        written = truncate_to
        print(f"Resume: offset={offset} chunk(s) rewind=1 MiB (truncate_to={human_size(truncate_to)})")

    last_edit = {"t": 0.0}

    def _progress(written_now):
        now = asyncio.get_event_loop().time()
        if now - last_edit["t"] < 2.0 and written_now < remote_size:
            return
        last_edit["t"] = now
        pct = (written_now / remote_size) * 100 if remote_size else 0
        suffix = " [net retry]" if _NET_ERROR_FLAG["active"] else ""
        line = f"{bar(pct)} {human_size(written_now)}/{human_size(remote_size)} {pct:5.1f}%{suffix}"
        print("\r" + line + " " * 4, end="", flush=True)

    _progress(written)

    mode = "ab" if offset > 0 or local_size > 0 else "wb"
    with open(partial_path, mode) as f:
        async for chunk in client.stream_media(target_msg, offset=offset):
            f.write(chunk)
            written += len(chunk)
            _progress(written)

    size = os.path.getsize(partial_path) if os.path.exists(partial_path) else 0
    final = f"{bar(100)} {human_size(size)}/{human_size(remote_size)} 100.0%"
    print("\r" + final + " " * 4, flush=True)

    if size == remote_size and partial_path != target_path and is_temp:
        final_path = _final_name_for(partial_path, target_path)
        try:
            os.replace(partial_path, final_path)
            partial_path = final_path
            print(f"Renamed: {os.path.basename(final_path)}")
        except OSError as e:
            print(f"Warning: could not rename {os.path.basename(partial_path)} -> {os.path.basename(final_path)}: {e}")
    elif size != remote_size:
        print(f"Warning: size mismatch (local={size} remote={remote_size}), run .dls again to resume")

    print(f"Saved: {partial_path}")


async def resolve_chat(client, source_chat_id):
    if isinstance(source_chat_id, str) and source_chat_id.startswith("@"):
        chat = await client.get_chat(source_chat_id)
        return chat.id
    return source_chat_id


async def _react(client, message, emoji):
    try:
        await client.send_reaction(
            chat_id=message.chat.id,
            message_id=message.id,
            emoji=emoji,
        )
    except Exception:
        pass


async def _finalize_reaction(client, message, success):
    try:
        await client.send_reaction(
            chat_id=message.chat.id,
            message_id=message.id,
            emoji="",
        )
    except Exception:
        pass
    try:
        await client.send_reaction(
            chat_id=message.chat.id,
            message_id=message.id,
            emoji="👍" if success else "👎",
        )
    except Exception:
        pass


async def _send_output(client, message, output):
    if not output:
        return
    max_len = 4000
    if len(output) <= max_len:
        await client.send_message(
            chat_id=message.chat.id,
            text=output,
            reply_to_message_id=message.id,
        )
        return
    for i in range(0, len(output), max_len):
        chunk = output[i:i + max_len]
        await client.send_message(
            chat_id=message.chat.id,
            text=chunk,
            reply_to_message_id=message.id,
        )


@app.on_message(
    (filters.private | filters.group | filters.me)
    & ~filters.channel
    & filters.command("dl", prefixes=".")
)
async def download_handler(client: Client, message: Message):
    await _react(client, message, "🤔")
    success = False
    try:
        source_chat_id, msg_id, custom_name = await resolve_source(message, "dl")
        if source_chat_id is None or msg_id is None:
            return

        source_chat_id = await resolve_chat(client, source_chat_id)

        try:
            target_msg = await client.get_messages(source_chat_id, msg_id)
        except Exception as e:
            print(f"Error: could not fetch message: {str(e)[:150]}")
            return

        if not target_msg or target_msg.empty:
            return

        if not target_msg.media:
            return

        await download_single(client, target_msg, custom_name)
        success = True

    except Exception as e:
        print(f"Error: {str(e)[:150]}")
    finally:
        await _finalize_reaction(client, message, success)


@app.on_message(
    (filters.private | filters.group | filters.me)
    & ~filters.channel
    & filters.command("dls", prefixes=".")
)
async def download_resume_handler(client: Client, message: Message):
    await _react(client, message, "🤔")
    success = False
    try:
        source_chat_id, msg_id, custom_name = await resolve_source(message, "dls")
        if source_chat_id is None or msg_id is None:
            return

        source_chat_id = await resolve_chat(client, source_chat_id)

        try:
            target_msg = await client.get_messages(source_chat_id, msg_id)
        except Exception as e:
            print(f"Error: could not fetch message: {str(e)[:150]}")
            return

        if not target_msg or target_msg.empty:
            return

        if not target_msg.media:
            return

        await download_resume_single(client, target_msg, custom_name)
        success = True

    except Exception as e:
        print(f"Error: {str(e)[:150]}")
    finally:
        await _finalize_reaction(client, message, success)


@app.on_message(
    (filters.private | filters.group | filters.me)
    & ~filters.channel
    & filters.command("dlg", prefixes=".")
)
async def download_group_handler(client: Client, message: Message):
    await _react(client, message, "🤔")
    success = False
    try:
        source_chat_id, msg_id, custom_name = await resolve_source(message, "dlg")
        if source_chat_id is None or msg_id is None:
            return

        source_chat_id = await resolve_chat(client, source_chat_id)

        try:
            target_msg = await client.get_messages(source_chat_id, msg_id)
        except Exception as e:
            print(f"Error: could not fetch message: {str(e)[:150]}")
            return

        if not target_msg or target_msg.empty:
            return

        if not target_msg.media:
            return

        if not target_msg.media_group_id:
            await download_single(client, target_msg, custom_name)
            success = True
            return

        try:
            media_group = await client.get_media_group(source_chat_id, msg_id)
        except Exception as e:
            print(f"Error: could not fetch media group: {str(e)[:150]}")
            return

        for idx, single_msg in enumerate(media_group, start=1):
            if not single_msg.media:
                continue
            name = None
            if custom_name and len(media_group) == 1:
                name = custom_name
            elif custom_name:
                base, ext = os.path.splitext(custom_name)
                name = f"{base}_{idx}{ext}"
            try:
                await download_single(client, single_msg, name)
            except Exception as e:
                print(f"Error downloading item {idx}: {str(e)[:150]}")

        success = True

    except Exception as e:
        print(f"Error: {str(e)[:150]}")
    finally:
        await _finalize_reaction(client, message, success)


async def _run_cmd(client, message, command_name):
    await _react(client, message, "🤔")
    success = False
    try:
        text = message.text or message.caption or ""
        prefix = "." + command_name
        if not text.startswith(prefix):
            return
        rest = text[len(prefix):]
        if rest and not rest[0].isspace():
            return
        command = rest.strip()

        no_telegram_print = False
        if command.startswith("-ntp"):
            after = command[4:]
            if not after or after[0].isspace():
                no_telegram_print = True
                command = after.strip()

        if not command:
            print(f"Usage: .{command_name} [-ntp] <command>")
            return
        print(f"Executing: {command}")
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
        )
        parts = []
        if result.stdout:
            parts.append(result.stdout)
        if result.stderr:
            parts.append(result.stderr)
        parts.append(f"Exit code: {result.returncode}")
        output = "\n".join(parts).rstrip()
        print(output)
        if not no_telegram_print:
            await _send_output(client, message, output)
        success = result.returncode == 0
    except Exception as e:
        print(f"Error: {str(e)[:150]}")
        try:
            await _send_output(client, message, f"Error: {str(e)[:150]}")
        except Exception:
            pass
    finally:
        await _finalize_reaction(client, message, success)


@app.on_message(
    (filters.private | filters.group | filters.me)
    & ~filters.channel
    & filters.command("cmd", prefixes=".")
)
async def cmd_handler(client: Client, message: Message):
    await _run_cmd(client, message, "cmd")


@app.on_message(
    (filters.private | filters.group | filters.me)
    & ~filters.channel
    & filters.command("c", prefixes=".")
)
async def c_handler(client: Client, message: Message):
    await _run_cmd(client, message, "c")


def _parse_up_args(message):
    text = message.text or message.caption or ""
    prefix = ".up"
    if not text.startswith(prefix):
        return None
    rest = text[len(prefix):]
    if rest and not rest[0].isspace():
        return None
    return rest.strip()


def _build_up_parser():
    import argparse

    parser = argparse.ArgumentParser(prog=".up", add_help=False)
    parser.add_argument("paths", nargs="*", default=None, metavar="RUTA")
    parser.add_argument("-F", "--file", dest="files", action="append", default=None, metavar="RUTA")
    parser.add_argument("--zip", metavar="TAMAÑO[/NIVEL]", default=None)
    parser.add_argument("-T", "--thumb", metavar="RUTA", default=None)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--video", action="store_true")
    mode_group.add_argument("--image", action="store_true")
    mode_group.add_argument("--audio", action="store_true")
    parser.add_argument("-IB", "--media-batch", dest="media_batch", action="store_true")
    parser.add_argument("--premium", action="store_true")
    parser.add_argument("--chat", metavar="CHAT_ID", default=None)
    return parser


def _expand_sources(raw_sources):
    expanded = []
    for s in raw_sources:
        if any(ch in s for ch in "*?["):
            matches = glob.glob(s, recursive=True)
            if matches:
                expanded.extend(matches)
            else:
                expanded.append(s)
        else:
            expanded.append(s)
    return expanded


class _AsyncUploader:
    def __init__(self, client, premium=False, zip_option=None, thumb_path=None):
        self.client = client
        self.premium = premium
        self.limit = default_limit(premium)
        self.zip_option = zip_option
        self.thumb_path = thumb_path
        self.errors = []

    def _progress(self, current, total):
        pct = (current / total) * 100 if total else 0
        suffix = " [net retry]" if _NET_ERROR_FLAG["active"] else ""
        line = f"{bar(pct)} {human_size(current)}/{human_size(total)} {pct:5.1f}%{suffix}"
        print("\r" + line + " " * 4, end="", flush=True)

    def _done(self, total):
        line = f"{bar(100)} {human_size(total)}/{human_size(total)} 100.0%"
        print("\r" + line + " " * 4, flush=True)

    def _resolve_thumb(self, path, mode, sidecar_source=None):
        if mode in ("image", "video"):
            return None, False
        if self.thumb_path and os.path.isfile(self.thumb_path):
            return prepare_thumb(self.thumb_path)
        base, _ = os.path.splitext(path)
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            candidate = base + ext
            if os.path.isfile(candidate):
                print(f"Thumb: {os.path.basename(candidate)} -> {os.path.basename(path)}")
                return prepare_thumb(candidate)
        search_in = sidecar_source or path
        sidecar = _find_sidecar_image(search_in)
        if sidecar:
            print(f"Sidecar thumb: {os.path.basename(sidecar)} -> {os.path.basename(path)}")
            return prepare_thumb(sidecar)
        return None, False

    async def _send_one(self, path, chat_id, mode, caption, sidecar_source=None):
        total = os.path.getsize(path)
        print(_short_name(caption))
        thumb_to_send, thumb_is_temp = self._resolve_thumb(path, mode, sidecar_source)
        try:
            if mode == "video":
                duration = get_video_duration(path)
                thumb = get_random_thumb(path)
                try:
                    await self.client.send_video(
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
                await self.client.send_photo(
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
                await self.client.send_audio(**kwargs)
            else:
                kwargs = {
                    "chat_id": chat_id,
                    "document": path,
                    "caption": caption,
                    "progress": self._progress,
                }
                if thumb_to_send:
                    kwargs["thumb"] = thumb_to_send
                await self.client.send_document(**kwargs)
        finally:
            if thumb_is_temp and thumb_to_send and os.path.exists(thumb_to_send):
                try:
                    os.remove(thumb_to_send)
                except OSError:
                    pass
        self._done(total)

    async def _send_media_batch(self, paths, chat_id, caption=None):
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
        await self.client.send_media_group(chat_id=chat_id, media=media)
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

    def _folder_needs_compress(self, folder):
        for f in sorted_files(folder):
            if os.path.getsize(f) > self.limit:
                return True
        return False

    async def send_file(self, path, chat_id, mode="document"):
        name = os.path.basename(path)
        if self._should_compress(path):
            try:
                parts = await asyncio.to_thread(self._compress, path)
            except Exception as e:
                self.errors.append((path, str(e)))
                print(f"Error compressing {path}: {e}")
                return
            try:
                for part in parts:
                    part_name = os.path.basename(part)
                    try:
                        await self._send_one(
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
                await self._send_one(path, chat_id, mode, name)
            except Exception as e:
                self.errors.append((path, str(e)))
                print(f"Error sending {path}: {e}")

    async def _send_paths(self, paths, chat_id, default_mode, media_batch):
        if media_batch:
            batchable = [p for p in paths if file_kind(p) in ("image", "video")]
            others = [p for p in paths if file_kind(p) not in ("image", "video")]
            for i in range(0, len(batchable), BATCH_SIZE):
                chunk = batchable[i:i + BATCH_SIZE]
                try:
                    await self._send_media_batch(chunk, chat_id)
                except Exception as e:
                    self.errors.append((f"batch {i // BATCH_SIZE + 1}", str(e)))
                    print(f"Error sending batch: {e}")
            for p in others:
                await self.send_file(p, chat_id=chat_id, mode=default_mode)
            return
        for p in paths:
            await self.send_file(p, chat_id=chat_id, mode=default_mode)

    async def send_folder(self, folder, chat_id, mode="document", media_batch=False):
        if not os.path.isdir(folder):
            raise NotADirectoryError(folder)
        needs = await asyncio.to_thread(self._folder_needs_compress, folder)
        if self.zip_option is not None or needs:
            try:
                parts = await asyncio.to_thread(self._compress, folder)
            except Exception as e:
                self.errors.append((folder, str(e)))
                print(f"Error compressing {folder}: {e}")
                return
            try:
                for part in parts:
                    part_name = os.path.basename(part)
                    try:
                        await self._send_one(
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
        await self._send_paths(paths, chat_id, mode, media_batch)

    async def send_files(self, paths, chat_id, mode="document", media_batch=False):
        valid = [p for p in paths if os.path.isfile(p)]
        missing = [p for p in paths if not os.path.isfile(p)]
        for m in missing:
            self.errors.append((m, "file does not exist"))
            print(f"Error: file does not exist: {m}")
        if valid:
            await self._send_paths(valid, chat_id, mode, media_batch)


async def _run_upload_async(client, args, thumb_path, default_chat):
    if args.video:
        mode = "video"
    elif args.image:
        mode = "image"
    elif args.audio:
        mode = "audio"
    else:
        mode = "document"

    zip_option = None
    if args.zip is not None:
        try:
            zip_option = parse_zip_option(args.zip, premium=args.premium)
        except ValueError as e:
            return f"Error: {e}", False

    chat_id = args.chat if args.chat is not None else default_chat

    uploader = _AsyncUploader(
        client,
        premium=args.premium,
        zip_option=zip_option,
        thumb_path=thumb_path,
    )

    sources = []
    if args.paths:
        sources.extend(args.paths)
    if args.files:
        sources.extend(args.files)
    sources = _expand_sources(sources)
    for s in sources:
        if os.path.isdir(s):
            await uploader.send_folder(s, chat_id=chat_id, mode=mode, media_batch=args.media_batch)
        elif os.path.isfile(s):
            await uploader.send_files([s], chat_id=chat_id, mode=mode, media_batch=args.media_batch)
        else:
            uploader.errors.append((s, "no existe"))

    if uploader.errors:
        lines = ["Errores:"]
        for path, msg in uploader.errors:
            lines.append(f"  - {path}: {msg}")
        return "\n".join(lines), False
    return "Subida completada.", True


@app.on_message(
    (filters.private | filters.group | filters.me)
    & ~filters.channel
    & filters.command("up", prefixes=".")
)
async def up_handler(client: Client, message: Message):
    await _react(client, message, "🤔")
    success = False
    try:
        raw = _parse_up_args(message)
        if raw is None:
            return
        if not raw:
            print("Usage: .up [--premium] [--chat CHAT] [--zip TAMAÑO[/NIVEL]] [-T thumb] [--video|--image|--audio] [-IB] RUTA... [-F RUTA ...]")
            return
        try:
            argv = shlex.split(raw)
        except ValueError as e:
            print(f"Error: {e}")
            return

        parser = _build_up_parser()
        try:
            args = parser.parse_args(argv)
        except SystemExit:
            return

        thumb_path = None
        if getattr(args, "thumb", None):
            if not os.path.isfile(args.thumb):
                await _send_output(client, message, f"Error: miniatura no existe: {args.thumb}")
                return
            if os.path.splitext(args.thumb)[1].lower() not in (".jpg", ".jpeg", ".png"):
                await _send_output(client, message, f"Error: miniatura debe ser .jpg/.jpeg/.png: {args.thumb}")
                return
            thumb_path = args.thumb

        sources = []
        if args.paths:
            sources.extend(args.paths)
        if args.files:
            sources.extend(args.files)
        if not sources:
            await _send_output(client, message, "Error: debes indicar al menos una ruta o -F/--file.")
            return

        output, ok = await _run_upload_async(
            client, args, thumb_path,
            default_chat=message.chat.id,
        )
        success = ok
        await _send_output(client, message, output)

    except Exception as e:
        print(f"Error: {str(e)[:150]}")
        try:
            await _send_output(client, message, f"Error: {str(e)[:150]}")
        except Exception:
            pass
    finally:
        await _finalize_reaction(client, message, success)


def _silence_peer_errors(loop, context):
    exc = context.get("exception")
    if isinstance(exc, ValueError) and "Peer id invalid" in str(exc):
        return
    print("[loop error]", context.get("message"))
    if exc:
        traceback.print_exception(type(exc), exc, exc.__traceback__)
    else:
        loop.default_exception_handler(context)


async def _main():
    loop = asyncio.get_event_loop()
    loop.set_exception_handler(_silence_peer_errors)
    await app.start()
    print("Bot started (download mode)")
    await asyncio.Future()


def run_bot():
    silence_pyrogram_network()
    app.run(_main())
