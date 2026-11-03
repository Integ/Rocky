"""Keep offline runtime links inside the bundle and independent of build paths."""

import os
import sys
from pathlib import Path


def relativize_bundle(directory: Path) -> None:
    root = directory.resolve(strict=True)
    for path in root.rglob("*"):
        if not path.is_symlink():
            continue
        target = path.resolve(strict=True)
        if not target.is_relative_to(root):
            raise ValueError("Offline bundle link points outside its packaged directory")
        if os.path.isabs(os.readlink(path)):
            path.unlink()
            path.symlink_to(os.path.relpath(target, path.parent))


if __name__ == "__main__":
    relativize_bundle(Path(sys.argv[1]))
