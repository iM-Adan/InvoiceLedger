"""Local or Vercel Blob storage for invoice documents."""
from __future__ import annotations

import mimetypes
import shutil
import tempfile
from pathlib import Path

from . import config

_BLOB_PREFIX = "blob://"


def store_file(src: Path, run_id: str) -> str:
    if not config.USE_BLOB_STORAGE:
        stored = config.UPLOAD_DIR / f"{run_id}{src.suffix.lower()}"
        shutil.copyfile(src, stored)
        return str(stored)

    from vercel.blob import sync as blob

    pathname = f"invoices/{run_id}{src.suffix.lower()}"
    content_type = mimetypes.guess_type(src.name)[0] or "application/octet-stream"
    result = blob.put_blob(pathname, src.read_bytes(), access="private", content_type=content_type)
    return f"{_BLOB_PREFIX}{result.pathname}"


def is_blob_path(stored_path: str) -> bool:
    return stored_path.startswith(_BLOB_PREFIX)


def _blob_path(stored_path: str) -> str:
    return stored_path[len(_BLOB_PREFIX):]


def read_file(stored_path: str) -> bytes:
    if is_blob_path(stored_path):
        from vercel.blob import sync as blob

        return blob.get_blob(_blob_path(stored_path), access="private").body
    return Path(stored_path).read_bytes()


def materialize_path(stored_path: str) -> tuple[Path, bool]:
    if not is_blob_path(stored_path):
        return Path(stored_path), False

    suffix = Path(_blob_path(stored_path)).suffix
    data = read_file(stored_path)
    with tempfile.NamedTemporaryFile(dir=config.UPLOAD_DIR, suffix=suffix, delete=False) as tmp:
        tmp.write(data)
    return Path(tmp.name), True


def delete_file(stored_path: str) -> None:
    if is_blob_path(stored_path):
        from vercel.blob import sync as blob

        blob.delete_blob(_blob_path(stored_path))
        return
    path = Path(stored_path)
    if path.exists():
        path.unlink()
