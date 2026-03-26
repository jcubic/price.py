#!/usr/bin/env python3

"""
Migrate the old price.db schema to the new one.

- Creates ~/.price/data/ directory
- Backs up the old database
- Creates new schema with website table
- Copies all existing data, inferring Ceneo.pl as the website
"""

import os
import shutil
import sqlite3
import sys

OLD_DB = os.path.join(os.path.dirname(os.path.realpath(__file__)), "price.db")
NEW_DIR = os.path.expanduser("~/.price/data")
NEW_DB = os.path.join(NEW_DIR, "price.db")
INIT_SQL = os.path.join(os.path.dirname(os.path.realpath(__file__)), "init.sql")


def migrate():
    if not os.path.exists(OLD_DB):
        print(f"Old database not found at {OLD_DB}, nothing to migrate.")
        return

    if os.path.exists(NEW_DB):
        print(f"New database already exists at {NEW_DB}.")
        print("Delete it first if you want to re-run migration.")
        sys.exit(1)

    # Create target directory
    os.makedirs(NEW_DIR, exist_ok=True)

    # Backup old db
    backup = OLD_DB + ".bak"
    if not os.path.exists(backup):
        shutil.copy2(OLD_DB, backup)
        print(f"Backed up {OLD_DB} -> {backup}")

    # Open old database
    old_conn = sqlite3.connect(OLD_DB)
    old_conn.row_factory = sqlite3.Row

    # Create new database with new schema
    new_conn = sqlite3.connect(NEW_DB)
    new_conn.execute("PRAGMA foreign_keys = ON")
    with open(INIT_SQL, "r", encoding="utf-8") as f:
        new_conn.executescript(f.read())
    new_conn.commit()

    # Create default website entry for Ceneo
    new_conn.execute(
        "INSERT INTO website(name, url) VALUES (?, ?)",
        ("Ceneo.pl", "https://www.ceneo.pl"),
    )
    new_conn.commit()
    website_id = new_conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Migrate shops (handle duplicates in old data)
    shop_map = {}  # old_id -> new_id
    shop_name_to_new_id = {}
    old_shops = old_conn.execute("SELECT id, name FROM shop").fetchall()
    for shop in old_shops:
        name = shop["name"]
        if name in shop_name_to_new_id:
            shop_map[shop["id"]] = shop_name_to_new_id[name]
        else:
            new_conn.execute("INSERT INTO shop(name) VALUES (?)", (name,))
            new_conn.commit()
            new_id = new_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            shop_map[shop["id"]] = new_id
            shop_name_to_new_id[name] = new_id
    print(f"Migrated {len(shop_name_to_new_id)} unique shops (from {len(old_shops)} old entries)")

    # Migrate products
    product_map = {}  # old_id -> new_id
    old_products = old_conn.execute("SELECT id, name FROM product").fetchall()
    for prod in old_products:
        # Infer URL — we don't have the original URL, use a placeholder
        url = f"https://www.ceneo.pl/product/{prod['id']}"
        new_conn.execute(
            "INSERT INTO product(name, website_id, url) VALUES (?, ?, ?)",
            (prod["name"], website_id, url),
        )
        new_conn.commit()
        new_id = new_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        product_map[prod["id"]] = new_id
    print(f"Migrated {len(old_products)} products")

    # Migrate prices (join with old time table for timestamp)
    count = 0
    old_prices = old_conn.execute(
        """SELECT p.shop, p.product, p.price, p.score, p.opinions,
                  p.avaiable, p.delivery, t.time as timestamp
           FROM price p
           JOIN time t ON p.time = t.id"""
    ).fetchall()

    for row in old_prices:
        old_shop_id = row["shop"]
        old_product_id = row["product"]

        new_shop_id = shop_map.get(old_shop_id)
        new_product_id = product_map.get(old_product_id)

        if new_shop_id is None or new_product_id is None:
            continue

        new_conn.execute(
            """INSERT INTO price (product_id, shop_id, price, score, opinions,
               available, delivery, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_product_id,
                new_shop_id,
                row["price"],
                row["score"],
                row["opinions"],
                row["avaiable"],
                row["delivery"],
                row["timestamp"],
            ),
        )
        count += 1

    new_conn.commit()
    print(f"Migrated {count} price records")

    old_conn.close()
    new_conn.close()

    print(f"\nMigration complete. New database at {NEW_DB}")


if __name__ == "__main__":
    migrate()
