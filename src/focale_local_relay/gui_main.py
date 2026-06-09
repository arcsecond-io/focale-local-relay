try:
    from .gui import main
except ImportError:
    # PyInstaller can execute this file as a top-level script on Windows.
    from focale_local_relay.gui import main


if __name__ == "__main__":
    raise SystemExit(main())
