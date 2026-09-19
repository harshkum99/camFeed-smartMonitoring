"""A Merkle root anyone can recompute, with nothing but SHA-256 and this description.

An evidence bundle is only worth its root if the other side's expert can reproduce the root
without our software. So the construction is fixed, small, and written down in every bundle's
README, following RFC 6962 (Certificate Transparency) section 2.1:

    leaf hash  = SHA-256( 0x00 || leaf bytes )
    node hash  = SHA-256( 0x01 || left || right )
    empty tree = SHA-256( "" )

A leaf is the UTF-8 line ``<sha256 of the file> <path inside the bundle>``, and leaves are ordered
by path. The 0x00/0x01 prefixes stop an attacker presenting an internal node as a leaf — without
them, two different file lists can share a root. The split point is the largest power of two
smaller than the leaf count, so an unbalanced tree is still deterministic.

Stdlib only: this module is copied verbatim into every bundle as part of its verifier.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence


def leaf_bytes(sha256_hex: str, path: str) -> bytes:
    if len(sha256_hex) != 64 or any(c not in "0123456789abcdef" for c in sha256_hex):
        raise ValueError(f"not a lowercase sha256 hex digest: {sha256_hex!r}")
    if "\n" in path or path != path.strip():
        raise ValueError(f"path must be a single trimmed line: {path!r}")
    return f"{sha256_hex} {path}".encode()


def _leaf(data: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + data).digest()


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _split(n: int) -> int:
    k = 1
    while k * 2 < n:
        k *= 2
    return k


def _root(leaves: Sequence[bytes]) -> bytes:
    if len(leaves) == 1:
        return _leaf(leaves[0])
    k = _split(len(leaves))
    return _node(_root(leaves[:k]), _root(leaves[k:]))


def merkle_root(items: Sequence[tuple[str, str]]) -> str:
    """Root over (sha256_hex, path) pairs. Order-independent: items are sorted by path."""
    ordered = sorted(items, key=lambda it: it[1])
    paths = [p for _, p in ordered]
    if len(set(paths)) != len(paths):
        raise ValueError("duplicate path in bundle")
    if not ordered:
        return hashlib.sha256(b"").hexdigest()
    return _root([leaf_bytes(h, p) for h, p in ordered]).hex()


def inclusion_proof(items: Sequence[tuple[str, str]], path: str) -> list[tuple[str, str]]:
    """Sibling hashes, bottom-up, proving one item is in the root: [("L"|"R", hex), ...].

    Lets a single frame be shown to belong to a bundle without disclosing the rest of it.
    """
    ordered = sorted(items, key=lambda it: it[1])
    leaves = [leaf_bytes(h, p) for h, p in ordered]
    index = next((i for i, (_, p) in enumerate(ordered) if p == path), None)
    if index is None:
        raise KeyError(path)

    def walk(lo: int, hi: int) -> list[tuple[str, str]]:
        if hi - lo == 1:
            return []
        k = _split(hi - lo)
        if index < lo + k:
            return [*walk(lo, lo + k), ("R", _root(leaves[lo + k:hi]).hex())]
        return [*walk(lo + k, hi), ("L", _root(leaves[lo:lo + k]).hex())]

    return walk(0, len(leaves))


def verify_inclusion(sha256_hex: str, path: str, proof: list[tuple[str, str]], root: str) -> bool:
    acc = _leaf(leaf_bytes(sha256_hex, path))
    for side, sibling in proof:
        s = bytes.fromhex(sibling)
        acc = _node(s, acc) if side == "L" else _node(acc, s)
    return acc.hex() == root
