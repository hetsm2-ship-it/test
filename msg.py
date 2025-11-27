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
    Windows-safe input:
    Always treat --names as ONE SINGLE string.
    """
    return str(raw)

def parse_messages(names_arg):
    """
    Always treat names_arg as a SINGLE RAW STRING.
    Windows CMD fix for 'and' separators.

    NOTE: This version will split on '&' OR the substring 'and' (case-insensitive),
    even if 'and' is glued to other characters (e.g. "spyther1andspyther2").
    If you want only standalone 'and' (surrounded by word boundaries), change 'and'
    to r'\band\b' in the pattern.
    """
    if isinstance(names_arg, list):   # If cmd breaks, join with space
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

    # Split on explicit separators: '&' or substring 'and' (case-insensitive).
    # This will split strings like "spyther1andspytger2andspytger3".
    pattern = r'\s*(?:&|and)\s*'
    parts = [part.strip() for part in re.split(pattern, content, flags=re.IGNORECASE) if part.strip()]
    return parts

def sender(page, tab_id, args, messages, headless):
    """
    Sender thread uses an already-created Playwright Page (tab).
    It will cycle messages infinitely on that page and perform preloads using page.context.
    """
    dm_selector = 'div[role="textbox"][aria-label="Message"]'
    try:
        page.goto(args.thread_url, timeout=60000)
        page.wait_for_selector(dm_selector, timeout=30000)
        print(f"Tab {tab_id} ready, starting infinite message loop.")
        cycle_start = time.time()
        new_page = None
        preloaded_this_cycle = False
        msg_index = 0
        current_page = page

        while True:
            elapsed = time.time() - cycle_start
            if elapsed >= 60:
                if new_page is not None:
                    try:
                        current_page.close()
                    except Exception:
                        pass
                    current_page = new_page
                    print(f"Tab {tab_id} switched to new page after {elapsed:.1f}s")
                else:
                    print(f"Tab {tab_id} reloading current after {elapsed:.1f}s")
                    try:
                        current_page.goto(args.thread_url, timeout=60000)
                        current_page.wait_for_selector(dm_selector, timeout=30000)
                    except Exception as e:
                        print(f"Tab {tab_id} reload error: {e}")
                cycle_start = time.time()
                new_page = None
                preloaded_this_cycle = False
                continue

            if elapsed >= 50 and not preloaded_this_cycle:
                preloaded_this_cycle = True
                try:
                    new_page = current_page.context.new_page()
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

                current_page.click(dm_selector)
                # preserve newlines for ASCII art; do NOT replace \n with spaces
                current_page.fill(dm_selector, msg)
                current_page.press(dm_selector, 'Enter')
                print(f"Tab {tab_id} sent message {msg_index + 1}/{len(messages)}")
                time.sleep(0.3)
            except Exception as e:
                print(f"Tab {tab_id} error sending message {msg_index + 1}: {e}")
                time.sleep(0.3)

            msg_index = (msg_index + 1) % len(messages)

    except Exception as e:
        print(f"Tab {tab_id} unexpected error: {e}")
    finally:
        try:
            page.close()
        except Exception:
            pass

def main():
    parser = argparse.ArgumentParser(description="Instagram DM Auto Sender using Playwright")
    parser.add_argument('--username', required=False, help='Instagram username (required for initial login)')
    parser.add_argument('--password', required=False, help='Instagram password (required for initial login)')
    parser.add_argument('--thread-url', required=True, help='Full Instagram direct thread URL')
    parser.add_argument('--names', required=True, help='Messages list, direct string, or .txt file (split on & or "and" for multiple; preserves newlines for art)')
    parser.add_argument('--headless', default='true', choices=['true', 'false'], help='Run in headless mode (default: true)')
    parser.add_argument('--storage-state', required=True, help='Path to JSON file for login state (persists session)')
    parser.add_argument('--tabs', type=int, default=1, help='Number of parallel tabs (1-5, default 1)')
    args = parser.parse_args()
    args.names = sanitize_input(args.names)  # Handle bot/shell-truncated inputs

    headless = args.headless == 'true'
    storage_path = args.storage_state

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

    # Launch a single Playwright browser + context and create pages (tabs)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        # Use storage state if exists, otherwise create a fresh context and do login
        do_login = not os.path.exists(storage_path)

        if do_login:
            if not args.username or not args.password:
                print("Error: Username and password required for initial login.")
                browser.close()
                return
            # temporary context for login, then save storage_state
            context = browser.new_context()
            page = context.new_page()
            try:
                print("Logging in to Instagram...")
                page.goto("https://www.instagram.com/", timeout=60000)
                page.wait_for_selector('input[name="username"]', timeout=30000)
                page.fill('input[name="username"]', args.username)
                page.fill('input[name="password"]', args.password)
                page.click('button[type="submit"]')
                # Wait for successful redirect (adjust if needed for 2FA)
                page.wait_for_url("**/home**", timeout=60000)
                print("Login successful, saving storage state.")
                context.storage_state(path=storage_path)
            except Exception as e:
                print(f"Login error: {e}")
                context.close()
                browser.close()
                return
            finally:
                try:
                    page.close()
                except Exception:
                    pass
                context.close()

        # Now create a new context that uses the stored state
        context = browser.new_context(storage_state=storage_path)
        pages = []
        for i in range(tabs):
            pg = context.new_page()
            pages.append(pg)

        # Ensure each page is navigated and ready before starting threads
        dm_selector = 'div[role="textbox"][aria-label="Message"]'
        for idx, pg in enumerate(pages):
            try:
                pg.goto(args.thread_url, timeout=60000)
                pg.wait_for_selector(dm_selector, timeout=30000)
                print(f"Prepared tab {idx + 1}")
            except Exception as e:
                print(f"Tab {idx + 1} preparation error: {e}")

        threads = []
        for i, pg in enumerate(pages):
            t = threading.Thread(target=sender, args=(pg, i + 1, args, messages, headless))
            t.daemon = True
            t.start()
            threads.append(t)

        print(f"Starting {tabs} tab(s) in infinite message loop. Press Ctrl+C to stop.")
        try:
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            print("\nStopping all tabs...")

        # cleanup
        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()