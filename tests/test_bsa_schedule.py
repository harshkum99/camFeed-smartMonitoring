"""The encoded Schedule must stay word-for-word the statutory form.

A draft that drifts from the Gazette text invites an argument about whether it is the prescribed
form at all. The fixture is the Gazette's own text for pages 46-47; every printed line of both
Parts must appear in it, and nothing may be added between them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from smartcam.evidence.bsa_schedule import (
    CONTROL_OPTIONS,
    DEVICE_OPTIONS,
    HASH_OPTIONS,
    PART_A,
    PART_B,
    blank_text,
)

GAZETTE = (Path(__file__).parent / "fixtures" / "bsa_schedule_gazette.txt").read_text()


def norm(s: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"\s+", " ", s)).strip()


def printed(part):
    for block in part:
        if block[0] == "ticks":
            yield from (blank_text(o) for o in block[2])
        else:
            yield blank_text(block[1])


@pytest.mark.parametrize("part", [PART_A, PART_B], ids=["part_a", "part_b"])
def test_every_printed_line_is_verbatim_statute(part):
    body = norm(GAZETTE)
    missing = [line for line in printed(part) if norm(line) not in body]
    assert missing == []


@pytest.mark.parametrize("part", [PART_A, PART_B], ids=["part_a", "part_b"])
def test_lines_appear_in_the_gazette_order(part):
    body, pos = norm(GAZETTE), 0
    if part is PART_B:
        pos = body.index("PART B")
    for line in printed(part):
        at = body.find(norm(line), pos)
        assert at >= 0, line
        pos = at


def test_part_b_has_no_lawful_control_affirmation():
    """Only the Party affirms how the device was run. The expert's part must not acquire it."""
    text = " ".join(printed(PART_B))
    assert "lawful control" not in text
    assert "Owned" not in text


def test_the_option_lists_are_the_printed_ones():
    assert DEVICE_OPTIONS == ["Computer / Storage Media", "DVR", "Mobile", "Flash Drive",
                              "CD/DVD", "Server", "Cloud", "Other"]
    assert CONTROL_OPTIONS == ["Owned", "Maintained", "Managed", "Operated"]
    assert [o.split("{")[0] for o in HASH_OPTIONS] == ["SHA1:", "SHA256:", "MD5:", "Other"]


def test_filled_blanks_replace_underscores_and_unfilled_ones_stay_blank():
    line = blank_text("Serial Number: {serial}", {"serial": "ABC123"})
    assert line == "Serial Number: ABC123"
    assert blank_text("Serial Number: {serial}") == "Serial Number: " + "_" * 15
