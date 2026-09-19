"""The certificate form in the Schedule to the Bharatiya Sakshya Adhiniyam, 2023, as printed.

This module is statute, not product copy. Every word below was checked against the Gazette of
India text; nothing is paraphrased, and the order of blanks and tick boxes is the order printed.
Keep it that way: a draft certificate that differs from the statutory form invites an argument
about whether it is the form at all, and that argument is lost by the person holding the footage.

Source: Gazette of India, Extraordinary, Part II Section 1, No. 55, 25 December 2023
(The Bharatiya Sakshya Adhiniyam, 2023, No. 47 of 2023), Schedule at pp. 46-47, section 63 at
pp. 22-24. https://egazette.gov.in/WriteReadData/2023/250882.pdf — SHA-256
31ee68ea57b5303bf6697b5d8cb2004b9eb9180d7e9cbcbbdbf5bccab7cda5c1 as retrieved 19 September 2026.
Cross-checked against the Ministry of Home Affairs copy of the same Gazette.

Representation: a form is a list of blocks. ``("text", s)`` is printed as is, with ``{name}``
marking a blank; ``("ticks", key, options)`` is a row of tick boxes; ``("signature", label)`` is a
signature line. The renderer decides what, if anything, goes in each blank — see certificate.py.
"""

from __future__ import annotations

from typing import Any

GAZETTE = {
    "title": "The Bharatiya Sakshya Adhiniyam, 2023 (No. 47 of 2023)",
    "publication": "Gazette of India, Extraordinary, Part II, Section 1, No. 55, 25 December 2023",
    "url": "https://egazette.gov.in/WriteReadData/2023/250882.pdf",
    "sha256": "31ee68ea57b5303bf6697b5d8cb2004b9eb9180d7e9cbcbbdbf5bccab7cda5c1",
    "pages": "Schedule pp. 46-47; section 63 pp. 22-24",
}

DEVICE_OPTIONS = ["Computer / Storage Media", "DVR", "Mobile", "Flash Drive",
                  "CD/DVD", "Server", "Cloud", "Other"]
CONTROL_OPTIONS = ["Owned", "Maintained", "Managed", "Operated"]
HASH_OPTIONS = ["SHA1:", "SHA256:", "MD5:", "Other{hash_other} (Legally acceptable standard)"]

_OPENING = ("I, {name} (Name), Son/daughter/spouse of {parent} residing/employed at {address} "
            "do hereby solemnly affirm and sincerely state and submit as follows:—")

_DEVICE_FIELDS: list[tuple[Any, ...]] = [
    ("text", "Other: {device_other}"),
    ("text", "Make & Model: {make_model} Color: {color}"),
    ("text", "Serial Number: {serial}"),
    ("text", "IMEI/UIN/UID/MAC/Cloud ID{device_id} (as applicable)"),
    ("text", "and any other relevant information, if any, about the device/digital "
             "record{device_other_info}(specify)."),
]

_HASH: list[tuple[Any, ...]] = [
    ("text", "I state that the HASH value/s of the electronic/digital record/s is {hash_value}, "
             "obtained through the following algorithm:—"),
    ("ticks", "hash_algorithm", HASH_OPTIONS),
    ("text", "(Hash report to be enclosed with the certificate)"),
]

_DATE: list[tuple[Any, ...]] = [
    ("text", "Date (DD/MM/YYYY): {date}"),
    ("text", "Time (IST): {time}hours (In 24 hours format)"),
    ("text", "Place: {place}"),
]

PART_A: list[tuple[Any, ...]] = [
    ("heading", "THE SCHEDULE"),
    ("subheading", "[See section 63(4)(c)]"),
    ("heading", "CERTIFICATE"),
    ("heading", "PART A"),
    ("subheading", "(To be filled by the Party)"),
    ("text", _OPENING),
    ("text", "I have produced electronic record/output of the digital record taken from the "
             "following device/digital record source (tick mark):—"),
    ("ticks", "device", DEVICE_OPTIONS),
    *_DEVICE_FIELDS,
    ("text", "The digital device or the digital record source was under the lawful control for "
             "regularly creating, storing or processing information for the purposes of carrying "
             "out regular activities and during this period, the computer or the communication "
             "device was working properly and the relevant information was regularly fed into the "
             "computer during the ordinary course of business. If the computer/digital device at "
             "any point of time was not working properly or out of operation, then it has not "
             "affected the electronic/digital record or its accuracy. The digital device or the "
             "source of the digital record is:—"),
    ("ticks", "control", CONTROL_OPTIONS),
    ("text", "by me (select as applicable)."),
    *_HASH,
    ("signature", "(Name and signature)"),
    *_DATE,
]

PART_B: list[tuple[Any, ...]] = [
    ("heading", "PART B"),
    ("subheading", "(To be filled by the Expert)"),
    ("text", _OPENING),
    ("text", "The produced electronic record/output of the digital record are obtained from the "
             "following device/digital record source (tick mark):—"),
    ("ticks", "device", DEVICE_OPTIONS),
    *_DEVICE_FIELDS,
    *_HASH,
    ("signature", "(Name, designation and signature)"),
    *_DATE,
]

#: Section 63(4), which the draft quotes so the reader knows what the signatures attest to.
SECTION_63_4 = (
    "(4) In any proceeding where it is desired to give a statement in evidence by virtue of this "
    "section, a certificate doing any of the following things shall be submitted along with the "
    "electronic record at each instance where it is being submitted for admission, namely:— "
    "(a) identifying the electronic record containing the statement and describing the manner in "
    "which it was produced; (b) giving such particulars of any device involved in the production "
    "of that electronic record as may be appropriate for the purpose of showing that the "
    "electronic record was produced by a computer or a communication device referred to in "
    "clauses (a) to (e) of sub-section (3); (c) dealing with any of the matters to which the "
    "conditions mentioned in sub-section (2) relate, and purporting to be signed by a person in "
    "charge of the computer or communication device or the management of the relevant activities "
    "(whichever is appropriate) and an expert shall be evidence of any matter stated in the "
    "certificate; and for the purposes of this sub-section it shall be sufficient for a matter to "
    "be stated to the best of the knowledge and belief of the person stating it in the "
    "certificate specified in the Schedule."
)

BLANK_WIDTH = {
    "name": 21, "parent": 19, "address": 26, "device_other": 40, "make_model": 15, "color": 15,
    "serial": 15, "device_id": 21, "device_other_info": 4, "hash_value": 17, "hash_other": 18,
    "date": 5, "time": 8, "place": 12,
}


def blank_text(block_text: str, values: dict[str, str] | None = None) -> str:
    """The printed line, with each blank either filled or shown as underscores."""
    values = values or {}
    return block_text.format(**{k: values.get(k) or "_" * w for k, w in BLANK_WIDTH.items()})
