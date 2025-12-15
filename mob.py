import time
import signal
import sys
from playwright.sync_api import sync_playwright

RUNNING = True

def handle_exit(signum, frame):
    global RUNNING
    print("\nCtrl+C detected. Closing browser...")
    RUNNING = False

signal.signal(signal.SIGINT, handle_exit)

with sync_playwright() as p:
    # Android device profile
    device = p.devices["Pixel 5"]

    browser = p.chromium.launch(
        headless=False,   # browser visible rahe
        slow_mo=50
    )

    context = browser.new_context(
        **device
    )

    page = context.new_page()

    # Yaha apni website daalo
    page.goto("https://www.instagram.com", timeout=60000)

    print("📱 Mobile browser opened")
    print("❌ Close karne ke liye Ctrl + C dabao")

    # Jab tak Ctrl+C na aaye
    while RUNNING:
        time.sleep(1)

    context.close()
    browser.close()
    print("✅ Browser closed safely")
