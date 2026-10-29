import plistlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "desktop" / "LaunchAgent"


def test_agent_plist_matches_bundle_layout():
    plist = plistlib.loads((AGENT / "dev.rocky.agent.plist").read_bytes())
    info = plistlib.loads((ROOT / "desktop" / "Info.plist").read_bytes())
    runtime = (ROOT / "desktop/Sources/RockyDesktop/RuntimeManager.swift").read_text()
    build = (ROOT / "scripts/build_macos.sh").read_text()

    assert plist["Label"] == "dev.rocky.agent"
    assert f'agentLabel = "{plist["Label"]}"' in runtime
    assert 'agentPlist = "dev.rocky.agent.plist"' in runtime
    assert plist["AssociatedBundleIdentifiers"] == [info["CFBundleIdentifier"]]
    assert plist["BundleProgram"] == "Contents/Resources/runtime/bin/rocky-agent"
    assert '"$RESOURCES/runtime/bin/rocky-agent"' in build
    assert '"$APP/Contents/Library/LaunchAgents/"' in build
    # Crashes restart; a clean stop by launchd does not loop.
    assert plist["KeepAlive"] == {"SuccessfulExit": False}


def test_agent_script_runs_the_supervisor_on_the_desktop_ports():
    script = (AGENT / "rocky-agent").read_text()
    runtime = (ROOT / "desktop/Sources/RockyDesktop/RuntimeManager.swift").read_text()
    subprocess.run(["bash", "-n", str(AGENT / "rocky-agent")], check=True)
    assert (AGENT / "rocky-agent").stat().st_mode & 0o111
    assert script.rstrip().endswith('exec "$PROJECT/.venv/bin/python" -m rocky.cli serve')
    assert "ROCKY_PORT=18741" in script
    assert 'URL(string: "http://127.0.0.1:18741")' in runtime
    for path in ("data/api-token", "run", "logs"):
        assert path in runtime
    assert 'ROCKY_API_TOKEN_FILE="$SUPPORT/data/api-token"' in script
    assert 'ROCKY_RUN_DIR="$RUN"' in script
