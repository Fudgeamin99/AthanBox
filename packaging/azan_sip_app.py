"""Entry point for the packaged AzanSIP.exe (built with PyInstaller - see .github/workflows/release.yml)."""
import sys


def self_test() -> int:
    """Used by the updater and the build: exit 0 only if the program is complete and imports cleanly."""
    try:
        import azan_sip
        from azan_sip import app, audio, live, look, praytimes, rtp, sip, sipreg, updater, web  # noqa: F401
        if not all((web.STATIC / f).is_file() for f in ("index.html", "display.html", "hls.min.js")):
            return 2
        praytimes.PrayerTimes(praytimes.Settings(51.5, -0.12, "local")).for_date(__import__("datetime").date.today())
        return 0 if azan_sip.__version__ else 3
    except Exception:
        return 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    if "--version" in sys.argv:
        import azan_sip
        print(azan_sip.__version__)
        sys.exit(0)
    from azan_sip.web import main
    main([a for a in sys.argv[1:] if a not in ("--self-test", "--version")])
