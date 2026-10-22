"""Repository hygiene: the things that, if broken, only show up on a deploy or in a leak."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from orb_intraday_bot import __version__, cli

ROOT = Path(__file__).resolve().parents[2]


def pyproject() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def test_package_version_matches_pyproject() -> None:
    project = pyproject()["project"]
    assert isinstance(project, dict)
    assert project["version"] == __version__


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.build_parser().parse_args(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"orb-bot {__version__}"


def test_secrets_and_runtime_data_are_git_ignored() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in (".env", "/data/", ".venv/", ".mypy_cache/"):
        assert pattern in ignored
    assert "!.env.example" in ignored
    # A bare "data/" would also hide the source package src/orb_intraday_bot/data.
    assert "data/" not in ignored


def test_env_example_ships_as_a_dry_run_with_no_secrets() -> None:
    values = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    assert values["DRY_RUN"] == "true"
    assert values["LIVE_MODE"] == "paper"
    assert values["LIVE_ARMED"] == "false"
    assert "LIVE_TRADING" not in values
    for secret in ("GROWW_TOTP_TOKEN", "GROWW_TOTP_SECRET", "TELEGRAM_BOT_TOKEN"):
        assert values[secret] == ""


def _pinned_version(lock_text: str, name: str) -> tuple[int, ...]:
    prefix = f"{name}=="
    for line in lock_text.splitlines():
        if line.lower().startswith(prefix):
            return tuple(int(part) for part in line.split("==", 1)[1].split("."))
    raise AssertionError(f"{name} missing from lock")


def test_lock_files_pin_every_line_and_include_the_broker_sdk() -> None:
    for name in ("requirements.lock", "requirements-dev.lock"):
        text = (ROOT / name).read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if line.strip()]
        assert lines, name
        assert all("==" in line for line in lines), f"{name} has an unpinned line"
        assert any(line.lower().startswith("growwapi==") for line in lines)
        # PYSEC-2026-4320. growwapi 1.5.0 still declares certifi<2026.0.0;
        # lock installs use --no-deps so the patched bundle can be pinned.
        assert _pinned_version(text, "certifi") >= (2026, 10, 19)
    dev = (ROOT / "requirements-dev.lock").read_text(encoding="utf-8")
    for tool in ("pytest==", "ruff==", "mypy==", "import-linter=="):
        assert tool in dev


def test_every_dev_tool_in_pyproject_is_locked() -> None:
    project = pyproject()["project"]
    assert isinstance(project, dict)
    dev = project["optional-dependencies"]["dev"]    # type: ignore[index]
    locked = (
        (ROOT / "requirements-dev.lock").read_text(encoding="utf-8").lower().replace("_", "-")
    )
    for requirement in dev:
        name = requirement.split(">")[0].split("=")[0].strip().lower().replace("_", "-")
        assert f"{name}==" in locked, f"{name} missing from requirements-dev.lock"


def test_import_contracts_are_configured() -> None:
    tool = pyproject()["tool"]
    assert isinstance(tool, dict)
    contracts = tool["importlinter"]["contracts"]
    names = " ".join(c["name"] for c in contracts)
    assert "Layers" in names and "Strategy core" in names and "Groww SDK" in names


def test_ci_pre_commit_and_contributing_files_exist() -> None:
    for path in (
        ".github/workflows/ci.yml",
        ".github/pull_request_template.md",
        ".github/dependabot.yml",
        ".pre-commit-config.yaml",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
        "scripts/dev.ps1",
    ):
        assert (ROOT / path).is_file(), path


def test_ci_runs_every_quality_gate() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    for gate in ("ruff check", "mypy", "lint-imports", "pytest", "pip-audit"):
        assert gate in ci, gate
    assert "pip install --no-deps -r requirements-dev.lock" in ci
    for path in (
        "scripts/dev.ps1",
        "scripts/deploy/Install-OrbBot.ps1",
        ".github/workflows/cd-ec2.yml",
    ):
        assert "--no-deps -r" in (ROOT / path).read_text(encoding="utf-8"), path


def test_deploy_stops_nssm_before_pip_and_waits_out_pending_start() -> None:
    helper = (ROOT / "scripts/deploy/Nssm-OrbServices.ps1").read_text(encoding="utf-8")
    assert "sc.exe stop" in helper
    assert "WaitForExit" in helper
    assert "& nssm stop" not in helper
    install = (ROOT / "scripts/deploy/Install-OrbBot.ps1").read_text(encoding="utf-8")
    assert "Nssm-OrbServices.ps1" in install
    assert install.index("-Action Stop") < install.index("pip install --no-deps")
    cd = (ROOT / ".github/workflows/cd-ec2.yml").read_text(encoding="utf-8")
    assert cd.index("stopping services before git") < cd.index("git fetch")
    assert cd.index("-Action Stop") < cd.index("pip install --no-deps")
    assert "-Action Install" in cd
    assert "powershell.exe -NoProfile" not in cd
