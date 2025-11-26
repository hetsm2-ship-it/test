import argparse
import os
import time
import re
import unicodedata
import json
import threading
from playwright.sync_api import sync_playwright

def sanitize_input(raw):
    """
    Fix shell-truncated input (e.g., when '&' breaks in CMD or bot execution).
    If input comes as a list (from nargs='+'), join it back into a single string.
    """
    if isinstance(raw, list):
        raw = " ".join(raw)
    return raw

def parse_messages(names_arg):
    """
    Robust parser for messages:
    - If names_arg is a .txt file, first try JSON-lines parsing (one JSON string per line, supporting multi-line messages).
    - If that fails, read the entire file content as a single block and split only on explicit separators '&' or 'and' (preserving newlines within each message for ASCII art).
    - For direct string input, treat as single block and split only on separators.
    This ensures ASCII art (multi-line blocks without separators) is preserved as a single message.
    """
    # Handle argparse nargs possibly producing a list
    if isinstance(names_arg, list):
        names_arg = " ".join(names_arg)

    content = None
    is_file = isinstance(names_arg, str) and names_arg.endswith('.txt') and os.path.exists(names_arg)

    if is_file:
        # Try JSON-lines first (each line is a JSON-encoded string, possibly with \n for multi-line)
        try:
            msgs = []
            with open(names_arg, 'r', encoding='utf-8') as f:
                lines = [ln.rstrip('\n') for ln in f if ln.strip()]  # Skip empty lines
            for ln in lines:
                m = json.loads(ln)
                if isinstance(m, str):
                    msgs.append(m)
                else:
                    raise ValueError("JSON line is not a string")
            if msgs:
                # Normalize each message (preserve \n for art)
                out = []
                for m in msgs:
                    m = unicodedata.normalize("NFKC", m)
                    m = re.sub(r'[\u200B-\u200F\uFEFF\u202A-\u202E\u2060-\u206F]', '', m)
                    out.append(m)
                return out
        except Exception:
            pass  # Fall through to block parsing on any error

        # Fallback: read entire file as one block for separator-based splitting
        try:
            with open(names_arg, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            raise ValueError(f"Failed to read file {names_arg}: {e}")
    else:
        # Direct string input
        content = str(names_arg)

    if content is None:
        raise ValueError("No valid content to parse")

    # Normalize content (preserve \n for ASCII art)
    content = unicodedata.normalize("NFKC", content)
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    content = re.sub(r'[\u200B-\u200F\uFEFF\u202A-\u202E\u2060-\u206F]', '', content)

    # Normalize ampersand-like characters to '&' for consistent splitting
    content = (
        content.replace('﹠', '&')
        .replace('＆', '&')
        .replace('⅋', '&')
        .replace('ꓸ', '&')
        .replace('︔', '&')
    )

    # Split only on explicit separators: '&' or the word 'and' (case-insensitive, with optional whitespace)
    # This preserves multi-line blocks like ASCII art unless explicitly separated
    pattern = r'\s*(?:&|\band\b)\s*'
    parts = [part.strip() for part in re.split(pattern, content, flags=re.IGNORECASE) if part.strip()]
    return parts

def sender(tab_id, args, messages, headless, storage_path):
    """
    Sender thread: Cycles through messages in an infinite loop, preloading/reloading pages every 60s to avoid issues.
    Preserves newlines in messages for multi-line content like ASCII art.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=storage_path)
        page = context.new_page()
        dm_selector = 'div[role="textbox"][aria-label="Message"]'
        try:
            page.goto(args.thread_url, timeout=60000)
            page.wait_for_selector(dm_selector, timeout=30000)
            print(f"Tab {tab_id} ready, starting infinite message loop.")
            current_page = page
            cycle_start = time.time()
            new_page = None
            preloaded_this_cycle = False
            msg_index = 0
            while True:
                elapsed = time.time() - cycle_start
                if elapsed >= 60:
                    if new_page is not None:
                        current_page.close()
                        current_page = new_page
                        print(f"Tab {tab_id} switched to new page after {elapsed:.1f}s")
                    else:
                        print(f"Tab {tab_id} no new page, reloading current after {elapsed:.1f}s")
                        current_page.goto(args.thread_url, timeout=60000)
                        current_page.wait_for_selector(dm_selector, timeout=30000)
                    cycle_start = time.time()
                    new_page = None
                    preloaded_this_cycle = False
                    continue
                if elapsed >= 50 and not preloaded_this_cycle:
                    preloaded_this_cycle = True
                    try:
                        new_page = context.new_page()
                        new_page.goto(args.thread_url, timeout=60000)
                        new_page.wait_for_selector(dm_selector, timeout=30000)
                        print(f"Tab {tab_id} preloaded new page at {elapsed:.1f}s")
                    except Exception as e:
                        new_page = None
                        print(f"Tab {tab_id} failed to preload new page at {elapsed:.1f}s: {e}")
                msg = messages[msg_index]
                try:
                    if not current_page.locator(dm_selector).is_visible():
                        print(f"Tab {tab_id} selector not visible, skipping '{msg[:50]}...'")
                        time.sleep(0.3)
                        msg_index = (msg_index + 1) % len(messages)
                        continue
                    # DO NOT replace \n with space: Preserve multi-line for ASCII art
                    # Instagram DM supports multi-line messages via fill()
                    current_page.click(dm_selector)
                    current_page.fill(dm_selector, msg)
                    current_page.press(dm_selector, 'Enter')
                    print(f"Tab {tab_id} sent message {msg_index + 1}/{len(messages)}")
                    time.sleep(0.3)  # Brief delay between sends
                except Exception as e:
                    print(f"Tab {tab_id} error sending message {msg_index + 1}: {e}")
                    time.sleep(0.3)
                msg_index = (msg_index + 1) % len(messages)
        except Exception as e:
            print(f"Tab {tab_id} unexpected error: {e}")
        finally:
            browser.close()

def main():
    parser = argparse.ArgumentParser(description="Instagram DM Auto Sender using Playwright")
    parser.add_argument('--username', required=False, help='Instagram username (required for initial login)')
    parser.add_argument('--password', required=False, help='Instagram password (required for initial login)')
    parser.add_argument('--thread-url', required=True, help='Full Instagram direct thread URL')
    parser.add_argument('--names', nargs='+', required=True, help='Messages list, direct string, or .txt file (split on & or "and" for multiple; preserves newlines for art)')
    parser.add_argument('--headless', default='true', choices=['true', 'false'], help='Run in headless mode (default: true)')
    parser.add_argument('--storage-state', required=True, help='Path to JSON file for login state (persists session)')
    parser.add_argument('--tabs', type=int, default=1, help='Number of parallel tabs (1-5, default 1)')
    args = parser.parse_args()
    args.names = sanitize_input(args.names)  # Handle bot/shell-truncated inputs

    headless = args.headless == 'true'
    storage_path = args.storage_state
    do_login = not os.path.exists(storage_path)

    if do_login:
        if not args.username or not args.password:
            print("Error: Username and password required for initial login.")
            return
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context()
            page = context.new_page()
            try:
                print("Logging in to Instagram...")
                page.goto("https://www.instagram.com/", timeout=60000)
                page.wait_for_selector('input[name="username"]', timeout=30000)
                page.fill('input[name="username"]', args.username)
                page.fill('input[name="password"]', args.password)
                page.click('button[type="submit"]')
                # Wait for successful redirect (adjust if needed for 2FA or errors)
                page.wait_for_url("**/home**", timeout=60000)  # More specific to profile/home
                print("Login successful, saving storage state.")
                context.storage_state(path=storage_path)
            except Exception as e:
                print(f"Login error: {e}")
                return
            finally:
                browser.close()
    else:
        print("Using existing storage state, skipping login.")

    try:
        messages = parse_messages(args.names)
    except ValueError as e:
        print(f"Error parsing messages: {e}")
        return

    if not messages:
        print("Error: No valid messages provided.")
        return

    print(f"Parsed {len(messages)} messages.")

    tabs = min(max(args.tabs, 1), 5)
    threads = []
    for i in range(tabs):
        t = threading.Thread(target=sender, args=(i + 1, args, messages, headless, storage_path))
        t.daemon = True
        t.start()
        threads.append(t)

    print(f"Starting {tabs} tab(s) in infinite message loop. Press Ctrl+C to stop.")
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\nStopping all tabs...")

if __name__ == "__main__":
    main()