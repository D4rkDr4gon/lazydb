"""
demo.py — `lazydb --demo`: una base SQLite de ejemplo (una tienda) para probar
la TUI sin servidor (y para las capturas del README).

Perfiles, estado y la base van a una carpeta temporal: no toca
~/.config/lazydb. Los otros perfiles de la lista son de muestra (no
conectan a nada) para ver cómo se ven los motores de red y el túnel SSH.
"""

from __future__ import annotations

import os
import random
import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path

PROFILE = "acme-shop"

CUSTOMERS = [
    ("Ana", "García", "ana.garcia@example.com", "Buenos Aires", "AR"),
    ("Bruno", "Fernández", "bruno.f@example.com", "Córdoba", "AR"),
    ("Carla", "Méndez", "cmendez@example.com", "Rosario", "AR"),
    ("Diego", "Torres", "dtorres@example.com", "Montevideo", "UY"),
    ("Elena", "Ruiz", "elena.ruiz@example.com", "Santiago", "CL"),
    ("Facundo", "López", "flopez@example.com", "Mendoza", "AR"),
    ("Gabriela", "Sosa", "gsosa@example.com", "Asunción", "PY"),
    ("Hernán", "Castro", "hcastro@example.com", "La Plata", "AR"),
    ("Inés", "Romero", "ines.romero@example.com", "Lima", "PE"),
    ("Julián", "Díaz", "jdiaz@example.com", "Bogotá", "CO"),
    ("Karina", "Vega", "kvega@example.com", "Salta", "AR"),
    ("Lucas", "Molina", "lmolina@example.com", "Neuquén", "AR"),
]
PRODUCTS = [
    ("KB-01", "Mechanical keyboard", "Peripherals", 89.90, 42),
    ("MS-02", "Wireless mouse", "Peripherals", 34.50, 120),
    ("MN-27", "27\" 4K monitor", "Monitors", 399.00, 15),
    ("MN-24", "24\" IPS monitor", "Monitors", 189.00, 33),
    ("HS-10", "Noise-cancelling headset", "Audio", 149.00, 27),
    ("SP-05", "USB speakers", "Audio", 45.00, 0),
    ("DK-08", "USB-C dock", "Accessories", 129.00, 18),
    ("CB-01", "HDMI cable 2 m", "Accessories", 9.90, 300),
    ("WC-04", "1080p webcam", "Peripherals", 59.00, 64),
    ("SS-1T", "1 TB NVMe SSD", "Storage", 79.00, 51),
]
STATUS = ["pending", "paid", "shipped", "delivered", "delivered", "delivered", "cancelled"]

SCHEMA = """
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    city TEXT,
    country TEXT NOT NULL DEFAULT 'AR',
    created_at TEXT NOT NULL DEFAULT (date('now'))
);
CREATE TABLE products (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    price NUMERIC NOT NULL CHECK (price >= 0),
    stock INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    ordered_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    notes TEXT
);
CREATE TABLE order_items (
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    sku TEXT NOT NULL REFERENCES products(sku),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price NUMERIC NOT NULL,
    PRIMARY KEY (order_id, sku)
);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_status ON orders(status, ordered_at);
CREATE VIEW order_totals AS
    SELECT o.id AS order_id, c.first_name || ' ' || c.last_name AS customer, o.status, o.ordered_at,
           SUM(i.quantity * i.unit_price) AS total
    FROM orders o JOIN customers c ON c.id = o.customer_id JOIN order_items i ON i.order_id = o.id
    GROUP BY o.id;
CREATE VIEW low_stock AS SELECT sku, name, stock FROM products WHERE stock < 20;
"""

QUERY = """-- F5 runs the statement under the cursor · F9 runs everything
SELECT customer, status, ROUND(total, 2) AS total
FROM order_totals
WHERE status <> 'cancelled'
ORDER BY total DESC;

SELECT * FROM customers WHERE country = 'AR';
"""


def build(path: Path) -> None:
    rnd = random.Random(7)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    start = date(2026, 1, 5)
    db.executemany("INSERT INTO customers (first_name, last_name, email, city, country, created_at) "
                   "VALUES (?, ?, ?, ?, ?, ?)",
                   [(*c, (start + timedelta(days=9 * i)).isoformat()) for i, c in enumerate(CUSTOMERS)])
    db.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?)", PRODUCTS)
    for oid in range(1, 61):
        day = start + timedelta(days=rnd.randint(0, 290))
        note = rnd.choice([None, None, None, "gift wrap", "call before delivery", "invoice A"])
        db.execute("INSERT INTO orders VALUES (?, ?, ?, ?, ?)",
                   (oid, rnd.randint(1, len(CUSTOMERS)), f"{day.isoformat()} {rnd.randint(8, 21):02}:"
                    f"{rnd.randint(0, 59):02}:00", rnd.choice(STATUS), note))
        for p in rnd.sample(PRODUCTS, rnd.randint(1, 4)):
            db.execute("INSERT INTO order_items VALUES (?, ?, ?, ?)", (oid, p[0], rnd.randint(1, 3), p[3]))
    db.commit()
    db.close()


def setup() -> str:
    """Prepara el modo demo antes de crear la app. Devuelve el perfil a conectar."""
    tmp = Path(tempfile.mkdtemp(prefix="lazydb-demo-"))
    os.environ["LAZYDB_CONF"] = str(tmp / "profiles.json")
    os.environ["LAZYDB_STATE"] = str(tmp / "state.json")

    from . import core

    core.CONF, core.STATE = tmp / "profiles.json", tmp / "state.json"
    core.secret_get = lambda name: None
    core.secret_set = lambda name, password: True
    core.secret_clear = lambda name: None

    path = tmp / "acme-shop.sqlite"
    build(path)
    core.save_profiles([
        core.Profile(PROFILE, "sqlite", database=str(path), mode="readwrite"),
        core.Profile("crm-prod", "postgres", host="db.internal.example.com", database="crm", user="reader",
                     ssl="require", ssh_host="bastion.example.com", ssh_user="ops"),
        core.Profile("shop-mariadb", "mysql", host="mariadb.lab.example.org", database="shop", user="app",
                     password="keyring", mode="readwrite"),
        core.Profile("idm-mssql", "mssql", host="sql01.example.com", database="OneIM", user="viewer",
                     ssl="require"),
    ])
    core.save_state({"editor": {PROFILE: QUERY}})
    return PROFILE
