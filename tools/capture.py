# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright>=1.47"]
# ///
"""Open a headed Chrome, let the user drive Fiscozen, record network traffic to a HAR file.

The HAR is written when the browser window is closed.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJECT_DIR = Path(__file__).resolve().parent.parent
CAPTURE_DIR = PROJECT_DIR / "captures"
START_URL = "https://app.fiscozen.it/"


def main() -> None:
    CAPTURE_DIR.mkdir(mode=0o700, exist_ok=True)
    os.chmod(CAPTURE_DIR, 0o700)
    har_path = CAPTURE_DIR / f"fiscozen-{datetime.now():%Y%m%d-%H%M%S}.har"

    with sync_playwright() as pw:
        # Use the installed Google Chrome so no extra browser download is needed
        browser = pw.chromium.launch(channel="chrome", headless=False)
        context = browser.new_context(
            record_har_path=str(har_path),
            record_har_content="embed",  # keep request/response bodies inline
            record_har_url_filter="**/*fiscozen*/**",  # only Fiscozen domains
        )
        page = context.new_page()
        page.goto(START_URL)

        print(f"Recording to {har_path}")
        print("Log in, create an invoice draft, then close the browser window.")

        # Block until the user closes the window, then flush the HAR
        page.wait_for_event("close", timeout=0)
        context.close()
        browser.close()

    # The HAR contains session cookies and the login request body (password)
    os.chmod(har_path, 0o600)
    print(f"HAR saved: {har_path} (contains secrets: keep it out of git and delete when done)")


if __name__ == "__main__":
    sys.exit(main())
