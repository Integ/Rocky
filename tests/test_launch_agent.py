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


def _bundle(tmp_path, *, offline: bool):
    resources = tmp_path / "Rocky.app/Contents/Resources"
    bin_dir = resources / "runtime/bin"
    backend = resources / "backend"
    (backend / "src").mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    for name in ("pyproject.toml", "uv.lock", "README.md"):
        (backend / name).write_text("")
    (backend / "BUILD_ID").write_text("build-1\n")
    agent = bin_dir / "rocky-agent"
    agent.write_bytes((AGENT / "rocky-agent").read_bytes())
    # The fake uv records its arguments and creates the environment.
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/bash\n"
        'echo "$*" >> "$HOME/uv-calls"\n'
        'mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"\n'
        "printf '#!/bin/bash\\nexit 0\\n' > \"$UV_PROJECT_ENVIRONMENT/bin/python\"\n"
        'chmod +x "$UV_PROJECT_ENVIRONMENT/bin/python"\n'
    )
    uv.chmod(0o755)
    agent.chmod(0o755)
    if offline:
        payload = tmp_path / "payload"
        (payload / "cache/uv").mkdir(parents=True)
        (payload / "runtime/python").mkdir(parents=True)
        (payload / "cache/uv/wheel").write_text("cached")
        subprocess.run(
            [
                "tar",
                "-czf",
                str(bin_dir.parent / "offline.tar.gz"),
                "-C",
                str(payload),
                "cache",
                "runtime",
            ],
            check=True,
        )
    return agent


def _start(agent, home):
    env = {"HOME": str(home), "TMPDIR": str(home), "PATH": "/usr/bin:/bin"}
    subprocess.run([str(agent)], env=env, check=True, timeout=30)
    calls = home / "uv-calls"
    return calls.read_text().splitlines() if calls.exists() else []


def test_agent_sets_up_offline_once_per_build(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    agent = _bundle(tmp_path, offline=True)
    first = _start(agent, home)
    assert len(first) == 1 and "--offline" in first[0] and "--frozen" in first[0]
    support = home / "Library/Application Support/Rocky"
    assert (support / "cache/uv/wheel").read_text() == "cached"
    # A restart of the same build needs neither uv nor a network.
    assert _start(agent, home) == first
    # A new build prepares the environment again.
    (agent.parents[2] / "backend/BUILD_ID").write_text("build-2\n")
    assert len(_start(agent, home)) == 2


def test_agent_without_the_bundle_downloads(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    calls = _start(_bundle(tmp_path, offline=False), home)
    assert len(calls) == 1 and "--offline" not in calls[0]
