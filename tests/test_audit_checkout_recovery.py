"""Restored Git metadata must never reconnect to another retained state copy."""

import shutil
from pathlib import Path

import pytest

from cirra.audit_checkout import prepare_checkout, remove_checkout, repair_checkout
from cirra.domain import CirraError
from cirra.observer_process import run_control


@pytest.fixture
def copied_checkout(world, tmp_path):
    original = world.state
    relative = Path("audit-attempts") / ("a" * 32) / "checkout"
    prepare_checkout(
        original,
        "github",
        "example/repo",
        "refs/pull/7/head",
        world.head,
        original / relative,
        base=world.base,
        url=str(world.origin.path),
        protocols=("file",),
    )
    restored = tmp_path / "restored"
    shutil.copytree(original, restored)
    return original, restored, relative, world.head


def test_cleanup_of_restored_checkout_leaves_original_copy_untouched(copied_checkout):
    original, restored, relative, _ = copied_checkout
    old_pointer = (original / relative / ".git").read_bytes()
    old_admin = Path(old_pointer.decode().strip().removeprefix("gitdir: "))
    old_backlink = (old_admin / "gitdir").read_bytes()

    remove_checkout(restored, "github", "example/repo", restored / relative)

    assert not (restored / relative).exists()
    assert (original / relative / ".git").read_bytes() == old_pointer
    assert (old_admin / "gitdir").read_bytes() == old_backlink
    assert (original / relative / "app.py").is_file()


def links(copied_checkout):
    original, restored, relative, head = copied_checkout
    pointer = restored / relative / ".git"
    old_admin = Path(pointer.read_text().strip().removeprefix("gitdir: "))
    admin = restored / old_admin.relative_to(original)
    return pointer, admin, head


@pytest.mark.parametrize("interrupted", [None, "pointer", "backlink"])
def test_reconnect_is_idempotent_and_recovers_either_interrupted_write(
    copied_checkout, interrupted
):
    _, restored, relative, head = copied_checkout
    pointer, admin, _ = links(copied_checkout)
    if interrupted == "pointer":
        pointer.write_text("gitdir: " + admin.as_posix() + "\n")
    if interrupted == "backlink":
        (admin / "gitdir").write_text(pointer.as_posix() + "\n")
    assert repair_checkout(restored, "github", "example/repo", restored / relative, head=head)
    assert not repair_checkout(restored, "github", "example/repo", restored / relative, head=head)
    assert run_control(["git", "rev-parse", "HEAD"], restored / relative).decode().strip() == head


def test_reconnect_parses_another_hosts_paths_without_opening_them(copied_checkout):
    original, restored, relative, head = copied_checkout
    pointer, admin, _ = links(copied_checkout)
    for path in (pointer, admin / "gitdir"):
        value = path.read_text().replace(original.as_posix(), "/Users/matt/state/old-restore")
        path.write_text(value)
    assert repair_checkout(restored, "github", "example/repo", restored / relative, head=head)
    assert run_control(["git", "rev-parse", "HEAD"], restored / relative).decode().strip() == head


@pytest.mark.parametrize("damage", ["attempt", "repository", "common", "head", "oversize", "roots"])
def test_reconnect_refuses_unmatched_metadata_before_either_write(copied_checkout, damage):
    original, restored, relative, head = copied_checkout
    pointer, admin, _ = links(copied_checkout)
    backlink = admin / "gitdir"
    if damage == "attempt":
        backlink.write_text(backlink.read_text().replace("a" * 32, "b" * 32))
    elif damage == "repository":
        pointer.write_text(pointer.read_text().replace(admin.parents[2].name, "f" * 64))
    elif damage == "common":
        (admin / "commondir").write_text("../../../other\n")
    elif damage == "head":
        (admin / "HEAD").write_text("f" * 40 + "\n")
    elif damage == "oversize":
        backlink.write_text("/" + "x" * 4096)
    else:
        backlink.write_text(backlink.read_text().replace(original.as_posix(), "/different/copy"))
    before = [path.read_bytes() for path in (pointer, backlink)]
    with pytest.raises(CirraError):
        repair_checkout(restored, "github", "example/repo", restored / relative, head=head)
    assert [path.read_bytes() for path in (pointer, backlink)] == before


def test_reconnect_refuses_symlink_metadata(copied_checkout, tmp_path):
    _, restored, relative, head = copied_checkout
    pointer, admin, _ = links(copied_checkout)
    foreign = tmp_path / "foreign-pointer"
    foreign.write_bytes(pointer.read_bytes())
    pointer.unlink()
    try:
        pointer.symlink_to(foreign)
    except OSError:
        pytest.skip("creating symbolic links is unavailable")
    before = foreign.read_bytes(), (admin / "gitdir").read_bytes()
    with pytest.raises(CirraError, match="symbolic link"):
        repair_checkout(restored, "github", "example/repo", restored / relative, head=head)
    assert before == (foreign.read_bytes(), (admin / "gitdir").read_bytes())
