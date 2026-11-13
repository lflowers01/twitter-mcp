# Twitter/X local MCP

Give an agent this repository link and say: **“Install and configure this Twitter MCP for Claude Code, Codex, or both. Follow the README, and let me sign in myself.”** The agent should read this file, inspect the code, and follow the installation steps below. No X developer API key is needed.

This is an unofficial browser automation server derived from an existing personal X MCP project. It uses Playwright Firefox and a signed-in X account. X can change its interface or restrict automation, so results and availability are not guaranteed; review X's current rules and use an account you control.

## What it does

- Search X, browse a profile, the home feed, or an individual post, and look up follower counts.
- Fetch a public post via oEmbed when available without a login; save and inspect local drafts.
- Like, unlike, create, reply to, and delete posts. These change the account and require `confirm=true`, which the agent should only use for the exact action explicitly authorized by the user. The server cannot independently verify user consent. A failed or unconfirmed submission must be checked on X before retrying.

## Install for an agent

Prerequisites: Git, Python 3.10+ with `venv` and `pip`, internet access, and the [Claude Code CLI](https://code.claude.com/docs/en/setup) and/or [Codex CLI](https://developers.openai.com/codex/quickstart/) installed and signed in. A graphical desktop is needed for the one-time X login. Linux may need Playwright's system browser dependencies; see [Playwright's instructions](https://playwright.dev/python/docs/intro#system-requirements).

1. Clone the repository into a permanent location (do not delete or move it after registration):

   ```sh
   git clone https://github.com/lflowers01/twitter-mcp.git
   cd twitter-mcp
   ```

2. Run one command, choosing the client(s) you actually use:

   ```sh
   python install.py --client codex
   # or: python install.py --client claude
   # or: python install.py --client both
   ```

   On Windows use `py` if `python` is unavailable. The installer creates `.venv`, installs the pinned Python dependencies and Playwright Firefox, registers `twitter-local` via the chosen client CLI, and installs the skill into that client's user skills directory. It leaves an existing `twitter-local` registration or skill untouched; inspect and update those yourself if they already exist. Run `python install.py --client none` to install dependencies without changing agent configuration. Installation downloads packages and browser binaries.

3. Ask the user which login method they prefer, then run the exact login command printed by the installer:

   - **Sign in in the browser (recommended):** The command opens a separate Firefox window. The user signs in and completes any verification themselves, then closes the window. Firefox saves the session in its private profile; no cookie export is necessary and the agent never needs the cookie values.
   - **Import an existing `cookies.json`:** Ask the user to put their exported JSON cookie array at a local path of their choosing (for example, `~/.twitter-mcp/cookies.json`) and tell the agent the path only. Append `--cookies "PATH_TO_COOKIES_JSON"` to the printed login command. The importer copies only X/Twitter-domain cookies into the private Firefox profile and opens X for the user to confirm the login, then they close the window. The user should delete the export securely when done if no longer needed.

   Never paste passwords or cookie contents into chat, logs, or the repository. To sign in again, first close the MCP browser using `close_browser` or stop the agent, then repeat the login command.

4. Restart the agent/client to load the MCP server and skill. Ask it to call `whoami`, or to search for a topic and cite the returned post URLs. `get_tweet` may work without login. An empty search is not proof that a search succeeded: check login, page display, and X availability.

The setup registers a local **stdio** MCP server for Claude Code and/or Codex. For Claude Desktop or another MCP client, add a stdio server named `twitter-local` using the absolute path of `.venv`'s Python executable as `command` and the absolute path to `server.py` as its sole argument. Restart that client. Installing the skill and the MCP server are separate; the skill gives usage guidance, while the MCP connection supplies the tools.

## Data and configuration

The default private data directory is `~/.twitter-mcp/`, containing the persistent Firefox profile, drafts, and the action-count log. Set `X_MCP_HOME` before both login and server startup to relocate it; `X_PROFILE` overrides only the browser profile path and must match for both. Set `X_HEADLESS=1` for headless server operation; login still requires a visible browser. Only one server/login process can use a Firefox profile at a time. Close it before restarting login. Never commit, upload, or share profiles, cookies, drafts, or logs.

The server uses the actual X website, so account permissions, regional restrictions, MFA, rate limits, and X UI changes may affect behavior. Search counts are capped and results may be incomplete. The "Following" tab is best effort and may fall back to For You. Media returns image URLs and video thumbnails, not a video downloader. Write confirmations and daily caps are safeguards, not a guarantee of safety; deletion is permanent.

## Troubleshooting

- **CLI not found:** Install the chosen client and restart the terminal. The installer will not replace an existing server of the same name; check `codex mcp get twitter-local` or `claude mcp get twitter-local`.
- **Browser missing:** Run the installer again; on Linux install the OS dependencies required by Playwright.
- **Profile in use / login required:** Stop the other browser or client, rerun the printed `login.py` command, and log in again. Avoid copying or deleting an active Firefox profile.
- **Tools unavailable:** Restart the client and check its MCP configuration. The repository and virtual environment must remain at the registered paths.
- **Posts missing / write uncertain:** Check X directly. Website selectors change and post-submission verification can fail; never assume an unconfirmed action did not occur.

## Development

Run `python -m unittest discover -s tests` inside the virtual environment. Tests are offline and do not post or sign into an account.
