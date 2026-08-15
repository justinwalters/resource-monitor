import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
INSTALLER = ROOT / "deploy" / "install-service.sh"
PLIST = ROOT / "deploy" / "com.resource-monitor.plist"


def run_installer(*args):
    return subprocess.run(
        [str(INSTALLER), "--python", str(ROOT / ".venv" / "bin" / "python"), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={**os.environ, "HOME": "/tmp/resource-monitor-test-home"},
    )


def test_committed_plist_has_external_token_path_only():
    content = PLIST.read_text()
    assert "RM_API_TOKEN_FILE" in content
    assert "__RM_TOKEN_FILE__" in content
    assert "RM_API_TOKEN" not in content.replace("RM_API_TOKEN_FILE", "")


def test_installer_requires_a_token_source():
    result = run_installer("--dry-run")
    assert result.returncode != 0
    assert "token source is required" in result.stderr


def test_dry_run_never_creates_secret_or_embeds_token():
    result = run_installer("--generate-token", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "api-token" in result.stdout
    assert "resource-monitor-test-home" in result.stdout
    assert "RM_API_TOKEN=" not in result.stdout
