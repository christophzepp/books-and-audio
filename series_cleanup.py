"""Optional host-side cleanup after a verified physical Listenarr move."""
import errno
import os
from contextlib import ExitStack
from pathlib import PurePosixPath


def prune_empty_authors(source, destination, book, job, *, manifest_verified,
                        catalog_base_paths, host_root="/audio-dramas"):
    """Remove only the two exact empty ancestors. Return the removed library paths."""
    old, new = PurePosixPath(source), PurePosixPath(destination)
    root = PurePosixPath("/audio-dramas")
    if (not manifest_verified or str(job.get("status", "")).lower() != "completed"
            or job.get("sourceRetained") is not False or book.get("basePath") != str(new)
            or not book.get("files") or old.parent.parent.parent != root
            or new.parent.parent != root or old.parent.name != new.parent.name
            or any(p in {".", ".."} for p in source.split("/") + destination.split("/"))):
        return []
    if any(PurePosixPath(f["path"]).parent != new for f in book["files"]):
        return []
    # Protect any paths assigned to other books, including titles awaiting download.
    protected = {p for bp in catalog_base_paths if bp
                 for p in (PurePosixPath(bp), *PurePosixPath(bp).parents)}
    if new not in protected or old in protected:
        return []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    removed = []
    with ExitStack() as stack:
        def open_dir(name, parent=None):
            fd = os.open(name, flags, dir_fd=parent)
            stack.callback(os.close, fd)
            return fd

        try:
            rootfd = open_dir(host_root)
            authorfd = open_dir(old.parent.parent.name, rootfd)
            try:
                seriesfd = open_dir(old.parent.name, authorfd)
            except FileNotFoundError:
                seriesfd = None
            if seriesfd is not None:
                try:
                    os.stat(old.name, dir_fd=seriesfd, follow_symlinks=False)
                    return []
                except FileNotFoundError:
                    pass
                if old.parent in protected:
                    return []
                os.rmdir(old.parent.name, dir_fd=authorfd)
                removed.append(str(old.parent))
            if old.parent.parent not in protected:
                os.rmdir(old.parent.parent.name, dir_fd=rootfd)
                removed.append(str(old.parent.parent))
        except OSError as exc:
            if exc.errno not in {errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST,
                                 errno.ELOOP, errno.ENOTDIR}:
                raise
    return removed
