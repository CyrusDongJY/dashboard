"""One-time cloud bootstrap: write only the new independent deployment root."""
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess

HOME = Path.home()
ROOT = HOME / "core_funding_monitor"
EXPECTED_FILES = "03bdf2d409ed94b11b418b9f00fb7dc2ebb958fe1bf9523c9a810d88f4501a47"
EXPECTED_CRON = "fcb0ca1b64c3d3683cce296d72b2e759caab6a48e874e63e5fdeea151cf6c7a3"


def protected_hashes():
    return {str(path.relative_to(HOME)): hashlib.sha256(path.read_bytes()).hexdigest()
            for name in ("market_dashboard", "quant_bot", "TradingRadar")
            for path in sorted((HOME / name).glob("*.py"))}


def main():
    os.umask(0o077)
    hashes = protected_hashes()
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    cron = subprocess.check_output(["crontab", "-l"])
    if digest != EXPECTED_FILES or hashlib.sha256(cron).hexdigest() != EXPECTED_CRON:
        raise SystemExit("Production changed since read-only audit; re-audit before deployment")
    if (ROOT / "backups" / "baseline.json").exists():
        raise SystemExit("Baseline already exists; bootstrap is deliberately insert-only")
    for name in ("backups", "releases", "reports", "cache", "logs"):
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    (ROOT / "backups" / "baseline.json").write_text(json.dumps({
        "files": hashes, "files_digest": digest, "cron_digest": EXPECTED_CRON}, indent=2))
    (ROOT / "backups" / "crontab.before").write_bytes(cron)
    tree = ast.parse((HOME / "market_dashboard" / "market_config.py").read_text())
    fields = {"FRED_API_KEY", "SUPABASE_URL", "SUPABASE_KEY", "SENDER_EMAIL", "APP_PASSWORD", "RECEIVER_EMAIL"}
    values = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
              if isinstance(n, ast.Assign) and len(n.targets) == 1
              and isinstance(n.targets[0], ast.Name) and n.targets[0].id in fields}
    if set(values) != fields or not all(isinstance(v, str) and v for v in values.values()):
        raise SystemExit("Required independent configuration is incomplete")
    path = ROOT / "config.json"
    with path.open("x") as handle:
        json.dump(values, handle)
    path.chmod(0o600)
    print(json.dumps({"protected_files": len(hashes), "digest": digest,
                      "config_mode": oct(path.stat().st_mode & 0o777),
                      "key_kind": "secret" if values["SUPABASE_KEY"].startswith("sb_secret_") else "other"}))


if __name__ == "__main__":
    main()
