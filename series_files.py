"""Filesystem operations for the dedicated audiobook organizer process."""
import contextlib
import ctypes
import errno
import os
import sys


def components(path):
    if not isinstance(path, str) or not path or path.startswith('/') or '\\' in path:
        raise ValueError('Ungültiger relativer Medienpfad')
    parts = path.split('/')
    if any(not part or part.startswith('.') or any(ord(ch) < 32 for ch in part)
           for part in parts):
        raise ValueError('Ungültiger relativer Medienpfad')
    return parts


@contextlib.contextmanager
def open_dir(root_fd, parts, *, create=False):
    fd = os.dup(root_fd)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        for name in parts:
            if create:
                try:
                    os.mkdir(name, mode=0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _rename_exclusive(source_fd, source, target_fd, target):
    # POSIX rename() can silently replace an existing *empty* directory.
    # Use the OS no-replace primitive even when another importer races us.
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith('linux'):
        operation = getattr(libc, 'renameat2', None)
        exclusive = 1  # RENAME_NOREPLACE
    elif sys.platform == 'darwin':
        operation = getattr(libc, 'renameatx_np', None)
        exclusive = 4  # RENAME_EXCL
    else:
        operation = None
    if operation is None:
        raise OSError(errno.ENOTSUP, 'Sicheres Verschieben wird nicht unterstützt')
    operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    operation.restype = ctypes.c_int
    if operation(source_fd, os.fsencode(source), target_fd, os.fsencode(target), exclusive):
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))

