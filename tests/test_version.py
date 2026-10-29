import plistlib
from importlib.metadata import version
from pathlib import Path

from rocky import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_bundle_matches_backend_version():
    info = plistlib.loads((ROOT / "desktop" / "Info.plist").read_bytes())
    assert info["CFBundleShortVersionString"] == __version__


def test_package_metadata_matches_backend_version():
    assert version("rocky-agent") == __version__
