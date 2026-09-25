"""
Uploaded files: where they land and what they are served as.

The upload's Content-Type is whatever the client claimed, and the stored
extension decides what the file is served as later. Both halves are pinned
here: nothing escapes the media root, and nothing is stored under an extension
a browser would execute.
"""
from __future__ import annotations

import io

import pytest

from app.services.storage.base import (
    build_relative_path,
    safe_folder,
    safe_stem,
    safe_suffix,
)
from app.services.storage.local import LocalDiskStorageProvider

pytestmark = pytest.mark.security


@pytest.mark.parametrize(
    "filename",
    [
        "page.html", "page.HTM", "x.svg", "x.xhtml", "x.js", "x.mjs", "x.php",
        "x.xml", "x.pdf", "x.exe", "x.sh", "x.png.html", "x.jpg%00.html",
    ],
)
def test_executable_or_document_extensions_are_never_kept(filename):
    assert safe_suffix(filename) == ""
    assert not build_relative_path("returns/abc", filename, "uploads").endswith(
        (".html", ".htm", ".svg", ".xhtml", ".js", ".mjs", ".php", ".xml", ".pdf", ".exe", ".sh")
    )


@pytest.mark.parametrize("filename", ["a.jpg", "a.JPEG", "a.png", "a.webp", "a.gif", "a.jfif", "a.mp4", "a.json"])
def test_media_extensions_survive(filename):
    assert safe_suffix(filename) == "." + filename.rsplit(".", 1)[1].lower()


@pytest.mark.parametrize(
    "folder",
    ["../../etc", "/abs/path", "returns/../../..", "..\\..\\windows", "a/./b"],
)
def test_folders_cannot_climb_out(folder):
    cleaned = safe_folder(folder)
    assert ".." not in cleaned.split("/")
    assert not cleaned.startswith("/")
    assert "\\" not in cleaned


def test_stems_are_reduced_to_safe_characters():
    assert safe_stem("../../evil name<script>.png") == "evil_name_script"
    assert safe_stem("<>.png") == "file"
    assert len(safe_stem("x" * 500 + ".png")) <= 60


def test_saved_files_stay_inside_the_media_root(tmp_path):
    store = LocalDiskStorageProvider(media_root=str(tmp_path), url_prefix="uploads")
    rel = store.save(io.BytesIO(b"x"), "../../../escape.png", folder="../../returns")
    on_disk = (tmp_path / rel.removeprefix("/uploads/")).resolve()
    assert tmp_path.resolve() in on_disk.parents
    assert on_disk.read_bytes() == b"x"


@pytest.mark.parametrize(
    "path",
    ["/uploads/../../etc/passwd", "../../outside.txt", "/uploads/../../../../boot.ini"],
)
def test_delete_refuses_paths_outside_the_media_root(tmp_path, path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("keep me")
    store = LocalDiskStorageProvider(media_root=str(tmp_path / "media"), url_prefix="uploads")
    (tmp_path / "media").mkdir()
    assert store.delete(path) is False
    assert outside.exists()
