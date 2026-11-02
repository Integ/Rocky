"""Open one stable lock inode when several callers initialize it together."""

import errno
import os
import stat


def open_lock_file(path, *, dir_fd=None):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        try:
            descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
        except FileExistsError:
            # Another creator won. Open its inode without another create race.
            descriptor = os.open(path, flags, dir_fd=dir_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(errno.EINVAL, "Lock must be a regular file without links")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor
