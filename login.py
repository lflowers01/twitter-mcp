"""Sign in in a dedicated Firefox profile or import an explicitly supplied cookie export."""
import argparse
import json
import os
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cookies", help="Path to your own exported cookies.json; never paste cookie contents into chat")
    args = parser.parse_args()
    cookies = load_cookies(args.cookies) if args.cookies else []
    profile = Path(os.environ.get("X_PROFILE", Path(os.environ.get("X_MCP_HOME", Path.home() / ".twitter-mcp")) / "firefox-profile")).expanduser()
    profile.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.firefox.launch_persistent_context(str(profile), headless=False)
        if cookies:
            context.add_cookies(cookies)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://x.com/home" if cookies else "https://x.com/login", wait_until="domcontentloaded")
        print("Check that you are signed in; complete login or verification yourself, then close the browser window.", flush=True)
        page.wait_for_event("close", timeout=0)

if __name__ == "__main__":
    main()
