#!/usr/bin/env python3
"""bump.py — update container image tags in Helm values / Kustomize files.

Each subcommand is a self-contained function so it can be wired up step-by-step
inside a composite GitHub Action (or called from a shell).

External dependency: `yq` (mikefarah/yq v4) on PATH.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# yq helpers
# ---------------------------------------------------------------------------
def yq(expr: str, path: str, *, inplace: bool = False, env: dict | None = None) -> str:
    """Run `yq e [-i] expr path` and return stdout (stripped)."""
    cmd = ["yq", "e"]
    if inplace:
        cmd.append("-i")
    cmd += [expr, path]
    res = subprocess.run(
        cmd,
        check=True,
        text=True,
        capture_output=True,
        env={**os.environ, **(env or {})},
    )
    return res.stdout.strip()


def sha1(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# file selection
# ---------------------------------------------------------------------------
def expand_files(spec: str) -> list[str]:
    """Expand newline / comma separated path/glob spec into a unique file list."""
    parts = [p.strip() for chunk in spec.splitlines() for p in chunk.split(",")]
    seen: dict[str, None] = {}
    for pattern in parts:
        if not pattern:
            continue
        matched = glob.glob(pattern, recursive=True)
        if not matched:
            print(f"::warning::no file matched pattern: {pattern}", file=sys.stderr)
            continue
        for m in matched:
            if os.path.isfile(m):
                seen[m] = None
    return list(seen)


# ---------------------------------------------------------------------------
# mode detection
# ---------------------------------------------------------------------------
def detect_mode(path: str, forced: str) -> str:
    if forced != "auto":
        return forced
    base = os.path.basename(path)
    if base in ("kustomization.yaml", "kustomization.yml"):
        return "kustomize"
    try:
        kind = yq(".kind", path)
        if kind.lower() == "kustomization":
            return "kustomize"
    except subprocess.CalledProcessError:
        pass
    try:
        has_images = yq('has("images") and (.images | type) == "!!seq"', path)
        if has_images == "true":
            return "kustomize"
    except subprocess.CalledProcessError:
        pass
    return "helm"


# ---------------------------------------------------------------------------
# update functions
# ---------------------------------------------------------------------------
def update_kustomize(
    path: str,
    *,
    image: str,
    tag: str,
    new_name: str | None,
    update_repository: bool,
) -> bool:
    """Return True if file was actually changed."""
    idx_str = yq(
        '.images // [] | to_entries | '
        'map(select(.value.name == env(IMG) or .value.newName == env(IMG))) '
        '| .[0].key',
        path,
        env={"IMG": image},
    )
    if not idx_str or idx_str == "null":
        print(
            f"::warning::{path}: no kustomize images entry matches '{image}', skipping",
            file=sys.stderr,
        )
        return False

    yq(f".images[{idx_str}].newTag = strenv(TAG)", path, inplace=True, env={"TAG": tag})

    resolved_new_name: str | None = None
    if update_repository:
        resolved_new_name = new_name or image
    elif new_name:
        resolved_new_name = new_name
    if resolved_new_name:
        yq(
            f".images[{idx_str}].newName = strenv(NEWNAME)",
            path,
            inplace=True,
            env={"NEWNAME": resolved_new_name},
        )
    return True


def update_helm(
    path: str,
    *,
    image: str,
    tag: str,
    new_name: str | None,
    update_repository: bool,
    tag_path: str,
    repo_path: str,
) -> bool:
    yq(f"{tag_path} = strenv(TAG)", path, inplace=True, env={"TAG": tag})
    if update_repository:
        repo = new_name or image
        yq(f"{repo_path} = strenv(REPO)", path, inplace=True, env={"REPO": repo})
    return True


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
@dataclass
class UpdateResult:
    updated: list[str]

    @property
    def changed(self) -> bool:
        return bool(self.updated)


def run_update(args: argparse.Namespace) -> UpdateResult:
    files = expand_files(args.files)
    if not files:
        print("::error::no files matched", file=sys.stderr)
        sys.exit(1)

    print("Matched files:")
    for f in files:
        print(f"  {f}")

    updated: list[str] = []
    for f in files:
        mode = detect_mode(f, args.mode)
        print(f"::group::Updating {f} (mode={mode})")
        before = sha1(f)
        try:
            if mode == "kustomize":
                ok = update_kustomize(
                    f,
                    image=args.image,
                    tag=args.tag,
                    new_name=args.new_name or None,
                    update_repository=args.update_repository,
                )
            else:
                ok = update_helm(
                    f,
                    image=args.image,
                    tag=args.tag,
                    new_name=args.new_name or None,
                    update_repository=args.update_repository,
                    tag_path=args.helm_tag_path,
                    repo_path=args.helm_repository_path,
                )
        except subprocess.CalledProcessError as e:
            print(f"::error::yq failed for {f}: {e.stderr or e}", file=sys.stderr)
            print("::endgroup::")
            sys.exit(1)
        after = sha1(f)
        if ok and before != after:
            print("changed.")
            updated.append(f)
        else:
            print("no diff.")
        print("::endgroup::")

    return UpdateResult(updated=updated)


def write_outputs(result: UpdateResult) -> None:
    out_path = os.environ.get("GITHUB_OUTPUT")
    if not out_path:
        return
    with open(out_path, "a") as f:
        f.write("updated-files<<__EOF__\n")
        for u in result.updated:
            f.write(u + "\n")
        f.write("__EOF__\n")
        f.write(f"changed={'true' if result.changed else 'false'}\n")


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------
_USER_RE = re.compile(r"^(?P<name>.+?)\s*<(?P<email>[^>]+)>\s*$")


def parse_git_user(s: str) -> tuple[str, str]:
    m = _USER_RE.match(s)
    if not m:
        raise ValueError(f"git-user must be in 'Name <email>' format, got: {s!r}")
    return m.group("name").strip(), m.group("email").strip()


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], check=check, text=True, capture_output=True)


# Hard caps for embedded source commit message so we don't write huge commit
# bodies into the gitops repo. Keep generous but bounded.
SOURCE_MSG_MAX_LINES = 20
SOURCE_MSG_MAX_CHARS = 2000


def truncate_source_message(
    text: str,
    *,
    max_lines: int = SOURCE_MSG_MAX_LINES,
    max_chars: int = SOURCE_MSG_MAX_CHARS,
) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    truncated = False
    lines = s.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    s = "\n".join(lines)
    if len(s) > max_chars:
        s = s[:max_chars].rstrip()
        truncated = True
    if truncated:
        s += "\n…[truncated]"
    return s


def run_commit(args: argparse.Namespace) -> None:
    name, email = parse_git_user(args.git_user)
    git("config", "user.name", name)
    git("config", "user.email", email)

    if args.branch:
        git("fetch", "origin", args.branch, check=False)
        git("checkout", "-B", args.branch)

    files = [f for f in (args.files or "").splitlines() if f.strip()]
    if not files:
        print("nothing to commit (no updated-files); skipping.")
        return
    for f in files:
        git("add", f.strip())

    staged = git("diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        print("nothing staged; skipping commit.")
        return

    msg = args.message or f"ci(gitops): bump {args.image or ''} to {args.tag or ''}".strip()
    source = truncate_source_message(args.source_message or "")
    if source:
        msg = f"{msg}\n\nSource commit:\n{source}"
    git("commit", "-m", msg)

    if args.branch:
        git("push", "origin", f"HEAD:{args.branch}")
    else:
        git("push")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bump",
        description="Update container image tags in Helm values / Kustomize files.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    upd = sub.add_parser("update", help="Update one or more YAML files in place.")
    upd.add_argument("--files", required=True,
                     help="Newline/comma separated path(s) or glob(s).")
    upd.add_argument("--image", required=True,
                     help="Image to match (kustomize images[].name) or reference image.")
    upd.add_argument("--tag", required=True, help="New image tag.")
    upd.add_argument("--mode", choices=["auto", "helm", "kustomize"], default="auto")
    upd.add_argument("--new-name", default="",
                     help="Optional new image name (kustomize newName / helm repository).")
    upd.add_argument("--helm-tag-path", default=".image.tag",
                     help="yq path to the tag field in Helm values.")
    upd.add_argument("--helm-repository-path", default=".image.repository",
                     help="yq path to the repository field in Helm values.")
    upd.add_argument("--update-repository", action="store_true",
                     help="Also update repository/newName.")

    com = sub.add_parser("commit", help="Stage listed files, commit and push.")
    com.add_argument("--files", default="",
                     help="Newline-separated files to stage (typically updated-files output).")
    com.add_argument("--message", default="", help="Commit message.")
    com.add_argument("--source-message", default="",
                     help="Original commit message from the source repo; appended as body.")
    com.add_argument("--image", default="", help="Used when auto-generating commit message.")
    com.add_argument("--tag", default="", help="Used when auto-generating commit message.")
    com.add_argument("--branch", default="", help="Branch to push to. Empty = current.")
    com.add_argument(
        "--git-user",
        default="github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>",
        help="Git author in 'Name <email>' form.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    if not shutil.which("yq"):
        print("::error::yq is not installed or not on PATH", file=sys.stderr)
        return 1

    args = build_parser().parse_args(argv)
    if args.cmd == "update":
        result = run_update(args)
        write_outputs(result)
        return 0
    if args.cmd == "commit":
        run_commit(args)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
