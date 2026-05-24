"""Tests for main.py — call functions directly instead of spawning subprocess."""
from __future__ import annotations

import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT))

import main  # noqa: E402


def yq_get(path: Path, expr: str) -> str:
    return subprocess.run(
        ["yq", "e", expr, str(path)],
        check=True, text=True, capture_output=True,
    ).stdout.strip()


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for f in FIXTURES.iterdir():
        shutil.copy2(f, tmp_path / f.name)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def update_ns(**overrides) -> Namespace:
    defaults = dict(
        files="",
        image="",
        tag="",
        mode="auto",
        new_name="",
        helm_tag_path=".image.tag",
        helm_repository_path=".image.repository",
        update_repository=False,
    )
    defaults.update(overrides)
    return Namespace(**defaults)


# ---------------------------------------------------------------------------
# update_kustomize
# ---------------------------------------------------------------------------
def test_update_kustomize_single(workdir: Path):
    f = workdir / "kustomize-single.yaml"
    changed = main.update_kustomize(
        str(f),
        image="ghcr.io/chasenio/api",
        tag="NEW_API",
        new_name=None,
        update_repository=False,
    )
    assert changed
    assert yq_get(f, ".images[0].newTag") == "NEW_API"


def test_update_kustomize_only_match_changes(workdir: Path):
    f = workdir / "kustomize-multi.yaml"
    main.update_kustomize(
        str(f),
        image="ghcr.io/chasenio/web",
        tag="NEW_WEB",
        new_name=None,
        update_repository=False,
    )
    assert yq_get(f, ".images[0].newTag") == "OLD_API"
    assert yq_get(f, ".images[1].newTag") == "NEW_WEB"
    assert yq_get(f, ".images[2].newTag") == "OLD_WORKER"


def test_update_kustomize_new_name(workdir: Path):
    f = workdir / "kustomize-multi.yaml"
    main.update_kustomize(
        str(f),
        image="ghcr.io/chasenio/worker",
        tag="NEW_WORKER",
        new_name="ghcr.io/chasenio/worker-fork",
        update_repository=False,
    )
    assert yq_get(f, ".images[2].newTag") == "NEW_WORKER"
    assert yq_get(f, ".images[2].newName") == "ghcr.io/chasenio/worker-fork"
    assert yq_get(f, ".images[2].name") == "ghcr.io/chasenio/worker"


def test_update_kustomize_no_match_returns_false(workdir: Path):
    f = workdir / "kustomize-single.yaml"
    changed = main.update_kustomize(
        str(f),
        image="ghcr.io/nope",
        tag="X",
        new_name=None,
        update_repository=False,
    )
    assert changed is False
    assert yq_get(f, ".images[0].newTag") == "OLD_API"


# ---------------------------------------------------------------------------
# update_helm
# ---------------------------------------------------------------------------
def test_update_helm_default_path(workdir: Path):
    f = workdir / "helm-single.yaml"
    main.update_helm(
        str(f),
        image="ghcr.io/chasenio/svc",
        tag="NEW_SVC",
        new_name=None,
        update_repository=False,
        tag_path=".image.tag",
        repo_path=".image.repository",
    )
    assert yq_get(f, ".image.tag") == "NEW_SVC"
    assert "Overrides the image tag" in f.read_text()


def test_update_helm_nested_custom_path(workdir: Path):
    f = workdir / "helm-nested.yaml"
    main.update_helm(
        str(f),
        image="ghcr.io/chasenio/sidecar",
        tag="NEW_SIDECAR",
        new_name=None,
        update_repository=False,
        tag_path=".sidecar.image.tag",
        repo_path=".sidecar.image.repository",
    )
    assert yq_get(f, ".sidecar.image.tag") == "NEW_SIDECAR"
    assert yq_get(f, ".image.tag") == "OLD_APP"


def test_update_helm_update_repository(workdir: Path):
    f = workdir / "helm-single.yaml"
    main.update_helm(
        str(f),
        image="ghcr.io/chasenio/svc",
        tag="v2",
        new_name="ghcr.io/chasenio/svc-staging",
        update_repository=True,
        tag_path=".image.tag",
        repo_path=".image.repository",
    )
    assert yq_get(f, ".image.tag") == "v2"
    assert yq_get(f, ".image.repository") == "ghcr.io/chasenio/svc-staging"


# ---------------------------------------------------------------------------
# detect_mode
# ---------------------------------------------------------------------------
def test_detect_mode_forced(workdir: Path):
    assert main.detect_mode(str(workdir / "helm-single.yaml"), "kustomize") == "kustomize"


def test_detect_mode_auto_kustomize(workdir: Path):
    assert main.detect_mode(str(workdir / "kustomize-single.yaml"), "auto") == "kustomize"


def test_detect_mode_auto_helm(workdir: Path):
    assert main.detect_mode(str(workdir / "helm-single.yaml"), "auto") == "helm"


# ---------------------------------------------------------------------------
# expand_files
# ---------------------------------------------------------------------------
def test_expand_files_glob(workdir: Path):
    files = main.expand_files("kustomize-*.yaml")
    assert sorted(files) == ["kustomize-multi.yaml", "kustomize-single.yaml"]


def test_expand_files_newline_comma(workdir: Path):
    files = main.expand_files("kustomize-single.yaml\nhelm-single.yaml,helm-nested.yaml")
    assert set(files) == {"kustomize-single.yaml", "helm-single.yaml", "helm-nested.yaml"}


def test_expand_files_empty_on_no_match(workdir: Path):
    assert main.expand_files("does-not-exist-*.yaml") == []


# ---------------------------------------------------------------------------
# run_update + write_outputs
# ---------------------------------------------------------------------------
def test_run_update_glob_and_outputs(workdir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    out_file = tmp_path / "gh_output"
    out_file.touch()
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))

    result = main.run_update(update_ns(
        files="kustomize-*.yaml",
        image="ghcr.io/chasenio/api",
        tag="GLOB_API",
    ))
    main.write_outputs(result)

    assert result.changed
    assert set(result.updated) == {"kustomize-single.yaml", "kustomize-multi.yaml"}
    assert yq_get(workdir / "kustomize-multi.yaml", ".images[1].newTag") == "OLD_WEB"

    out = out_file.read_text()
    assert "changed=true" in out
    assert "kustomize-single.yaml" in out
    assert "kustomize-multi.yaml" in out


def test_run_update_no_files_exits(workdir: Path):
    with pytest.raises(SystemExit):
        main.run_update(update_ns(
            files="does-not-exist-*.yaml",
            image="x",
            tag="y",
        ))


# ---------------------------------------------------------------------------
# parse_git_user
# ---------------------------------------------------------------------------
def test_parse_git_user_valid():
    assert main.parse_git_user("alice <a@x.com>") == ("alice", "a@x.com")
    assert main.parse_git_user("Bob Smith <bob@x.com>") == ("Bob Smith", "bob@x.com")


def test_parse_git_user_invalid():
    with pytest.raises(ValueError):
        main.parse_git_user("no-angle-brackets@x.com")


# ---------------------------------------------------------------------------
# truncate_source_message
# ---------------------------------------------------------------------------
def test_truncate_source_message_empty():
    assert main.truncate_source_message("") == ""
    assert main.truncate_source_message("   \n  \n") == ""


def test_truncate_source_message_short_untouched():
    s = "fix: small thing\n\nbody line"
    assert main.truncate_source_message(s) == s


def test_truncate_source_message_line_cap():
    src = "\n".join(f"line{i}" for i in range(50))
    out = main.truncate_source_message(src, max_lines=5, max_chars=10000)
    assert out.splitlines()[:5] == [f"line{i}" for i in range(5)]
    assert out.endswith("…[truncated]")


def test_truncate_source_message_char_cap():
    src = "x" * 5000
    out = main.truncate_source_message(src, max_lines=1000, max_chars=100)
    assert len(out) <= 100 + len("\n…[truncated]")
    assert out.endswith("…[truncated]")
