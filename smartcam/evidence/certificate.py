"""A draft certificate under section 63(4) of the Bharatiya Sakshya Adhiniyam, 2023 — for people
to complete and sign. It certifies nothing itself.

The statute is precise about who speaks in a certificate. Part A is the Party's affirmation that
the device was under their lawful control and working properly; Part B is an expert's. Software
can do neither. What it can do, and what makes this worth having, is everything around those
statements: compute the hashes the form asks for, enclose the hash report the form requires, set
out the device and recording particulars section 63(4)(a) and (b) ask for, describe how the
record was produced, and reproduce the form exactly so nobody has to retype it.

So the rules for this document are:

- **The Schedule pages are the statutory text, word for word** (bsa_schedule.py, tested against
  the Gazette). Nothing is added inside the form.
- **Only two things go into Part A, in blue and marked for checking**: the SHA-256 values of the
  original recordings (by reference to the hash report, with SHA256 ticked because that is what
  was computed), and a pointer to the particulars annex. Device make, model and serial are left
  blank: the form means the *recorder*, and the system holds camera details, not a recorder
  inventory. Names, parentage, address, device type, ownership, date, time, place and signatures
  are the signatory's own statements and are never filled. Part B is left entirely blank.
- **Derived material never enters the form's hash line.** Evidence frames and index records are
  re-encoded or generated; they are listed separately as "not the electronic record".
- **Nothing in it claims the record is admissible.** Whether it is, is for the court (BSA s.141).
- **A fresh draft, with its own ID, for each submission**, because s.63(4) requires a certificate
  "at each instance" a record is submitted. Every draft generated is logged in the custody log.

These rules follow a reconciled reading of the Gazette text, Arjun Panditrao Khotkar (2020) 7 SCC
1, and Pune Bar Association v. Union of India (SC, order of 22 May 2026); see
docs/research/bsa-s63-certificate.md. An Indian litigation advocate should review the wording
before it is used with a customer.

Rendered with reportlab (BSD). `invariant=True` keeps the output byte-identical for identical
input, so the certificate's own hash can be recomputed.
"""

from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from smartcam.evidence.bsa_schedule import (
    BLANK_WIDTH,
    GAZETTE,
    PART_A,
    PART_B,
    SECTION_63_4,
)

IST = ZoneInfo("Asia/Kolkata")
FILLED = "#1d4ed8"
MARK = "†"   # dagger: "filled from system records — verify"

_base = getSampleStyleSheet()
S = {
    "title": ParagraphStyle("t", parent=_base["Title"], fontSize=15, leading=19, spaceAfter=4),
    "h": ParagraphStyle("h", parent=_base["Heading2"], fontSize=11.5, leading=14, spaceBefore=8),
    "form_h": ParagraphStyle("fh", parent=_base["Normal"], fontName="Helvetica-Bold",
                             fontSize=11, leading=14, alignment=TA_CENTER),
    "form_sub": ParagraphStyle("fs", parent=_base["Normal"], fontSize=10, leading=13,
                               alignment=TA_CENTER),
    "form": ParagraphStyle("f", parent=_base["Normal"], fontSize=10, leading=15, spaceAfter=5),
    "body": ParagraphStyle("b", parent=_base["Normal"], fontSize=9, leading=12.5, spaceAfter=4),
    "small": ParagraphStyle("s", parent=_base["Normal"], fontSize=7.5, leading=9.5),
    "mono": ParagraphStyle("m", parent=_base["Normal"], fontName="Courier", fontSize=7,
                           leading=9),
    "warn": ParagraphStyle("w", parent=_base["Normal"], fontSize=9, leading=12.5,
                           textColor=colors.HexColor("#92400e")),
}


def _esc(s: Any) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _filled(v: str) -> str:
    mark = f'<super><font color="{FILLED}">{MARK}</font></super>'
    return f'<font color="{FILLED}">{_esc(v)}</font>{mark}'


def _box(ticked: bool) -> Table:
    """A drawn square, so an empty box prints as empty — the ZapfDingbats square glyph prints
    solid at form size, which reads as ticked."""
    mark = Paragraph(f'<font name="ZapfDingbats" color="{FILLED}" size="8">4</font>', S["small"]) \
        if ticked else ""
    t = Table([[mark]], colWidths=[3.6 * mm], rowHeights=[3.6 * mm])
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.7, colors.black),
                           ("LEFTPADDING", (0, 0), (-1, -1), 0.4),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                           ("TOPPADDING", (0, 0), (-1, -1), 0),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                           ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    return t


def _line(text: str, values: dict[str, str]) -> str:
    out = {}
    for k, w in BLANK_WIDTH.items():
        out[k] = _filled(values[k]) if values.get(k) else "_" * w
    return _esc(text).replace("{", "\x00").replace("}", "\x01").translate(
        {0: "{", 1: "}"}).format(**out)


def _form(blocks: list[tuple], values: dict[str, str], ticks: dict[str, set[str]]) -> list:
    flow: list = []
    for b in blocks:
        kind = b[0]
        if kind == "heading":
            flow.append(Paragraph(_esc(b[1]), S["form_h"]))
        elif kind == "subheading":
            flow.append(Paragraph(_esc(b[1]), S["form_sub"]))
            flow.append(Spacer(1, 3 * mm))
        elif kind == "text":
            flow.append(Paragraph(_line(b[1], values), S["form"]))
        elif kind == "ticks":
            chosen = ticks.get(b[1], set())
            one_per_row = b[1] == "hash_algorithm"
            label_w = 150 * mm if one_per_row else None
            cells = []
            for o in b[2]:
                label = Paragraph(_line(o, values), S["form"])
                w = label_w or ((46 if o.startswith("Computer") else 30) * mm)
                pair = Table([[_box(o in chosen), label]], colWidths=[5.5 * mm, w],
                             hAlign="LEFT")
                pair.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                          ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                          ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                                          ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
                cells.append(pair)
            per_row = 1 if one_per_row else 4
            rows = [cells[i:i + per_row] for i in range(0, len(cells), per_row)]
            rows = [r + [""] * (per_row - len(r)) for r in rows]
            t = Table(rows, hAlign="LEFT",
                      colWidths=None if one_per_row else [53 * mm, 37 * mm, 37 * mm, 37 * mm])
            t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
            flow.append(t)
        elif kind == "signature":
            flow.append(Spacer(1, 12 * mm))
            flow.append(Paragraph(_esc(b[1]), ParagraphStyle(
                "sig", parent=S["form"], alignment=2)))
    return flow


def _t(iso: str | None, tz: str) -> str:
    if not iso:
        return "—"
    dt = datetime.fromisoformat(iso)
    local = dt.astimezone(ZoneInfo(tz))
    ist = dt.astimezone(IST)
    if tz == "Asia/Kolkata":
        return f"{local:%d/%m/%Y %H:%M:%S} IST"
    return f"{local:%d/%m/%Y %H:%M:%S %Z} (= {ist:%d/%m/%Y %H:%M:%S} IST)"


def _table(rows: list[list], widths: list[float], header: bool = True) -> Table:
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
    style = [("FONTSIZE", (0, 0), (-1, -1), 7.5), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#9ca3af")),
             ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")),
                  ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
    t.setStyle(TableStyle(style))
    return t


def prefill(manifest: dict[str, Any], cameras: list[dict[str, Any]]) -> tuple[dict, dict]:
    """The values this system holds for Part A. Everything returned here is printed in blue.

    Make & Model, Serial Number and the MAC/ID line describe the device the record was taken
    from — the recorder. The system holds camera details, not a recorder inventory, and a camera's
    make and model on that line would misdescribe the device. They stay blank; the camera details
    are set out in Annexure B for the signatory to use or not.
    """
    del cameras  # deliberately unused on the form itself; see Annexure B
    values: dict[str, str] = {}
    ticks: dict[str, set[str]] = {}
    included = [s for s in manifest["sources"] if s.get("included")]
    if included:
        values["hash_value"] = (f"the SHA-256 value of each of the {len(included)} original "
                                f"recording file(s) listed in Annexure A")
        ticks["hash_algorithm"] = {"SHA256:"}
    values["device_other_info"] = " see Annexure B (recording particulars held by the system) "
    return values, ticks


def render_certificate(manifest: dict[str, Any], *, draft_id: str = "D1",
                       generated_at: str | None = None,
                       custody: list[dict[str, Any]] | None = None) -> bytes:
    """Render one draft. `draft_id` distinguishes the drafts made for successive submissions of
    the same bundle; the draft sealed inside the bundle is D1, generated when the bundle was."""
    work = Path(manifest["_work_dir"]) if manifest.get("_work_dir") else None
    cameras = json.loads((work / "records/cameras.json").read_text()) if work else []
    tracks = json.loads((work / "records/tracks.json").read_text()) if work else []
    tz = manifest["site"]["timezone"]
    generated_at = generated_at or manifest["created_at"]
    values, ticks = prefill(manifest, cameras)

    buf = io.BytesIO()
    doc = BaseDocTemplate(
        buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
        bottomMargin=16 * mm, invariant=True,
        title=f"DRAFT certificate, BSA 2023 s.63(4) — bundle {manifest['bundle_id']} "
              f"draft {draft_id}",
        author="Prepared by Smart Cam Monitoring; not signed")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body")
    # The Schedule pages carry no draft marking: they are what gets completed and signed, and a
    # signed certificate must not say "not signed" on its own face. Everything else does.
    doc.addPageTemplates([
        PageTemplate(id="draft", frames=[frame], onPage=_footer(manifest, draft_id)),
        PageTemplate(id="form", frames=[frame], onPage=_form_footer(manifest, draft_id)),
    ])
    width = A4[0] - 36 * mm
    flow: list = []

    # --- cover --------------------------------------------------------------------------------
    flow += [
        Paragraph("DRAFT: certificate under section 63(4), Bharatiya Sakshya Adhiniyam, 2023 "
                  "(Schedule form), prepared for review and signature", S["title"]),
        Paragraph("<b>Not a certificate until affirmed and signed by the person in charge "
                  "(Part A) and an expert (Part B).</b>", S["body"]),
        Spacer(1, 3 * mm),
        _table([[Paragraph(
            "Section 63(4) requires the certificate to be signed by a person in charge of the "
            "computer or communication device or the management of the relevant activities, and "
            "by an expert; each states matters to the best of their own knowledge and belief. "
            "Smart Cam Monitoring has signed nothing and certifies nothing. "
            f"Values printed in <font color='{FILLED}'>blue</font> and marked {MARK} were "
            "filled from the system's records: check each one, and strike out and replace any "
            "that is wrong. Every other field is for the signatory alone. The Schedule pages "
            "(Part A and Part B) are printed without the draft marking: they are the pages to "
            "be completed and signed; everything else is supporting material. Whether the record "
            "is admitted in evidence is for the court to decide.", S["body"])]],
            [width], header=False),
        Spacer(1, 4 * mm),
        Paragraph("Identification", S["h"]),
        _table([
            ["Bundle", manifest["bundle_id"]],
            ["This draft", f"{draft_id}, generated {_t(generated_at, 'Asia/Kolkata')}"],
            ["Bundle prepared", _t(manifest["created_at"], "Asia/Kolkata")],
            ["For", Paragraph(_esc(f"{manifest['tenant']['name']} — "
                                   f"{manifest['site']['name']}"), S["small"])],
            ["Requested by (system user)", manifest["requested_by"]],
            ["Purpose", Paragraph(_esc(manifest["purpose"]), S["small"])],
            ["Recording window", Paragraph(f"{_t(manifest['window']['from'], tz)} to "
                                           f"{_t(manifest['window']['to'], tz)}", S["small"])],
            ["Original recordings", str(len(manifest["sources"]))],
            ["manifest.json SHA-256", Paragraph(manifest["manifest_sha256"], S["mono"])],
            ["Merkle root (RFC 6962)", Paragraph(manifest["merkle"]["root"], S["mono"])],
            ["Prepared with", manifest.get("software") or "Smart Cam Monitoring"],
        ], [48 * mm, width - 48 * mm], header=False),
        Paragraph("Before anyone signs", S["h"]),
        *[Paragraph(f"{i}. {t}", S["body"]) for i, t in enumerate([
            "Read Annexure D, the facts the system holds that bear on the Part A statement that "
            "the recorder was working properly — gaps in the footage, clock status, files not "
            "included. They are shown so the signatory can weigh them; they are not decided "
            "here.",
            "Part A and Part B are solemn affirmations. Making a false statement in a "
            "declaration that is by law receivable as evidence is an offence under section 236 "
            "of the Bharatiya Nyaya Sanhita, 2023.",
            "Section 63(4) requires a certificate at each instance the record is submitted for "
            "admission. Generate a fresh draft for each submission; each is logged.",
            "For proceedings pending before 1 July 2024, section 170(2) of the Adhiniyam keeps "
            "the Indian Evidence Act, 1872 in force for them, and its section 65B(4) governs "
            "instead. Confirm the applicable regime with your advocate.",
            "Producing the original recorder or storage device is an alternative to a "
            "certified copy (Arjun Panditrao Khotkar v. Kailash Kushanrao Gorantyal, "
            "(2020) 7 SCC 1). Preserve the original recorder or export, and stop it being "
            "overwritten, wherever the footage matters.",
            "Have your advocate review the completed certificate before it is filed.",
        ], start=1)],
        Paragraph("Section 63(4), for reference", S["h"]),
        Paragraph(_esc(SECTION_63_4), S["small"]),
        Spacer(1, 2 * mm),
        Paragraph(f"Form text reproduced from {_esc(GAZETTE['publication'])}, "
                  f"{_esc(GAZETTE['pages'])}.", S["small"]),
    ]

    # --- the Schedule ------------------------------------------------------------------------
    flow += [NextPageTemplate("form"), PageBreak(), *_form(PART_A, values, ticks),
             Spacer(1, 4 * mm),
             Paragraph(f"<font color='{FILLED}'>{MARK} Filled from Smart Cam Monitoring's "
                       f"records. Verify before signing.</font>", S["small"]),
             PageBreak(), *_form(PART_B, {}, {}),
             Spacer(1, 4 * mm),
             Paragraph("Part B is left entirely blank: it is the expert's statement. Annexure E "
                       "sets out what the expert may wish to check.", S["small"])]

    # --- Annexure A: hash report (the electronic record) ------------------------------------
    originals = [it for it in manifest["items"] if it["kind"] == "original_recording"]
    by_path = {src.get("path"): src for src in manifest["sources"]}
    flow += [NextPageTemplate("draft"), PageBreak(),
             Paragraph("Annexure A — Hash report: the original recordings", S["h"]),
             Paragraph("These files are the electronic record. Their SHA-256 values are the ones "
                       "referred to in Part A.", S["body"])]
    for it in originals:
        src = by_path.get(it["path"], {})
        rows = [
            ["Original file", Paragraph(_esc(src.get("file_name") or it["path"]), S["mono"])],
            ["Size", f"{it['size_bytes']:,} bytes"],
            ["SHA-256", Paragraph(it["sha256"], S["mono"])],
            ["Hashed", Paragraph(_hash_times(src), S["small"])],
            ["Also computed at bundling", Paragraph(f"SHA-1 {it['sha1']}<br/>MD5 {it['md5']}",
                                                    S["mono"])],
        ]
        flow += [KeepTogether([_table(rows, [38 * mm, width - 38 * mm], header=False),
                               Spacer(1, 3 * mm)])]
    flow += [Spacer(1, 2 * mm), Paragraph(_esc(manifest["hash_provenance"]), S["body"])]
    for src in (x for x in manifest["sources"] if not x.get("included")):
        flow.append(Paragraph(f"Not included: {_esc(src['file_name'])} — "
                              f"{_esc(src.get('not_included_reason', ''))}. SHA-256 recorded on "
                              f"receipt: {src['sha256_at_import']}", S["warn"]))

    derived = [it for it in manifest["items"] if it["kind"] != "original_recording"]
    flow += [Paragraph("Annexure A2 — Derived material: not the electronic record", S["h"]),
             Paragraph("Still images decoded and re-encoded from the recordings, and index "
                       "records generated by the system. Hashed so that they, too, can be "
                       "checked, but none of these values belongs in the form's hash line.",
                       S["body"])]
    rows = [["File in bundle", "Kind", "Bytes", "SHA-256"]]
    rows += [[Paragraph(_esc(it["path"]), S["mono"]), it["kind"].replace("_", " "),
              f"{it['size_bytes']:,}", Paragraph(it["sha256"], S["mono"])] for it in derived]
    flow.append(_table(rows, [62 * mm, 24 * mm, 18 * mm, width - 104 * mm]))

    # --- Annexure B: particulars held by the system ------------------------------------------
    flow += [PageBreak(), Paragraph("Annexure B — Recording particulars held by the system",
                                    S["h"]),
             Paragraph("As recorded in Smart Cam Monitoring. Claimed by the party, to be "
                       "checked. Camera details are not recorder details: the device lines of "
                       "the form refer to the recorder the record was taken from.", S["body"])]
    by_cam = {c["camera_id"]: c for c in cameras}
    for src in manifest["sources"]:
        c = by_cam.get(src["camera_id"], {})
        rows = [
            ["Camera (as named in the system)", src["camera"]],
            ["Original file", Paragraph(_esc(src["file_name"]), S["mono"])],
            ["Obtained from", Paragraph(_esc(src.get("source_uri") or "—"), S["mono"])],
            ["Recording covers", Paragraph(f"{_t(src.get('recorded_from'), tz)} to "
                                           f"{_t(src.get('recorded_to'), tz)}", S["small"])],
            ["Recorder / channel (system reference)", _recorder(c)],
            ["Camera make & model", " ".join(x for x in (c.get("make"), c.get("model")) if x)
             or "not recorded in the system"],
            ["Camera serial / MAC", " / ".join(x for x in (c.get("serial_no"), c.get("mac")) if x)
             or "not recorded in the system"],
            ["Stream", f"{c.get('detect_w')}x{c.get('detect_h')} at {c.get('detect_fps')} fps, "
                       f"{c.get('codec') or 'codec unknown'}" if c.get("detect_w") else "—"],
            ["Clock check", "offset "
             f"{c['clock_offset_ms'] / 1000:+.1f} s at {_t(c.get('clock_checked_at'), tz)}"
             if c.get("clock_offset_ms") is not None else
             "never checked against a reference clock"],
        ]
        flow += [KeepTogether([_table(rows, [58 * mm, width - 58 * mm], header=False),
                               Spacer(1, 3 * mm)])]

    # --- Annexure C: how the record was produced ---------------------------------------------
    flow += [Paragraph("Annexure C — How this electronic record was produced "
                       "(section 63(4)(a))", S["h"]),
             Paragraph(
                 "Smart Cam Monitoring does not keep the recordings themselves. Each file listed "
                 "in Annexure A was hashed with SHA-256 when it was imported from the source "
                 "shown in Annexure B. When this bundle was prepared, a file with that same "
                 "SHA-256 was found in the footage location, copied byte for byte, and hashed "
                 "again; a file whose hash did not match would have been refused. The system "
                 "therefore shows that the file in the bundle is the file that was imported; it "
                 "has no custody of the file between those two moments beyond that match. "
                 "Separately, the system analysed the recordings to build a searchable index and "
                 "extracted still images to show where to look (Annexure A2). Neither alters the "
                 "recordings, and neither is the electronic record.", S["body"])]
    if manifest.get("question"):
        flow.append(Paragraph(f"The bundle was prepared from the evidence behind the question: "
                              f"<i>{_esc(manifest['question'])}</i>", S["body"]))
    if manifest.get("attribution"):
        flow.append(Paragraph(f"Attribution: {_esc(manifest['attribution'])}", S["small"]))

    # --- Annexure D: facts held that bear on Part A ------------------------------------------
    flow += [Paragraph("Annexure D — Facts held by the system that bear on the Part A "
                       "statement", S["h"]),
             Paragraph("Part A affirms that the device was working properly during the period, "
                       "or that any failure did not affect the record. These are the facts the "
                       "system holds about that period. They are for the signatory to weigh.",
                       S["body"])]
    facts: list[str] = []
    for g in manifest.get("coverage_gaps", []):
        facts.append(f"{g['camera']}: no footage held from {_t(g['from'], tz)} to "
                     f"{_t(g['to'], tz)}.")
    facts += [w["message"] + "." for w in manifest.get("clock_warnings", [])]
    facts += [f"{x['file_name']} was not included: {x.get('not_included_reason', '')}."
              for x in manifest["sources"] if not x.get("included")]
    if not facts:
        facts.append("No gaps in the footage held for the recording window, no clock warnings, "
                     "and every original recording is included.")
    flow += [Paragraph(f"• {_esc(f)}", S["body"]) for f in facts]

    # --- Annexure E: for the expert -----------------------------------------------------------
    flow += [Paragraph("Annexure E — For the expert completing Part B", S["h"]),
             Paragraph(
                 "Part B is the expert's own finding and is left blank. Recompute the hash of "
                 "each original recording, with your own tool, on the media actually produced, "
                 "and compare it with Annexure A. The bundle can also be checked as a whole "
                 "without Smart Cam Monitoring software: <font name='Courier'>shasum -a 256 -c "
                 "SHA256SUMS</font> in the extracted folder checks every file (on Windows, "
                 "<font name='Courier'>certutil -hashfile &lt;file&gt; SHA256</font>), and "
                 "<font name='Courier'>python3 verify_bundle.py .</font> recomputes the Merkle "
                 f"root under the scheme: {_esc(manifest['merkle']['scheme'])}. The SHA-256 of "
                 "manifest.json must equal the value on page 1. Particulars in Annexure B are "
                 "the party's claims, for you to check.", S["body"])]
    if custody:
        rows = [["When (IST)", "Who (system user)", "Action"]]
        rows += [[_t(c["at"], "Asia/Kolkata"), c["actor"], c["action"]] for c in custody]
        flow += [Paragraph("Chain of custody recorded by the system up to this draft:",
                           S["body"]), _table(rows, [48 * mm, 60 * mm, width - 108 * mm])]

    # --- Annexure F: frames ------------------------------------------------------------------
    frames = [f for f in manifest["frames"] if f.get("included")]
    if frames and work:
        flow += [PageBreak(), Paragraph("Annexure F — Evidence frames (derived, illustrative)",
                                        S["h"]),
                 Paragraph("Still images decoded from the original recordings, reduced for "
                           "printing. Not the electronic record; shown to identify the moments "
                           "cited. Full-resolution copies are in the bundle.", S["body"])]
        by_track = {t["track_id"]: t for t in tracks}
        cells = []
        for f in frames[:12]:
            img = Image(_thumbnail(work / f["path"]), width=82 * mm, height=46 * mm,
                        kind="proportional")
            t = by_track.get(f["track_id"], {})
            # Image above caption in one cell, so a caption can never be drawn over a picture.
            cells.append([img, Paragraph(
                f"{_esc(t.get('camera', ''))} — {_t(f.get('captured_at'), tz)}<br/>"
                f"<font name='Courier' size='6'>{f['path']}</font>", S["small"])])
        rows = [cells[i:i + 2] + ([""] if i + 1 >= len(cells) else [])
                for i in range(0, len(cells), 2)]
        grid = Table(rows, colWidths=[width / 2, width / 2], hAlign="LEFT")
        grid.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                  ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        flow.append(grid)

    doc.build(flow)
    return buf.getvalue()


def _hash_times(src: dict[str, Any]) -> str:
    received = (_t(src["hashed_at_import"], "Asia/Kolkata") if src.get("hashed_at_import")
                else "time of receipt not recorded (imported before it was)")
    tool = _esc(src.get("hashed_by") or "Smart Cam Monitoring")
    return (f"{received} by {tool}; recomputed and matched "
            f"{_t(src.get('rehashed_at'), 'Asia/Kolkata')}")


def _thumbnail(path: Path) -> io.BytesIO:
    """A reduced copy for the printed annexure. The full-resolution frame stays in the bundle;
    embedding twelve of them would make a 5 MB PDF nobody can email."""
    from PIL import Image as PILImage

    with PILImage.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((960, 960))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=82)
    out.seek(0)
    return out


def _recorder(c: dict[str, Any]) -> str:
    rec = c.get("recorder_id") or "not recorded"
    return f"{rec} / channel {c['channel_no']}" if c.get("channel_no") else rec


def _form_footer(manifest: dict[str, Any], draft_id: str):
    """On the pages to be signed: only enough to tie the page to its bundle and hash report."""
    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 6.5)
        canvas.setFillColor(colors.HexColor("#6b7280"))
        canvas.drawString(18 * mm, 9 * mm, f"Bundle {manifest['bundle_id']} · {draft_id} · hash "
                                            f"report: Annexure A")
        canvas.drawRightString(A4[0] - 18 * mm, 9 * mm, f"page {doc.page}")
        canvas.restoreState()
    return draw


def _footer(manifest: dict[str, Any], draft_id: str):
    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica-Bold", 30)
        canvas.setFillColor(colors.Color(0.85, 0.85, 0.85, alpha=0.35))
        canvas.translate(A4[0] / 2, A4[1] / 2)
        canvas.rotate(55)
        canvas.drawCentredString(0, 18, "DRAFT — NOT SIGNED")
        canvas.setFont("Helvetica", 11)
        canvas.drawCentredString(0, -4, "Not a certificate until affirmed and signed by the "
                                        "person in charge (Part A) and an expert (Part B)")
        canvas.restoreState()
        canvas.saveState()
        canvas.setFont("Helvetica", 6.5)
        canvas.setFillColor(colors.HexColor("#6b7280"))
        canvas.drawString(18 * mm, 9 * mm,
                          f"DRAFT {draft_id} prepared by Smart Cam Monitoring — not signed — "
                          f"bundle {manifest['bundle_id']} — manifest SHA-256 "
                          f"{manifest['manifest_sha256'][:16]}…")
        canvas.drawRightString(A4[0] - 18 * mm, 9 * mm, f"page {doc.page}")
        canvas.restoreState()
    return draw
