"""Sign in in a dedicated Firefox profile or import an explicitly supplied cookie export."""
import argparse
import json
import os
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright


def load_cookies(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(raw, list):
        raise ValueError("Cookie export must be a JSON array")
    same_site = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}
    cookies = []
    for item in raw:
        domain = item.get("domain", "").lstrip(".").lower()
        if domain not in ("x.com", "twitter.com") and not domain.endswith((".x.com", ".twitter.com")):
            continue
        cookie = {"name": item["name"], "value": item["value"], "domain": item["domain"],
                  "path": item.get("path", "/"), "secure": item.get("secure", True),
                  "httpOnly": item.get("httpOnly", False),
                  "sameSite": same_site.get(str(item.get("sameSite", "lax")).lower(), "Lax")}
        expiry = item.get("expirationDate", item.get("expires"))
        if isinstance(expiry, (int, float)) and expiry > 0:
            cookie["expires"] = expiry
        cookies.append(cookie)
    if not cookies:
        raise ValueError("No X/Twitter cookies found in the export")
    return cookies


def save_cookies(context, destination):
    cookies = context.cookies(["https://x.com", "https://twitter.com"])
    if not any(c.get("name") == "auth_token" and c.get("value") for c in cookies):
        raise RuntimeError("No X login cookie found. Sign in fully and try again; the existing cookie backup was not changed.")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(cookies, output)
            output.flush()
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cookies", help="Path to your own exported cookies.json; never paste cookie contents into chat")
    args = parser.parse_args()
    cookies = load_cookies(args.cookies) if args.cookies else []
    data = Path(os.environ.get("X_MCP_HOME", Path.home() / ".twitter-mcp")).expanduser()
    profile = Path(os.environ.get("X_PROFILE", data / "firefox-profile")).expanduser()
    profile.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.firefox.launch_persistent_context(str(profile), headless=False)
        if cookies:
            context.add_cookies(cookies)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://x.com/home" if cookies else "https://x.com/login", wait_until="domcontentloaded")
        try:
            input("Sign in and verify the home timeline in Firefox, then return here and press Enter to save and close Firefox: ")
            saved = save_cookies(context, data / "cookies.json")
            print(f"X cookies saved locally to {saved}; keep this file private.", flush=True)
        finally:
            context.close()

if __name__ == "__main__":
    main()
