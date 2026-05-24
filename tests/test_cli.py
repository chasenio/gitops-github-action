"""Integration tests: invoke main.py as a subprocess (mirrors GitHub Action usage)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "main.py"
FIXTURES = ROOT / "tests" / "fixtures"


def yq_get(path: Path, expr: str) -> str:
    return subprocess.run(
        ["yq", "e", expr, str(path)],
        check=True, text=True, capture_output=True,
    ).stdout.strip()


def run_cli(*args: str, cwd: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd, text=True, capture_output=True, env=env,
    )


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    for f in FIXTURES.iterdir():
        shutil.copy2(f, tmp_path / f.name)
    return tmp_path


def test_cli_update_kustomize(workdir: Path):
    r = run_cli(
        "update",
        "--files", "kustomize-single.yaml",
        "--image", "ghcr.io/chasenio/api",
        "--tag", "CLI_API",
        cwd=workdir,
    )
    assert r.returncode == 0, r.stderr
    assert yq_get(workdir / "kustomize-single.yaml", ".images[0].newTag") == "CLI_API"


def test_cli_update_helm_with_outputs(workdir: Path, tmp_path: Path):
    out_file = tmp_path / "gh_output"
    out_file.touch()
    r = run_cli(
        "update",
        "--files", "helm-single.yaml",
        "--image", "ghcr.io/chasenio/svc",
        "--tag", "CLI_SVC",
        "--mode", "helm",
        cwd=workdir,
        env_extra={"GITHUB_OUTPUT": str(out_file)},
    )
    assert r.returncode == 0, r.stderr
    assert yq_get(workdir / "helm-single.yaml", ".image.tag") == "CLI_SVC"
    out = out_file.read_text()
    assert "changed=true" in out
    assert "helm-single.yaml" in out


def test_cli_no_files_matched_exits_nonzero(workdir: Path):
    r = run_cli(
        "update",
        "--files", "nope-*.yaml",
        "--image", "x",
        "--tag", "y",
        cwd=workdir,
    )
    assert r.returncode != 0
    assert "no files matched" in (r.stderr + r.stdout)


def test_cli_help(workdir: Path):
    r = run_cli("--help", cwd=workdir)
    assert r.returncode == 0
    assert "update" in r.stdout
    assert "commit" in r.stdout


def test_cli_commit_subcommand_help(workdir: Path):
    r = run_cli("commit", "--help", cwd=workdir)
    assert r.returncode == 0
    assert "--git-user" in r.stdout
