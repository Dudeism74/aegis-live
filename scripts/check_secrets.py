"""Check tracked text for common credentials without printing matching values."""
from pathlib import Path
import re
import subprocess
import sys

RULES = {
    "Telegram bot token": re.compile(rb"(?<![A-Za-z0-9_])[0-9]{6,15}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"),
    "GitHub token": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{30,255})"),
    "Private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
}


def findings(data):
    return [name for name, pattern in RULES.items() if pattern.search(data)]


def main():
    if "--self-test" in sys.argv:
        assert findings(b"123456789:" + b"A" * 35) == ["Telegram bot token"]
        assert findings(b"ghp_" + b"a" * 36) == ["GitHub token"]
        assert findings(b"-----BEGIN " + b"PRIVATE KEY-----") == ["Private key"]
        assert not findings(b"os.environ.get('TELEGRAM_BOT_TOKEN')")
        print("Credential pattern self-tests passed.")
        return 0
    root = Path(__file__).resolve().parents[1]
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).split(b"\0")
    found = []
    for raw in paths:
        if not raw:
            continue
        path = Path(raw.decode("utf-8"))
        full = root / path
        if not full.is_file() or full.is_symlink():
            continue
        for rule in findings(full.read_bytes()):
            found.append((path.as_posix(), rule))
    for path, rule in found:
        print(f"Credential pattern detected: {rule} in {path}; value withheld.")
    print(f"Checked tracked files: {len(found)} credential pattern findings.")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
