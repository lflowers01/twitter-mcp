"""Set up the local environment and optionally register the server with an agent."""
import argparse
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent

def run(*args):
    print("Running:", *args, flush=True)
    subprocess.run(list(map(str, args)), check=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", choices=("claude", "codex", "both", "none"), default="none")
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        parser.error("Python 3.10 or newer is required")
    clients = ("claude", "codex") if args.client == "both" else (() if args.client == "none" else (args.client,))
    for client in clients:
        if not shutil.which(client):
            parser.error(f"{client} CLI was not found on PATH; install it first")
    env = ROOT / ".venv"
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        venv.create(env, with_pip=True)
    run(python, "-m", "pip", "install", "-r", ROOT / "requirements.txt")
    run(python, "-m", "playwright", "install", "firefox")
    for client in clients:
        check = subprocess.run([client, "mcp", "get", "twitter-local"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if check.returncode == 0:
            print(f"{client}: twitter-local already exists; leaving existing configuration unchanged.")
            continue
        cmd = [client, "mcp", "add"]
        if client == "claude":
            cmd += ["--scope", "user", "--transport", "stdio"]
        run(*cmd, "twitter-local", "--", python, ROOT / "server.py")
    for client in clients:
        skill_root = Path.home() / (".claude/skills" if client == "claude" else ".agents/skills")
        target = skill_root / "twitter-local"
        if target.exists():
            print(f"{client}: {target} already exists; leaving it unchanged.")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(ROOT / "skills" / "twitter-local", target)
            print(f"{client}: installed skill to {target}")
    login_command = [str(python), str(ROOT / "login.py")]
    print("Setup complete. Sign in manually:", subprocess.list2cmdline(login_command) if os.name == "nt" else shlex.join(login_command))
    print("Restart your agent so it discovers the server. Keep this folder and its .venv in place.")

if __name__ == "__main__":
    main()
