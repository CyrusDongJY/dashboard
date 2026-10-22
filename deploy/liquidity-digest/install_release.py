"""Install one isolated release on the user's server; never modify old cron jobs."""
import argparse
import ast
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from zoneinfo import ZoneInfo


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def private_json(path, value):
    with open(path, "x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    path.chmod(0o600)


def provision_config(home):
    dest = home / ".config/liquidity_digest/config.json"
    if dest.exists():
        if dest.stat().st_mode & 0o077:
            raise ValueError("Existing digest configuration is not private")
        return "existing"
    source = home / "market_dashboard/market_config.py"
    required = {"FRED_API_KEY", "SUPABASE_URL", "SUPABASE_KEY",
                "SENDER_EMAIL", "RECEIVER_EMAIL", "APP_PASSWORD"}
    values = {"SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": 465}
    for node in ast.parse(source.read_text()).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in required | {"SMTP_HOST", "SMTP_PORT"}:
                values[target.id] = ast.literal_eval(node.value)
    if any(not isinstance(values.get(key), str) or not values[key] for key in required):
        raise ValueError("Existing configuration does not provide all required literal fields")
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_json(dest, values)
    return "copied_server_side"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--release", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    home = Path.home()
    if home != Path("/home/winters_dong426"):
        raise ValueError("Service templates are scoped to the configured deployment user")
    if not args.release.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Invalid release name")
    now = datetime.now(ZoneInfo("America/New_York"))
    if (9, 15) <= (now.hour, now.minute) < (9, 25):
        raise ValueError("Do not change releases during the freeze/send window")
    if sha(args.archive) != args.sha256:
        raise ValueError("Archive integrity mismatch")
    root = home / "liquidity_digest_app"
    release = root / "releases" / args.release
    release.mkdir(parents=True, exist_ok=False)
    with tarfile.open(args.archive) as bundle:
        for member in bundle.getmembers():
            if (member.name.startswith("/") or ".." in Path(member.name).parts
                    or not (member.isdir() or member.isfile())):
                raise ValueError("Unexpected archive entry")
        bundle.extractall(release)
    manifest = json.loads((release / "release_manifest.json").read_text())
    for name, digest in manifest["files"].items():
        if sha(release / name) != digest:
            raise ValueError("Release file integrity mismatch")

    backup = home / "deploy_backups" / ("liquidity_digest_" + args.release)
    backup.mkdir(parents=True, exist_ok=False)
    cron = run(["crontab", "-l"], capture_output=True).stdout
    (backup / "crontab.before").write_bytes(cron)
    shutil.copy2(args.archive, backup / "release.tgz")
    current = root / "current"
    previous = str(current.resolve()) if current.exists() else None
    units = sorted((release / "deploy/liquidity-digest").glob("*.service"))
    units += sorted((release / "deploy/liquidity-digest").glob("*.timer"))
    for unit in units:
        existing = Path("/etc/systemd/system") / unit.name
        if existing.exists():
            shutil.copy2(existing, backup / unit.name)
    private_json(backup / "before.json", {
        "previous_release": previous, "cron_sha256": hashlib.sha256(cron).hexdigest(),
        "archive_sha256": args.sha256, "release": str(release),
    })
    print(json.dumps({"phase": "backup_ready", "backup": str(backup)}), flush=True)

    run(["sudo", "-n", "apt-get", "install", "-y", "python3-venv", "fonts-wqy-zenhei"])
    venv = home / "liquidity_digest_venv"
    if not (venv / "bin/python").exists():
        run([sys.executable, "-m", "venv", str(venv)])
    python = str(venv / "bin/python")
    run([python, "-m", "pip", "install", "--disable-pip-version-check",
         "-r", str(release / "requirements-liquidity-digest.txt"), "pytest", "pypdf"])
    run([python, "-m", "compileall", "-q", str(release / "liquidity_digest")])
    run([python, "-m", "pytest", "-q", "tests/test_liquidity_digest.py",
         "tests/test_liquidity_digest_states.py", "tests/test_liquidity_digest_domains.py",
         "tests/test_liquidity_digest_html.py", "tests/test_liquidity_digest_availability.py",
         "tests/test_market_internals.py", "tests/test_liquidity_digest_internals.py",
         "tests/test_sector_backfill.py"],
        cwd=release, env={**os.environ, "MPLCONFIGDIR": str(backup / "test_matplotlib"),
                          "PYTHONDONTWRITEBYTECODE": "1"})
    config_status = provision_config(home)
    state = home / "liquidity_digest_state"
    state.mkdir(mode=0o700, exist_ok=True)
    run([python, "-m", "liquidity_digest.job", "demo", "--date", str(now.date()),
         "--state-dir", str(backup / "smoke_state")], cwd=release)
    run(["sudo", "-n", "systemd-analyze", "verify", *[str(p) for p in units]])
    # Timers are enabled separately, only after read-only credential/source checks.
    link = root / ("current-" + args.release)
    link.symlink_to(release, target_is_directory=True)
    os.replace(link, current)
    for unit in units:
        run(["sudo", "-n", "install", "-m", "644", str(unit), "/etc/systemd/system/"])
    run(["sudo", "-n", "systemctl", "daemon-reload"])
    after_cron = run(["crontab", "-l"], capture_output=True).stdout
    if after_cron != cron:
        raise RuntimeError("Cron changed concurrently; inspect before enabling timers")
    private_json(backup / "installed.json", {
        "version": manifest["version"], "release": str(release), "config_status": config_status,
        "cron_unchanged": True, "timers_enabled_by_installer": False, "files": manifest["files"],
    })
    print(json.dumps({"phase": "installed_not_enabled", "release": str(release),
                      "backup": str(backup), "config_status": config_status,
                      "cron_unchanged": True}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"phase": "failed", "error_type": type(error).__name__}), flush=True)
        raise SystemExit(1)
