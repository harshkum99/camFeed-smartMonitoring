"""The draft certificate: statutory text intact, only held values filled, and no claim that the
record is admissible — that is for the court, and saying otherwise is a misleading claim."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from smartcam.evidence.bsa_schedule import PART_A, blank_text
from smartcam.evidence.certificate import prefill, render_certificate
from smartcam.ingest.keyframes import encode_jpeg

H = "a" * 64


def camera(**kw):
    base = {"camera_id": "c1", "name": "Admin G329 indoor", "make": None, "model": None,
            "serial_no": None, "mac": None, "recorder_id": "meva:admin.G329", "channel_no": None,
            "detect_w": 1920, "detect_h": 1072, "detect_fps": 30.0, "codec": "h264",
            "clock_offset_ms": None, "clock_checked_at": None}
    base.update(kw)
    return base


def manifest(work: Path, cams: list[dict], included: bool = True) -> dict:
    (work / "records").mkdir(parents=True, exist_ok=True)
    (work / "frames").mkdir(exist_ok=True)
    img = (np.random.default_rng(3).random((108, 192, 3)) * 255).astype("uint8")
    (work / "frames/t1.jpg").write_bytes(encode_jpeg(img))
    (work / "records/cameras.json").write_text(json.dumps(cams))
    (work / "records/tracks.json").write_text(
        json.dumps([{"track_id": "t1", "camera": cams[0]["name"]}]))
    return {
        "bundle_id": "0f0e0d0c-0000-4000-8000-000000000001",
        "created_at": "2026-09-19T11:00:00+00:00",
        "tenant": {"id": "t", "name": "Acme Manufacturing"},
        "site": {"id": "s", "name": "Plant 2", "timezone": "America/New_York"},
        "requested_by": "operator", "purpose": "Complaint 42", "question": "Who was there?",
        "window": {"from": "2018-03-11T15:55:24+00:00", "to": "2018-03-11T15:59:50+00:00"},
        "merkle": {"scheme": "rfc6962", "root": "b" * 64, "leaves": 2},
        "items": [{"path": "source/cam/x/rec.avi", "kind": "original_recording", "sha256": H,
                   "sha1": "c" * 40, "md5": "d" * 32, "size_bytes": 1000},
                  {"path": "frames/t1.jpg", "kind": "derived_frame", "sha256": "e" * 64,
                   "sha1": "f" * 40, "md5": "0" * 32, "size_bytes": 10}],
        "sources": [{"clip_id": "k", "camera_id": "c1", "camera": cams[0]["name"],
                     "file_name": "rec.avi", "source_uri": "file:rec.avi",
                     "sha256_at_import": H, "recorded_from": "2018-03-11T15:55:00+00:00",
                     "recorded_to": "2018-03-11T16:00:00+00:00", "included": included,
                     **({} if included else {"not_included_reason": "not found"})}],
        "frames": [{"track_id": "t1", "included": True, "path": "frames/t1.jpg",
                    "captured_at": "2018-03-11T15:55:56+00:00", "bbox": [0.1, 0.1, 0.2, 0.5]}],
        "clock_warnings": [{"camera_id": "c1", "camera": cams[0]["name"], "kind": "unchecked",
                            "message": "clock never checked"}],
        "hash_provenance": "computed on import", "derived_frames_note": "frames are derived",
        "attribution": None, "warnings": [], "manifest_sha256": "9" * 64,
        "_work_dir": str(work),
    }


def pdf_text(pdf: bytes, tmp_path: Path) -> str:
    if shutil.which("pdftotext") is None:
        pytest.skip("pdftotext not installed")
    p = tmp_path / "c.pdf"
    p.write_bytes(pdf)
    # -nodiag drops the diagonal DRAFT watermark, which would otherwise interleave with the text.
    out = subprocess.run(["pdftotext", "-nodiag", "-layout", str(p), "-"], capture_output=True,
                         text=True, check=True).stdout
    return re.sub(r"\s+", " ", out)


def test_renders_a_deterministic_pdf(tmp_path):
    """Identical input, identical bytes — so the certificate's own hash can be recomputed."""
    m = manifest(tmp_path, [camera()])
    a, b = render_certificate(m), render_certificate(m)
    assert a.startswith(b"%PDF") and a == b


def test_says_it_is_an_unsigned_draft_and_claims_nothing(tmp_path):
    text = pdf_text(render_certificate(manifest(tmp_path, [camera()])), tmp_path)
    assert "DRAFT" in text and "prepared for review and signature" in text
    assert "Not a certificate until affirmed and signed" in text
    assert "has signed nothing and certifies nothing" in text
    lowered = text.lower()
    for claim in ("court-admissible", "court admissible", "is admissible", "legally valid",
                  "court-filable", "guaranteed", "bsa-compliant", "certified evidence",
                  "tamper-proof", "forensically sound"):
        assert claim not in lowered, claim


def test_carries_the_bundle_identity_and_hashes(tmp_path):
    text = pdf_text(render_certificate(manifest(tmp_path, [camera()])), tmp_path)
    assert "0f0e0d0c-0000-4000-8000-000000000001" in text
    assert "9" * 64 in text and "b" * 64 in text
    assert H in text


def test_part_a_statutory_affirmation_is_printed_unaltered(tmp_path):
    text = pdf_text(render_certificate(manifest(tmp_path, [camera()])), tmp_path)
    affirmation = next(b[1] for b in PART_A if b[0] == "text" and "lawful control" in b[1])
    assert re.sub(r"\s+", " ", blank_text(affirmation)) in text


def test_times_are_given_in_ist_as_the_form_requires(tmp_path):
    text = pdf_text(render_certificate(manifest(tmp_path, [camera()])), tmp_path)
    assert "IST" in text and "EDT" in text


def test_prefill_never_puts_camera_details_on_the_recorder_lines():
    """The form's Make & Model, Serial and MAC lines describe the recorder the record was taken
    from. The system holds camera details; printing them there would misdescribe the device."""
    values, ticks = prefill({"sources": [{"included": True}]},
                            [camera(make="CP Plus", model="CP-UNC-TA21", serial_no="SN1",
                                    mac="00:11:22:33:44:55")])
    for recorder_line in ("make_model", "serial", "device_id", "color"):
        assert recorder_line not in values
    assert ticks == {"hash_algorithm": {"SHA256:"}}
    assert "Annexure B" in values["device_other_info"]
    for never in ("name", "parent", "address", "date", "time", "place", "device_other"):
        assert never not in values


def test_prefill_states_no_hash_when_no_recording_is_included():
    values, ticks = prefill({"sources": [{"included": False}]}, [camera()])
    assert "hash_value" not in values and "hash_algorithm" not in ticks


def test_prefill_never_ticks_device_type_or_ownership():
    """Which device the record came from, and in what capacity the Party controls it, are the
    Party's statements. The system can suggest in an annexure; it never ticks those boxes."""
    _, ticks = prefill({"sources": [{"included": True}]}, [camera(make="X")])
    assert "device" not in ticks and "control" not in ticks


def test_missing_recording_is_disclosed_in_the_hash_report(tmp_path):
    text = pdf_text(render_certificate(manifest(tmp_path, [camera()], included=False)), tmp_path)
    assert "Not included: rec.avi" in text


def test_derived_frames_are_kept_out_of_the_electronic_record_hash_report(tmp_path):
    """Re-encoded frames must never appear alongside the original's hash as if they were it."""
    text = pdf_text(render_certificate(manifest(tmp_path, [camera()])), tmp_path)
    a = text.index("Annexure A — Hash report")
    a2 = text.index("Annexure A2 — Derived material: not the electronic record")
    assert "frames/t1.jpg" not in text[a:a2]
    assert "frames/t1.jpg" in text[a2:]


def test_each_draft_is_identified_and_distinct(tmp_path):
    """s.63(4) asks for a certificate at each instance of submission; each draft is its own."""
    m = manifest(tmp_path, [camera()])
    d1 = render_certificate(m)
    d2 = render_certificate(m, draft_id="D2", generated_at="2026-09-20T05:00:00+00:00")
    assert d1 != d2
    text = pdf_text(d2, tmp_path)
    assert "D2" in text and "20/09/2026" in text


def test_facts_bearing_on_part_a_are_disclosed(tmp_path):
    m = manifest(tmp_path, [camera()])
    m["coverage_gaps"] = [{"camera": "Admin G329 indoor", "from": "2018-03-11T16:00:00+00:00",
                           "to": "2018-03-11T17:50:00+00:00"}]
    text = pdf_text(render_certificate(m), tmp_path)
    assert "Annexure D" in text and "no footage held from" in text
    assert "clock never checked" in text


def test_offence_notice_and_old_regime_are_stated(tmp_path):
    text = pdf_text(render_certificate(manifest(tmp_path, [camera()])), tmp_path)
    assert "section 236 of the Bharatiya Nyaya Sanhita, 2023" in text
    assert "65B(4)" in text and "1 July 2024" in text


def test_custody_is_listed_for_the_expert_when_given(tmp_path):
    m = manifest(tmp_path, [camera()])
    pdf = render_certificate(m, custody=[{"at": "2026-09-19T11:00:00+00:00", "actor": "op",
                                          "action": "created"}])
    assert "Chain of custody recorded" in pdf_text(pdf, tmp_path)


def test_schedule_pages_carry_no_draft_marking_but_the_rest_does(tmp_path):
    """A certificate someone signs must not say "NOT SIGNED" on its own face; the supporting
    pages keep the marking so nobody mistakes them for the certificate."""
    if shutil.which("pdftotext") is None:
        pytest.skip("pdftotext not installed")
    pdf = tmp_path / "c.pdf"
    pdf.write_bytes(render_certificate(manifest(tmp_path, [camera()])))

    def page(n: int) -> str:
        return subprocess.run(["pdftotext", "-f", str(n), "-l", str(n), str(pdf), "-"],
                              capture_output=True, text=True, check=True).stdout

    part_a = next(n for n in range(1, 8) if "(To be filled by the Party)" in page(n))
    part_b = next(n for n in range(1, 8) if "(To be filled by the Expert)" in page(n))
    for n in (part_a, part_b):
        assert "not signed" not in page(n).lower() and "DRAFT" not in page(n)
    assert "not signed" in page(1).lower()


def test_draft_does_not_claim_the_system_kept_the_recordings(tmp_path):
    text = pdf_text(render_certificate(manifest(tmp_path, [camera()])), tmp_path)
    assert "stored without modification" not in text
    assert "does not keep the recordings" in text
