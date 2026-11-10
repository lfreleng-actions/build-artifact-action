# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The 'overlay' option of build-artifact-action's 'unpack' mode.

With overlay, unpack adds an artefact's files to a directory that
already exists instead of creating one. The artefact must replace
nothing: every member is checked against the destination's current
contents before anything is written. The tar is then extracted into a
fresh staging directory inside the destination and verified there by
the same rules as any unpack, and only then is each new top-level path
moved in, by a rename that fails rather than replace a path that
appeared in the meantime. A failure at any point removes the staging
directory and everything added, so the destination is left as it was.

Everything after plan_overlay runs with the destination, held open,
as the working directory (unpack_anchor.py), and reaches it as '.'.
"""

from __future__ import annotations

import os
import posixpath
import stat
import tarfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from scripts.manifest import open_dir, open_parent
from scripts.manifest_schema import fail
from scripts.unpack_anchor import names_working_directory
from scripts.unpack_modes import Identity
from scripts.unpack_names import REASON, ArtefactNames, DestinationNames, LinkScan
from scripts.unpack_pins import Pins
from scripts.unpack_rename import rename_noreplace
from scripts.unpack_rollback import (
    DIRECTORY_FLAGS,
    Added,
    make_private_dir,
    remove_held,
    warn,
)
from scripts.validate import PATH_RULE, inside, path_grammar_ok

STAGING_PREFIX = ".build-artifact-staging-"
# Linux's MAXSYMLINKS: a chain any longer cannot resolve at all.
MAX_LINK_HOPS = 40


def overlay_enabled(env: Mapping[str, str]) -> bool:
    """Return the INPUT_OVERLAY flag, failing unless it is 'true' or 'false'."""
    value = env.get("INPUT_OVERLAY", "false")
    if value not in ("true", "false"):
        fail("overlay: must be exactly 'true' or 'false'")
    return value == "true"


@dataclass
class OverlayPlan:
    """What an overlay unpack adds to the destination.

    roots holds, for each member, the shallowest path of it that does
    not yet exist; moving each root from staging adds every member.
    existing holds the directory members that already exist, whose
    modes unpack leaves alone.
    """

    roots: set[str] = field(default_factory=set)
    existing: set[str] = field(default_factory=set)


def open_child(parent: int, segment: str, part: str, value: str) -> int:
    """Open directory segment in parent, never following a symlink.

    part names it below the workspace, and value the destination, in
    the error that says why it cannot be opened.
    """
    try:
        return os.open(segment, DIRECTORY_FLAGS, dir_fd=parent)
    except OSError as error:
        reason = error.strerror
    try:
        mode = os.stat(segment, dir_fd=parent, follow_symlinks=False).st_mode
    except FileNotFoundError:
        fail(f"destination: {value!r} must already exist with overlay")
    except OSError as error:
        fail(f"destination: cannot stat {part!r}: {error.strerror}")
    if stat.S_ISLNK(mode):
        fail(f"destination: {part!r} is a symlink")
    if not stat.S_ISDIR(mode):
        fail(f"destination: {part!r} is not a directory")
    fail(f"destination: cannot open {part!r}: {reason}")


def plan_overlay(workspace: str, value: str) -> tuple[str, int]:
    """Return the absolute overlay destination, which must exist, and a descriptor.

    workspace is already canonical. Every component from the workspace
    down, the destination included, must be a real directory and never
    a symlink. '.' names the workspace itself. Each is opened from the
    one above with O_NOFOLLOW, so the descriptor returned names the
    directory checked, whatever later happens to its path; the caller
    closes it.
    """
    if not path_grammar_ok(value):
        fail(f"destination: {PATH_RULE}")
    value = posixpath.normpath(value)
    current = workspace
    fd = open_dir(workspace, f"workspace {workspace}", None)
    try:
        if value != ".":
            segments = value.split("/")
            for depth, segment in enumerate(segments, start=1):
                current = os.path.join(current, segment)
                part = "/".join(segments[:depth])
                child = open_child(fd, segment, part, value)
                os.close(fd)
                fd = child
        if os.path.realpath(current) != current or not inside(current, workspace):
            fail(f"destination: {current} does not resolve inside the workspace")
    except BaseException:
        os.close(fd)
        raise
    return current, fd


def existing_kind(root: str, relative: str) -> str | None:
    """Return what root/relative is now: 'dir', 'symlink', 'file' or None.

    The parent is reached through open_parent, so no symlink above the
    path is followed; the path itself is not followed either.
    """
    parent, name = open_parent(root, relative)
    try:
        mode = os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return None
    except OSError as error:
        fail(f"destination: cannot stat {relative!r}: {error.strerror}")
    finally:
        os.close(parent)
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    return "file"


def check_member(
    member: tarfile.TarInfo,
    kinds: dict[str, str | None],
    names: DestinationNames,
    plan: OverlayPlan,
) -> None:
    """Fail if member would replace, or pass through, anything but a directory.

    So does a path of it spelled differently from an equivalent entry
    already in the destination, which some filesystems treat as one.
    """
    name = member.name
    segments = name.split("/")
    for depth in range(1, len(segments) + 1):
        prefix = "/".join(segments[:depth])
        if prefix not in kinds:
            alias = names.alias(prefix)
            if alias is not None:
                fail(f"tar member {name!r}: {prefix!r} and existing {alias!r} {REASON}")
            kinds[prefix] = existing_kind(os.curdir, prefix)
        kind = kinds[prefix]
        if kind is None:
            plan.roots.add(prefix)
            return
        last = depth == len(segments)
        if kind == "dir" and last and member.isdir():
            plan.existing.add(name)
        elif kind == "symlink" and not last:
            fail(f"tar member {name!r}: would be written through symlink {prefix!r}")
        elif kind == "file" and not last:
            fail(f"tar member {name!r}: lies below existing file {prefix!r}")
        elif last:
            noun = "directory" if kind == "dir" else kind
            fail(f"tar member {name!r}: would replace existing {noun} {prefix!r}")


def existing_link(root: str, relative: str) -> str | None:
    """Return the target of the symlink at root/relative now, or None.

    Each parent is opened with O_NOFOLLOW, as open_parent does. None
    also covers a parent that is missing or not a directory: nothing
    below one can be followed.
    """
    *parents, name = relative.split("/")
    fd = open_dir(root, f"root {root}", None)
    try:
        for depth, segment in enumerate(parents, start=1):
            try:
                child = os.open(segment, DIRECTORY_FLAGS, dir_fd=fd)
            except (FileNotFoundError, NotADirectoryError):
                return None
            except OSError as error:
                part = "/".join(parents[:depth])
                fail(f"destination: cannot open {part!r}: {error.strerror}")
            os.close(fd)
            fd = child
        try:
            mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
            return os.readlink(name, dir_fd=fd) if stat.S_ISLNK(mode) else None
        except FileNotFoundError:
            return None
        except OSError as error:
            fail(f"destination: cannot read {relative!r}: {error.strerror}")
    finally:
        os.close(fd)


def inside_root(root: str, target: str) -> str | None:
    """Return absolute target relative to root, or None if it may lie outside.

    root is the destination's path and the working directory the
    destination itself. A target holding '..' is refused, since its
    meaning depends on links above root that this check does not
    follow. So is any target once root no longer reaches the working
    directory without a symlink: a parent swapped since plan_overlay
    would send the target somewhere else.
    """
    if ".." in target.split("/"):
        return None
    normal = posixpath.normpath(target)
    if normal == root:
        within = ""
    elif normal.startswith(root.rstrip("/") + "/"):
        within = normal[len(root.rstrip("/")) + 1 :]
    else:
        return None
    return within if names_working_directory(root) else None


def check_link_escape(root: str, member: tarfile.TarInfo, scan: LinkScan) -> None:
    """Fail if symlink member would resolve outside root once unpacked.

    Each segment is resolved against the tree as it will stand after
    the unpack: planned members, which never replace an existing path,
    and the destination's current contents. A chain may therefore pass
    through new and existing symlinks in any order. A segment matching
    a member or an existing entry only under equivalence fails, never
    resolving to the other spelling.
    """
    planned, links = scan.planned, scan.links
    name = member.name
    resolved = name.split("/")[:-1]
    pending = list(reversed(member.linkname.split("/")))
    hops = 1
    while pending:
        segment = pending.pop()
        if segment in ("", "."):
            continue
        if segment == "..":
            if not resolved:
                fail(
                    f"tar member {name!r}: symlink target resolves outside the destination"
                )
            _ = resolved.pop()
            continue
        relative = "/".join([*resolved, segment])
        scan.check_spelling(name, relative)
        if relative in planned:
            target = planned[relative].linkname if planned[relative].issym() else None
        else:
            if relative not in links:
                links[relative] = existing_link(os.curdir, relative)
            target = links[relative]
        if target is None:
            resolved.append(segment)
            continue
        hops += 1
        if hops > MAX_LINK_HOPS:
            fail(f"tar member {name!r}: symlink chain too long to resolve")
        if target.startswith("/"):
            within = inside_root(root, target)
            if within is None:
                fail(
                    f"tar member {name!r}: symlink target resolves outside the destination"
                )
            resolved = []
            target = within
        pending.extend(reversed(target.split("/")))


def scan_overlay(root: str, members: Iterable[tarfile.TarInfo]) -> OverlayPlan:
    """Check every member against root's current contents; return the plan.

    root is the destination's absolute path, against which an absolute
    symlink target is judged; its contents are read through the working
    directory, which is the destination.

    Fails on the first member with a path or prefix that another
    member, or an existing entry, spells differently but equivalently
    (unpack_names.py), and on the first that would replace an existing
    file, directory or symlink, lie below an existing file, or be
    written through an existing symlink. A directory member may name an
    existing directory. Then fails on the first symlink member that
    would resolve outside root through any chain of new and existing
    symlinks.
    """
    plan = OverlayPlan()
    kinds: dict[str, str | None] = {}
    planned: dict[str, tarfile.TarInfo] = {}
    scan = LinkScan(planned, ArtefactNames(), DestinationNames())
    for member in members:
        scan.spellings.add(member.name)
        check_member(member, kinds, scan.existing, plan)
        planned[member.name] = member
    for member in planned.values():
        if member.issym():
            check_link_escape(root, member, scan)
    return plan


def make_staging(
    members: Iterable[tarfile.TarInfo], prefix: str = STAGING_PREFIX
) -> tuple[str, int]:
    """Create a private directory in '.' whose name no member starts with.

    Returns its name and a descriptor holding it, which the caller
    closes, so cleanup can reach it whatever later holds the name.
    """
    tops = {member.name.split("/")[0] for member in members}
    while True:
        try:
            staging = make_private_dir(None, prefix)
        except OSError as error:
            fail(f"destination: cannot create a staging directory: {error.strerror}")
        if staging in tops:
            os.rmdir(staging)
            continue
        try:
            return staging, os.open(staging, DIRECTORY_FLAGS)
        except OSError as error:
            os.rmdir(staging)
            fail(f"destination: cannot open {staging}: {error.strerror}")


def record_tree(staging: str, root: str, relative: str) -> list[Added]:
    """Return records of staging/relative and all below it, parents first.

    Each record carries the identity the entry has in staging, which no
    other user can enter, and keeps across a rename: what it names is
    what this step made, whatever later holds the path in root.
    """
    records: list[Added] = []
    pending = [relative]
    while pending:
        current = pending.pop()
        path = os.path.join(staging, current)
        info = os.lstat(path)
        directory = stat.S_ISDIR(info.st_mode)
        records.append(Added(root, current, info.st_dev, info.st_ino, directory))
        if directory:
            with os.scandir(path) as entries:
                names = sorted((entry.name for entry in entries), reverse=True)
            pending.extend(f"{current}/{name}" for name in names)
    return records


def move_root(
    staging: str, root: str, relative: str, records: list[Added], added: list[Added]
) -> None:
    """Move staging/relative, with all below it, to root/relative.

    root/relative must not exist. os.rename would silently replace a
    file or empty directory created there since scan_overlay looked, so
    the move is one rename_noreplace call, which fails instead. records,
    what record_tree found there, join added once it is in root.
    """
    source, source_name = open_parent(staging, relative)
    try:
        target, name = open_parent(root, relative)
        try:
            rename_noreplace(source, source_name, target, name)
        except FileExistsError:
            fail(f"{relative}: appeared in the destination during unpack")
        except OSError as error:
            fail(f"{relative}: cannot move into the destination: {error.strerror}")
        finally:
            os.close(target)
    finally:
        os.close(source)
    added.extend(records)


def place(
    staging: str, root: str, plan: OverlayPlan, added: list[Added], pins: Pins
) -> dict[str, Identity]:
    """Move each new path from staging to root.

    Every entry is pinned first, so its identity stays its own until
    pins are released. Each entry moved is recorded in added as soon as
    it is in root, so a failure part-way leaves remove_added a full list
    to undo. Returns the identity each entry had in staging, by its path
    in root, so the modes set afterwards reach only what this step added.
    """
    trees = {
        relative: record_tree(staging, root, relative)
        for relative in sorted(plan.roots)
    }
    pins.reserve(sum(entry.directory for tree in trees.values() for entry in tree))
    for records in trees.values():
        pins.pin(staging, records)
    for relative, records in trees.items():
        move_root(staging, root, relative, records, added)
    return {
        entry.relative: (entry.device, entry.inode)
        for entry in added
        if not entry.staging
    }


def drop_staging(staging: str, fd: int, added: list[Added]) -> None:
    """Remove staging, held as fd, from '.', and its record from added.

    It is emptied through fd and removed only while its name still
    names it, so a directory another process put in its place keeps its
    contents, with a warning.
    """
    if not remove_held(None, staging, fd):
        warn(f"left {staging}: another process replaced it")
    added[:] = [entry for entry in added if not entry.staging]
