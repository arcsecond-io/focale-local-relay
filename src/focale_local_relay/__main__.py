import sys


if __name__ == "__main__":
    if len(sys.argv) > 1:
        from .cli import main as cli_main

        cli_main()
    else:
        from .gui import main as gui_main

        raise SystemExit(gui_main())
