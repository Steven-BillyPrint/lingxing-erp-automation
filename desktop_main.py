"""PyInstaller/desktop entry point; the user launches the generated EXE."""

from erp_automation.app import main


if __name__ == "__main__":
    raise SystemExit(main())
