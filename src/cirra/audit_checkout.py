"""Local checkouts for audit agents: a hardened bare cache per repository and one
detached worktree per attempt.

As with observer sources, no repository-supplied hooks, filters, credential
helpers or submodules run, and the reader token is only ever sent to the
configured target repository, never to a fork's remote.
"""

import base64
import os
from pathlib import Path

from cirra.domain import CirraError, digest, sha
from cirra.observer_process import control_environment, run_control

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


def remove_checkout(state_dir, provider, repository, destination):
    cache = Path(state_dir).resolve() / "audit-repos" / digest([provider, repository]) / "git"
    if Path(destination).exists() and (cache / "HEAD").exists():
        run_control(
            ["git", "worktree", "remove", "--force", str(Path(destination).resolve())],
            cache,
            env=_environment(cache, "", "", None, ()),
        )
