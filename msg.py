#!/usr/bin/env python3
"""
Instagram DM Auto Sender (recode)

- Uses a single Playwright browser and context; each thread creates a page (tab).
- After initial login, directly opens the provided thread URL (so it won't hang waiting for /home).
- Message splitter: splits on '&' or substring 'and' (case-insensitive).
- Preserves newline characters in messages (for ASCII art).
- Supports JSON-lines .txt (each line is a JSON string) or plain .txt with separators.
- Intended for Windows Server 2022 (tested for Playwright usage patterns).
"""

import argparse
import os
import time
import re
import unicodedata
import json
import threading
import sys
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# Shared resources (one browser & context for all threads)
_GLOBAL_PW = None
_GLOBAL_BROWSER = None
_GLOBAL_CONTEXT = None

def sanitize_input(raw):
    """Make sure names passed from argparse (maybe nargs) are treated as a single string."""
    if isinstance(raw, list):
        return " ".join(raw)
    return str(raw)

def parse_messages(names_arg):
    """
    Parse messages from either:
     - .txt file (tries JSON-lines first; otherwise reads whole file and splits on separators)
     - direct string input

    Splitting separators: '&' or substring 'and' (case-insensitive).
    Preserves newlines inside messages.
    """
    if isinstance(names_arg, list):
        names_arg = " ".join(names_arg)

    content = None
    is_file = isinstance(names_arg, str) and names_arg.endswith('.txt') and os.path.exists(names_arg)

    if is_file:
        # Try JSON-lines: each non-empty line is JSON-encoded string message
        try:
            msgs = []
            with open(names_arg, 'r', encoding='utf-8') as f:
                lines = [ln.rstrip('\n') for ln in f if ln.strip()]
            for ln in lines:
                # if line is JSON string
                m = json.loads(ln)
                if isinstance(m, str):
                    msgs.append(m)
                else:
                    raise ValueError("JSON line is not a JSON string")
            if msgs:
                # normalize unicode and strip zero-width characters
                out = []
                for m in msgs:
                    mm = unicodedata.normalize("NFKC", m)
                    mm = re.sub(r'[\u200B-\u200F\uFEFF\u202A-\u202E\u2060-\u206F]', '', mm)
                    out.append(mm)
                return out
        except Exception:
            # fallback to whole-file parsing
            pass

        try:
            with open(names_arg, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            raise ValueError(f"Failed to read file {names_arg}: {e}")
    else:
        content = str(names_arg)

    if content is None:
        raise ValueError("No content to parse")

    # normalize
    content = unicodedata.normalize("NFKC", content)
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    content = re.sub(r'[\u200B-\u200F\uFEFF\u202A-\u202E\u2060-\u206F]', '', content)

    # normalize variants of '&'
    content = (content.replace('﹠', '&').replace('＆', '&').replace('⅋', '&')
               .replace('ꓸ', '&').replace('︔', '&'))

    # Split on & or substring "and" (case-insensitive). This will split "spyther1andspyther2"
    pattern = r'\s*(?:&|and)\s*'
    parts = [part.strip() for part in re.split(pattern, content, flags=re.IGNORECASE) if part.strip()]
    return parts

def do_initial_login(thread_url, username, password, storage_path, headless):
    """
    Perform an initial login using a temporary browser/context and save storage_state to storage_path.
    After successful login submit, immediately navigate to thread_url and wait for thread textbox to appear.
    Return True on success, False otherwise.
    """
    print("Starting initial login flow...")
    p = sync_playwright().start()
    browser = None
    context = None
    page = None
    try:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()

        # go to login page (use accounts/login to reduce landing redirects)
        page.goto("https://www.instagram.com/accounts/login/", timeout=60000)
        # wait for username input
        page.wait_for_selector('input[name="username"]', timeout=30000)
        page.fill('input[name="username"]', username)
        page.fill('input[name="password"]', password)

        # click submit
        # Instagram's login button may be a button[type=submit] or [role=button]
        try:
            page.click('button[type="submit"]')
        except Exception:
            # fallback: press Enter in password
            page.press('input[name="password"]', 'Enter')

        # After submit, don't wait for /home — directly attempt to open the thread_url.
        # If login failed, the thread page will either redirect to login or show the login form again.
        try:
            page.goto(thread_url, timeout=60000)
            # Wait for the DM textarea selector (same as sender uses)
            dm_selector = 'div[role="textbox"][aria-label="Message"]'
            page.wait_for_selector(dm_selector, timeout=30000)
            # If selector found, save storage state and return success
            context.storage_state(path=storage_path)
            print("Login succeeded and thread opened — storage state saved.")
            return True
        except PWTimeoutError:
            # If timeout opening thread, try a short check: if login form still present, login failed
            try:
                if page.query_selector('input[name="username"]'):
                    print("Login appears to have failed (login form still present).")
                    return False
            except Exception:
                pass
            # Try one more time to save storage (if logged in but thread slow)
            try:
                context.storage_state(path=storage_path)
                print("Saved storage state (login may have completed).")
                return True
            except Exception as e:
                print(f"Could not confirm login and could not save storage state: {e}")
                return False
    except Exception as e:
        print(f"Initial login error: {e}")
        return False
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        try:
            p.stop()
        except Exception:
            pass

def sender_loop(tab_id, page, args, messages):
    """
    The per-tab sender loop. Uses the provided Playwright Page (tab).
    Behavior preserved from original: preload a new page at ~50s, switch at 60s, preserve newlines, small delays.
    """
    dm_selector = 'div[role="textbox"][aria-label="Message"]'
    try:
        page.goto(args.thread_url, timeout=60000)
        page.wait_for_selector(dm_selector, timeout=30000)
    except Exception as e:
        print(f"Tab {tab_id} initial navigation error: {e}")

    print(f"Tab {tab_id} ready, starting infinite message loop.")
    current_page = page
    cycle_start = time.time()
    new_page = None
    preloaded_this_cycle = False
    msg_index = 0

    while True:
        elapsed = time.time() - cycle_start
        if elapsed >= 60:
            if new_page:
                try:
                    current_page.close()
                except Exception:
                    pass
                current_page = new_page
                print(f"Tab {tab_id} switched to new page after {elapsed:.1f}s")
            else:
                # reload
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

        # send message
        msg = messages[msg_index]
        try:
            # ensure visible
            if not current_page.locator(dm_selector).is_visible():
                print(f"Tab {tab_id} selector not visible, skipping '{msg[:50]}...'")
                time.sleep(0.3)
                msg_index = (msg_index + 1) % len(messages)
                continue

            current_page.click(dm_selector)
            # preserve newlines (ASCII art)
            current_page.fill(dm_selector, msg)
            current_page.press(dm_selector, 'Enter')
            print(f"Tab {tab_id} sent message {msg_index + 1}/{len(messages)}")
            time.sleep(0.3)
        except Exception as e:
            print(f"Tab {tab_id} error sending message {msg_index + 1}: {e}")
            time.sleep(0.3)

        msg_index = (msg_index + 1) % len(messages)

def main():
    global _GLOBAL_PW, _GLOBAL_BROWSER, _GLOBAL_CONTEXT

    parser = argparse.ArgumentParser(description="Instagram DM Auto Sender (fixed)")
    parser.add_argument('--username', required=False, help='Instagram username (required for initial login)')
    parser.add_argument('--password', required=False, help='Instagram password (required for initial login)')
    parser.add_argument('--thread-url', required=True, help='Full Instagram direct thread URL')
    parser.add_argument('--names', nargs='+', required=True, help='Messages list, direct string, or .txt file (split on & or "and")')
    parser.add_argument('--headless', default='true', choices=['true', 'false'], help='Run in headless mode (default: true)')
    parser.add_argument('--storage-state', required=True, help='Path to JSON file for login state (persists session)')
    parser.add_argument('--tabs', type=int, default=1, help='Number of parallel tabs (1-5, default 1)')
    args = parser.parse_args()

    # Normalize names input
    names_raw = sanitize_input(args.names)

    headless = args.headless == 'true'
    storage_path = args.storage_state
    tabs = min(max(args.tabs, 1), 5)

    # If storage state doesn't exist, perform initial login and directly open thread URL there.
    if not os.path.exists(storage_path):
        if not args.username or not args.password:
            print("Error: Username and password required for initial login.")
            return
        ok = do_initial_login(args.thread_url, args.username, args.password, storage_path, headless)
        if not ok:
            print("Initial login failed — please check credentials or 2FA and try again.")
            return
    else:
        print("Using existing storage state, skipping initial login.")

    # Create single Playwright instance / browser / context for all tabs
    try:
        _GLOBAL_PW = sync_playwright().start()
        _GLOBAL_BROWSER = _GLOBAL_PW.chromium.launch(headless=headless)
        _GLOBAL_CONTEXT = _GLOBAL_BROWSER.new_context(storage_state=storage_path)
    except Exception as e:
        print(f"Failed to start Playwright/browser/context: {e}")
        # attempt cleanup
        try:
            if _GLOBAL_BROWSER:
                _GLOBAL_BROWSER.close()
        except Exception:
            pass
        try:
            if _GLOBAL_PW:
                _GLOBAL_PW.stop()
        except Exception:
            pass
        return

    # Parse messages (may raise)
    try:
        messages = parse_messages(names_raw)
    except Exception as e:
        print(f"Error parsing messages: {e}")
        # cleanup
        try:
            if _GLOBAL_CONTEXT:
                _GLOBAL_CONTEXT.close()
        except Exception:
            pass
        try:
            if _GLOBAL_BROWSER:
                _GLOBAL_BROWSER.close()
        except Exception:
            pass
        try:
            if _GLOBAL_PW:
                _GLOBAL_PW.stop()
        except Exception:
            pass
        return

    if not messages:
        print("No messages parsed. Exiting.")
        # cleanup
        try:
            if _GLOBAL_CONTEXT:
                _GLOBAL_CONTEXT.close()
        except Exception:
            pass
        try:
            if _GLOBAL_BROWSER:
                _GLOBAL_BROWSER.close()
        except Exception:
            pass
        try:
            if _GLOBAL_PW:
                _GLOBAL_PW.stop()
        except Exception:
            pass
        return

    print(f"Parsed {len(messages)} messages.")

    # Create pages (tabs) and start threads
    threads = []
    pages = []
    for i in range(tabs):
        try:
            pg = _GLOBAL_CONTEXT.new_page()
            pages.append(pg)
        except Exception as e:
            print(f"Failed to create page {i+1}: {e}")

    # Start sender threads, each receives its own page
    for i, pg in enumerate(pages):
        t = threading.Thread(target=sender_loop, args=(i + 1, pg, args, messages), daemon=True)
        t.start()
        threads.append(t)

    print(f"Starting {len(threads)} tab(s) in infinite message loop. Press Ctrl+C to stop.")
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\nStopping all tabs...")

    # Cleanup
    try:
        if _GLOBAL_CONTEXT:
            _GLOBAL_CONTEXT.close()
    except Exception:
        pass
    try:
        if _GLOBAL_BROWSER:
            _GLOBAL_BROWSER.close()
    except Exception:
        pass
    try:
        if _GLOBAL_PW:
            _GLOBAL_PW.stop()
    except Exception:
        pass

if __name__ == "__main__":
    main()