# Price Tracker

A generic, config-driven price tracker that monitors e-commerce websites, stores price history in SQLite, and sends email notifications when prices cross configured thresholds. Define what to scrape using CSS selectors in a JSON config file, and format notifications with [Liquid](https://shopify.github.io/liquid/) templates.

Designed to run as a cron job. Each product has its own schedule (cron expression), so the script can be invoked frequently and each product runs only when its schedule is due.

## Installation

### Dependencies

```bash
pip install requests beautifulsoup4 python-liquid croniter numexpr jsonschema babel
```

### First run

On the first run, the tool creates `~/.price/` with a skeleton config and an example Ceneo product:

```bash
python3 price.py
# Config not found at /home/user/.price/config.json
# Creating skeleton configuration in /home/user/.price...
# Done. Edit /home/user/.price/config.json to configure your price tracking.
```

Edit `~/.price/config.json` with your SMTP credentials and products, then run again.

## Usage

```bash
python3 price.py                  # process products whose schedule is due (silent on success)
python3 price.py --force          # ignore schedules, run all products now
python3 price.py --dry-run        # fetch and display prices, no db writes, no emails
python3 price.py --save-email     # save emails to file instead of sending
python3 price.py --validate       # validate config against schema and exit
python3 price.py --verbose        # show detailed progress (fetching, parsing, db writes)
python3 price.py -q               # suppress all output including errors (useful for cron)
```

### Cron example

```cron
# Every day at 10:15 (matches products with schedule "15 10 * * *")
15 10 * * * bash -l -c 'python3 /path/to/price.py' >> ~/.price/price.log 2>&1

# Every hour (matches any hourly schedule)
0 * * * * bash -l -c 'python3 /path/to/price.py' >> ~/.price/price.log 2>&1
```

## File structure

```
~/.price/
  config.json              # main configuration (sites, products, SMTP)
  templates/               # Liquid email templates
    price-alert
  data/
    price.db               # SQLite database with all price history
    <product_name>          # state file for threshold crossing detection
    .lastrun_<product>      # last run timestamp for schedule tracking
    emails/                 # saved copies of sent emails
```

## Configuration

A [JSON Schema](config.schema.json) is provided for editor autocompletion and validation. The config is validated on every run. If invalid, an error email is sent to all product recipients and the script exits.

The config file (`~/.price/config.json`) has three sections:

### `email` -- SMTP server

```json
"email": {
  "server": {
    "host": "smtp.example.com",
    "port": 587,
    "password": "your-password",
    "email": "you@example.com"
  }
}
```

### `sites` -- Reusable scraping definitions

Each site definition describes how to fetch and parse prices from an e-commerce website.

```json
"ceneo": {
  "name": "Ceneo.pl",
  "url": "https://www.ceneo.pl/{{product_id}}",
  "params": ["product_id"],
  "query": {
    "type": "list",
    "selector": ".product-offer",
    "expect": [".product-offer .price-format"],
    "variables": {
      "shop": {
        "selector": ".store-logo img",
        "value": { "type": "attribute", "name": "alt" }
      },
      "price": {
        "selector": ".price-format",
        "value": { "type": "text", "parse": "money" }
      }
    }
  }
}
```

**Fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `name` | no | Human-readable site name (e.g. "Ceneo.pl") |
| `url` | yes | URL template with Liquid variables, e.g. `https://ceneo.pl/{{product_id}}` |
| `params` | no | List of parameter names used in the URL template |
| `pagination` | no | Pagination config (see [Pagination](#pagination)) |
| `query.type` | yes | `"list"` (multiple offers per page) or `"single"` (one price per page) |
| `query.selector` | yes | CSS selector for the offer container(s) |
| `query.expect` | no | CSS selectors that must exist on the page. Sends error email if missing (detects HTML changes). |
| `query.filter` | no | Filter to exclude offers (see [Filtering](#filtering)) |
| `query.variables` | yes | Named fields to extract (see [Variable extraction](#variable-extraction)) |

### `products` -- What to track

Each product references a site definition and configures notifications.

```json
{
  "site": "ceneo",
  "name": "Kingston FURY Impact 64GB DDR5 5600MHz CL40",
  "params": { "product_id": "147663474" },
  "schedule": "15 10 * * *",
  "subject": "[Ceneo] Price alert: {{ product_name }}",
  "template": "./templates/price-alert",
  "email": "you@example.com",
  "notify": [
    { "test": "{{price}} < 3000" },
    { "test": "{{price}} < 2800" },
    { "test": "{{price}} < 2500" }
  ]
}
```

**Fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `site` | yes | Name of the site definition in `sites` |
| `name` | yes | Product name. Used for display, state files, and database entries. |
| `params` | no | Values for the site's URL template variables |
| `schedule` | no | Cron expression or array of expressions (see [Schedule](#schedule)). If omitted, runs every time. |
| `subject` | no | Liquid template for the email subject line |
| `template` | no | Path to the Liquid template file (relative to `~/.price/`) |
| `email` | no | Recipient email address. Falls back to SMTP sender. |
| `notify` | no | Validator(s) for price alerts (see [Price alerts](#price-alerts)) |

## Variable extraction

Each variable in `query.variables` defines how to extract a value from a matched offer element:

```json
"price": {
  "selector": ".price-format",
  "value": {
    "type": "text",
    "parse": "money"
  }
}
```

**Variable fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `selector` | yes | CSS selector relative to the offer container |
| `value` | yes | How to extract the value (see below) |
| `default` | no | Fallback value when selector doesn't match |
| `sibling` | no | When `true`, search in the next sibling element |

**Value types:**

| Type | Description | Extra fields |
|------|-------------|-------------|
| `text` | Inner text content of the matched element | -- |
| `attribute` | HTML attribute value | `name` -- attribute name (e.g. `"alt"`, `"href"`) |

**Value modifiers:**

| Field | Description |
|-------|-------------|
| `regex` | Extract a capture group from the raw value. Uses group(1) if available. |
| `prefix` | String prepended to the final value. |
| `parse` | `"number"`: plain integers/floats, strips commas as thousands separators. `"money"`: locale-aware currency parsing via [babel](https://babel.pocoo.org/), auto-detects page language from `<html lang>`. |

## Price alerts

Each product can have a `notify` field with validator(s) that trigger email notifications.

### `test` -- Numeric expression

A [numexpr](https://numexpr.readthedocs.io/) expression with Liquid variable placeholders:

```json
"notify": { "test": "{{price}} < 300" }
```

Supported operations:

| Operator | Example |
|----------|---------|
| Comparison | `{{price}} < 300`, `{{price}} >= 2500` |
| AND | `({{price}} < 300) & ({{score}} > 4)` |
| OR | `({{price}} < 200) \| ({{price}} > 5000)` |
| Arithmetic | `{{price}} + {{delivery}} < 350` |

### `match` -- Regex match

Matches a Liquid-rendered variable against a regex:

```json
"notify": {
  "match": {
    "value": "{{shop}}",
    "regex": "allegro"
  }
}
```

Set `"exist": false` to match when the pattern is NOT found.

### Array of validators (OR logic)

When `notify` is an array, the offer triggers an alert if **any** validator passes. This is useful for price threshold steps:

```json
"notify": [
  { "test": "{{price}} < 3000" },
  { "test": "{{price}} < 2800" },
  { "test": "{{price}} < 2500" }
]
```

### Required validators (`require`)

Set `"require": true` to make a validator mandatory (AND logic), while the rest use OR:

```json
"notify": [
  { "require": true, "match": { "value": "{{shop}}", "regex": "allegro|x-kom" } },
  { "test": "{{price}} < 3000" },
  { "test": "{{price}} < 2500" }
]
```

This only alerts for offers from allegro or x-kom that also cross a price threshold.

### Threshold crossing detection

The tracker remembers which offers passed or failed validators on the previous run. An offer triggers a notification when it crosses from failing to passing:

1. Price is 3100 zl -- validator `< 3000` fails -- no alert, saved as `_valid: false`
2. Price drops to 2900 zl -- validator passes, was `_valid: false` -- **alert sent**
3. Price stays at 2900 zl -- validator passes, was `_valid: true` -- no alert
4. Price rises to 3100 zl -- validator fails -- saved as `_valid: false`
5. Price drops to 2800 zl -- validator passes, was `_valid: false` -- **alert sent again**

## Filtering

Exclude offers based on a sub-element's CSS class:

```json
"filter": {
  "selector": ".product-availability",
  "exclude_class": "unavailable"
}
```

Items where the filter selector doesn't match any element are also excluded.

## Expected structure

Detect when a website changes its HTML layout:

```json
"expect": [".product-offer .price-format", ".product-offer .store-logo img"]
```

If any selector is missing from the page, an error email is sent and the product is skipped. This prevents silent failures when a site redesigns.

## Pagination

### `next_link` -- Follow a "next page" link

```json
"pagination": {
  "type": "next_link",
  "selector": "a.next",
  "base_url": "https://example.com/",
  "max_pages": 3
}
```

### `numbered` -- Click through numbered pages

```json
"pagination": {
  "type": "numbered",
  "selector": ".pagination a",
  "active_class": "active",
  "base_url": "https://example.com/",
  "max_pages": 5
}
```

## Schedule

Each product can have a `schedule` field with a cron expression or array of expressions:

```
 +------------ minute (0-59)
 | +---------- hour (0-23)
 | | +-------- day of month (1-31)
 | | | +------ month (1-12)
 | | | | +---- day of week (0-7, 0 and 7 are Sunday)
 | | | | |
 * * * * *
```

| Expression | Meaning |
|------------|---------|
| `15 10 * * *` | Daily at 10:15 |
| `0 */6 * * *` | Every 6 hours |
| `0 8,20 * * *` | Twice daily at 8:00 and 20:00 |

Array form for complex schedules:

```json
"schedule": ["0 8 * * *", "0 20 * * *"]
```

## Email templates

Templates use [Liquid](https://shopify.github.io/liquid/) syntax via [python-liquid](https://github.com/jg-rp/liquid). Available variables:

| Variable | Description |
|----------|-------------|
| `{{ count }}` | Number of matching offers |
| `{{ now }}` | Current date and time |
| `{{ search_url }}` | The rendered product URL |
| `{{ product_name }}` | Product name from config |
| `{{ site_name }}` | Site name from the site definition |
| `{% for item in items %}` | Loop over matching offers |
| `{{ item.index }}` | 1-based position in the list |
| Any extracted variable | e.g. `{{ item.shop }}`, `{{ item.price }}`, `{{ item.delivery }}` |
| Any product `params` | e.g. `{{ product_id }}` |

### Example template

```
Price Alert
Checked at: {{ now }}

Product: {{ product_name }}
URL: {{ search_url }}

{{ count }} offer{% if count != 1 %}s{% endif %} matched:
============================================================
{% for item in items %}

{{ item.index }}. {{ item.shop }}
   Price:    {{ item.price_display }}
   Delivery: {{ item.delivery }}
{% endfor %}

============================================================
```

## Database

Prices are stored in SQLite at `~/.price/data/price.db` with the following schema:

| Table | Purpose |
|-------|---------|
| `website` | Site registry with name and base URL |
| `product` | Product catalog with name, website FK, and full URL |
| `shop` | Retailer names (auto-created on first encounter) |
| `price` | Price history: product, shop, price, score, opinions, delivery, timestamp |

### Querying prices

```sql
-- Latest prices for a product
SELECT s.name AS shop, p.price, p.delivery,
       datetime(p.timestamp, 'unixepoch', 'localtime') AS time
FROM price p
JOIN shop s ON p.shop_id = s.id
JOIN product pr ON p.product_id = pr.id
WHERE pr.name LIKE '%DDR5%'
ORDER BY p.timestamp DESC, p.price ASC
LIMIT 20;

-- Price history for a specific shop
SELECT p.price, datetime(p.timestamp, 'unixepoch', 'localtime') AS time
FROM price p
JOIN shop s ON p.shop_id = s.id
JOIN product pr ON p.product_id = pr.id
WHERE pr.name LIKE '%DDR5%' AND s.name = 'allegro.pl'
ORDER BY p.timestamp;
```

### Web UI

`index.php` provides a Chart.js visualization of price history. It reads from `~/.price/data/price.db` and shows an interactive scatter plot with per-shop price lines.

### Migration from old schema

If you have an existing `price.db` from the pre-refactor version:

```bash
python3 migrate.py
```

This backs up the old database and creates a new one at `~/.price/data/price.db` with the new schema, inferring Ceneo.pl as the website for all existing products.

## Error handling

| Error | Email subject | Behavior |
|-------|--------------|----------|
| Missing dependency | `[price] Missing dependency` | Sends traceback, exits |
| Invalid config | `[price] Invalid configuration` | Sends validation errors, exits |
| HTML structure change (`expect` failed) | `[price] HTML structure changed for '<product>'` | Sends missing selectors, skips product |
| Fatal runtime crash | `[price] Fatal error` | Sends full traceback |

The error email function uses only Python's standard library, so it works even when the crash is caused by a missing dependency.

## How it works

1. On each run, all products in the config are processed sequentially
2. Products whose cron schedule doesn't match the current time are skipped
3. For each product, the site URL is rendered and the page is fetched
4. The page language is detected from `<html lang>` for locale-aware price parsing
5. Offers are extracted using CSS selectors and stored in the SQLite database
6. If `notify` validators are configured, offers are checked against thresholds
7. Threshold crossings (was failing, now passing) trigger an email notification
8. All offers are saved in a state file with `_valid` flags for next-run comparison

## License

Copyright (C) 2020-2026 [Jakub T. Jankiewicz](https://jakub.jankiewicz.org)<br/>
Released under [MIT](https://opensource.org/licenses/MIT) license
