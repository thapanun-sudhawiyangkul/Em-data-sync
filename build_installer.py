"""
build_installer.py - rebuild the portable package  installer/EmbroiSync  and  EmbroiSync_Setup.zip

Run after every change to sync_passport.py, mdf_reader.py or installer_src/*:

    py -3 -m pip install -r requirements.txt pyinstaller
    py -3 build_installer.py              package with the Supabase url/key from config.json prefilled
    py -3 build_installer.py --no-key     package with placeholders instead of the secret key
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PKG = ROOT / "installer" / "EmbroiSync"
SRC = ROOT / "installer_src"
ZIP = ROOT / "EmbroiSync_Setup"          # .zip is added by make_archive

COLLECT = ["supabase", "postgrest", "supabase_auth", "storage3", "realtime", "supabase_functions"]


def build_exe(work: Path) -> Path:
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--console",
           "--name", "EmbroiSync", "--distpath", str(work / "dist"), "--workpath", str(work / "work"),
           "--specpath", str(work), "--paths", str(ROOT), "--collect-data", "certifi"]
    for mod in COLLECT:
        cmd += ["--collect-submodules", mod]
    cmd.append(str(ROOT / "sync_passport.py"))
    print("building EmbroiSync.exe (about one minute)...")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-3000:], r.stderr[-3000:])
        raise SystemExit("PyInstaller failed")
    return work / "dist" / "EmbroiSync"


def crlf(text: str) -> bytes:
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


def assemble(app_dir: Path, with_key: bool):
    if PKG.exists():
        shutil.rmtree(PKG)
    PKG.mkdir(parents=True)
    shutil.copytree(app_dir, PKG / "app")
    shutil.copy2(SRC / "setup.ps1", PKG / "setup.ps1")
    shutil.copy2(ROOT / "supabase_schema.sql", PKG / "supabase_schema.sql")
    (PKG / "README.txt").write_bytes(crlf((SRC / "README.txt").read_text(encoding="utf-8")))
    (PKG / "Setup.bat").write_bytes(crlf(
        '@echo off\n'
        'rem Opens the setup window.\n'
        'cd /d "%~dp0"\n'
        'start "" powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0setup.ps1"\n'))
    (PKG / "Sync now.bat").write_bytes(crlf(
        '@echo off\n'
        'rem Runs one sync and shows the result. Options are passed on, e.g.  "Sync now.bat" --dry-run\n'
        'cd /d "%~dp0"\n'
        '"%~dp0app\\EmbroiSync.exe" %*\n'
        'echo.\n'
        'pause\n'))

    url, key = "https://YOUR-PROJECT-REF.supabase.co", "YOUR-SECRET-KEY"
    cfg_file = ROOT / "config.json"
    if with_key and cfg_file.exists():
        cur = json.load(open(cfg_file, encoding="utf-8-sig")).get("supabase", {})
        url, key = cur.get("url") or url, cur.get("key") or key
    cfg = {
        "data_dir": "",
        "file_pattern": "*.MDF",
        "exclude": [],
        "supabase": {"url": url, "key": key, "table": "passport_log", "state_table": "passport_sync_state",
                     "run_table": "passport_sync_run", "batch_size": 1000},
        "overlap_rows": 5, "interval_minutes": 60, "staging_dir": "staging", "keep_copies": 2,
        "copy_retries": 3, "copy_retry_seconds": 20, "log_dir": "logs",
    }
    (PKG / "config.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return key != "YOUR-SECRET-KEY"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-key", action="store_true", help="do not copy the Supabase url/key into the package")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="embroi_build_") as tmp:
        app_dir = build_exe(Path(tmp))
        has_key = assemble(app_dir, not args.no_key)
    zip_path = Path(shutil.make_archive(str(ZIP), "zip", root_dir=PKG.parent, base_dir=PKG.name))
    print(f"package : {PKG}")
    print(f"zip     : {zip_path}  ({zip_path.stat().st_size // (1024 * 1024)} MB)")
    print("secret key included in package: " + ("YES - treat the zip like a password" if has_key else "no"))

    r = subprocess.run([str(PKG / "app" / "EmbroiSync.exe"), "--help"], capture_output=True, text=True)
    print("exe self test: " + ("OK" if r.returncode == 0 and "--check" in r.stdout else "FAILED\n" + r.stdout + r.stderr))


if __name__ == "__main__":
    main()
