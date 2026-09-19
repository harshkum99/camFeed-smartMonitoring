"""The Merkle root is the number printed on a certificate, so it is tested against an independent
reimplementation and against the known ways naive Merkle trees are forged."""

from __future__ import annotations

import hashlib

import pytest

from smartcam.evidence.merkle import (
    inclusion_proof,
    leaf_bytes,
    merkle_root,
    verify_inclusion,
)


def items(n: int) -> list[tuple[str, str]]:
    return [(hashlib.sha256(f"file {i}".encode()).hexdigest(), f"source/cam/{i:03d}.avi")
            for i in range(n)]


def reference_root(pairs: list[tuple[str, str]]) -> str:
    """RFC 6962 section 2.1, written out again from the text rather than from our module."""
    leaves = [f"{h} {p}".encode() for h, p in sorted(pairs, key=lambda x: x[1])]

    def mth(d: list[bytes]) -> bytes:
        if not d:
            return hashlib.sha256(b"").digest()
        if len(d) == 1:
            return hashlib.sha256(b"\x00" + d[0]).digest()
        k = 1 << ((len(d) - 1).bit_length() - 1)
        return hashlib.sha256(b"\x01" + mth(d[:k]) + mth(d[k:])).digest()

    return mth(leaves).hex()


@pytest.mark.parametrize("n", [0, 1, 2, 3, 4, 5, 7, 8, 9, 16, 17])
def test_root_matches_an_independent_rfc6962_implementation(n):
    """If our root and the RFC's disagree, an opposing expert following the README would
    conclude the bundle was tampered with."""
    assert merkle_root(items(n)) == reference_root(items(n))


def test_root_does_not_depend_on_the_order_items_were_listed():
    assert merkle_root(items(6)) == merkle_root(list(reversed(items(6))))


def test_changing_one_byte_of_one_file_changes_the_root():
    base = items(5)
    altered = list(base)
    h, p = altered[2]
    altered[2] = (hashlib.sha256(b"different").hexdigest(), p)
    assert merkle_root(base) != merkle_root(altered)


def test_renaming_a_file_changes_the_root():
    """The path is part of the leaf, so a frame cannot be relabelled as another camera's."""
    base = items(3)
    renamed = [(base[0][0], "source/other/000.avi"), *base[1:]]
    assert merkle_root(base) != merkle_root(renamed)


def test_an_internal_node_cannot_be_presented_as_a_leaf():
    """The second-preimage forgery the 0x00/0x01 prefixes exist to stop."""
    two = items(2)
    root = merkle_root(two)
    left = hashlib.sha256(b"\x00" + leaf_bytes(*two[0])).digest()
    right = hashlib.sha256(b"\x00" + leaf_bytes(*two[1])).digest()
    forged_leaf = hashlib.sha256(left + right).hexdigest()
    assert merkle_root([(forged_leaf, "x")]) != root


def test_duplicate_paths_are_refused():
    h = items(1)[0][0]
    with pytest.raises(ValueError, match="duplicate"):
        merkle_root([(h, "a"), (h, "a")])


@pytest.mark.parametrize("bad", ["ABC", "g" * 64, "a" * 63])
def test_malformed_hashes_are_refused(bad):
    with pytest.raises(ValueError):
        merkle_root([(bad, "a")])


def test_paths_with_newlines_are_refused():
    """A newline in a path would let one leaf line masquerade as two in a printed report."""
    with pytest.raises(ValueError):
        merkle_root([(items(1)[0][0], "a\nb")])


@pytest.mark.parametrize("n", [1, 2, 3, 5, 8, 13])
def test_every_item_has_a_valid_inclusion_proof(n):
    its = items(n)
    root = merkle_root(its)
    for h, p in its:
        assert verify_inclusion(h, p, inclusion_proof(its, p), root)


def test_an_inclusion_proof_fails_for_an_altered_file():
    its = items(5)
    root = merkle_root(its)
    h, p = its[3]
    assert not verify_inclusion(hashlib.sha256(b"x").hexdigest(), p,
                                inclusion_proof(its, p), root)
