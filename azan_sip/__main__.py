"""Command line: python -m azan_sip {panel,run,times,test,box-password,factory-reset} [-c config.toml]"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
from datetime import date, datetime, timedelta

from .app import ADHAN_PRAYERS, AzanApp, Clip


def main(argv=None):
    # Global options are accepted both before and after the command
    # (e.g. "-c config.toml run --log-file azan.log" or "run -c config.toml").
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS, help="config file (default: config.toml)")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="debug logging")
    common.add_argument("--log-file", default=argparse.SUPPRESS, help="also write the log to this file")

    ap = argparse.ArgumentParser(prog="azan-sip", parents=[common],
                                 description="Automatic azan over VoIP phones (SIP / multicast)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", parents=[common], help="run the scheduler + control panel without opening a browser")
    r.add_argument("--box", action="store_true",
                   help="Azan SIP Box: open the panel to the network (port 8765 and 80), password needed for changes")
    sub.add_parser("panel", parents=[common], help="run and open the control panel in the browser")

    t = sub.add_parser("times", parents=[common], help="show prayer times")
    t.add_argument("--days", type=int, default=7)
    t.add_argument("--from", dest="start", help="start date YYYY-MM-DD (default today)")

    s = sub.add_parser("test", parents=[common], help="broadcast now, to check phones and audio")
    s.add_argument("--prayer", choices=ADHAN_PRAYERS, help="play the audio configured for this prayer")
    s.add_argument("--file", help="play this audio file")
    s.add_argument("--tone", action="store_true", help="play a short test chime")
    s.add_argument("--reminder", action="store_true", help="play the reminder audio")
    s.add_argument("--seconds", type=float, help="only play the first N seconds")

    b = sub.add_parser("box-password", parents=[common], help="Azan SIP Box: set the control panel password")
    b.add_argument("--factory", action="store_true", help="set the factory (sticker) password instead of the owner's")
    b.add_argument("--serial", default="", help="serial number to keep with the factory password")
    b.add_argument("--stdin", action="store_true", help="read the password from standard input (for scripts)")

    sub.add_parser("factory-reset", parents=[common],
                   help="Azan SIP Box: remove settings, recordings and the owner's password (stop the service first)")

    args = ap.parse_args(argv)
    for name, default in (("config", "config.toml"), ("verbose", False), ("log_file", None)):
        if not hasattr(args, name):
            setattr(args, name, default)
    if args.cmd == "run":
        # Same service as the desktop launcher (scheduler + control panel), without opening a browser.
        from .web import serve
        sys.exit(serve(args.config, open_browser=False, verbose=args.verbose, box=args.box))

    if args.cmd in ("box-password", "factory-reset"):
        sys.exit(box_command(args))

    if args.cmd == "panel":
        from .web import serve
        sys.exit(serve(args.config, open_browser=True, verbose=args.verbose))

    handlers = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    try:
        app = AzanApp(args.config)
    except FileNotFoundError as exc:
        sys.exit(f"Config error: {exc}. Copy config.example.toml to config.toml and edit it.")
    except (ValueError, KeyError) as exc:
        sys.exit(f"Config error: {exc}")

    if args.cmd == "times":
        start = date.fromisoformat(args.start) if args.start else app.times.now().date()
        names = ["fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha"]
        print(f"Method: {app.settings.method}   Asr: {app.settings.asr}   "
              f"Location: {app.settings.latitude}, {app.settings.longitude} ({app.settings.timezone})")
        print(f"{'Date':<16}" + "".join(f"{n.title():>9}" for n in names))
        for i in range(args.days):
            d = start + timedelta(days=i)
            tt = app.times.for_date(d)
            print(f"{d:%a %d %b %Y}" + "".join(f"{tt[n]:%H:%M}".rjust(9) for n in names))
        return

    stop = app.stop
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stop.set())

    if args.cmd == "test":
        a = app.cfg.get("audio", {})
        vol, lead = float(a.get("volume_db", 0)), int(a.get("lead_in_ms", 400))
        if args.tone:
            clip = Clip("tone", app.base, vol, lead)
        elif args.file:
            clip = Clip(args.file, app.base, vol, lead)
        elif args.reminder:
            clip = app.reminder_clip or sys.exit("No reminder audio configured")
        else:
            clip = app.clip_for(args.prayer or "dhuhr", app.times.now().date())
        if args.seconds:
            clip.max_seconds = args.seconds
        try:
            clip.check()
        except FileNotFoundError as exc:
            sys.exit(str(exc))
        ok = app.broadcast(clip, "test")
        sys.exit(0 if ok else 1)

def box_command(args) -> int:
    from pathlib import Path
    from .auth import Auth
    base = Path(args.config).resolve().parent
    auth = Auth(base / "auth")
    if args.cmd == "factory-reset":
        for name in ("config.toml", "config.toml.bak", "config.tmp", "timetable.csv"):
            (base / name).unlink(missing_ok=True)
        if (base / "audio").is_dir():
            for f in (base / "audio").iterdir():
                if f.is_file() and f.name != ".gitkeep":
                    f.unlink()
        auth.reset_to_factory()
        print("Factory reset done. Start the service again.")
        return 0
    if args.stdin:
        pw = sys.stdin.readline().rstrip("\r\n")
    else:
        import getpass
        pw = getpass.getpass("New password: ")
        if getpass.getpass("Type it again: ") != pw:
            print("The passwords don't match", file=sys.stderr)
            return 1
    try:
        if args.factory:
            auth.set_factory_password(pw, args.serial)
        else:
            auth.set_password(pw)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("Factory password saved." if args.factory else "Password saved. Restart the service to sign everyone out.")
    return 0


if __name__ == "__main__":
    main()
