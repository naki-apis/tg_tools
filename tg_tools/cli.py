import argparse
import os
import sys
from .config import ensure_credentials, clear_config
from .compressor import parse_zip_option
from .uploader import TelegramUploader, PROFILE_EXTS


def run_server():
    try:
        from .downloader import run_bot
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)

    try:
        run_bot()
    except KeyboardInterrupt:
        print()
        print("Force Exit (Ctrl+C)")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


def main():
    parser = argparse.ArgumentParser(
        prog="tg-tool",
        usage="tg-tool [-h] [--premium] [--chat CHAT_ID] <command> ...",
        description="CLI para subir archivos a Telegram con Pyrogram. Soporta archivos individuales, carpetas, compresión automática y bot de descarga.",
        epilog="Ejemplos:\n"
               "  tg-tool upload archivo.zip\n"
               "  tg-tool upload -F a.mp4 -F b.mp4 --video\n"
               "  tg-tool folder ./fotos -IB\n"
               "  tg-tool upload -T preview.jpg a.pdf b.zip c.doc\n"
               "  tg-tool profile ./avatar.png\n"
               "  tg-tool server\n"
               "  tg-tool logout",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--premium", action="store_true",
        help="Usa el límite de 3995MB para cuentas Premium (por defecto 1995MB)"
    )
    parser.add_argument(
        "--chat", metavar="CHAT_ID", default=None,
        help="ID del chat destino o @usuario (por defecto: Mensajes Guardados)"
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    p_upload = subparsers.add_parser(
        "upload", aliases=["u"],
        help="Sube uno o varios archivos",
        description="Sube archivos individuales. Puedes pasar varias rutas como argumentos posicionales.",
    )
    p_upload.add_argument("paths", nargs="*", default=None, metavar="RUTA",
                          help="Rutas de archivos a subir (una o varias)")
    p_upload.add_argument(
        "-F", "--file", dest="files", action="append", default=None, metavar="RUTA",
        help="Añade un archivo a la lista de subida (repetible)"
    )
    p_upload.add_argument(
        "--zip", metavar="TAMAÑO[/NIVEL]", default=None,
        help="Fuerza compresión 7z. TAMAÑO acepta K/M/G. NIVEL opcional 0-9"
    )
    p_upload.add_argument(
        "-T", "--thumb", metavar="RUTA", default=None,
        help="Miniatura personalizada para documentos (.jpg/.jpeg/.png)"
    )
    mode_group = p_upload.add_mutually_exclusive_group()
    mode_group.add_argument("--video", action="store_true", help="Envía como video")
    mode_group.add_argument("--image", action="store_true", help="Envía como foto")
    mode_group.add_argument("--audio", action="store_true", help="Envía como audio")
    p_upload.add_argument(
        "-IB", "--media-batch", dest="media_batch", action="store_true",
        help="Agrupa imágenes y videos en álbumes de hasta 10 elementos"
    )

    p_folder = subparsers.add_parser(
        "folder", aliases=["f"],
        help="Sube todos los archivos de un directorio",
        description="Sube todos los archivos de un directorio en orden alfabético.",
    )
    p_folder.add_argument("dir", metavar="DIR", help="Directorio a subir")
    p_folder.add_argument(
        "--zip", metavar="TAMAÑO[/NIVEL]", default=None,
        help="Fuerza compresión 7z. TAMAÑO acepta K/M/G. NIVEL opcional 0-9"
    )
    p_folder.add_argument(
        "-T", "--thumb", metavar="RUTA", default=None,
        help="Miniatura personalizada para documentos (.jpg/.jpeg/.png)"
    )
    mode_group_f = p_folder.add_mutually_exclusive_group()
    mode_group_f.add_argument("--video", action="store_true", help="Envía como video")
    mode_group_f.add_argument("--image", action="store_true", help="Envía como foto")
    mode_group_f.add_argument("--audio", action="store_true", help="Envía como audio")
    p_folder.add_argument(
        "-IB", "--media-batch", dest="media_batch", action="store_true",
        help="Agrupa imágenes y videos en álbumes de hasta 10 elementos"
    )

    p_profile = subparsers.add_parser(
        "profile", aliases=["p"],
        help="Establece la foto de perfil",
        description="Establece la foto de perfil desde archivo(s) o directorio.",
    )
    p_profile.add_argument("paths", nargs="*", default=None, metavar="RUTA",
                           help="Rutas de imágenes (una o varias)")
    p_profile.add_argument(
        "-F", "--file", dest="files", action="append", default=None, metavar="RUTA",
        help="Añade una imagen a la lista (repetible)"
    )
    p_profile.add_argument(
        "--folder", metavar="DIR", default=None,
        help="Usa todas las imágenes de este directorio"
    )
    p_profile.add_argument(
        "--delay", type=float, default=0.0, metavar="SEG",
        help="Segundos de espera entre subidas (por defecto 0)"
    )

    subparsers.add_parser(
        "server", aliases=["s"],
        help="Inicia el bot de descarga",
        description="Inicia el bot de descarga que escucha comandos .dl y .dlg.",
    )

    subparsers.add_parser(
        "logout", aliases=["lo"],
        help="Elimina credenciales API y sesiones guardadas",
        description="Elimina las credenciales API guardadas y los archivos de sesión.",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command in ("logout", "lo"):
        removed = clear_config()
        print("Credenciales API eliminadas (config.json vaciado).")
        if removed:
            print("Sesiones eliminadas:")
            for p in removed:
                print(f"  - {p}")
        else:
            print("No se encontraron archivos de sesión.")
        return

    if args.command in ("server", "s"):
        run_server()
        return

    thumb_path = None
    if getattr(args, "thumb", None):
        if not os.path.isfile(args.thumb):
            print(f"Error: el archivo de miniatura no existe: {args.thumb}", file=sys.stderr)
            sys.exit(1)
        ext = os.path.splitext(args.thumb)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png"):
            print(f"Error: la miniatura debe ser .jpg/.jpeg/.png: {args.thumb}", file=sys.stderr)
            sys.exit(1)
        thumb_path = args.thumb

    if args.command in ("profile", "p"):
        sources = []
        if args.paths:
            sources.extend(args.paths)
        if args.files:
            sources.extend(args.files)

        if args.folder and sources:
            parser.error("Use --folder o archivos individuales, no ambos.")

        if args.folder:
            profile_paths = sorted(
                os.path.join(args.folder, e)
                for e in os.listdir(args.folder)
                if os.path.isfile(os.path.join(args.folder, e))
                and os.path.splitext(e)[1].lower() in PROFILE_EXTS
            )
        elif sources:
            profile_paths = [s for s in sources if os.path.splitext(s)[1].lower() in PROFILE_EXTS]
        else:
            profile_paths = sorted(
                os.path.join(os.getcwd(), e)
                for e in os.listdir(os.getcwd())
                if os.path.isfile(os.path.join(os.getcwd(), e))
                and os.path.splitext(e)[1].lower() in PROFILE_EXTS
            )

        if not profile_paths:
            print("No se encontraron imágenes de perfil válidas (.jpg/.jpeg/.png).", file=sys.stderr)
            sys.exit(1)

        api_id, api_hash = ensure_credentials()
        uploader = None
        try:
            uploader = TelegramUploader(api_id, api_hash)
            uploader.start()
            uploader.set_profile_photos(profile_paths, delay=args.delay)
        except KeyboardInterrupt:
            print()
            print("Force Exit (Ctrl+C)")
            sys.stdout.flush()
            sys.stderr.flush()
            if uploader is not None:
                try:
                    uploader.stop()
                except Exception:
                    pass
            os._exit(0)
        else:
            if uploader is not None:
                uploader.stop()
            if uploader.errors:
                print()
                print("Errores:")
                for path, msg in uploader.errors:
                    print(f"  - {path}: {msg}")
        return

    if args.command in ("upload", "u"):
        sources = []
        if args.paths:
            sources.extend(args.paths)
        if args.files:
            sources.extend(args.files)

        if not sources:
            parser.error("Debe proporcionar al menos una ruta o -F/--file.")

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
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)

        api_id, api_hash = ensure_credentials()

        uploader = None
        try:
            uploader = TelegramUploader(
                api_id, api_hash,
                premium=args.premium,
                zip_option=zip_option,
                thumb_path=thumb_path,
            )
            uploader.start()
            for s in sources:
                if not os.path.isfile(s):
                    print(f"Error: el archivo no existe: {s}", file=sys.stderr)
                    sys.exit(1)
            uploader.send_files(sources, chat_id=args.chat, mode=mode, media_batch=args.media_batch)
        except KeyboardInterrupt:
            print()
            print("Force Exit (Ctrl+C)")
            sys.stdout.flush()
            sys.stderr.flush()
            if uploader is not None:
                try:
                    uploader.stop()
                except Exception:
                    pass
            os._exit(0)
        else:
            if uploader is not None:
                uploader.stop()
            if uploader.errors:
                print()
                print("Errores:")
                for path, msg in uploader.errors:
                    print(f"  - {path}: {msg}")
        return

    if args.command in ("folder", "f"):
        if not os.path.isdir(args.dir):
            print(f"Error: el directorio no existe: {args.dir}", file=sys.stderr)
            sys.exit(1)

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
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)

        api_id, api_hash = ensure_credentials()

        uploader = None
        try:
            uploader = TelegramUploader(
                api_id, api_hash,
                premium=args.premium,
                zip_option=zip_option,
                thumb_path=thumb_path,
            )
            uploader.start()
            uploader.send_folder(args.dir, chat_id=args.chat, mode=mode, media_batch=args.media_batch)
        except KeyboardInterrupt:
            print()
            print("Force Exit (Ctrl+C)")
            sys.stdout.flush()
            sys.stderr.flush()
            if uploader is not None:
                try:
                    uploader.stop()
                except Exception:
                    pass
            os._exit(0)
        else:
            if uploader is not None:
                uploader.stop()
            if uploader.errors:
                print()
                print("Errores:")
                for path, msg in uploader.errors:
                    print(f"  - {path}: {msg}")
        return
