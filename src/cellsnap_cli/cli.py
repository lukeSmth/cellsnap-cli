"""Publish local .ipynb artifacts to a single-commit parentless git branch.

Notebooks are paired to their tracked .py sources, stamped with a permalink to the source commit,
and force-pushed to `<prefix>/<source-branch>`. Each push builds a fresh root commit and replaces
the ref, so the branch is a snapshot of current state.

Every run reconciles local notebooks against the published branch, and each artifact resolves to one
outcome: stage, carry (no-op), delete, or ignore. Notebooks publish as-is. Executing them is the user's job.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import NoReturn

import click
import jupytext
import nbformat

from cellsnap_cli.git import GitError, git, git_ok, git_out, git_paths, remote_to_web
from cellsnap_cli.pairing import cell_inputs, paired_source, read_jupytext_config

__all__ = ["main"]

# Stable id for the injected banner cell, pinned by banner().
BANNER_ID = "cellsnap-cli-banner"


def log(msg: str) -> None:
    """Progress line. stderr, so stdout carries only the result."""
    click.echo(msg, err=True)


def abort(msg: str) -> NoReturn:
    raise SystemExit(f"cellsnap-cli: {msg}")


## --- Source Repository Context --- ##


class Context:
    """Repo facts resolved once and reused for every artifact in a run."""

    def __init__(self, remote_name: str, prefix: str) -> None:
        here = Path.cwd()
        if not git_ok("rev-parse", "--git-dir", cwd=here):
            abort("not inside a git repository")

        self.root = Path(git_out("rev-parse", "--show-toplevel", cwd=here))
        self.branch = git_out("rev-parse", "--abbrev-ref", "HEAD", cwd=self.root)
        if self.branch == "HEAD":
            abort("detached HEAD -- check out a branch first")
        if self.branch == prefix or self.branch.startswith(prefix + "/"):
            abort(f"refusing to run from an artifact branch ({self.branch})")

        self.sha = git_out("rev-parse", "HEAD", cwd=self.root)
        self.remote = git_out("remote", "get-url", remote_name, cwd=self.root)

        # Explicit weburl wins
        cfg_web = git("config", "cellsnap-cli.weburl", cwd=self.root, check=False).stdout.strip()
        self.web = (cfg_web or remote_to_web(self.remote) or "").rstrip("/") or None
        if not self.web:
            log(
                f"warn:   no web URL derivable from {self.remote!r}; source and browse links omitted. "
                "Set one with:\n        git config cellsnap-cli.weburl <base-url>"
            )

        # Artifact commits are machine-made: fall back to a house identity rather than failing when
        # git has none configured.
        self.user = git("config", "user.name", cwd=self.root, check=False).stdout.strip() or "cellsnap-cli"
        self.email = git("config", "user.email", cwd=self.root, check=False).stdout.strip() or "cellsnap-cli@localhost"

    def blob_url(self, py: str, sha: str) -> str | None:
        """Permalink to the source .py at the commit that last changed it."""
        return f"{self.web}/blob/{sha}/{py}" if self.web else None

    def commit_url(self, sha: str) -> str | None:
        return f"{self.web}/commit/{sha}" if self.web else None

    def tree_url(self, artifact_branch: str) -> str | None:
        """Browse URL for the artifact branch."""
        return f"{self.web}/tree/{artifact_branch}" if self.web else None


## --- Artifact Staging --- ##


def banner(ctx: Context, py: str, sha: str, nb) -> nbformat.NotebookNode:
    """Provenance cell prepended to a published notebook."""
    label = f"`{ctx.branch}` @ `{sha[:8]}` -- `{Path(py).name}`"
    url = ctx.blob_url(py, sha)
    cell = nbformat.v4.new_markdown_cell(f"[{label}]({url})" if url else label)

    # Machine-readable twin of the link, so the manifest reports each artifact's own generating
    # commit instead of re-deriving it from HEAD.
    cell["metadata"] = {"cellsnap_cli": {"source": py, "commit": sha}}

    # new_markdown_cell() assigns a RANDOM cell id. Left alone, every run emits a different notebook,
    # nothing compares equal, and every run pushes. Pin it -- but only where ids are valid, since
    # they arrived in nbformat 4.5.
    if int(nb.get("nbformat_minor", 0)) >= 5:
        cell["id"] = BANNER_ID
    else:
        cell.pop("id", None)
    return cell


def banner_provenance(nb_path: Path) -> dict:
    """Source and commit recorded in a published artifact's banner.

    Each artifact carries its own provenance, so one with no local copy can still name its source.
    """
    try:
        cells = json.loads(nb_path.read_text(encoding="utf-8"))["cells"]
        return (cells[0].get("metadata") or {}).get("cellsnap_cli") or {}
    except Exception:
        return {}


def artifact_paths(ctx: Context, work: Path, glob: str) -> list[str]:
    """Local notebooks unioned with already-published ones. Including remote notebooks handles cases
    where the source file exists locally but the rich notebook artifact does not (fresh repo pulls for example).
    """
    rels = set()

    for root in (ctx.root, work):
        if not root.is_dir():
            continue
        for p in root.glob(glob):
            if p.suffix != ".ipynb" or not p.is_file():
                continue
            rel = p.relative_to(root)
            # skip dot-directories
            if any(part.startswith(".") for part in rel.parts):
                continue
            rels.add(rel.as_posix())

    return sorted(rels)


def reconcile(ctx: Context, config, work: Path, glob: str) -> tuple[list[str], list[str], list[str]]:
    """Bring the fetched artifact tree in line with the repo. Returns (staged, carried, removed).

    Carrying is a deliberate no-op.
    """
    tracked = git_paths("ls-files", "-z", cwd=ctx.root)

    # Check if tracked source file is dirty (changes not yet committed)
    dirty = git_paths("diff", "--name-only", "-z", "HEAD", cwd=ctx.root)

    staged: list[str] = []
    carried: list[str] = []
    removed: list[str] = []

    for rel in artifact_paths(ctx, work, glob):
        local = ctx.root / rel
        published = work / rel
        nb = None

        if local.is_file():
            try:
                nb = nbformat.read(local, as_version=4)
            except Exception as exc:
                log(f"skip:   {rel} (unreadable notebook: {exc})")
                if published.is_file():
                    carried.append(rel)
                continue
            py = paired_source(nb, local, ctx.root, config)
        else:
            py = None

        if py is None and published.is_file():
            # Published once already, so the pairing was settled earlier and the artifact records
            # it. Trusting that rather than re-deriving.
            py = banner_provenance(published).get("source") or Path(rel).with_suffix(".py").as_posix()

        if py is None:
            log(f"ignore: {rel} (no jupytext-paired .py source)")
            continue

        if py not in tracked:
            if published.is_file():
                published.unlink()
                removed.append(rel)
                log(f"delete: {rel} ({py} no longer tracked)")
            else:
                log(f"ignore: {rel} ({py} not tracked)")
            continue

        if nb is None:
            carried.append(rel)
            log(f"carry:  {rel} (no local copy)")
            continue

        if py in dirty:
            carried.append(rel)
            log(f"dirty:  {rel} ({py} has uncommitted changes; not republished)")
            continue

        try:
            py_nb = jupytext.read(ctx.root / py)
        except Exception as exc:
            log(f"skip:   {rel} (jupytext could not read {py}: {exc})")
            if published.is_file():
                carried.append(rel)
            continue

        # Compare inputs to detect sync drift
        if cell_inputs(py_nb) != cell_inputs(nb):
            log(f"drift:  {rel} (cells differ from {py}; re-sync with jupytext)")
            if published.is_file():
                carried.append(rel)
            continue

        # The commit that last touched this source, not HEAD
        sha = git_out("log", "-1", "--format=%H", "--", py, cwd=ctx.root) or ctx.sha
        nb.cells.insert(0, banner(ctx, py, sha, nb))

        # Straight over the fetched copy; publish() lets git decide whether the bytes really moved.
        published.parent.mkdir(parents=True, exist_ok=True)
        nbformat.write(nb, published)

        staged.append(rel)
        log(f"stage:  {rel}")

    return staged, carried, removed


## --- Branch Manifest --- ##


def write_readme(ctx: Context, work: Path, artifact_branch: str) -> None:
    """Write manifest README at the branch root."""
    rows = []
    for nb_path in sorted(work.rglob("*.ipynb")):
        rel = nb_path.relative_to(work)
        if ".git" in rel.parts:
            continue
        rel = rel.as_posix()

        prov = banner_provenance(nb_path)
        py = prov.get("source") or Path(rel).with_suffix(".py").as_posix()
        sha = prov.get("commit")
        if not sha:  # no banner to read: fall back to git
            sha = git_out("log", "-1", "--format=%H", "--", py, cwd=ctx.root) or ctx.sha

        source_url, commit_url = ctx.blob_url(py, sha), ctx.commit_url(sha)
        source_cell = f"[`{py}`]({source_url})" if source_url else f"`{py}`"
        commit_cell = f"[`{sha[:8]}`]({commit_url})" if commit_url else f"`{sha[:8]}`"
        rows.append(f"| [`{rel}`]({rel}) | {source_cell} | {commit_cell} |")

    branch = f"[`{ctx.branch}`]({ctx.web}/tree/{ctx.branch})" if ctx.web else f"`{ctx.branch}`"
    lines = [
        f"# `{artifact_branch}`",
        "",
        f"Rendered notebooks built from {branch}. Each row links to the source at the commit that "
        "generated that notebook.",
        "",
        "| Notebook | Source | Commit |",
        "| --- | --- | --- |",
        *rows,
        "",
        "---",
        "",
        "Generated by `cellsnap-cli`. This branch is force-pushed and keeps no history; do not commit to it directly.",
        "",
    ]
    (work / "README.md").write_text("\n".join(lines), encoding="utf-8")


## --- Publish --- ##


def fetch_prior(work: Path, remote: str, artifact_branch: str) -> bool:
    """Init the scratch repo and lay the published tree into it. False = no such branch yet."""
    git("init", "-q", cwd=work)
    if not git_ok("fetch", "-q", "--depth", "1", remote, artifact_branch, cwd=work):
        return False
    # `checkout FETCH_HEAD -- .` fills index and worktree without moving HEAD,
    # forcing the scratch repo to keep zero commits.
    return git_ok("checkout", "-q", "FETCH_HEAD", "--", ".", cwd=work)


def publish(ctx: Context, work: Path, artifact_branch: str, message: str, have_prior: bool, dry_run: bool) -> bool:
    """Commit the reconciled tree and force-push it. False = nothing needed pushing.

    Diffed against the fetched tip, so an unchanged run is a no-op rather than an identical commit
    under a fresh SHA.
    """
    git("add", "-A", cwd=work)

    if have_prior and git_ok("diff", "--cached", "--quiet", "FETCH_HEAD", cwd=work):
        log("no changes; skipping push")
        return False

    if not have_prior and not git_out("diff", "--cached", "--name-only", cwd=work):
        log("nothing publishable; skipping push")
        return False

    if dry_run:
        base = ["FETCH_HEAD"] if have_prior else []
        log("dry run -- would commit and force-push:")
        log(git_out("diff", "--cached", "--stat", *base, cwd=work))
        return False

    # No prior commits in the scratch repo, so this is a root commit and the force-push leaves the
    # remote branch with exactly one.
    identity = ["-c", f"user.name={ctx.user}", "-c", f"user.email={ctx.email}"]
    git(*identity, "commit", "-q", "-m", message, cwd=work)

    # Leased against the tip we fetched, or against absence when the branch is new -- an empty
    # expectation requires the ref to not exist. Either way a concurrent push fails loudly rather
    # than being silently erased.
    tip = git_out("rev-parse", "FETCH_HEAD", cwd=work) if have_prior else ""
    lease = f"--force-with-lease={artifact_branch}:{tip}"

    if not git_ok("push", "-q", lease, ctx.remote, f"HEAD:{artifact_branch}", cwd=work):
        abort(
            f"push rejected -- {artifact_branch} changed since it was fetched (concurrent run?); "
            "re-run to pick up the new tip"
        )
    return True


## --- CLI --- ##


@click.command(help=__doc__.strip().splitlines()[0])
@click.option(
    "-b",
    "--prefix",
    default="artifacts",
    envvar="ARTIFACT_PREFIX",
    show_default=True,
    show_envvar=True,
    help="artifact branch prefix; output goes to <prefix>/<source-branch>",
)
@click.option(
    "-g",
    "--glob",
    default="**/*.ipynb",
    envvar="GLOB",
    show_default=True,
    show_envvar=True,
    help="glob matching notebooks, relative to the repo root; applies to local and "
    "already-published artifacts alike. Dot-directories (.git, .ipynb_checkpoints, ...) "
    "are always skipped",
)
@click.option("-r", "--remote", default="origin", show_default=True, help="git remote to publish through")
@click.option("-n", "--dry-run", is_flag=True, help="stage and diff, but do not commit or push")
def main(prefix: str, glob: str, remote: str, dry_run: bool) -> None:
    try:
        run(prefix, glob, remote, dry_run)
    except GitError as exc:
        abort(str(exc))  # git's own stderr, already in the message


def run(prefix: str, glob: str, remote: str, dry_run: bool) -> None:
    if Path(glob).is_absolute():  # Path.glob raises NotImplementedError on these
        abort(f"--glob must be relative to the repo root: {glob!r}")

    ctx = Context(remote, prefix)

    # One branch per source branch
    artifact_branch = f"{prefix}/{ctx.branch}"

    with tempfile.TemporaryDirectory() as work_dir:
        work = Path(work_dir)

        have_prior = fetch_prior(work, ctx.remote, artifact_branch)

        config = read_jupytext_config(ctx.root)
        staged, carried, removed = reconcile(ctx, config, work, glob)
        if not (staged or carried or removed):
            abort(f"nothing to publish: no notebooks matching {glob!r} resolve to a tracked .py source")

        write_readme(ctx, work, artifact_branch)

        message = f"artifacts: {ctx.branch} @ {ctx.sha[:8]} (+{len(staged)} -{len(removed)})"
        pushed = publish(ctx, work, artifact_branch, message, have_prior, dry_run)

    if pushed:
        click.echo(
            f"pushed to {artifact_branch} -- {len(staged)} staged, {len(carried)} carried, {len(removed)} removed"
        )

    url = ctx.tree_url(artifact_branch)
    if url:
        click.echo(url)
