"""Local checkouts for audit agents: a hardened bare cache per repository and one
detached worktree per attempt.

As with observer sources, no repository-supplied hooks, filters, credential
helpers or submodules run, and the reader token is only ever sent to the
configured target repository, never to a fork's remote.
"""

import base64
import os
import re
import uuid
from pathlib import Path, PurePosixPath

from cirra.domain import CirraError, digest, sha
from cirra.observer_process import control_environment, run_control
from cirra.publication_fence import require_state_owner
from cirra.publish import sync_directory
from cirra.quorum import current_owner

ORIGINS = {
    "github": ("https://github.com", "x-access-token"),
    "gitlab": ("https://gitlab.com", "oauth2"),
}


def change_ref(provider, change):
    return (
        f"refs/pull/{change}/head" if provider == "github" else f"refs/merge-requests/{change}/head"
    )


def _environment(cache, url, username, token, protocols):
    hooks = cache.parent / "empty-hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    env = control_environment()
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_ATTR_NOSYSTEM="1",
    )
    values = [
        ("core.hooksPath", str(hooks)),
        ("credential.helper", ""),
        ("protocol.allow", "never"),
        ("http.followRedirects", "false"),
        ("fetch.fsckObjects", "true"),
        ("transfer.fsckObjects", "true"),
        ("core.autocrlf", "false"),
        ("core.symlinks", "false"),
        ("submodule.recurse", "false"),
        # Run automatic maintenance in the foreground: a detached gc is orphaned and,
        # without an init process (a container), never reaped.
        ("gc.autoDetach", "false"),
        ("maintenance.autoDetach", "false"),
    ]
    values += [(f"protocol.{name}.allow", "always") for name in protocols]
    if token:
        auth = base64.b64encode((username + ":" + token).encode()).decode()
        values.append((f"http.{url}.extraHeader", "Authorization: Basic " + auth))
    env["GIT_CONFIG_COUNT"] = str(len(values))
    for index, (key, value) in enumerate(values):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def prepare_checkout(
    state_dir,
    provider,
    repository,
    ref,
    head,
    destination,
    *,
    base=None,
    token=None,
    url=None,
    protocols=("https",),
):
    """Fetch `ref` into the repository's cache and add a worktree at `head`.

    `url` and `protocols` exist for local tests; production uses the provider's
    HTTPS origin for the configured repository.
    """
    sha(head)
    if base is not None:
        sha(base)
    origin, username = ORIGINS.get(provider, (None, None))
    if url is None:
        if origin is None:
            raise CirraError("audit checkouts support GitHub and GitLab routes")
        url = f"{origin}/{repository}.git"
    cache = Path(state_dir).resolve() / "audit-repos" / digest([provider, repository]) / "git"
    env = _environment(cache, url, username or "", token, protocols)
    if not (cache / "HEAD").exists():
        cache.mkdir(parents=True, exist_ok=True)
        run_control(["git", "init", "--bare", "--template="], cache, env=env)

    def git(*args, timeout=600):
        return run_control(["git", *args], cache, env=env, timeout=timeout).decode().strip()

    git("fetch", "--no-tags", "--no-recurse-submodules", url, f"+{ref}:refs/cirra/audit")
    for revision in filter(None, (head, base)):
        try:
            found = git("rev-parse", "--verify", revision + "^{commit}")
        except CirraError:
            found = None
        if found != revision:
            raise CirraError("audit revision is not reachable from the fetched ref; refresh")
    destination = Path(destination).resolve()
    if destination.exists():
        raise CirraError("audit checkout destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "--detach", str(destination), head)
    return destination


def _link_text(path):
    # Never follow links out of the selected copy, including through a parent.
    if path.resolve() != path.absolute() or not path.is_file():
        raise CirraError("audit checkout metadata is missing or contains a symbolic link")
    with path.open("rb") as stream:
        raw = stream.read(4097)
    try:
        value = raw.decode("utf-8").rstrip("\r\n")
    except UnicodeError:
        raise CirraError("invalid audit checkout metadata") from None
    if len(raw) > 4096 or any(c in value for c in ("\0", "\n", "\r")):
        raise CirraError("invalid audit checkout metadata")
    return value


def _old_root(value, suffix):
    # Parse the other host's paths as text; do not resolve or open their targets.
    path = PurePosixPath(value.replace("\\", "/"))
    if (
        not (path.is_absolute() or re.match(r"^[A-Za-z]:/", str(path)))
        or ".." in path.parts
        or tuple(path.parts[-len(suffix.parts) :]) != suffix.parts
    ):
        raise CirraError("audit checkout links do not match this attempt and repository")
    return path.parents[len(suffix.parts) - 1]


def _replace_link(path, text):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            os.chmod(temporary, 0o600)
            stream.write((text + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def repair_checkout(state_dir, provider, repository, destination, *, head=None):
    """Rebase only this attempt's two Git links after a private-state restore.

    A retained source copy can still exist. Do not run a repository-wide Git
    repair, which can follow its registered paths back into that other copy.
    Validate both ends before writing, and tolerate an interrupted two-file repair.
    """
    root = Path(state_dir).resolve(strict=True)
    destination = Path(destination).absolute()
    relative = destination.relative_to(root) if destination.is_relative_to(root) else Path()
    if (
        len(relative.parts) != 3
        or relative.parts[0] != "audit-attempts"
        or not re.fullmatch("[0-9a-f]{32}", relative.parts[1])
        or relative.parts[2] != "checkout"
    ):
        raise CirraError("audit checkout is outside the selected attempt directory")
    pointer = destination / ".git"
    value = _link_text(pointer)
    if not value.startswith("gitdir: "):
        raise CirraError("audit checkout is not a linked worktree")
    name = PurePosixPath(value[8:].replace("\\", "/")).name
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise CirraError("invalid audit checkout worktree name")
    cache_relative = PurePosixPath("audit-repos", digest([provider, repository]), "git")
    admin_relative = cache_relative / "worktrees" / name
    original = _old_root(value[8:], admin_relative)
    admin = root.joinpath(*admin_relative.parts)
    backlink = admin / "gitdir"
    back = _link_text(backlink)
    back_root = _old_root(back, PurePosixPath(relative.as_posix()) / ".git")
    selected = PurePosixPath(root.as_posix())
    if original != back_root and selected not in (original, back_root):
        raise CirraError("audit checkout links name different state copies")
    if _link_text(admin / "commondir") != "../..":
        raise CirraError("audit checkout has an unexpected common directory")
    revision = _link_text(admin / "HEAD")
    sha(revision)
    if head is not None and revision != sha(head):
        raise CirraError("audit checkout revision differs from its immutable attempt")
    changes = [
        (pointer, value, "gitdir: " + admin.as_posix()),
        (backlink, back, pointer.as_posix()),
    ]
    changed = False
    for path, before, after in changes:
        if before.replace("\\", "/") == after:
            continue
        require_state_owner(root)
        if owner := current_owner():
            owner.compare({}, {})
        _replace_link(path, after)
        changed = True
    return changed


def remove_checkout(state_dir, provider, repository, destination, *, head=None):
    cache = Path(state_dir).resolve() / "audit-repos" / digest([provider, repository]) / "git"
    if Path(destination).exists() and (cache / "HEAD").exists():
        repair_checkout(state_dir, provider, repository, destination, head=head)
        require_state_owner(state_dir)
        if owner := current_owner():
            owner.compare({}, {})
        run_control(
            ["git", "worktree", "remove", "--force", str(Path(destination).resolve())],
            cache,
            env=_environment(cache, "", "", None, ()),
        )
