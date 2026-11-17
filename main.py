# -*- coding: utf-8 -*-
"""APEX OTT — 실행 진입점

- Discord 봇을 백그라운드 스레드에서 실행
- Flask 관리자 대시보드 제공 (상품 / 주문 지급 / 유저 포인트 / 충전 / 설정)
- 토스 입금 알림 웹훅으로 포인트 자동 충전

실행: python main.py
"""
import asyncio
import hmac
import io
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for

load_dotenv()

import database as db  # noqa: E402
from toss_deposit import TossDepositMonitor  # noqa: E402

db.init_db()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger('apex')

app = Flask(__name__)
try:
    app.json.ensure_ascii = False
except Exception:
    pass

_secret = os.environ.get('SECRET_KEY', '').strip() or db.get_setting('flask_secret', '')
if not _secret:
    _secret = secrets.token_hex(32)
    db.set_setting('flask_secret', _secret)
app.secret_key = _secret
app.permanent_session_lifetime = timedelta(hours=12)

if not str(db.get_setting('deposit_webhook_key', '') or '').strip():
    db.set_setting('deposit_webhook_key', os.environ.get('DEPOSIT_WEBHOOK_KEY') or db.generate_webhook_key())

SETTING_KEYS = {
    'purchase_log_channel_id', 'charge_log_channel_id', 'ticket_category_id',
    'notice_channel_id', 'ticket_ping_role_id', 'bank_name', 'bank_account',
    'bank_holder', 'delivery_window_minutes', 'card_image_enabled', 'store_notice',
    'review_channel_id', 'panel_banner_url', 'panel_channel_id',
}


# ============ 봇 런타임 ============
class WebAdmin:
    """웹 대시보드에서 지급할 때 기록되는 처리자 정보"""
    id = 'web'

    def __str__(self):
        return '웹 대시보드'


class BotRuntime:
    def __init__(self):
        self.bot = None
        self.loop = None
        self.thread = None
        self.error = None

    @property
    def online(self):
        try:
            return bool(self.bot and self.bot.is_ready())
        except Exception:
            return False

    def start(self):
        token = os.environ.get('DISCORD_TOKEN', '').strip()
        if not token:
            logger.warning('DISCORD_TOKEN 이 없어 봇을 시작하지 않습니다. (웹 전용 모드)')
            return
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name='apex-bot', daemon=True)
        self.thread.start()

    def _run(self):
        from bot import create_bot
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.bot = create_bot()
        try:
            self.loop.run_until_complete(self.bot.start(os.environ['DISCORD_TOKEN'].strip()))
        except Exception as exc:
            self.error = str(exc)
            logger.error(f'[bot] 실행 오류: {exc}')

    def call(self, coro, timeout=40):
        if not self.online:
            try:
                coro.close()
            except Exception:
                pass
            raise RuntimeError('봇이 오프라인 상태입니다. DISCORD_TOKEN과 봇 실행 상태를 확인해 주세요.')
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def fire(self, coro):
        if self.online and self.loop:
            try:
                asyncio.run_coroutine_threadsafe(coro, self.loop)
                return
            except Exception as exc:
                logger.warning(f'[bot] 백그라운드 작업 실패: {exc}')
        try:
            coro.close()
        except Exception:
            pass


runtime = BotRuntime()


# ============ 로그인 ============
_login_failures = {}
_login_lock = threading.Lock()


def _client_key():
    return request.headers.get('X-Forwarded-For', request.remote_addr or '?').split(',')[0].strip()


def _blocked(key):
    with _login_lock:
        entry = _login_failures.get(key)
        if not entry:
            return False
        count, first = entry
        if time.time() - first > 300:
            _login_failures.pop(key, None)
            return False
        return count >= 8


def _record_failure(key):
    with _login_lock:
        count, first = _login_failures.get(key, (0, time.time()))
        if time.time() - first > 300:
            count, first = 0, time.time()
        _login_failures[key] = (count + 1, first)


def _clear_failures(key):
    with _login_lock:
        _login_failures.pop(key, None)


def login_required(func):
    def wrapper(*args, **kwargs):
        if not session.get('auth'):
            if request.path.startswith('/api/'):
                return jsonify({'ok': False, 'error': '로그인이 필요합니다.'}), 401
            return redirect(url_for('login', next=request.path))
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        key = _client_key()
        if _blocked(key):
            error = '로그인 시도가 너무 많습니다. 5분 후 다시 시도해 주세요.'
        else:
            password = str(request.form.get('password') or '')
            stored = str(db.get_setting('admin_password', '') or '')
            if stored and hmac.compare_digest(password, stored):
                session.permanent = True
                session['auth'] = True
                _clear_failures(key)
                target = request.args.get('next') or ''
                if not (target.startswith('/') and not target.startswith('//')):
                    target = url_for('dashboard')
                return redirect(target)
            _record_failure(key)
            error = '비밀번호가 올바르지 않습니다.'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
def index():
    return redirect(url_for('dashboard') if session.get('auth') else url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html', webhook_key=db.get_setting('deposit_webhook_key', ''))


# ============ 대시보드 API ============
@app.route('/api/overview')
@login_required
def api_overview():
    stats = db.get_stats()
    stats['total_points'] = db.total_points()
    stats['db_size'] = db.db_size()
    stats['bot_online'] = runtime.online
    stats['bot_error'] = runtime.error
    return jsonify({
        'ok': True,
        'stats': stats,
        'pending': db.list_orders(status='paid', limit=20),
        'recent': db.list_orders(status='delivered', limit=8),
        'charges': db.list_charges(status='pending', limit=8),
    })


@app.route('/api/orders')
@login_required
def api_orders():
    status = request.args.get('status', 'paid')
    try:
        limit = min(200, max(1, int(request.args.get('limit', 100))))
    except ValueError:
        limit = 100
    return jsonify({'ok': True, 'orders': db.list_orders(status=status, limit=limit)})


@app.route('/api/orders/<int:order_id>/deliver', methods=['POST'])
@login_required
def api_deliver(order_id):
    data = request.get_json(silent=True) or {}
    kind = str(data.get('type') or '').strip()
    message = str(data.get('message') or '').strip()

    if kind == 'idpw':
        account_id = str(data.get('id') or '').strip()
        password = str(data.get('pw') or '').strip()
        if not account_id or not password:
            return jsonify({'ok': False, 'error': '아이디와 비밀번호를 모두 입력해 주세요.'}), 400
        values = {'id': account_id, 'pw': password}
    elif kind == 'key':
        key_value = str(data.get('key') or '').strip()
        if not key_value:
            return jsonify({'ok': False, 'error': 'KEY를 입력해 주세요.'}), 400
        values = {'key': key_value}
    elif kind == 'note':
        note = str(data.get('note') or '').strip()
        if not note:
            return jsonify({'ok': False, 'error': '전달 내용을 입력해 주세요.'}), 400
        values = {'note': note}
    else:
        return jsonify({'ok': False, 'error': '지급 방식을 선택해 주세요.'}), 400

    order = db.get_order(order_id)
    if not order:
        return jsonify({'ok': False, 'error': '주문을 찾을 수 없습니다.'}), 404
    if order['status'] != 'paid':
        return jsonify({'ok': False, 'error': '이미 처리된 주문입니다.'}), 400

    try:
        import bot as bot_mod
        ok, result, dm_ok = runtime.call(
            bot_mod.deliver_and_notify(runtime.bot, order_id, kind, values, message, WebAdmin())
        )
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 503

    if not ok:
        return jsonify({'ok': False, 'error': result}), 400
    return jsonify({'ok': True, 'order': result, 'dm_sent': dm_ok})


@app.route('/api/orders/<int:order_id>/cancel', methods=['POST'])
@login_required
def api_cancel(order_id):
    async def action():
        import bot as bot_mod
        ok, result = db.cancel_order(order_id, decided_by='웹 대시보드')
        if not ok:
            return False, result
        try:
            user = runtime.bot.get_user(int(result['user_id'])) or await runtime.bot.fetch_user(int(result['user_id']))
            embed = bot_mod.make_embed(
                '주문이 취소되었습니다',
                f'주문 `{result["order_no"]}` 이 취소되어 **{bot_mod.money(result["amount"])}** 이 복구되었습니다.',
                bot_mod.COLOR_GOLD,
            )
            await bot_mod.try_dm(user, embed=embed)
        except Exception:
            pass
        await bot_mod.edit_order_log(runtime.bot, result)
        return True, result

    try:
        ok, result = runtime.call(action())
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 503
    if not ok:
        return jsonify({'ok': False, 'error': result}), 400
    return jsonify({'ok': True, 'order': result})


@app.route('/api/orders/<int:order_id>/resend', methods=['POST'])
@login_required
def api_resend(order_id):
    order = db.get_order(order_id)
    if not order:
        return jsonify({'ok': False, 'error': '주문을 찾을 수 없습니다.'}), 404
    try:
        import bot as bot_mod
        ok, message = runtime.call(bot_mod.resend_delivery(runtime.bot, order))
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 503
    return jsonify({'ok': ok, 'message': message, 'error': None if ok else message})


# ---------- 상품 ----------
@app.route('/api/products')
@login_required
def api_products():
    return jsonify({'ok': True, 'products': db.list_products()})


@app.route('/api/products', methods=['POST'])
@login_required
def api_add_product():
    data = request.get_json(silent=True) or {}
    name = str(data.get('name') or '').strip()
    try:
        price = int(data.get('price') or 0)
        stock = int(data.get('stock') or 0)
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': '가격과 재고는 숫자여야 합니다.'}), 400
    if not name or price <= 0 or stock < -1:
        return jsonify({'ok': False, 'error': '상품명, 가격, 재고를 확인해 주세요. (재고 -1 = 무제한)'}), 400
    pid = db.add_product(name, price, str(data.get('category') or '기타'),
                         str(data.get('description') or ''), stock)
    return jsonify({'ok': True, 'id': pid})


@app.route('/api/products/<int:product_id>', methods=['PUT'])
@login_required
def api_update_product(product_id):
    if not db.get_product(product_id):
        return jsonify({'ok': False, 'error': '상품을 찾을 수 없습니다.'}), 404
    data = request.get_json(silent=True) or {}
    fields = {}
    for key in ('name', 'category', 'description'):
        if key in data:
            fields[key] = str(data[key])
    for key in ('price', 'stock', 'active'):
        if key in data:
            try:
                fields[key] = int(data[key])
            except (TypeError, ValueError):
                return jsonify({'ok': False, 'error': '숫자 형식이 올바르지 않습니다.'}), 400
    if 'price' in fields and fields['price'] <= 0:
        return jsonify({'ok': False, 'error': '가격은 1P 이상이어야 합니다.'}), 400
    if 'stock' in fields and fields['stock'] < -1:
        return jsonify({'ok': False, 'error': '재고는 -1(무제한) 또는 0 이상이어야 합니다.'}), 400
    db.update_product(product_id, **fields)
    return jsonify({'ok': True, 'product': db.get_product(product_id)})


@app.route('/api/products/<int:product_id>', methods=['DELETE'])
@login_required
def api_delete_product(product_id):
    db.delete_product(product_id)
    return jsonify({'ok': True})


# ---------- 유저 / 포인트 ----------
@app.route('/api/users')
@login_required
def api_users():
    query = request.args.get('q', '').strip()
    return jsonify({'ok': True, 'users': db.list_users(query, 200)})


@app.route('/api/users/<user_id>/points', methods=['POST'])
@login_required
def api_user_points(user_id):
    data = request.get_json(silent=True) or {}
    try:
        amount = int(data.get('amount') or 0)
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': '금액은 숫자여야 합니다.'}), 400
    if amount == 0:
        return jsonify({'ok': False, 'error': '0이 아닌 금액을 입력해 주세요.'}), 400
    reason = str(data.get('reason') or '').strip() or '웹 대시보드 조정'
    user = db.get_user(user_id) or {}
    ok, balance = db.add_balance(
        user_id, amount, 'admin_add' if amount > 0 else 'admin_remove',
        f'{reason} · 웹 대시보드', user.get('username') or '',
    )
    if not ok:
        return jsonify({'ok': False, 'error': f'잔액이 부족합니다. (현재 {balance:,}P)'}), 400

    if runtime.online and amount > 0:
        async def notify():
            import bot as bot_mod
            try:
                target = runtime.bot.get_user(int(user_id)) or await runtime.bot.fetch_user(int(user_id))
                await bot_mod.try_dm(target, embed=bot_mod.make_embed(
                    '포인트가 지급되었습니다',
                    f'**{amount:,}P** 이 지급되었습니다.\n현재 보유 포인트: **{balance:,}P**',
                    bot_mod.COLOR_MINT,
                ))
            except Exception:
                pass
        runtime.fire(notify())
    return jsonify({'ok': True, 'balance': balance})


@app.route('/api/users/<user_id>/orders')
@login_required
def api_user_orders(user_id):
    return jsonify({'ok': True, 'orders': db.list_orders(limit=50, user_id=user_id)})


# ---------- 충전 ----------
@app.route('/api/charges')
@login_required
def api_charges():
    status = request.args.get('status', 'pending')
    return jsonify({'ok': True, 'charges': db.list_charges(status=status, limit=100)})


@app.route('/api/charges/<int:charge_id>/approve', methods=['POST'])
@login_required
def api_approve_charge(charge_id):
    async def action():
        import bot as bot_mod
        ok, result = db.approve_charge(charge_id, decided_by='웹 대시보드')
        if ok:
            await bot_mod.notify_charge_completed(runtime.bot, result)
        return ok, result

    try:
        ok, result = runtime.call(action())
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 503
    if not ok:
        return jsonify({'ok': False, 'error': result}), 400
    return jsonify({'ok': True, 'charge': result})


@app.route('/api/charges/<int:charge_id>/cancel', methods=['POST'])
@login_required
def api_cancel_charge(charge_id):
    db.cancel_charge(charge_id, decided_by='웹 대시보드')
    return jsonify({'ok': True})


# ---------- 설정 ----------
@app.route('/api/settings', methods=['GET'])
@login_required
def api_get_settings():
    settings = db.get_settings()
    data = {key: settings.get(key, '') for key in SETTING_KEYS}
    webhook_key = str(settings.get('deposit_webhook_key', '') or '')
    data['webhook_key_masked'] = (webhook_key[:6] + '…' + webhook_key[-4:]) if len(webhook_key) > 12 else webhook_key
    data['admin_password_set'] = bool(settings.get('admin_password'))
    return jsonify({'ok': True, 'settings': data})


@app.route('/api/settings', methods=['POST'])
@login_required
def api_update_settings():
    data = request.get_json(silent=True) or {}
    values = {}
    for key in SETTING_KEYS:
        if key not in data:
            continue
        value = str(data.get(key) or '').strip()
        if key in ('purchase_log_channel_id', 'charge_log_channel_id', 'ticket_category_id',
                   'notice_channel_id', 'ticket_ping_role_id', 'review_channel_id',
                   'panel_channel_id'):
            if value and not value.isdigit():
                return jsonify({'ok': False, 'error': f'{key} 는 채널/역할 ID(숫자)여야 합니다.'}), 400
        if key == 'panel_banner_url':
            if value and not (value.startswith('https://') or value.startswith('http://')):
                return jsonify({'ok': False, 'error': '배너 이미지는 http(s) 주소여야 합니다.'}), 400
        if key == 'delivery_window_minutes':
            if not value.isdigit() or not (1 <= int(value) <= 180):
                return jsonify({'ok': False, 'error': '전달 제한 시간은 1~180분 사이여야 합니다.'}), 400
        if key == 'card_image_enabled':
            value = 'true' if value.lower() in ('1', 'true', 'yes', 'on') else 'false'
        values[key] = value

    new_password = str(data.get('new_password') or '').strip()
    if new_password:
        if len(new_password) < 6:
            return jsonify({'ok': False, 'error': '새 비밀번호는 6자 이상이어야 합니다.'}), 400
        values['admin_password'] = new_password

    if values:
        db.update_settings(values)
    return jsonify({'ok': True, 'updated': list(values.keys())})


@app.route('/api/settings/rotate-webhook', methods=['POST'])
@login_required
def api_rotate_webhook():
    key = db.generate_webhook_key()
    db.set_setting('deposit_webhook_key', key)
    return jsonify({'ok': True, 'webhook_key': key})


@app.route('/api/discord/channels')
@login_required
def api_discord_channels():
    token = os.environ.get('DISCORD_TOKEN', '').strip()
    guild_id = os.environ.get('GUILD_ID', '').strip()
    if not token or not guild_id:
        return jsonify({'ok': False, 'error': 'DISCORD_TOKEN/GUILD_ID 가 설정되지 않았습니다.'}), 400
    headers = {'Authorization': f'Bot {token}'}
    try:
        channels_res = requests.get(
            f'https://discord.com/api/v10/guilds/{guild_id}/channels',
            headers=headers, timeout=15,
        )
        roles_res = requests.get(
            f'https://discord.com/api/v10/guilds/{guild_id}/roles',
            headers=headers, timeout=15,
        )
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 503
    if channels_res.status_code != 200:
        return jsonify({'ok': False, 'error': f'채널 조회 실패 ({channels_res.status_code})'}), 502
    if roles_res.status_code != 200:
        return jsonify({'ok': False, 'error': f'역할 조회 실패 ({roles_res.status_code})'}), 502

    channels = channels_res.json()
    roles = roles_res.json()
    text = sorted(
        [{'id': c['id'], 'name': c['name'], 'parent_id': c.get('parent_id')}
         for c in channels if c.get('type') in (0, 5)],
        key=lambda c: c['name'],
    )
    categories = sorted(
        [{'id': c['id'], 'name': c['name']} for c in channels if c.get('type') == 4],
        key=lambda c: c['name'],
    )
    role_list = sorted(
        [{'id': r['id'], 'name': r['name'], 'color': r.get('color', 0)}
         for r in roles if r['id'] != guild_id],
        key=lambda r: r['name'],
    )
    return jsonify({'ok': True, 'text': text, 'categories': categories, 'roles': role_list})


@app.route('/api/panel/install', methods=['POST'])
@login_required
def api_panel_install():
    data = request.get_json(silent=True) or {}
    channel_id = str(data.get('channel_id') or db.get_setting('panel_channel_id', '') or '').strip()
    banner_url = data.get('banner_url')
    if not channel_id.isdigit():
        return jsonify({'ok': False, 'error': '패널을 설치할 채널을 선택해 주세요.'}), 400
    if banner_url is not None:
        banner_url = str(banner_url).strip()
        if banner_url and not (banner_url.startswith('https://') or banner_url.startswith('http://')):
            return jsonify({'ok': False, 'error': '배너 이미지는 http(s) 주소여야 합니다.'}), 400

    try:
        import bot as bot_mod
        ok, message = runtime.call(bot_mod.install_panel(runtime.bot, int(channel_id), banner_url))
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 503
    if not ok:
        return jsonify({'ok': False, 'error': message}), 400
    return jsonify({'ok': True, 'message': message})


@app.route('/api/reviews')
@login_required
def api_reviews():
    return jsonify({
        'ok': True,
        'reviews': db.list_reviews(100),
        'summary': db.review_summary(),
    })


@app.route('/api/db/download')
@login_required
def api_db_download():
    data = db.backup_bytes()
    filename = f"APEX-OTT-{datetime.now().strftime('%Y%m%d-%H%M')}.db"
    return send_file(
        io.BytesIO(data),
        as_attachment=True,
        download_name=filename,
        mimetype='application/octet-stream',
    )


# ============ 입금 웹훅 ============
def _public_base_url():
    env_url = os.environ.get('PUBLIC_URL', '').strip()
    if env_url:
        return env_url.rstrip('/')
    return request.host_url.rstrip('/')


def handle_deposit(amount, text, depositor):
    """토스 입금 알림 → 대기 중 충전 신청과 매칭 후 자동 충전"""
    charge = db.find_charge_match(depositor, amount)
    if charge:
        ok, result = db.approve_charge(charge['id'], 'auto')
        if ok:
            logger.info(f'[deposit] 자동 충전: {depositor} {amount:,}원 → {charge["user_id"]}')
            runtime.fire(_deposit_log(amount, depositor, result, True))
            return
        logger.warning(f'[deposit] 충전 승인 실패: {result}')
    logger.info(f'[deposit] 미매칭 입금: {depositor} {amount:,}원')
    runtime.fire(_deposit_log(amount, depositor, None, False))


async def _deposit_log(amount, depositor, charge, matched):
    import bot as bot_mod
    channel = await bot_mod.resolve_channel(runtime.bot, 'charge_log_channel_id')
    if channel is None:
        return
    if matched:
        description = f'입금자 `{depositor}` · **{amount:,}원**\n<@{charge["user_id"]}> 계정에 포인트를 지급했습니다.'
    else:
        description = f'입금자 `{depositor}` · **{amount:,}원**\n대기 중인 충전 신청과 일치하지 않습니다. 대시보드에서 확인해 주세요.'
    embed = bot_mod.make_embed(
        '자동 충전 완료' if matched else '미매칭 입금',
        description,
        bot_mod.COLOR_MINT if matched else bot_mod.COLOR_GOLD,
        footer='APEX OTT · DEPOSIT',
    )
    try:
        await channel.send(embed=embed)
    except Exception:
        pass


monitor = TossDepositMonitor(on_deposit=handle_deposit)



@app.route('/api/deposit-webhook', methods=['GET', 'POST'])
def deposit_webhook():
    if request.method == 'GET':
        return jsonify({'ok': True, 'service': 'APEX OTT deposit webhook', 'method': 'POST'})

    stored = str(db.get_setting('deposit_webhook_key', '') or '')
    provided = (request.args.get('key') or request.headers.get('X-Webhook-Key') or '').strip()
    if not provided:
        payload = request.get_json(silent=True) or {}
        provided = str(payload.get('key') or '').strip()
    if not stored or not provided or not hmac.compare_digest(provided, stored):
        return jsonify({'ok': False, 'error': 'invalid key'}), 403

    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    title = str(payload.get('title') or payload.get('subject') or '')
    body = str(payload.get('body') or payload.get('text') or payload.get('message') or '')
    if not title and not body:
        return jsonify({'ok': False, 'error': 'title 또는 body 값이 필요합니다.'}), 400

    detected, depositor, amount = monitor.process(title, body)
    return jsonify({'ok': True, 'detected': detected, 'depositor': depositor, 'amount': amount})


@app.route('/api/deposit-webhook/test', methods=['POST'])
@login_required
def api_test_deposit_webhook():
    data = request.get_json(silent=True) or {}
    title = str(data.get('title') or '토스뱅크')
    body = str(data.get('body') or '5,000원 입금 홍길동')
    depositor, amount = monitor.parse(title, body)
    return jsonify({
        'ok': True,
        'depositor': depositor,
        'amount': amount,
        'parsed': bool(depositor and amount),
        'webhook_url': f'{_public_base_url()}/api/deposit-webhook?key={db.get_setting("deposit_webhook_key", "")}',
    })


@app.route('/ping')
def ping():
    return jsonify({'ok': True, 'bot_online': runtime.online, 'time': datetime.now(timezone.utc).isoformat()})


@app.after_request
def security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    if request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', '').lower() in ('1', 'true', 'yes'),
)


def main():
    runtime.start()
    port = int(os.environ.get('PORT', '5000'))
    logger.info(f'대시보드: http://127.0.0.1:{port}')
    try:
        from waitress import serve
        serve(app, host='0.0.0.0', port=port, threads=8, channel_timeout=60)
    except ImportError:
        app.run(host='0.0.0.0', port=port, debug=False, threaded=True, use_reloader=False)


if __name__ == '__main__':
    main()



