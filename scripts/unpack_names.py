# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Equivalent spellings, which the overlay of 'unpack' mode refuses.

A case-insensitive or normalising filesystem treats 'Jump' and 'jump',
or the NFC and NFD forms of 'café', as one name; case-insensitive HFS+
also ignores code points such as U+200D ZERO WIDTH JOINER. The overlay
checks compare names exactly, so on such a filesystem an artefact could
pass them with one spelling and land on, or resolve through, the
other. Two segments are equivalent when name_key gives the same key
and the strings differ; overlay refuses every such pair, within the
artefact and against the destination, whatever filesystem it runs on.
"""

from __future__ import annotations

import errno
import os
import tarfile
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field

from scripts.manifest_schema import fail
from scripts.unpack_rollback import DIRECTORY_FLAGS

REASON = "differ only by case or Unicode normalisation, or by ignorable code points"
ABSENT = (errno.ENOENT, errno.ENOTDIR, errno.ELOOP)

# Unicode's Default_Ignorable_Code_Point ranges, a superset of the code
# points HFS+ leaves out when it compares names (Apple TN1150).
IGNORABLE_RANGES = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
IGNORABLE = {
    point: None for first, last in IGNORABLE_RANGES for point in range(first, last + 1)
}


def name_key(segment: str) -> str:
    """Return the key under which equivalent spellings of segment collide.

    Ignorable code points are dropped, then the NFC form is case folded,
    and folded again after decomposition, since case folding can leave a
    string that is not normalised. Names any of the filesystems above
    would treat as one share a key.
    """
    folded = unicodedata.normalize("NFC", segment.translate(IGNORABLE)).casefold()
    decomposed = unicodedata.normalize("NFD", folded)
    return unicodedata.normalize("NFC", decomposed.casefold())


def path_key(path: str) -> tuple[str, ...]:
    """Return the key of each segment of path, in order."""
    return tuple(name_key(segment) for segment in path.split("/"))


class ArtefactNames:
    """The one spelling the artefact uses for each path and prefix."""

    def __init__(self) -> None:
        self.spellings: dict[tuple[str, ...], str] = {}

    def add(self, name: str) -> None:
        """Record member name and its prefixes; fail on a second spelling."""
        segments = name.split("/")
        for depth in range(1, len(segments) + 1):
            prefix = "/".join(segments[:depth])
            seen = self.spellings.setdefault(path_key(prefix), prefix)
            if seen != prefix:
                fail(f"tar member {name!r}: {prefix!r} and {seen!r} {REASON}")

    def alias(self, relative: str) -> str | None:
        """Return the artefact's other spelling of relative, if it has one."""
        seen = self.spellings.get(path_key(relative))
        return None if seen in (None, relative) else seen


class DestinationNames:
    """The entries of the working directory's directories, as they are spelled.

    Each directory is read from its parent with O_NOFOLLOW, never
    looked up by name, so the answer is the same on a case-sensitive
    filesystem as on one that is not.
    """

    def __init__(self) -> None:
        self.listings: dict[str, dict[str, list[str]]] = {}

    def listing(self, directory: str) -> dict[str, list[str]]:
        """Return the entries of directory by key, or none if it is not one."""
        if directory not in self.listings:
            self.listings[directory] = self.read(directory)
        return self.listings[directory]

    @staticmethod
    def read(directory: str) -> dict[str, list[str]]:
        """List directory, reached from '.' without following a symlink."""
        fd = os.open(os.curdir, DIRECTORY_FLAGS)
        try:
            for segment in directory.split("/") if directory else []:
                try:
                    child = os.open(segment, DIRECTORY_FLAGS, dir_fd=fd)
                except OSError as error:
                    if error.errno in ABSENT:
                        return {}
                    fail(f"destination: cannot open {directory!r}: {error.strerror}")
                os.close(fd)
                fd = child
            try:
                names = os.listdir(fd)
            except OSError as error:
                fail(f"destination: cannot list {directory!r}: {error.strerror}")
        finally:
            os.close(fd)
        keyed: dict[str, list[str]] = {}
        for entry in sorted(names):
            keyed.setdefault(name_key(entry), []).append(entry)
        return keyed

    def alias(self, relative: str) -> str | None:
        """Return an existing path equivalent to relative's last segment alone.

        None when an entry spelled exactly as relative exists, since the
        exact match is the one every other check judges, or when no
        equivalent entry exists.
        """
        parent, _, name = relative.rpartition("/")
        spellings = self.listing(parent).get(name_key(name), [])
        if not spellings or name in spellings:
            return None
        return f"{parent}/{spellings[0]}" if parent else spellings[0]


@dataclass
class LinkScan:
    """What check_link_escape resolves symlink targets against.

    planned holds the members by name, and spellings their names and
    prefixes; existing reads the destination's entries, and links
    caches the destination's current symlink targets.
    """

    planned: Mapping[str, tarfile.TarInfo]
    spellings: ArtefactNames
    existing: DestinationNames
    links: dict[str, str | None] = field(default_factory=dict)

    def check_spelling(self, name: str, relative: str) -> None:
        """Fail if relative matches a member or existing path only by equivalence."""
        alias = self.spellings.alias(relative)
        noun = ""
        if alias is None:
            alias = self.existing.alias(relative)
            noun = "existing "
        if alias is not None:
            target = f"symlink target {relative!r} and {noun}{alias!r}"
            fail(f"tar member {name!r}: {target} {REASON}")
