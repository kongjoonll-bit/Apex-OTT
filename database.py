# -*- coding: utf-8 -*-
"""APEX OTT — SQLite 데이터 계층

주문은 '구매 즉시 자동 지급'이 아니라 아래 흐름을 따른다.

    paid  ──(관리자 지급)──▶ delivered
      │
      └──(관리자 취소)──▶ cancelled  (포인트/재고 복구)

paid 상태에서만 지급/취소할 수 있고, 모든 잔액 변동은 transactions 에 기록된다.
"""
import json
import os
import random
import secrets
import sqlite3
import string
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_PATH = os.environ.get('APEX_DB_PATH') or os.path.join(DATA_DIR, 'apex_ott.db')
LEGACY_DB_PATH = os.path.join(DATA_DIR, 'vending.db')

# ============ 기본 설정 ============
DEFAULT_SETTINGS = {
    'store_name': 'APEX OTT',
    'store_tagline': 'PREMIUM OTT STORE',
    'admin_password': os.environ.get('ADMIN_PASSWORD', ''),
    'purchase_log_channel_id': '',
    'charge_log_channel_id': '',
    'ticket_category_id': '',
    'notice_channel_id': '',
    'ticket_ping_role_id': '',
    'bank_name': '토스뱅크',
    'bank_account': '',
    'bank_holder': '',
    'deposit_webhook_key': '',
    'delivery_window_minutes': '30',
    'card_image_enabled': 'true',
    'charge_expire_minutes': '30',
    'store_notice': '구매 후 30분 이내에 관리자가 확인하여 DM으로 상품을 전달합니다.',
    'review_channel_id': '',
    'panel_banner_url': '',
    'panel_channel_id': '',
    'panel_message_id': '',
    'panel_image_message_id': '',
}

# ============ 기본 상품 (OTT) ============
# (카테고리, 상품명, 가격, 설명)
DEFAULT_PRODUCTS = [
    ('넷플릭스', '넷플릭스 프리미엄 1개월', 12000, '4K UHD · 4인 프로필 · 1개월 이용'),
    ('넷플릭스', '넷플릭스 스탠다드 1개월', 8000, 'FHD · 2인 프로필 · 1개월 이용'),
    ('넷플릭스', '넷플릭스 7일 이용권', 3500, 'FHD · 1인 프로필 · 7일 이용'),
    ('디즈니+', '디즈니+ 프리미엄 1개월', 7000, '4K UHD · 4인 프로필 · 1개월 이용'),
    ('디즈니+', '디즈니+ 프리미엄 3개월', 18000, '4K UHD · 4인 프로필 · 3개월 이용'),
    ('디즈니+', '디즈니+ 7일 이용권', 3000, 'FHD · 1인 프로필 · 7일 이용'),
    ('티빙', '티빙 스탠다드 1개월', 6500, 'FHD · 2인 동시 시청 · 1개월 이용'),
    ('티빙', '티빙 프리미엄 1개월', 9500, '4K · 4인 동시 시청 · 1개월 이용'),
    ('티빙', '티빙 프리미엄 3개월', 25000, '4K · 4인 동시 시청 · 3개월 이용'),
    ('웨이브', '웨이브 베이직 1개월', 5500, 'FHD · 1인 이용 · 1개월'),
    ('웨이브', '웨이브 프리미엄 1개월', 8500, '4K · 4인 동시 시청 · 1개월'),
    ('웨이브', '웨이브 프리미엄 3개월', 22000, '4K · 4인 동시 시청 · 3개월'),
    ('왓챠', '왓챠 베이직 1개월', 4500, 'FHD · 1인 이용 · 1개월'),
    ('왓챠', '왓챠 프리미엄 1개월', 8000, '4K · 4인 동시 시청 · 1개월'),
    ('쿠팡플레이', '쿠팡플레이 1개월', 4000, 'FHD · 1인 이용 · 1개월'),
    ('쿠팡플레이', '쿠팡플레이 3개월', 10000, 'FHD · 1인 이용 · 3개월'),
    ('애플TV+', '애플TV+ 1개월', 6000, '4K HDR · 가족 공유 · 1개월'),
    ('애플TV+', '애플TV+ 3개월', 15000, '4K HDR · 가족 공유 · 3개월'),
    ('프라임비디오', '프라임비디오 1개월', 4500, 'FHD · 1인 이용 · 1개월'),
    ('라프텔', '라프텔 1개월', 4500, 'FHD · 애니메이션 전용 · 1개월'),
    ('라프텔', '라프텔 3개월', 11000, 'FHD · 애니메이션 전용 · 3개월'),
    ('U+모바일tv', 'U+모바일tv 1개월', 5000, 'FHD · 1인 이용 · 1개월'),
    ('음악·기타', '스포티파이 프리미엄 1개월', 7000, '개인 계정 · 1개월'),
    ('음악·기타', '멜론 스트리밍 1개월', 5000, '스트리밍 이용권 · 1개월'),
    ('음악·기타', '유튜브 프리미엄 1개월', 6500, '개인 계정 · 1개월'),
    ('음악·기타', 'ChatGPT Plus 1개월', 12000, '개인 계정 · 1개월'),
]

ORDER_STATUS_LABEL = {
    'paid': '구매 완료',
    'delivered': '지급 완료',
    'cancelled': '취소',
}

DELIVERY_TYPE_LABEL = {
    'idpw': 'ID · PW',
    'key': 'KEY',
    'note': '기타',
}


def now_utc():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def _row(row):
    return dict(row) if row is not None else None


def _rows(rows):
    return [dict(r) for r in rows]


# ============ 스키마 ============
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    price INTEGER NOT NULL,
    category TEXT DEFAULT '',
    description TEXT DEFAULT '',
    stock INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL DEFAULT '',
    balance INTEGER DEFAULT 0,
    total_spent INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT UNIQUE,
    user_id TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    product_id INTEGER,
    product_name TEXT NOT NULL,
    category TEXT DEFAULT '',
    amount INTEGER NOT NULL,
    status TEXT DEFAULT 'paid',
    delivery_type TEXT DEFAULT NULL,
    delivery_json TEXT DEFAULT NULL,
    delivery_message TEXT DEFAULT '',
    delivered_by TEXT DEFAULT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    delivered_at TEXT DEFAULT NULL,
    cancelled_at TEXT DEFAULT NULL,
    log_channel_id TEXT DEFAULT NULL,
    log_message_id TEXT DEFAULT NULL,
    deadline_notified INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    type TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS charge_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    depositor_name TEXT NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT DEFAULT 'pending',
    decided_by TEXT DEFAULT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER,
    user_id TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    channel_id TEXT NOT NULL,
    status TEXT DEFAULT 'open',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS admins (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER UNIQUE,
    user_id TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    product_id INTEGER,
    product_name TEXT NOT NULL DEFAULT '',
    rating INTEGER NOT NULL,
    content TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_charges_status ON charge_requests(status);
CREATE INDEX IF NOT EXISTS idx_tickets_order ON tickets(order_id);
"""


def init_db():
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()

    _seed_settings()
    _migrate_legacy_data()
    _seed_products()
    _ensure_admin_password()


def _ensure_admin_password():
    current = str(get_setting('admin_password', '') or '').strip()
    if current:
        return
    password = os.environ.get('ADMIN_PASSWORD', '').strip() or secrets.token_urlsafe(9)
    set_setting('admin_password', password)
    print(f'[APEX OTT] 초기 관리자 비밀번호: {password}')


def _seed_settings():
    conn = get_conn()
    try:
        existing = {r['key'] for r in conn.execute('SELECT key FROM settings')}
        for key, value in DEFAULT_SETTINGS.items():
            if key not in existing:
                conn.execute('INSERT INTO settings (key, value) VALUES (?, ?)', (key, str(value)))
        conn.commit()
    finally:
        conn.close()


def _migrate_legacy_data():
    """기존 APEX VENDING DB가 있으면 유저 포인트/관리자/채널 설정만 1회 이전한다."""
    if not os.path.exists(LEGACY_DB_PATH) or os.path.abspath(LEGACY_DB_PATH) == os.path.abspath(DB_PATH):
        return
    if get_setting('legacy_migrated'):
        return

    try:
        legacy = sqlite3.connect(LEGACY_DB_PATH)
        legacy.row_factory = sqlite3.Row
        tables = {r[0] for r in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except Exception:
        return

    conn = get_conn()
    try:
        if 'users' in tables:
            for row in legacy.execute('SELECT user_id, username, balance, created_at FROM users'):
                conn.execute(
                    """INSERT INTO users (user_id, username, balance, total_spent, created_at)
                       VALUES (?, ?, ?, 0, ?)
                       ON CONFLICT(user_id) DO UPDATE SET
                           balance = CASE WHEN users.balance = 0 THEN excluded.balance ELSE users.balance END,
                           username = CASE WHEN users.username = '' THEN excluded.username ELSE users.username END""",
                    (str(row['user_id']), row['username'] or '', int(row['balance'] or 0), row['created_at']),
                )

        if 'admins' in tables:
            for row in legacy.execute('SELECT user_id, username FROM admins'):
                conn.execute(
                    'INSERT OR IGNORE INTO admins (user_id, username) VALUES (?, ?)',
                    (str(row['user_id']), row['username'] or ''),
                )

        if 'settings' in tables:
            carried = {
                'admin_password', 'purchase_log_channel_id', 'charge_log_channel_id',
                'ticket_category_id', 'notice_channel_id', 'bank_name', 'bank_account',
                'bank_holder', 'deposit_webhook_key', 'purchase_role_id',
            }
            for row in legacy.execute('SELECT key, value FROM settings'):
                if row['key'] in carried and str(row['value'] or '').strip():
                    target = 'ticket_ping_role_id' if row['key'] == 'purchase_role_id' else row['key']
                    conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                                 (target, str(row['value'])))

        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('legacy_migrated', ?)", (now_utc(),))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()
        try:
            legacy.close()
        except Exception:
            pass


def _seed_products():
    conn = get_conn()
    try:
        count = conn.execute('SELECT COUNT(*) FROM products').fetchone()[0]
        if count:
            return
        for index, (category, name, price, desc) in enumerate(DEFAULT_PRODUCTS):
            conn.execute(
                """INSERT INTO products (name, price, category, description, stock, active, sort_order)
                   VALUES (?, ?, ?, ?, -1, 1, ?)""",
                (name, price, category, desc, index),
            )
        conn.commit()
    finally:
        conn.close()


# ============ 설정 ============
def get_setting(key, default=None):
    conn = get_conn()
    try:
        row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        if row is None:
            return DEFAULT_SETTINGS.get(key, default)
        return row['value']
    finally:
        conn.close()


def set_setting(key, value):
    conn = get_conn()
    try:
        conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, str(value)))
        conn.commit()
    finally:
        conn.close()


def get_settings():
    conn = get_conn()
    try:
        data = dict(DEFAULT_SETTINGS)
        for row in conn.execute('SELECT key, value FROM settings'):
            data[row['key']] = row['value']
        return data
    finally:
        conn.close()


def update_settings(values):
    conn = get_conn()
    try:
        for key, value in (values or {}).items():
            conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, str(value)))
        conn.commit()
    finally:
        conn.close()


# ============ 유저 / 포인트 ============
def ensure_user(user_id, username=''):
    uid = str(user_id)
    conn = get_conn()
    try:
        conn.execute('INSERT OR IGNORE INTO users (user_id, username, balance) VALUES (?, ?, 0)', (uid, username or ''))
        conn.execute('UPDATE users SET username = ?, last_seen = ? WHERE user_id = ?',
                     (username or '', now_utc(), uid))
        conn.commit()
    finally:
        conn.close()


def get_user(user_id):
    conn = get_conn()
    try:
        return _row(conn.execute('SELECT * FROM users WHERE user_id = ?', (str(user_id),)).fetchone())
    finally:
        conn.close()


def get_balance(user_id):
    user = get_user(user_id)
    return int(user['balance']) if user else 0


def add_balance(user_id, amount, tx_type='admin_add', description='', username=''):
    """포인트 증감. 반환: (ok, 잔액)"""
    amount = int(amount)
    conn = get_conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT OR IGNORE INTO users (user_id, username, balance) VALUES (?, ?, 0)',
                     (str(user_id), username or ''))
        cur = conn.execute('SELECT balance FROM users WHERE user_id = ?', (str(user_id),)).fetchone()
        balance = int(cur['balance'] or 0)
        if amount < 0 and balance + amount < 0:
            conn.rollback()
            return False, balance
        conn.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, str(user_id)))
        conn.execute('INSERT INTO transactions (user_id, amount, type, description) VALUES (?, ?, ?, ?)',
                     (str(user_id), amount, tx_type, description or ''))
        conn.commit()
        return True, balance + amount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_users(query='', limit=200):
    conn = get_conn()
    try:
        if query:
            like = f'%{query}%'
            rows = conn.execute(
                'SELECT * FROM users WHERE user_id LIKE ? OR username LIKE ? ORDER BY balance DESC LIMIT ?',
                (like, like, int(limit)),
            )
        else:
            rows = conn.execute('SELECT * FROM users ORDER BY balance DESC LIMIT ?', (int(limit),))
        return _rows(rows)
    finally:
        conn.close()


def list_transactions(user_id, limit=20):
    conn = get_conn()
    try:
        return _rows(conn.execute(
            'SELECT * FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?',
            (str(user_id), int(limit)),
        ))
    finally:
        conn.close()


# ============ 상품 ============
def list_products(active_only=False):
    conn = get_conn()
    try:
        sql = 'SELECT * FROM products'
        if active_only:
            sql += ' WHERE active = 1'
        sql += ' ORDER BY sort_order, id'
        return _rows(conn.execute(sql))
    finally:
        conn.close()


def get_product(product_id):
    conn = get_conn()
    try:
        return _row(conn.execute('SELECT * FROM products WHERE id = ?', (int(product_id),)).fetchone())
    finally:
        conn.close()


def add_product(name, price, category='', description='', stock=0, active=1):
    conn = get_conn()
    try:
        order = conn.execute('SELECT COALESCE(MAX(sort_order), 0) + 1 FROM products').fetchone()[0]
        cur = conn.execute(
            """INSERT INTO products (name, price, category, description, stock, active, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name.strip(), int(price), category.strip(), description.strip(), int(stock), int(active), order),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_product(product_id, **fields):
    allowed = {'name', 'price', 'category', 'description', 'stock', 'active', 'sort_order'}
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return False
    conn = get_conn()
    try:
        assignments = ', '.join(f'{k} = ?' for k in data)
        conn.execute(f'UPDATE products SET {assignments} WHERE id = ?', (*data.values(), int(product_id)))
        conn.commit()
        return True
    finally:
        conn.close()


def delete_product(product_id):
    conn = get_conn()
    try:
        conn.execute('DELETE FROM products WHERE id = ?', (int(product_id),))
        conn.commit()
        return True
    finally:
        conn.close()


def add_stock(product_id, amount):
    conn = get_conn()
    try:
        cur = conn.execute('UPDATE products SET stock = stock + ? WHERE id = ? AND stock >= 0',
                           (int(amount), int(product_id)))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def is_admin(user_id, extra_ids=None):
    uid = str(user_id)
    if extra_ids and uid in {str(x) for x in extra_ids}:
        return True
    conn = get_conn()
    try:
        row = conn.execute('SELECT 1 FROM admins WHERE user_id = ?', (uid,)).fetchone()
        return row is not None
    finally:
        conn.close()


def add_admin(user_id, username=''):
    conn = get_conn()
    try:
        conn.execute('INSERT OR REPLACE INTO admins (user_id, username) VALUES (?, ?)',
                     (str(user_id), username or ''))
        conn.commit()
    finally:
        conn.close()


def remove_admin(user_id):
    conn = get_conn()
    try:
        conn.execute('DELETE FROM admins WHERE user_id = ?', (str(user_id),))
        conn.commit()
    finally:
        conn.close()


def list_admins():
    conn = get_conn()
    try:
        return _rows(conn.execute('SELECT * FROM admins'))
    finally:
        conn.close()


# ============ 주문 ============
def _new_order_no(conn, order_id, created_at=None):
    base = datetime.now(timezone.utc)
    if created_at:
        try:
            base = datetime.strptime(created_at[:19], '%Y-%m-%d %H:%M:%S')
        except Exception:
            pass
    return f'OTT-{base.strftime("%y%m%d")}-{int(order_id):04d}'


def purchase_product(user_id, username, product_id):
    """포인트 차감 + 재고 차감 + 주문 생성(paid). 반환: (ok, order|사유)"""
    uid = str(user_id)
    conn = get_conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        product = conn.execute('SELECT * FROM products WHERE id = ?', (int(product_id),)).fetchone()
        if product is None or not int(product['active']):
            conn.rollback()
            return False, '상품을 찾을 수 없습니다.'
        stock = int(product['stock'])
        if stock != -1 and stock <= 0:
            conn.rollback()
            return False, '품절된 상품입니다.'

        conn.execute('INSERT OR IGNORE INTO users (user_id, username, balance) VALUES (?, ?, 0)', (uid, username or ''))
        user = conn.execute('SELECT balance FROM users WHERE user_id = ?', (uid,)).fetchone()
        balance = int(user['balance'] or 0)
        price = int(product['price'])
        if balance < price:
            conn.rollback()
            return False, '보유 포인트가 부족합니다.'

        conn.execute('UPDATE users SET balance = balance - ?, total_spent = total_spent + ? WHERE user_id = ?',
                     (price, price, uid))
        if stock != -1:
            conn.execute('UPDATE products SET stock = stock - 1 WHERE id = ?', (int(product_id),))
        cur = conn.execute(
            """INSERT INTO orders (user_id, username, product_id, product_name, category, amount, status)
               VALUES (?, ?, ?, ?, ?, ?, 'paid')""",
            (uid, username or '', int(product_id), product['name'], product['category'] or '', price),
        )
        order_id = cur.lastrowid
        order_no = _new_order_no(conn, order_id)
        conn.execute('UPDATE orders SET order_no = ? WHERE id = ?', (order_no, order_id))
        conn.execute(
            'INSERT INTO transactions (user_id, amount, type, description) VALUES (?, ?, ?, ?)',
            (uid, -price, 'purchase', f'{order_no} · {product["name"]}'),
        )
        conn.commit()
        row = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
        return True, _row(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_order(order_id):
    conn = get_conn()
    try:
        return _row(conn.execute('SELECT * FROM orders WHERE id = ?', (int(order_id),)).fetchone())
    finally:
        conn.close()


def get_order_by_no(order_no):
    conn = get_conn()
    try:
        return _row(conn.execute('SELECT * FROM orders WHERE order_no = ?', (str(order_no).strip(),)).fetchone())
    finally:
        conn.close()


def list_orders(status=None, limit=100, user_id=None):
    conn = get_conn()
    try:
        where, args = [], []
        if status and status != 'all':
            where.append('status = ?')
            args.append(status)
        if user_id:
            where.append('user_id = ?')
            args.append(str(user_id))
        sql = 'SELECT * FROM orders'
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY id DESC LIMIT ?'
        args.append(int(limit))
        return _rows(conn.execute(sql, args))
    finally:
        conn.close()


def set_order_log(order_id, channel_id, message_id):
    conn = get_conn()
    try:
        conn.execute('UPDATE orders SET log_channel_id = ?, log_message_id = ? WHERE id = ?',
                     (str(channel_id), str(message_id), int(order_id)))
        conn.commit()
    finally:
        conn.close()


def deliver_order(order_id, delivery_type, delivery, message='', delivered_by=''):
    """paid 주문을 delivered로 전환. 반환: (ok, order|사유)"""
    if delivery_type not in DELIVERY_TYPE_LABEL:
        return False, '전달 유형이 올바르지 않습니다.'
    conn = get_conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        order = conn.execute('SELECT * FROM orders WHERE id = ?', (int(order_id),)).fetchone()
        if order is None:
            conn.rollback()
            return False, '주문을 찾을 수 없습니다.'
        if order['status'] != 'paid':
            conn.rollback()
            return False, '이미 처리된 주문입니다.'
        conn.execute(
            """UPDATE orders SET status = 'delivered', delivery_type = ?, delivery_json = ?,
               delivery_message = ?, delivered_by = ?, delivered_at = ? WHERE id = ?""",
            (delivery_type, json.dumps(delivery, ensure_ascii=False), message or '',
             str(delivered_by or ''), now_utc(), int(order_id)),
        )
        conn.commit()
        row = conn.execute('SELECT * FROM orders WHERE id = ?', (int(order_id),)).fetchone()
        return True, _row(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cancel_order(order_id, decided_by=''):
    """paid 주문 취소 + 포인트/재고 복구. 반환: (ok, order|사유)"""
    conn = get_conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        order = conn.execute('SELECT * FROM orders WHERE id = ?', (int(order_id),)).fetchone()
        if order is None:
            conn.rollback()
            return False, '주문을 찾을 수 없습니다.'
        if order['status'] != 'paid':
            conn.rollback()
            return False, '지급 대기 상태의 주문만 취소할 수 있습니다.'
        conn.execute(
            "UPDATE orders SET status = 'cancelled', cancelled_at = ?, delivered_by = ? WHERE id = ?",
            (now_utc(), str(decided_by or ''), int(order_id)),
        )
        conn.execute('UPDATE users SET balance = balance + ?, total_spent = MAX(0, total_spent - ?) WHERE user_id = ?',
                     (int(order['amount']), int(order['amount']), order['user_id']))
        if order['product_id']:
            conn.execute('UPDATE products SET stock = stock + 1 WHERE id = ? AND stock >= 0',
                         (int(order['product_id']),))
        conn.execute(
            'INSERT INTO transactions (user_id, amount, type, description) VALUES (?, ?, ?, ?)',
            (order['user_id'], int(order['amount']), 'refund', f'{order["order_no"]} 주문 취소'),
        )
        conn.commit()
        row = conn.execute('SELECT * FROM orders WHERE id = ?', (int(order_id),)).fetchone()
        return True, _row(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def overdue_orders(minutes=30, limit=50):
    conn = get_conn()
    try:
        modifier = f'-{int(minutes)} minutes'
        return _rows(conn.execute(
            """SELECT * FROM orders
               WHERE status = 'paid' AND deadline_notified = 0
                 AND created_at <= datetime('now', ?)
               ORDER BY id ASC LIMIT ?""",
            (modifier, int(limit)),
        ))
    finally:
        conn.close()


def mark_deadline_notified(order_id):
    conn = get_conn()
    try:
        conn.execute('UPDATE orders SET deadline_notified = 1 WHERE id = ?', (int(order_id),))
        conn.commit()
    finally:
        conn.close()


def pending_order_count():
    conn = get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'paid'").fetchone()[0]
    finally:
        conn.close()


def user_recent_orders(user_id, limit=3):
    return list_orders(limit=limit, user_id=user_id)


def user_order_summary(user_id):
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END), 0) AS delivered,
                      COALESCE(SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END), 0) AS pending_amount
               FROM orders WHERE user_id = ?""",
            (str(user_id),),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


# ============ 충전 ============
def create_charge(user_id, username, depositor_name, amount):
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO charge_requests (user_id, username, depositor_name, amount, status)
               VALUES (?, ?, ?, ?, 'pending')""",
            (str(user_id), username or '', depositor_name.strip(), int(amount)),
        )
        conn.commit()
        return _row(conn.execute('SELECT * FROM charge_requests WHERE id = ?', (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def get_charge(charge_id):
    conn = get_conn()
    try:
        return _row(conn.execute('SELECT * FROM charge_requests WHERE id = ?', (int(charge_id),)).fetchone())
    finally:
        conn.close()


def list_charges(status=None, limit=100):
    conn = get_conn()
    try:
        if status and status != 'all':
            rows = conn.execute('SELECT * FROM charge_requests WHERE status = ? ORDER BY id DESC LIMIT ?',
                                (status, int(limit)))
        else:
            rows = conn.execute('SELECT * FROM charge_requests ORDER BY id DESC LIMIT ?', (int(limit),))
        return _rows(rows)
    finally:
        conn.close()


def find_charge_match(depositor, amount):
    """대기 중 충전 신청 중 입금자명+금액이 일치하는 가장 오래된 건"""
    normalized = ''.join((depositor or '').split())
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM charge_requests WHERE status = 'pending' AND amount = ? ORDER BY id ASC",
            (int(amount),),
        )
        for row in rows:
            if ''.join((row['depositor_name'] or '').split()) == normalized:
                return dict(row)
        return None
    finally:
        conn.close()


def approve_charge(charge_id, decided_by='auto'):
    """충전 승인 + 포인트 지급. 반환: (ok, charge|사유)"""
    conn = get_conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        charge = conn.execute('SELECT * FROM charge_requests WHERE id = ?', (int(charge_id),)).fetchone()
        if charge is None:
            conn.rollback()
            return False, '충전 신청을 찾을 수 없습니다.'
        if charge['status'] != 'pending':
            conn.rollback()
            return False, '이미 처리된 충전 신청입니다.'
        conn.execute("UPDATE charge_requests SET status = 'completed', decided_by = ?, completed_at = ? WHERE id = ?",
                     (str(decided_by or 'auto'), now_utc(), int(charge_id)))
        conn.execute('INSERT OR IGNORE INTO users (user_id, username, balance) VALUES (?, ?, 0)',
                     (charge['user_id'], charge['username'] or ''))
        conn.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?',
                     (int(charge['amount']), charge['user_id']))
        conn.execute(
            'INSERT INTO transactions (user_id, amount, type, description) VALUES (?, ?, ?, ?)',
            (charge['user_id'], int(charge['amount']), 'charge', f'충전 #{charge_id} · {charge["depositor_name"]}'),
        )
        conn.commit()
        row = conn.execute('SELECT * FROM charge_requests WHERE id = ?', (int(charge_id),)).fetchone()
        return True, _row(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cancel_charge(charge_id, decided_by=''):
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE charge_requests SET status = 'cancelled', decided_by = ?, completed_at = ? WHERE id = ? AND status = 'pending'",
            (str(decided_by or ''), now_utc(), int(charge_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ============ 티켓 ============
def create_ticket(order_id, user_id, username, channel_id):
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO tickets (order_id, user_id, username, channel_id, status)
               VALUES (?, ?, ?, ?, 'open')""",
            (None if order_id is None else int(order_id), str(user_id), username or '', str(channel_id)),
        )
        conn.commit()
        return _row(conn.execute('SELECT * FROM tickets WHERE id = ?', (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def find_open_ticket(order_id):
    conn = get_conn()
    try:
        return _row(conn.execute(
            "SELECT * FROM tickets WHERE order_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
            (int(order_id),),
        ).fetchone())
    finally:
        conn.close()


def find_open_ticket_by_user(user_id):
    conn = get_conn()
    try:
        return _row(conn.execute(
            """SELECT * FROM tickets WHERE user_id = ? AND order_id IS NULL
               AND status = 'open' ORDER BY id DESC LIMIT 1""",
            (str(user_id),),
        ).fetchone())
    finally:
        conn.close()


def get_ticket_by_channel(channel_id):
    conn = get_conn()
    try:
        return _row(conn.execute(
            "SELECT * FROM tickets WHERE channel_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
            (str(channel_id),),
        ).fetchone())
    finally:
        conn.close()


def close_ticket(channel_id):
    conn = get_conn()
    try:
        conn.execute("UPDATE tickets SET status = 'closed', closed_at = ? WHERE channel_id = ? AND status = 'open'",
                     (now_utc(), str(channel_id)))
        conn.commit()
    finally:
        conn.close()


def list_open_tickets():
    conn = get_conn()
    try:
        return _rows(conn.execute("SELECT * FROM tickets WHERE status = 'open' ORDER BY id DESC"))
    finally:
        conn.close()


# ============ 후기 ============
def create_review(order_id, user_id, username, product_id, product_name, rating, content):
    rating = max(1, min(5, int(rating)))
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO reviews
               (order_id, user_id, username, product_id, product_name, rating, content)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (int(order_id), str(user_id), username or '', product_id, product_name or '', rating, content or ''),
        )
        conn.commit()
        return _row(conn.execute('SELECT * FROM reviews WHERE id = ?', (cur.lastrowid,)).fetchone())
    except sqlite3.IntegrityError:
        conn.rollback()
        return None
    finally:
        conn.close()


def get_review(order_id):
    conn = get_conn()
    try:
        return _row(conn.execute('SELECT * FROM reviews WHERE order_id = ?', (int(order_id),)).fetchone())
    finally:
        conn.close()


def has_review(order_id):
    return get_review(order_id) is not None


def list_reviews(limit=50):
    conn = get_conn()
    try:
        return _rows(conn.execute('SELECT * FROM reviews ORDER BY id DESC LIMIT ?', (int(limit),)))
    finally:
        conn.close()


def review_summary():
    conn = get_conn()
    try:
        row = conn.execute(
            'SELECT COUNT(*) AS count, COALESCE(AVG(rating), 0) AS average FROM reviews'
        ).fetchone()
        return {'count': row['count'], 'average': round(float(row['average'] or 0), 2)}
    finally:
        conn.close()


# ============ 통계 ============
def get_stats():
    conn = get_conn()
    try:
        today = conn.execute(
            """SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS amount
               FROM orders WHERE status = 'delivered'
                 AND date(delivered_at, '+9 hours') = date('now', '+9 hours')"""
        ).fetchone()
        return {
            'pending': conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'paid'").fetchone()[0],
            'delivered': conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'delivered'").fetchone()[0],
            'cancelled': conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'cancelled'").fetchone()[0],
            'users': conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],
            'products': conn.execute('SELECT COUNT(*) FROM products').fetchone()[0],
            'open_tickets': conn.execute("SELECT COUNT(*) FROM tickets WHERE status = 'open'").fetchone()[0],
            'pending_charges': conn.execute(
                "SELECT COUNT(*) FROM charge_requests WHERE status = 'pending'"
            ).fetchone()[0],
            'today_delivered': today['cnt'],
            'today_amount': today['amount'],
            'revenue': conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status = 'delivered'"
            ).fetchone()[0],
            'reviews': conn.execute('SELECT COUNT(*) FROM reviews').fetchone()[0],
            'avg_rating': round(float(conn.execute(
                'SELECT COALESCE(AVG(rating), 0) FROM reviews'
            ).fetchone()[0] or 0), 2),
        }
    finally:
        conn.close()


def total_points():
    conn = get_conn()
    try:
        return conn.execute('SELECT COALESCE(SUM(balance), 0) FROM users').fetchone()[0]
    finally:
        conn.close()


def backup_bytes():
    """SQLite DB를 일관성 있게 백업해 bytes로 반환한다."""
    import tempfile

    conn = get_conn()
    handle, path = tempfile.mkstemp(suffix='.db')
    os.close(handle)
    try:
        dest = sqlite3.connect(path)
        try:
            conn.backup(dest)
        finally:
            dest.close()
        with open(path, 'rb') as reader:
            return reader.read()
    finally:
        conn.close()
        try:
            os.unlink(path)
        except OSError:
            pass


def db_size():
    try:
        return os.path.getsize(DB_PATH)
    except OSError:
        return 0


def generate_webhook_key():
    alphabet = string.ascii_lowercase + string.digits
    return ''.join(random.choice(alphabet) for _ in range(32))








