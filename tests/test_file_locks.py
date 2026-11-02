import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_tools import run
from test_tools import tools as _tools

from rocky import file_locks
from rocky.file_locks import open_lock_file
from rocky.models import Action, ToolInput

tools = _tools


def test_simultaneous_first_creators_share_one_private_lock_inode(tmp_path, monkeypatch):
    path = tmp_path / "lock"
    original = os.open
    first_opens = threading.Barrier(2)
    seen = threading.local()

    def simultaneous(name, flags, *args, **kwargs):
        if name == path and not getattr(seen, "entered", False):
            seen.entered = True
            first_opens.wait(timeout=5)
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(file_locks.os, "open", simultaneous)

    def opened(_):
        descriptor = open_lock_file(path)
        try:
            info = os.fstat(descriptor)
            assert not os.get_inheritable(descriptor)
            return info.st_ino, info.st_mode & 0o777
        finally:
            os.close(descriptor)

    with ThreadPoolExecutor(2) as pool:
        values = list(pool.map(opened, range(2)))
    assert len(set(values)) == 1 and values[0][1] == 0o600


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_locks_reject_linked_and_special_files_without_blocking(tmp_path, kind):
    target = tmp_path / "target"
    target.write_bytes(b"private")
    path = tmp_path / "lock"
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    else:
        os.mkfifo(path)
    with pytest.raises(OSError):
        open_lock_file(path)
    assert target.read_bytes() == b"private"


def test_parallel_first_reads_preserve_both_results_and_replay_receipts(tools):
    with ThreadPoolExecutor(2) as pool:
        for index in range(12):
            goal = run().model_copy(update={"goal_id": f"fresh-{index}"})
            directory = tools.root / goal.goal_id
            directory.mkdir()
            for name in ("one.txt", "two.txt"):
                (directory / name).write_text(name)
            requests = [
                ToolInput(run=goal, operation_id=name, action=Action(tool="read_file", path=name))
                for name in ("one.txt", "two.txt")
            ]
            results = list(pool.map(tools.execute, requests))
            assert all(result.success for result in results), results
            assert {result.content for result in results} == {"one.txt", "two.txt"}
            for name in ("one.txt", "two.txt"):
                (directory / name).write_text("changed")
            assert list(pool.map(tools.execute, requests)) == results
