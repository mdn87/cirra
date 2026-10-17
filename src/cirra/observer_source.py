"""Fetch an exact PR/MR revision into a fresh bare repository and export its blobs.

No worktree, hooks, filters, LFS, attributes, submodules, or repository-supplied
programs run on the observer host. The only export is a bounded regular-file tar.
"""

import base64
import io
import os
import tarfile
from pathlib import Path

from cirra.domain import CirraError, number, path_name, sha
from cirra.github import repository_name
from cirra.gitlab import project_name
from cirra.observer_process import control_environment, run_control
from cirra.service_config import branch_name

MAX_SOURCE = 64 * 1024 * 1024


def export_source(
    repository, change, expected_sha, directory, token=None, *, provider="github", branch=None
):
    if provider == "github":
        repository_name(repository)
        origin, username, ref = "https://github.com", "x-access-token", "pull"
    elif provider == "gitlab":
        project_name(repository)
        origin, username, ref = "https://gitlab.com", "oauth2", "merge-requests"
    else:
        raise CirraError("independent checkout requires GitHub or GitLab")
    if branch is None:
        number(change, "change number", 1, 2**31 - 1, integral=True)
        source_ref = f"refs/{ref}/{change}/head"
    else:
        source_ref = "refs/heads/" + branch_name(branch)
    sha(expected_sha)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    bare = directory / "git"
    bare.mkdir()
    hooks = directory / "empty-hooks"
    hooks.mkdir()
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
        ("protocol.https.allow", "always"),
        ("http.followRedirects", "false"),
        ("fetch.fsckObjects", "true"),
        ("transfer.fsckObjects", "true"),
        ("core.autocrlf", "false"),
    ]
    if token:
        auth = base64.b64encode((username + ":" + token).encode()).decode()
        values.append(
            (f"http.{origin}/{repository}.git.extraHeader", "Authorization: Basic " + auth)
        )
    env["GIT_CONFIG_COUNT"] = str(len(values))
    for index, (key, value) in enumerate(values):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value

    def git(*args, maximum=1024 * 1024, data=b""):
        return run_control(["git", *args], bare, env=env, maximum=maximum, data=data)

    git("init", "--bare", "--template=")
    # The target repository exposes the change ref, including forks. Never use a
    # webhook-supplied clone URL or send a reader token to a fork's remote.
    git(
        "fetch",
        "--no-tags",
        "--depth=1",
        "--no-recurse-submodules",
        origin + "/" + repository + ".git",
        source_ref,
    )
    actual = git("rev-parse", "--verify", "FETCH_HEAD^{commit}").decode().strip()
    if actual != expected_sha:
        raise CirraError("change head moved before the independent checkout; refresh and retry")
    commit = git("cat-file", "commit", actual)
    parents = [
        sha(line[7:].decode())
        for line in commit.split(b"\n\n", 1)[0].splitlines()
        if line.startswith(b"parent ")
    ]
    if len(parents) > 16:
        raise CirraError("observer does not support commits with more than 16 parents")
    listing = git("ls-tree", "-rlz", "--full-tree", actual, maximum=4 * 1024 * 1024)
    entries, total = [], 0
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, kind, oid, raw_size = metadata.split()
        name = path_name(raw_name.decode("utf-8"))
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise CirraError(
                "observer source must contain regular files; symlinks/submodules refused"
            )
        if any(part.lower() == ".git" for part in name.split("/")):
            raise CirraError("observer source contains a reserved Git path")
        size = int(raw_size)
        total += size
        entries.append((name, mode, sha(oid.decode()), size))
        if total > MAX_SOURCE or len(entries) > 5000:
            raise CirraError("observer source exceeds 64 MiB or 5000 files")
    # cat-file reads blobs directly; .gitattributes cannot exclude or rewrite files.
    objects = io.BytesIO(
        git(
            "cat-file",
            "--batch",
            maximum=MAX_SOURCE + 1024 * 1024,
            data="".join(oid + "\n" for _, _, oid, _ in entries).encode(),
        )
    )
    archive = directory / "source.tar"
    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as tar:
        for name, mode, oid, size in entries:
            if objects.readline() != f"{oid} blob {size}\n".encode():
                raise CirraError("Git blob response did not match the observed tree")
            content = objects.read(size)
            if len(content) != size or objects.read(1) != b"\n":
                raise CirraError("Git blob export was truncated")
            item = tarfile.TarInfo(name)
            item.size, item.mode = size, 0o755 if mode == b"100755" else 0o644
            item.uid = item.gid = 65534
            tar.addfile(item, io.BytesIO(content))
    return archive, parents
