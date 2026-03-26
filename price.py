#!/usr/bin/env python3

"""
Generic price tracker.

Reads product definitions from ~/.price/config.json, scrapes prices from
e-commerce sites using CSS selectors, stores history in SQLite, and sends
email notifications when prices cross configured thresholds.

Usage:
    price.py                  # process products whose schedule is due
    price.py --force          # ignore schedules, run all products now
    price.py --dry-run        # fetch and display prices, no db writes, no emails
    price.py --save-email     # save emails to file instead of sending
    price.py --validate       # validate config against schema and exit
    price.py --verbose        # show detailed progress output
    price.py -q               # run silently (for cron)

Designed to run as a daily cron job.
"""

# === Standard library imports (always available) ===
import argparse
import json
import os
import re
import shutil
import smtplib
import sqlite3
import sys
import traceback
from datetime import datetime
from email.message import EmailMessage
from urllib.parse import urljoin

# ========================= PATHS =========================
PRICE_DIR = os.path.expanduser("~/.price")
CONFIG_FILE = os.path.join(PRICE_DIR, "config.json")
TEMPLATES_DIR = os.path.join(PRICE_DIR, "templates")
DATA_DIR = os.path.join(PRICE_DIR, "data")
DB_FILE = os.path.join(DATA_DIR, "price.db")
SKELETON_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "skeleton")
SCHEMA_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "config.schema.json")
INIT_SQL = os.path.join(os.path.dirname(os.path.realpath(__file__)), "init.sql")

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"

verbose = False


def log(msg):
    """Print a message only when --verbose is enabled."""
    if verbose:
        print(msg)

# =========================================================


def send_error_email(subject, body):
    """
    Send an error notification using only stdlib.

    Reads SMTP config and recipient emails directly from config.json.
    This function must not depend on any third-party library so it works
    even when the error is a missing dependency.
    """
    try:
        if not os.path.exists(CONFIG_FILE):
            return

        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)

        server_config = config.get("email", {}).get("server", {})
        host = server_config.get("host")
        port = server_config.get("port", 587)
        sender = server_config.get("email")
        password = server_config.get("password")

        if not all([host, sender, password]):
            return

        recipients = set()
        for product in config.get("products", []):
            email = product.get("email")
            if email:
                recipients.add(email)
        if not recipients:
            recipients.add(sender)

        for recipient in recipients:
            msg = EmailMessage()
            msg["From"] = f"Price Tracker <{sender}>"
            msg["To"] = recipient
            msg["Subject"] = subject
            msg.set_content(body)

            with smtplib.SMTP(host, port, timeout=30) as server:
                server.starttls()
                server.login(sender, password)
                server.send_message(msg)

    except Exception:
        traceback.print_exc()


# === Third-party imports ===
try:
    import requests
    from liquid import Environment as LiquidEnvironment
    import numexpr
    from babel import Locale
    from babel.numbers import parse_decimal
    from croniter import croniter
    from bs4 import BeautifulSoup
    from jsonschema import Draft202012Validator
except ImportError:
    tb = traceback.format_exc()
    print(tb, file=sys.stderr)
    send_error_email(
        "[price] Missing dependency",
        f"The price tracker failed to start at {datetime.now()}.\n\n{tb}",
    )
    sys.exit(1)


# ========================= INIT =========================

def init_config():
    """Create ~/.price with skeleton files if config is missing."""
    if os.path.exists(CONFIG_FILE):
        return

    print(f"Config not found at {CONFIG_FILE}")
    print(f"Creating skeleton configuration in {PRICE_DIR}...")

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(TEMPLATES_DIR, exist_ok=True)

    if os.path.isdir(SKELETON_DIR):
        src_config = os.path.join(SKELETON_DIR, "config.json")
        if os.path.exists(src_config):
            shutil.copy2(src_config, CONFIG_FILE)

        src_templates = os.path.join(SKELETON_DIR, "templates")
        if os.path.isdir(src_templates):
            for name in os.listdir(src_templates):
                src = os.path.join(src_templates, name)
                dst = os.path.join(TEMPLATES_DIR, name)
                if os.path.isfile(src) and not os.path.exists(dst):
                    shutil.copy2(src, dst)
    else:
        minimal = {
            "email": {
                "server": {
                    "host": "smtp.example.com",
                    "port": 587,
                    "password": "your-password-here",
                    "email": "you@example.com",
                }
            },
            "sites": {},
            "products": [],
        }
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(minimal, f, ensure_ascii=False, indent=2)

    print(f"Done. Edit {CONFIG_FILE} to configure your price tracking.")
    sys.exit(0)


def load_config():
    """Load the configuration file."""
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_config(config):
    """Validate config against the JSON Schema."""
    if not os.path.exists(SCHEMA_FILE):
        print(f"Warning: Schema file not found at {SCHEMA_FILE}, skipping validation.", file=sys.stderr)
        return

    with open(SCHEMA_FILE, "r", encoding="utf-8") as f:
        schema = json.load(f)

    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(config))

    if not errors:
        return

    lines = [f"Config validation failed with {len(errors)} error(s):\n"]
    for i, err in enumerate(errors, 1):
        path = ".".join(str(p) for p in err.absolute_path) or "(root)"
        lines.append(f"  {i}. [{path}] {err.message}")

    msg = "\n".join(lines)
    print(msg, file=sys.stderr)
    send_error_email(
        "[price] Invalid configuration",
        f"The price tracker config at {CONFIG_FILE} is invalid.\n\n{msg}",
    )
    sys.exit(1)


# ========================= DATABASE =========================

def init_db():
    """Initialize the SQLite database from init.sql."""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON")
    with open(INIT_SQL, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    return conn


def get_or_create_website(conn, name, base_url):
    """Get or create a website record, return its id."""
    row = conn.execute("SELECT id FROM website WHERE url = ?", (base_url,)).fetchone()
    if row:
        return row[0]
    conn.execute("INSERT INTO website(name, url) VALUES (?, ?)", (name, base_url))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_or_create_product(conn, name, website_id, url):
    """Get or create a product record, return its id."""
    row = conn.execute("SELECT id FROM product WHERE url = ?", (url,)).fetchone()
    if row:
        return row[0]
    conn.execute(
        "INSERT INTO product(name, website_id, url) VALUES (?, ?, ?)",
        (name, website_id, url),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_or_create_shop(conn, name):
    """Get or create a shop record, return its id."""
    row = conn.execute("SELECT id FROM shop WHERE name = ?", (name,)).fetchone()
    if row:
        return row[0]
    conn.execute("INSERT INTO shop(name) VALUES (?)", (name,))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def store_prices(conn, product_id, items):
    """Store price records for all items."""
    timestamp = int(datetime.now().timestamp())
    for item in items:
        shop_name = item.get("shop", "Unknown")
        shop_id = get_or_create_shop(conn, shop_name)
        price_val = item.get("price", 0)
        score = item.get("score")
        if score == "":
            score = None
        opinions = item.get("opinions")
        if opinions == "" or opinions == "0":
            opinions = None
        available = 1 if item.get("available", "") else 0
        delivery = item.get("delivery", "")

        conn.execute(
            """INSERT INTO price (product_id, shop_id, price, score, opinions,
               available, delivery, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (product_id, shop_id, price_val, score, opinions, available, delivery, timestamp),
        )
    conn.commit()


# ========================= SCRAPING =========================

liquid = LiquidEnvironment()


def render_url(url_template, params):
    """Render a URL template with Liquid params."""
    return liquid.from_string(url_template).render(**params)


def detect_language(html, response_headers=None):
    """Detect the page language from HTML lang attribute or Content-Language header."""
    soup = BeautifulSoup(html[:2000], "html.parser")
    html_tag = soup.find("html")
    lang = str(html_tag.get("lang", "")) if html_tag else ""

    if not lang and response_headers:
        lang = response_headers.get("Content-Language", "")

    lang = lang.strip().split(",")[0].strip() if lang else "en"

    try:
        return Locale.parse(lang.replace("-", "_"))
    except Exception:
        return Locale.parse("en")


def fetch_page(url):
    """Fetch a single page and return its HTML and detected locale."""
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    locale = detect_language(resp.text, resp.headers)
    return resp.text, locale


def extract_value(element, value_spec, default=None, locale=None):
    """Extract a value from a BeautifulSoup element based on the value spec."""
    if element is None:
        return default

    if value_spec["type"] == "text":
        raw = element.get_text(strip=True)
    elif value_spec["type"] == "attribute":
        raw = element.get(value_spec["name"], "")
        if raw is None:
            raw = ""
    else:
        return default

    if not raw:
        return default if default is not None else raw

    regex = value_spec.get("regex")
    if regex:
        match = re.search(regex, raw)
        if match:
            raw = match.group(1) if match.lastindex else match.group(0)
        else:
            return default if default is not None else ""

    prefix = value_spec.get("prefix")
    if prefix:
        raw = prefix + raw

    parse = value_spec.get("parse")
    if parse == "number":
        raw = parse_number(raw)
    elif parse == "money":
        raw = parse_money(raw, locale=locale)

    return raw


def parse_number(value):
    """Parse a plain numeric string into int or float."""
    if isinstance(value, (int, float)):
        return value
    s = str(value).strip()
    s = re.sub(r"[^\d,.\-+]", "", s)
    s = s.replace(",", "")
    if not s:
        return 0
    try:
        result = float(s)
        if result == int(result):
            return int(result)
        return result
    except (ValueError, TypeError):
        return 0


def parse_money(value, locale=None):
    """Parse a monetary string into a float using locale-aware parsing via babel."""
    if isinstance(value, (int, float)):
        return value
    s = str(value).strip()
    s = re.sub(r"^[^\d\-+]+", "", s)
    s = re.sub(r"[^\d,.]+$", "", s)
    s = s.replace("\xa0", " ").strip()

    if not s:
        return 0.0

    if locale is None:
        locale = Locale.parse("en")

    try:
        return float(parse_decimal(s, locale=locale))
    except Exception:
        s = re.sub(r"[^\d.\-]", "", s)
        try:
            return float(s)
        except (ValueError, TypeError):
            return 0.0


def extract_variables(element, variables_spec, locale=None):
    """Extract all defined variables from an element."""
    data = {}
    for var_name, var_spec in variables_spec.items():
        search_root = element
        if var_spec.get("sibling"):
            sibling = element.find_next_sibling()
            while (
                sibling
                and sibling.get("class")
                and "spacer" in sibling.get("class", [])
            ):
                sibling = sibling.find_next_sibling()
            search_root = sibling

        if search_root is None:
            data[var_name] = var_spec.get("default", "")
            continue

        sub_element = search_root.select_one(var_spec["selector"])
        default = var_spec.get("default")
        value = extract_value(sub_element, var_spec["value"], default, locale=locale)
        data[var_name] = value if value is not None else ""
    return data


def check_expect(html, expect_selectors, url):
    """Verify that expected CSS selectors exist on the page."""
    if not expect_selectors:
        return []

    soup = BeautifulSoup(html, "html.parser")
    missing = []
    for selector in expect_selectors:
        if not soup.select_one(selector):
            missing.append(selector)
    return missing


def should_include(element, filter_spec):
    """Check if an element passes the filter."""
    if not filter_spec:
        return True

    target = element.select_one(filter_spec["selector"])
    if target is None:
        return False

    exclude_class = filter_spec.get("exclude_class")
    if exclude_class:
        classes = target.get("class") or []
        if exclude_class in classes:
            return False

    return True


def parse_items(html, query_spec, locale=None):
    """Parse items from HTML based on query specification."""
    soup = BeautifulSoup(html, "html.parser")
    query_type = query_spec["type"]
    selector = query_spec["selector"]
    variables = query_spec.get("variables", {})
    filter_spec = query_spec.get("filter")

    if query_type == "single":
        element = soup.select_one(selector)
        if element is None:
            return []
        if not should_include(element, filter_spec):
            return []
        data = extract_variables(element, variables, locale=locale)
        return [data]

    elif query_type == "list":
        elements = soup.select(selector)
        items = []
        for el in elements:
            if not should_include(el, filter_spec):
                continue
            data = extract_variables(el, variables, locale=locale)
            items.append(data)
        return items

    return []


def find_next_page_url(html, pagination_spec, current_url):
    """Find the next page URL based on pagination config."""
    if not pagination_spec:
        return None

    soup = BeautifulSoup(html, "html.parser")
    base_url = pagination_spec.get("base_url", current_url)
    pag_type = pagination_spec.get("type", "next_link")

    if pag_type == "next_link":
        link = soup.select_one(pagination_spec["selector"])
        if link:
            href = str(link.get("href", ""))
            if href:
                return urljoin(base_url, href)
        return None

    elif pag_type == "numbered":
        all_pages = soup.select(pagination_spec["selector"])
        active_class = pagination_spec.get("active_class", "")
        found_active = False
        for page_link in all_pages:
            classes = page_link.get("class") or []
            if active_class and active_class in classes:
                found_active = True
                continue
            if found_active:
                href = str(page_link.get("href", ""))
                if href:
                    return urljoin(base_url, href)
                break
        return None

    return None


def fetch_all_items(site_def, params):
    """
    Fetch all items across all pages for a site definition.
    Returns list of item dicts.

    Raises ValueError if 'expect' selectors are missing from the page.
    """
    pagination_spec = site_def.get("pagination")
    query_spec = site_def["query"]
    max_pages = pagination_spec.get("max_pages", 1) if pagination_spec else 1
    expect_selectors = query_spec.get("expect")
    all_items = []
    page_num = 1
    url = render_url(site_def["url"], params)

    while page_num <= max_pages:
        log(f"  [{datetime.now()}] Fetching page {page_num}: {url}")
        html, locale = fetch_page(url)

        if page_num == 1 and expect_selectors:
            missing = check_expect(html, expect_selectors, url)
            if missing:
                raise ValueError(
                    f"HTML structure changed at {url}. "
                    f"Missing expected selector(s): {', '.join(missing)}"
                )

        items = parse_items(html, query_spec, locale=locale)

        if not items:
            break

        all_items.extend(items)

        next_url = find_next_page_url(html, pagination_spec, url)
        if next_url:
            url = next_url
            page_num += 1
        else:
            break

    return all_items


# ========================= TEMPLATES & EMAIL =========================

def load_template(template_path):
    """Load a Liquid template file."""
    if not os.path.isabs(template_path):
        template_path = os.path.join(PRICE_DIR, template_path)

    if not os.path.exists(template_path):
        print(f"Warning: Template not found at {template_path}", file=sys.stderr)
        return None

    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()


def render_email(template_str, subject_template, items, params, site_def, product_name):
    """Render the email body and subject using Liquid templates."""
    search_url = render_url(site_def["url"], params)

    indexed_items = []
    for i, item in enumerate(items, 1):
        item_copy = dict(item)
        item_copy["index"] = i
        indexed_items.append(item_copy)

    context = dict(params)
    context["items"] = indexed_items
    context["count"] = len(items)
    context["now"] = str(datetime.now())
    context["search_url"] = search_url
    context["product_name"] = product_name
    context["site_name"] = site_def.get("name", "")

    body = liquid.from_string(template_str).render(**context)
    subject = liquid.from_string(subject_template).render(**context) if subject_template else ""

    return subject, body


def send_email(config, recipient, subject, body):
    """Send an email notification."""
    server_config = config["email"]["server"]
    sender = server_config["email"]

    msg = EmailMessage()
    msg["From"] = f"Price Tracker <{sender}>"
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(server_config["host"], server_config["port"]) as server:
            server.starttls()
            server.login(sender, server_config["password"])
            server.send_message(msg)
        log(f"  [{datetime.now()}] Email sent to {recipient}")
    except Exception as e:
        print(f"Error: Failed to send email: {e}", file=sys.stderr)


def save_email_to_file(product_name, subject, body):
    """Save email to a file for debugging/verification."""
    email_dir = os.path.join(DATA_DIR, "emails")
    os.makedirs(email_dir, exist_ok=True)
    safe_name = re.sub(r"[^\w\-]", "_", product_name)
    email_file = os.path.join(email_dir, f"{safe_name}.txt")
    with open(email_file, "w", encoding="utf-8") as f:
        f.write(f"Subject: {subject}\n")
        f.write(f"Date: {datetime.now()}\n")
        f.write("=" * 60 + "\n\n")
        f.write(body)
    log(f"  [{datetime.now()}] Email saved to {email_file}")


# ========================= VALIDATORS =========================

def evaluate_single_validator(validator, item):
    """Evaluate a single validator object against an item's variables."""
    test_expr = validator.get("test")
    if test_expr:
        try:
            rendered = liquid.from_string(test_expr).render(**item)
            if not bool(numexpr.evaluate(rendered)):
                return False
        except Exception as e:
            print(f"Warning: Validator test failed for '{test_expr}': {e}", file=sys.stderr)
            return False

    match_spec = validator.get("match")
    if match_spec:
        match_list = match_spec if isinstance(match_spec, list) else [match_spec]
        for m in match_list:
            try:
                value = liquid.from_string(m["value"]).render(**item)
                pattern = m["regex"]
                should_exist = m.get("exist", True)
                matched = bool(re.search(pattern, value))
                if not should_exist:
                    matched = not matched
                if not matched:
                    return False
            except Exception as e:
                print(f"Warning: Validator match failed for '{m}': {e}", file=sys.stderr)
                return False

    return True


def evaluate_validator(validator, item):
    """Evaluate a validator against an item's variables."""
    if not validator:
        return True

    if isinstance(validator, list):
        required = [v for v in validator if v.get("require")]
        optional = [v for v in validator if not v.get("require")]

        for v in required:
            if not evaluate_single_validator(v, item):
                return False

        if optional:
            return any(evaluate_single_validator(v, item) for v in optional)

        return True

    return evaluate_single_validator(validator, item)


# ========================= SCHEDULING =========================

def load_last_run(product_name):
    """Load the last run timestamp for a product."""
    safe_name = re.sub(r"[^\w\-]", "_", product_name)
    run_file = os.path.join(DATA_DIR, f".lastrun_{safe_name}")
    if os.path.exists(run_file):
        try:
            with open(run_file, "r", encoding="utf-8") as f:
                return datetime.fromisoformat(f.read().strip())
        except (ValueError, IOError):
            pass
    return None


def save_last_run(product_name):
    """Save the current timestamp as last run for a product."""
    safe_name = re.sub(r"[^\w\-]", "_", product_name)
    run_file = os.path.join(DATA_DIR, f".lastrun_{safe_name}")
    with open(run_file, "w", encoding="utf-8") as f:
        f.write(datetime.now().isoformat())


def should_run_now(product):
    """Check if a product should run based on its cron schedule."""
    schedule = product.get("schedule")
    if not schedule:
        return True

    now = datetime.now().replace(second=0, microsecond=0)

    schedules = schedule if isinstance(schedule, list) else [schedule]
    if not any(croniter.match(s, now) for s in schedules):
        return False

    product_name = product["name"]
    last_run = load_last_run(product_name)
    if last_run is not None:
        last_run_minute = last_run.replace(second=0, microsecond=0)
        if last_run_minute >= now:
            return False

    return True


# ========================= STATE (for threshold crossing) =========================

def load_state(product_name):
    """Load previous state for a product."""
    safe_name = re.sub(r"[^\w\-]", "_", product_name)
    state_file = os.path.join(DATA_DIR, safe_name)
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []


def save_state(product_name, items):
    """Save state for a product."""
    safe_name = re.sub(r"[^\w\-]", "_", product_name)
    state_file = os.path.join(DATA_DIR, safe_name)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


# ========================= MAIN PROCESSING =========================

def process_product(config, product, conn, save_only=False):
    """Process a single product: fetch prices, store in db, notify if thresholds crossed."""
    product_name = product["name"]
    site_name = product["site"]
    params = product.get("params", {})
    recipient = product.get("email", config["email"]["server"]["email"])
    template_path = product.get("template", "")
    subject_template = product.get("subject", "")
    validator = product.get("notify")

    log(f"\n[{datetime.now()}] Processing product: '{product_name}' (site: {site_name})")

    site_def = config["sites"].get(site_name)
    if not site_def:
        print(f"Error: Site '{site_name}' not found in config.sites", file=sys.stderr)
        return

    # Fetch items
    try:
        items = fetch_all_items(site_def, params)
    except ValueError as e:
        msg = str(e)
        print(f"Warning: {msg}", file=sys.stderr)
        send_error_email(
            f"[price] HTML structure changed for '{product_name}'",
            f"Product '{product_name}' (site: {site_name}) detected a page structure change.\n\n{msg}",
        )
        return
    except Exception as e:
        print(f"Error: fetching data for '{product_name}': {e}", file=sys.stderr)
        return

    log(f"  [{datetime.now()}] Found {len(items)} offer(s).")

    if not items:
        log(f"  [{datetime.now()}] No offers found. Nothing to do.")
        save_last_run(product_name)
        return

    # Store prices in database
    url = render_url(site_def["url"], params)
    website_id = get_or_create_website(conn, site_def.get("name", site_name), site_def["url"])
    product_id = get_or_create_product(conn, product_name, website_id, url)
    store_prices(conn, product_id, items)
    log(f"  [{datetime.now()}] Stored {len(items)} price(s) in database.")

    # Add shop as ID for each item (for state tracking)
    for item in items:
        if "id" not in item:
            item["id"] = item.get("shop", str(hash(frozenset(item.items()))))

    # Mark each item with validator result
    for item in items:
        item["_valid"] = evaluate_validator(validator, item) if validator else True

    valid_count = sum(1 for i in items if i["_valid"])
    if validator and valid_count != len(items):
        log(f"  [{datetime.now()}] Validator: {valid_count}/{len(items)} offer(s) passed")

    # Threshold crossing detection
    known_items = load_state(product_name)
    known_by_id = {}
    for item in known_items:
        if "id" in item:
            known_by_id[item["id"]] = item

    notify_items = []
    for item in items:
        item_id = item.get("id")
        if not item["_valid"]:
            continue
        prev = known_by_id.get(item_id)
        if prev is None:
            notify_items.append(item)
        elif not prev.get("_valid", True):
            notify_items.append(item)

    if notify_items:
        log(f"  [{datetime.now()}] {len(notify_items)} offer(s) to notify about!")

        template_str = load_template(template_path) if template_path else None
        if template_str:
            subject, body = render_email(
                template_str, subject_template, notify_items, params, site_def, product_name
            )
            if save_only:
                save_email_to_file(product_name, subject, body)
            else:
                send_email(config, recipient, subject, body)
                save_email_to_file(product_name, subject, body)
        else:
            if template_path:
                print(f"Warning: No template found, skipping email.", file=sys.stderr)
    else:
        log(f"  [{datetime.now()}] No threshold crossings to notify about.")

    save_state(product_name, items)
    save_last_run(product_name)
    log(f"  [{datetime.now()}] State saved for '{product_name}'")


def main():
    parser = argparse.ArgumentParser(
        description="Generic price tracker and email notifier."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and display prices but don't store or send emails",
    )
    parser.add_argument(
        "--save-email",
        action="store_true",
        help="Save email to file instead of sending",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore schedules and run all products immediately",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate the config file against the schema and exit",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed progress output",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress all output",
    )
    args = parser.parse_args()

    global verbose
    verbose = args.verbose

    if args.quiet:
        sys.stdout = open(os.devnull, "w")
        sys.stderr = open(os.devnull, "w")

    init_config()
    config = load_config()

    if args.validate:
        validate_config(config)
        print(f"Config at {CONFIG_FILE} is valid.")
        return

    validate_config(config)

    products = config.get("products", [])
    if not products:
        log("No products to process.")
        return

    conn = init_db()

    log(f"[{datetime.now()}] Processing {len(products)} product(s)...")

    for product in products:
        product_name = product["name"]

        if not args.force and not args.dry_run and not should_run_now(product):
            schedule = product.get("schedule", "")
            log(f"\n[{datetime.now()}] Skipping '{product_name}' (schedule: {schedule})")
            continue

        if args.dry_run:
            site_name = product["site"]
            site_def = config["sites"].get(site_name)
            if not site_def:
                print(f"Error: Site '{site_name}' not found", file=sys.stderr)
                continue
            print(f"\n[DRY RUN] Product: '{product_name}'")
            params = product.get("params", {})
            validator = product.get("notify")
            try:
                items = fetch_all_items(site_def, params)
                if validator:
                    items = [i for i in items if evaluate_validator(validator, i)]
                print(f"  Found {len(items)} offer(s)")
                for item in items[:5]:
                    shop = item.get("shop", "?")
                    price = item.get("price_display", item.get("price", "?"))
                    print(f"    {shop:30s} {price}")
                if len(items) > 5:
                    print(f"    ... and {len(items) - 5} more")
            except Exception as e:
                print(f"  Error: {e}", file=sys.stderr)
        else:
            process_product(config, product, conn, save_only=args.save_email)

    conn.close()
    log(f"\n[{datetime.now()}] Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        send_error_email(
            "[price] Fatal error",
            f"The price tracker crashed at {datetime.now()}.\n\n{tb}",
        )
        sys.exit(1)
