"""The licence gate is a CI control, so it is tested like one: each case is a way a forbidden
licence has actually been known to slip through a naive check."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("license_gate", ROOT / "scripts/license_gate.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


@pytest.mark.parametrize("licence", [
    "CC-BY-NC-4.0",                       # SPDX form, as model cards write it
    "CC-BY-NC-SA-4.0",
    "Non-Commercial",
    "AGPL-3.0-only",
    "GPLv3",
    "GNU General Public License v2 or later (GPLv2+)",
    "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
    "LGPL-3.0-only OR GPL-3.0-only",      # the LGPL half must not excuse the GPL half
    "research use only",
    "OpenRAIL-M",
    "GPL",                                # bare, unversioned
    "GNU GPL",
])
def test_forbidden_licences_are_caught(licence):
    assert gate.forbidden_reason(licence), licence


@pytest.mark.parametrize("licence", [
    "MIT", "Apache-2.0", "BSD-3-Clause", "LGPL-3.0-only",
    "License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)",
    "PSF-2.0", "MPL-2.0",
])
def test_permissive_and_lgpl_licences_pass(licence):
    """A gate that flags LGPL psycopg trains people to ignore it."""
    assert gate.forbidden_reason(licence) is None, licence


def test_the_real_manifest_passes():
    assert gate.check_manifest(ROOT) == []


def _repo_copy(tmp_path: Path) -> Path:
    shutil.copy(ROOT / "third_party.toml", tmp_path / "third_party.toml")
    shutil.copy(ROOT / "THIRD_PARTY_NOTICES.md", tmp_path / "THIRD_PARTY_NOTICES.md")
    (tmp_path / "smartcam/ingest").mkdir(parents=True)
    shutil.copy(ROOT / "smartcam/ingest/track.py", tmp_path / "smartcam/ingest/track.py")
    return tmp_path


def _edit(root: Path, old: str, new: str) -> None:
    p = root / "third_party.toml"
    text = p.read_text()
    assert old in text
    p.write_text(text.replace(old, new))


def test_unpinned_model_url_fails(tmp_path):
    """A branch URL means the file behind the declared hash can change under us."""
    root = _repo_copy(tmp_path)
    _edit(root, "/resolve/a3cf03147a9b86c78475139115c8ac142577352d/", "/resolve/main/")
    assert any("pinned" in why for *_, why in gate.check_manifest(root))


def test_banned_weights_fail_even_with_a_permissive_licence_claimed(tmp_path):
    root = _repo_copy(tmp_path)
    _edit(root, 'name = "dfine-s-coco"', 'name = "yolo11-s"')
    assert any("banned" in why for *_, why in gate.check_manifest(root))


def test_non_commercial_model_licence_fails(tmp_path):
    root = _repo_copy(tmp_path)
    _edit(root, 'licence = "Apache-2.0"', 'licence = "CC-BY-NC-4.0"')
    assert any("allow-list" in why for *_, why in gate.check_manifest(root))


def test_algorithm_that_loses_its_provenance_header_fails(tmp_path):
    """The clean-room claim lives in the file header; deleting it must be noticed."""
    root = _repo_copy(tmp_path)
    track = root / "smartcam/ingest/track.py"
    track.write_text(track.read_text().replace("No code was taken from", "Adapted from"))
    assert any("provenance" in why for *_, why in gate.check_manifest(root))


def test_undeclared_weights_literal_in_code_fails(tmp_path):
    root = _repo_copy(tmp_path)
    (root / "smartcam/ingest/extra.py").write_text('MODEL = "weights/buffalo_l.onnx"\n')
    assert any("not declared" in why for *_, why in gate.check_manifest(root))


def test_prose_mentioning_onnx_files_does_not_trip_the_scan(tmp_path):
    root = _repo_copy(tmp_path)
    (root / "smartcam/ingest/extra.py").write_text(
        '"""The loader refuses an undeclared model.onnx file."""\n'
        'def f(x):\n    return f"refusing {x}.onnx"\n')
    assert gate.check_manifest(root) == []
