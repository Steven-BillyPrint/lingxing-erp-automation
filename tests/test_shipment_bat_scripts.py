from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_shipment_bat_is_removed_and_desktop_entry_exists():
    assert not (ROOT / "启动自动标发候选扫描.bat").exists()
    assert (ROOT / "desktop_main.py").is_file()
