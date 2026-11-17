# -*- coding: utf-8 -*-
"""APEX OTT — Discord 봇

구매 → 구매 로그 → 관리자 지급(ID/PW · KEY · 기타 + 메시지) → 구매자 DM(이미지 + 클릭 복사)
30분 경과 시 구매자 DM에서 티켓을 열 수 있다.
"""
import asyncio
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands, ui
from discord.ext import commands

import database as db
import ott_card as cards

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

COLOR_MINT = 0x3DDC97
COLOR_VIOLET = 0x7C7CFF
COLOR_GOLD = 0xF0B429
COLOR_RED = 0xFF5D73
COLOR_GRAY = 0x8A96A6
COLOR_INK = 0x12161E

ADMIN_IDS = {
    int(part) for part in str(os.environ.get('ADMIN_IDS', '')).replace(' ', '').split(',')
    if part.isdigit()
}

STATUS_META = {
    'paid': ('구매 완료', COLOR_GOLD),
    'delivered': ('지급 완료', COLOR_MINT),
    'cancelled': ('취소', COLOR_RED),
}


def window_minutes():
    try:
        return max(1, int(db.get_setting('delivery_window_minutes', '30')))
    except (TypeError, ValueError):
        return 30


def is_admin(user):
    if isinstance(user, discord.Member):
        perms = getattr(user, 'guild_permissions', None)
        if perms is not None and perms.administrator:
            return True
    return db.is_admin(getattr(user, 'id', 0), ADMIN_IDS)


def to_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace(' ', 'T'))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def discord_ts(value, style='f'):
    dt = to_dt(value)
    if dt is None:
        return '-'
    return f'<t:{int(dt.timestamp())}:{style}>'


def make_embed(title=None, description=None, color=COLOR_MINT, footer='APEX OTT'):
    embed = discord.Embed(title=title, description=description, color=color)
    if footer:
        embed.set_footer(text=footer)
    return embed


class CardView(ui.LayoutView):
    """Components V2 카드: 텍스트·버튼·이미지를 하나의 컨테이너에 넣는다."""

    def __init__(self, text, rows=None, image=None, accent=COLOR_MINT, timeout=None):
        super().__init__(timeout=timeout)
        self.file = image if isinstance(image, discord.File) else None
        children = [ui.TextDisplay(text)]
        for row in rows or []:
            children.append(row)
        if image is not None:
            children.append(ui.MediaGallery(discord.MediaGalleryItem(image)))
        self.add_item(ui.Container(*children, accent_colour=accent))


def money(value):
    return f'{int(value or 0):,}P'


async def resolve_channel(bot, setting_key):
    raw = str(db.get_setting(setting_key, '') or '').strip()
    if not raw.isdigit():
        return None
    channel = bot.get_channel(int(raw))
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(int(raw))
    except Exception:
        return None


async def resolve_guild_channel(bot, channel_id):
    if not channel_id:
        return None
    channel = bot.get_channel(int(channel_id))
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(int(channel_id))
    except Exception:
        return None


async def try_dm(user, **kwargs):
    try:
        return await user.send(**kwargs)
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return None


def order_meta(order):
    return ' · '.join(part for part in [order.get('category'), order.get('order_no')] if part)


def status_label(order):
    return STATUS_META.get(order.get('status'), ('알 수 없음', COLOR_GRAY))[0]


def delivery_values(order):
    try:
        return json.loads(order.get('delivery_json') or '{}')
    except Exception:
        return {}


def credential_rows(order):
    values = delivery_values(order)
    kind = order.get('delivery_type')
    if kind == 'idpw':
        return [('아이디', str(values.get('id') or '')), ('비밀번호', str(values.get('pw') or ''))]
    if kind == 'key':
        return [('KEY', str(values.get('key') or ''))]
    if kind == 'note':
        return [('전달 내용', str(values.get('note') or ''))]
    return []


def grouped_products(products=None):
    products = products if products is not None else db.list_products(active_only=True)
    groups = {}
    for product in products:
        groups.setdefault(product.get('category') or '기타', []).append(product)
    return groups


def category_embed(category, products):
    lines = []
    for product in products:
        stock = int(product.get('stock') or 0)
        stock_text = stock_label(stock)
        lines.append(f'**{product["name"]}** · {money(product["price"])} · {stock_text}')
        if product.get('description'):
            lines.append(f'-# {product["description"]}')
    description = '\n'.join(lines) or '이 카테고리에 등록된 상품이 없습니다.'
    if len(description) > 3800:
        description = description[:3800] + '\n…'
    return make_embed(
        title=category,
        description=description + '\n\n-# 아래 메뉴에서 상품을 선택하세요.',
        footer='APEX OTT · 상품 선택',
    )


def product_embed(product, user_id=None):
    stock = int(product.get('stock') or 0)
    embed = make_embed(
        title=product['name'],
        description=product.get('description') or '관리자 확인 후 DM으로 전달되는 상품입니다.',
        footer='APEX OTT · 결제 후 관리자 확인',
    )
    embed.add_field(name='가격', value=money(product['price']), inline=True)
    embed.add_field(name='카테고리', value=product.get('category') or '기타', inline=True)
    embed.add_field(name='재고', value=stock_label(stock), inline=True)
    if user_id is not None:
        balance = db.get_balance(user_id)
        remain = balance - int(product['price'])
        embed.add_field(
            name='보유 포인트',
            value=f'{money(balance)}' + (f' · 결제 후 {money(remain)}' if remain >= 0 else ' · 포인트 부족'),
            inline=False,
        )
    embed.add_field(
        name='전달 안내',
        value=f'결제 후 {window_minutes()}분 이내에 관리자가 확인하여 DM으로 전달합니다.',
        inline=False,
    )
    return embed


def category_text(category, products):
    lines = [f'## {category}', '']
    for product in products:
        stock = int(product.get('stock') or 0)
        lines.append(f'**{product["name"]}** · {money(product["price"])} · {stock_label(stock)}')
        if product.get('description'):
            lines.append(f'-# {product["description"]}')
    if not products:
        lines.append('등록된 상품이 없습니다.')
    lines.extend(['', '-# 아래 메뉴에서 상품을 선택하세요.'])
    return '\n'.join(lines)[:3900]


def product_text(product, user_id=None):
    stock = int(product.get('stock') or 0)
    lines = [
        f'## {product["name"]}',
        product.get('description') or '관리자 확인 후 DM으로 전달되는 상품입니다.',
        '',
        f'**가격** {money(product["price"])}',
        f'**카테고리** {product.get("category") or "기타"}',
        f'**재고** {stock_label(stock)}',
    ]
    if user_id is not None:
        balance = db.get_balance(user_id)
        remain = balance - int(product['price'])
        lines.append(f'**보유 포인트** {money(balance)}'
                     + (f' · 결제 후 {money(remain)}' if remain >= 0 else ' · 포인트 부족'))
    lines.extend(['', f'-# 결제 후 {window_minutes()}분 이내에 관리자가 확인하여 DM으로 전달합니다.'])
    return '\n'.join(lines)


class CategorySelect(ui.Select):
    def __init__(self, bot):
        self.bot = bot
        groups = grouped_products()
        options = []
        for category, items in list(groups.items())[:25]:
            prices = [int(p['price']) for p in items]
            options.append(discord.SelectOption(
                label=category[:100],
                value=category[:100],
                description=f'{len(items)}개 상품 · {min(prices):,}P~ · {category_stock_text(items)}',
            ))
        if not options:
            options = [discord.SelectOption(label='등록된 상품이 없습니다', value='__none__', description='관리자에게 문의하세요')]
        super().__init__(placeholder='사용할 카테고리를 선택하세요', min_values=1, max_values=1,
                         options=options, custom_id='ott:catalog:category')

    async def callback(self, interaction: discord.Interaction):
        category = self.values[0]
        if category == '__none__':
            await interaction.response.send_message('현재 구매 가능한 상품이 없습니다.', ephemeral=True)
            return
        products = [p for p in db.list_products(active_only=True)
                    if (p.get('category') or '기타') == category]
        await interaction.response.send_message(
            view=ProductSelectView(self.bot, category, products),
            ephemeral=True,
        )


class CategoryPickSelect(ui.Select):
    def __init__(self):
        groups = grouped_products()
        options = []
        for category, items in list(groups.items())[:25]:
            prices = [int(p['price']) for p in items]
            options.append(discord.SelectOption(
                label=category[:100],
                value=category[:100],
                description=f'{len(items)}개 상품 · {min(prices):,}P~ · {category_stock_text(items)}',
            ))
        if not options:
            options = [discord.SelectOption(label='등록된 상품이 없습니다', value='__none__')]
        super().__init__(placeholder='사용할 카테고리를 선택하세요', min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        category = self.values[0]
        if category == '__none__':
            await interaction.response.edit_message(
                embed=make_embed('상품 준비 중', '현재 구매 가능한 상품이 없습니다.', COLOR_GOLD), view=None)
            return
        products = [p for p in db.list_products(active_only=True)
                    if (p.get('category') or '기타') == category]
        await interaction.response.edit_message(
            view=ProductSelectView(interaction.client, category, products)
        )


class CategoryPickView(ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(ui.Container(
            ui.TextDisplay('## 상품 선택\n사용할 카테고리를 선택해주세요.'),
            ui.ActionRow(CategoryPickSelect()),
            accent_colour=COLOR_VIOLET,
        ))


class PanelButton(ui.Button):
    def __init__(self, bot, label, custom_id, action, style=discord.ButtonStyle.secondary, emoji=None):
        self.bot = bot
        self.action = action
        super().__init__(label=label, style=style, custom_id=custom_id, emoji=emoji)

    async def callback(self, interaction: discord.Interaction):
        if self.action == 'products':
            await interaction.response.send_message(
                view=CategoryPickView(), ephemeral=True)
        elif self.action == 'charge':
            await show_charge_guide(interaction)
        elif self.action == 'me':
            await show_my_info(interaction)


def stock_label(stock):
    try:
        value = int(stock)
    except (TypeError, ValueError):
        value = 0
    if value < 0:
        return '무제한'
    if value == 0:
        return '품절'
    return f'{value}개'


def stock_summary(products):
    total = 0
    unlimited = 0
    for product in products:
        stock = int(product.get('stock') or 0)
        if stock < 0:
            unlimited += 1
        else:
            total += stock
    if products and unlimited == len(products):
        return '재고 무제한'
    if unlimited:
        return f'재고 {total}개 · 무제한 {unlimited}종'
    return f'재고 {total}개'


def category_stock_text(items):
    if any(int(p.get('stock') or 0) < 0 for p in items):
        return '재고 무제한'
    total = sum(max(0, int(p.get('stock') or 0)) for p in items)
    return f'재고 {total}'


class CatalogView(ui.LayoutView):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
        products = db.list_products(active_only=True)
        in_stock = sum(1 for p in products if int(p.get('stock') or 0) != 0)
        store_name = str(db.get_setting('store_name', 'APEX OTT') or 'APEX OTT')

        children = [
            ui.TextDisplay(f'## {store_name}'),
            ui.TextDisplay(
                '원하는 메뉴를 선택해주세요.\n\n'
                f'**상품 {len(products)}개 · {stock_summary(products)} · 판매중 {in_stock}종**'
            ),
            ui.ActionRow(CategorySelect(bot)),
            ui.ActionRow(
                PanelButton(bot, '제품', 'ott:panel:products', 'products',
                            discord.ButtonStyle.primary, '🛍️'),
                PanelButton(bot, '충전', 'ott:panel:charge', 'charge',
                            discord.ButtonStyle.success, '💳'),
                PanelButton(bot, '정보', 'ott:panel:me', 'me',
                            discord.ButtonStyle.secondary, 'ℹ️'),
            ),
            ui.Separator(),
            ui.TextDisplay('-# APEX OTT · 24/7 ORDER'),
        ]

        banner = str(db.get_setting('panel_banner_url', '') or '').strip()
        if banner.startswith('http://') or banner.startswith('https://'):
            children.append(ui.MediaGallery(discord.MediaGalleryItem(banner)))

        self.add_item(ui.Container(*children, accent_colour=COLOR_MINT))


async def install_panel(bot, channel_id, banner_url=None):
    channel = await resolve_guild_channel(bot, channel_id)
    if channel is None:
        return False, '채널을 찾을 수 없습니다.'

    if banner_url is not None:
        db.set_setting('panel_banner_url', str(banner_url).strip())

    store_name = str(db.get_setting('store_name', 'APEX OTT') or 'APEX OTT')

    # 기존 패널/배너 메시지 정리
    old_panel_id = str(db.get_setting('panel_message_id', '') or '').strip()
    if old_panel_id.isdigit():
        try:
            old = await channel.fetch_message(int(old_panel_id))
            await old.delete()
        except Exception:
            pass

    old_image_id = str(db.get_setting('panel_image_message_id', '') or '').strip()
    if old_image_id.isdigit():
        try:
            old_image = await channel.fetch_message(int(old_image_id))
            await old_image.delete()
        except Exception:
            pass

    try:
        history = [message async for message in channel.history(limit=30)]
    except Exception:
        history = []
    for message in history:
        if message.author.id != bot.user.id:
            continue
        if message.embeds and message.embeds[0].title in (store_name, '상품 카탈로그', 'APEX VENDING'):
            try:
                await message.delete()
            except Exception:
                pass

    view = CatalogView(bot)
    try:
        message = await channel.send(view=view)
    except Exception as exc:
        return False, f'패널을 전송할 수 없습니다: {exc}'

    db.update_settings({
        'panel_channel_id': str(channel.id),
        'panel_message_id': str(message.id),
        'panel_image_message_id': '',
    })
    return True, '패널을 설치했습니다.'


class ProductSelect(ui.Select):
    def __init__(self, bot, products):
        self.bot = bot
        options = []
        for product in products[:25]:
            stock = int(product.get('stock') or 0)
            options.append(discord.SelectOption(
                label=product['name'][:100],
                value=str(product['id']),
                description=f'{int(product["price"]):,}P · {stock_label(stock)}',
            ))
        super().__init__(placeholder='상품을 선택하세요', min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        product = db.get_product(int(self.values[0]))
        if not product:
            await interaction.response.edit_message(embed=make_embed('상품 없음', '삭제된 상품입니다.', COLOR_RED), view=None)
            return
        category = product.get('category') or '기타'
        await interaction.response.edit_message(
            view=ProductDetailView(self.bot, product, category, interaction.user.id)
        )


class ProductSelectView(ui.LayoutView):
    def __init__(self, bot, category, products):
        super().__init__(timeout=300)
        self.add_item(ui.Container(
            ui.TextDisplay(category_text(category, products)),
            ui.ActionRow(ProductSelect(bot, products)),
            accent_colour=COLOR_VIOLET,
        ))


class BackToCategoryButton(ui.Button):
    def __init__(self, bot, category):
        self.bot = bot
        self.category = category
        super().__init__(label='목록으로', style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        products = [p for p in db.list_products(active_only=True)
                    if (p.get('category') or '기타') == self.category]
        await interaction.response.edit_message(
            view=ProductSelectView(self.bot, self.category, products)
        )


class BuyButton(ui.Button):
    def __init__(self, bot, product_id, price):
        self.bot = bot
        self.product_id = int(product_id)
        super().__init__(label=f'구매하기 · {int(price):,}P', style=discord.ButtonStyle.success, row=0)

    async def callback(self, interaction: discord.Interaction):
        user = interaction.user
        db.ensure_user(user.id, user.name)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, result = db.purchase_product(user.id, user.name, self.product_id)
        if not ok:
            balance = db.get_balance(user.id)
            embed = make_embed(
                '구매할 수 없습니다',
                f'{result}\n\n현재 보유 포인트: **{money(balance)}**',
                COLOR_RED,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        await finalize_purchase(self.bot, interaction, result)


class ProductDetailView(ui.LayoutView):
    def __init__(self, bot, product, category, user_id=None):
        super().__init__(timeout=300)
        stock = int(product.get('stock') or 0)
        if stock != 0 and int(product.get('active') or 0):
            buy = BuyButton(bot, product['id'], product['price'])
        else:
            buy = ui.Button(label='품절', style=discord.ButtonStyle.secondary, disabled=True)
        self.add_item(ui.Container(
            ui.TextDisplay(product_text(product, user_id)),
            ui.ActionRow(buy, BackToCategoryButton(bot, category)),
            accent_colour=COLOR_MINT,
        ))


async def finalize_purchase(bot, interaction, order):
    user = interaction.user
    balance = db.get_balance(user.id)
    dm_message = await send_purchase_dm(bot, user, order, balance)
    await send_order_log(bot, order, balance)

    if dm_message:
        description = (
            '주문이 접수되었습니다. 전송된 DM에서 주문 정보를 확인해 주세요.\n'
            f'관리자 확인 후 **{window_minutes()}분 이내**에 상품이 전달됩니다.'
        )
        color = COLOR_MINT
    else:
        description = (
            '주문은 정상적으로 접수되었습니다.\n'
            '다만 DM 수신이 차단되어 안내 메시지를 보내지 못했습니다. 구매 로그를 통해 관리자가 확인합니다.'
        )
        color = COLOR_GOLD

    embed = make_embed(f'주문 완료 · {order["order_no"]}', description, color)
    embed.add_field(name='상품', value=order['product_name'], inline=True)
    embed.add_field(name='결제 금액', value=money(order['amount']), inline=True)
    embed.add_field(name='결제 후 잔액', value=money(balance), inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)


async def send_purchase_dm(bot, user, order, balance):
    png = None
    if str(db.get_setting('card_image_enabled', 'true')).lower() != 'false':
        try:
            png = await asyncio.to_thread(
                cards.render_order_card,
                order['product_name'], order['order_no'], order.get('category') or '',
                order['amount'], user.name, balance, None, window_minutes(),
            )
        except Exception as exc:
            logger.warning(f'[card] 주문 카드 실패: {exc}')

    text = '\n'.join([
        '## 주문이 접수되었습니다',
        '관리자가 상품을 확인한 뒤 이 DM으로 전달합니다.',
        f'-# 전달까지 최대 {window_minutes()}분이 소요될 수 있습니다.',
        '',
        f'**상품** {order["product_name"]}',
        f'**주문번호** `{order["order_no"]}`',
        f'**결제 금액** {money(order["amount"])}',
        f'**결제 후 잔액** {money(balance)}',
        f'**접수 시각** {discord_ts(order["created_at"])}',
    ])
    image = discord.File(io.BytesIO(png), filename=f'APEX-OTT-{order["order_no"]}.png') if png else None
    view = CardView(
        text,
        rows=[ui.ActionRow(TicketOpenButton(bot, order['id'], order['user_id']))],
        image=image,
        accent=COLOR_VIOLET,
    )
    kwargs = {'view': view}
    if view.file is not None:
        kwargs['file'] = view.file
    return await try_dm(user, **kwargs)


def order_log_text(order, balance=None, status=None):
    status = status or order.get('status') or 'paid'
    label, _ = STATUS_META.get(status, ('알 수 없음', COLOR_GRAY))
    lines = [
        f'## {label} · `{order["order_no"]}`',
        f'구매자 <@{order["user_id"]}> · `{order.get("username") or order["user_id"]}`',
        '',
        f'**상품** {order["product_name"]}',
        f'**결제 금액** {money(order["amount"])}',
        f'**주문 상태** {label}',
    ]
    if balance is not None:
        lines.append(f'**결제 후 잔액** {money(balance)}')
    lines.append(f'**주문 시각** {discord_ts(order["created_at"])}')
    if status == 'delivered':
        lines.append(f'**지급 시각** {discord_ts(order.get("delivered_at"))}')
        lines.append(f'**지급 방식** {db.DELIVERY_TYPE_LABEL.get(order.get("delivery_type"), "-")}')
    if status == 'cancelled':
        lines.append(f'**취소 시각** {discord_ts(order.get("cancelled_at"))}')
    return '\n'.join(lines)


def order_log_card(bot, order, balance=None):
    _, color = STATUS_META.get(order.get('status'), ('알 수 없음', COLOR_GRAY))
    rows = []
    if order.get('status') == 'paid':
        rows.append(ui.ActionRow(
            DeliverButton(bot, order['id']),
            CancelOrderButton(bot, order['id']),
        ))
    return CardView(order_log_text(order, balance), rows=rows, accent=color)


async def send_order_log(bot, order, balance):
    view = order_log_card(bot, order, balance)
    channel = await resolve_channel(bot, 'purchase_log_channel_id')
    message = None
    if channel is not None:
        try:
            message = await channel.send(view=view)
        except Exception as exc:
            logger.warning(f'[order] 구매 로그 전송 실패: {exc}')
    if message is None:
        ids = set(ADMIN_IDS)
        ids |= {int(a['user_id']) for a in db.list_admins() if str(a['user_id']).isdigit()}
        for admin_id in sorted(ids):
            admin = bot.get_user(admin_id) or await bot.fetch_user(admin_id)
            if admin:
                try:
                    message = await admin.send(view=order_log_card(bot, order, balance))
                except Exception:
                    continue
    if message is not None:
        db.set_order_log(order['id'], message.channel.id, message.id)
    return message


def safe_code(value):
    text = str(value or '').replace('```', '` ` `').strip()
    return text[:950]


def delivery_embed(order, for_admin=False):
    values = delivery_values(order)
    kind = order.get('delivery_type')
    embed = make_embed(
        '상품이 전달되었습니다',
        '아래 코드 블록을 클릭하면 값이 그대로 복사됩니다.\n'
        '-# 계정 정보는 본인만 확인할 수 있도록 보관해 주세요.',
        COLOR_MINT,
        footer='APEX OTT · DELIVERY',
    )
    embed.add_field(name='상품', value=order['product_name'], inline=True)
    embed.add_field(name='주문번호', value=order['order_no'], inline=True)
    embed.add_field(name='지급 방식', value=db.DELIVERY_TYPE_LABEL.get(kind, '-'), inline=True)

    if kind == 'idpw':
        embed.add_field(name='아이디', value=f'```\n{safe_code(values.get("id"))}\n```', inline=False)
        embed.add_field(name='비밀번호', value=f'```\n{safe_code(values.get("pw"))}\n```', inline=False)
    elif kind == 'key':
        embed.add_field(name='KEY', value=f'```\n{safe_code(values.get("key"))}\n```', inline=False)
    elif kind == 'note':
        embed.add_field(name='전달 내용', value=safe_code(values.get('note')) or '-', inline=False)

    if order.get('delivery_message'):
        embed.add_field(name='메시지', value=str(order['delivery_message'])[:1000], inline=False)

    embed.add_field(name='지급 시각', value=discord_ts(order.get('delivered_at')), inline=False)
    if for_admin:
        embed.set_footer(text='APEX OTT · 관리자 확인용')
    return embed


async def send_delivery_dm(bot, order):
    user_id = int(order['user_id'])
    user = bot.get_user(user_id)
    if user is None:
        try:
            user = await bot.fetch_user(user_id)
        except Exception:
            return False

    png = None
    if str(db.get_setting('card_image_enabled', 'true')).lower() != 'false':
        try:
            png = await asyncio.to_thread(
                cards.render_delivery_card,
                order['product_name'], order['order_no'], order.get('category') or '',
                order.get('delivery_type'), delivery_values(order),
                order.get('delivery_message') or '', user.name, to_dt(order.get('delivered_at')),
            )
        except Exception as exc:
            logger.warning(f'[card] 전달 카드 실패: {exc}')

    values = delivery_values(order)
    kind = order.get('delivery_type')
    lines = [
        '## 상품이 전달되었습니다',
        '아래 코드 블록을 클릭하면 값이 그대로 복사됩니다.',
        '-# 계정 정보는 본인만 확인할 수 있도록 보관해 주세요.',
        '',
        f'**상품** {order["product_name"]}',
        f'**주문번호** `{order["order_no"]}`',
        f'**지급 방식** {db.DELIVERY_TYPE_LABEL.get(kind, "-")}',
    ]
    if kind == 'idpw':
        lines.extend([
            '',
            '**아이디**',
            f'```\n{safe_code(values.get("id"))}\n```',
            '**비밀번호**',
            f'```\n{safe_code(values.get("pw"))}\n```',
        ])
    elif kind == 'key':
        lines.extend(['', '**KEY**', f'```\n{safe_code(values.get("key"))}\n```'])
    elif kind == 'note':
        lines.extend(['', '**전달 내용**', safe_code(values.get('note')) or '-'])
    if order.get('delivery_message'):
        lines.extend(['', f'**메시지** {order["delivery_message"]}'])
    lines.append(f'**지급 시각** {discord_ts(order.get("delivered_at"))}')

    image = discord.File(
        io.BytesIO(png), filename=f'APEX-OTT-{order["order_no"]}.png'
    ) if png else None
    view = CardView(
        '\n'.join(lines),
        rows=[ui.ActionRow(
            TicketOpenButton(bot, order['id'], order['user_id']),
            ReviewButton(bot, order['id'], order['user_id']),
        )],
        image=image,
        accent=COLOR_MINT,
    )
    kwargs = {'view': view}
    if view.file is not None:
        kwargs['file'] = view.file
    message = await try_dm(user, **kwargs)
    return message is not None


async def edit_order_log(bot, order):
    channel = await resolve_guild_channel(bot, order.get('log_channel_id'))
    if channel is None:
        channel = await resolve_channel(bot, 'purchase_log_channel_id')
    view = order_log_card(bot, order)
    if channel is not None and order.get('log_message_id'):
        try:
            message = await channel.fetch_message(int(order['log_message_id']))
            await message.edit(view=view, embeds=[])
            return True
        except Exception as exc:
            logger.warning(f'[order] 로그 수정 실패: {exc}')
    if channel is not None:
        try:
            message = await channel.send(view=view)
            db.set_order_log(order['id'], channel.id, message.id)
            return True
        except Exception:
            pass
    return False


async def deliver_and_notify(bot, order_id, delivery_type, values, message, admin_user):
    ok, result = db.deliver_order(
        order_id, delivery_type, values, message,
        delivered_by=f'{admin_user} ({getattr(admin_user, "id", "")})',
    )
    if not ok:
        return False, result, None

    order = result
    dm_ok = await send_delivery_dm(bot, order)
    await edit_order_log(bot, order)

    if not dm_ok:
        channel = await resolve_guild_channel(bot, order.get('log_channel_id'))
        if channel is not None:
            try:
                await channel.send(
                    content=f'<@{order["user_id"]}> DM 전송에 실패했습니다. '
                            f'주문 `{order["order_no"]}` — `/재전송` 으로 다시 시도할 수 있습니다.'
                )
            except Exception:
                pass
    return True, order, dm_ok


async def resend_delivery(bot, order):
    if order.get('status') != 'delivered':
        return False, '지급 완료된 주문만 재전송할 수 있습니다.'
    ok = await send_delivery_dm(bot, order)
    return (True, '전송했습니다.') if ok else (False, 'DM 전송에 실패했습니다. 사용자가 봇 DM을 차단했을 수 있습니다.')


class OrderAdminView(ui.View):
    def __init__(self, bot, order_id):
        super().__init__(timeout=None)
        self.add_item(DeliverButton(bot, order_id))
        self.add_item(CancelOrderButton(bot, order_id))


class DeliverButton(ui.Button):
    def __init__(self, bot, order_id):
        self.bot = bot
        self.order_id = int(order_id)
        super().__init__(label='상품 지급', style=discord.ButtonStyle.success,
                         custom_id=f'ott:order:deliver:{self.order_id}', row=0)

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message('관리자만 사용할 수 있습니다.', ephemeral=True)
            return
        order = db.get_order(self.order_id)
        if not order or order['status'] != 'paid':
            await interaction.response.send_message('이미 처리된 주문입니다.', ephemeral=True)
            return
        await interaction.response.send_message(
            view=DeliveryTypeView(self.bot, self.order_id), ephemeral=True)


class CancelOrderButton(ui.Button):
    def __init__(self, bot, order_id):
        self.bot = bot
        self.order_id = int(order_id)
        super().__init__(label='주문 취소', style=discord.ButtonStyle.danger,
                         custom_id=f'ott:order:cancel:{self.order_id}', row=0)

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message('관리자만 사용할 수 있습니다.', ephemeral=True)
            return
        order = db.get_order(self.order_id)
        if not order or order['status'] != 'paid':
            await interaction.response.send_message('이미 처리된 주문입니다.', ephemeral=True)
            return
        await interaction.response.send_message(
            view=ConfirmCancelView(self.bot, self.order_id), ephemeral=True)


class CloseEphemeralButton(ui.Button):
    def __init__(self):
        super().__init__(label='닫기', style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=None)


class ConfirmCancelButton(ui.Button):
    def __init__(self, bot, order_id):
        self.bot = bot
        self.order_id = int(order_id)
        super().__init__(label='취소 확정', style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message('관리자만 사용할 수 있습니다.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, result = db.cancel_order(self.order_id, decided_by=f'{interaction.user} ({interaction.user.id})')
        if not ok:
            await interaction.followup.send(embed=make_embed('취소 실패', result, COLOR_RED), ephemeral=True)
            return

        order = result
        user = self.bot.get_user(int(order['user_id']))
        if user:
            embed = make_embed(
                '주문이 취소되었습니다',
                f'주문 `{order["order_no"]}` 이 취소되어 **{money(order["amount"])}** 이 복구되었습니다.',
                COLOR_GOLD,
                footer='APEX OTT · 주문 취소',
            )
            await try_dm(user, embed=embed)
        await edit_order_log(self.bot, order)
        embed = make_embed('주문 취소 완료', f'`{order["order_no"]}` · {money(order["amount"])} 복구', COLOR_MINT)
        await interaction.followup.send(embed=embed, ephemeral=True)


class ConfirmCancelView(ui.LayoutView):
    def __init__(self, bot, order_id):
        super().__init__(timeout=120)
        order = db.get_order(order_id)
        product = order['product_name'] if order else '주문'
        amount = money(order['amount']) if order else '-'
        self.add_item(ui.Container(
            ui.TextDisplay(
                '## 주문 취소 확인\n'
                f'**{product}** · {amount}\n'
                '취소하면 결제 포인트와 재고가 자동으로 복구됩니다.'
            ),
            ui.ActionRow(ConfirmCancelButton(bot, order_id), CloseEphemeralButton()),
            accent_colour=COLOR_RED,
        ))


class DeliveryModalButton(ui.Button):
    def __init__(self, bot, order_id, kind, label, style):
        self.bot = bot
        self.order_id = int(order_id)
        self.kind = kind
        super().__init__(label=label, style=style)

    async def callback(self, interaction: discord.Interaction):
        modal_class = {'idpw': IdPwModal, 'key': KeyModal, 'note': NoteModal}[self.kind]
        await interaction.response.send_modal(modal_class(self.bot, self.order_id))


class DeliveryTypeView(ui.LayoutView):
    def __init__(self, bot, order_id):
        super().__init__(timeout=300)
        order = db.get_order(order_id)
        product = order['product_name'] if order else '주문'
        self.add_item(ui.Container(
            ui.TextDisplay(
                '## 지급 방식 선택\n'
                f'**{product}**\n'
                '전달할 정보의 종류를 선택하면 입력 창이 열립니다.'
            ),
            ui.ActionRow(
                DeliveryModalButton(bot, order_id, 'idpw', 'ID · PW', discord.ButtonStyle.primary),
                DeliveryModalButton(bot, order_id, 'key', 'KEY', discord.ButtonStyle.primary),
                DeliveryModalButton(bot, order_id, 'note', '기타', discord.ButtonStyle.secondary),
            ),
            ui.ActionRow(CloseEphemeralButton()),
            accent_colour=COLOR_VIOLET,
        ))


async def report_delivery_result(interaction, ok, result, dm_ok):
    if not ok:
        await interaction.followup.send(embed=make_embed('지급 실패', result, COLOR_RED), ephemeral=True)
        return

    order = result
    if dm_ok:
        embed = make_embed(
            '지급 완료',
            f'`{order["order_no"]}` 상품을 구매자 DM으로 전송했습니다.',
            COLOR_MINT,
        )
        embed.add_field(name='상품', value=order['product_name'], inline=True)
        embed.add_field(name='구매자', value=f'<@{order["user_id"]}>', inline=True)
        embed.add_field(name='지급 방식', value=db.DELIVERY_TYPE_LABEL.get(order['delivery_type'], '-'), inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    embed = make_embed(
        '지급 처리 완료 · DM 실패',
        '주문은 지급 완료로 변경했지만 구매자 DM 전송에 실패했습니다.\n'
        '사용자가 DM을 차단했을 수 있습니다. 아래 정보를 확인한 뒤 `/재전송` 으로 재시도하세요.',
        COLOR_GOLD,
    )
    for name, value in credential_rows(order):
        embed.add_field(name=name, value=f'```\n{safe_code(value)}\n```', inline=False)
    if order.get('delivery_message'):
        embed.add_field(name='메시지', value=str(order['delivery_message'])[:1000], inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


class IdPwModal(ui.Modal, title='계정 지급 · ID / PW'):
    account_id = ui.TextInput(label='아이디', placeholder='전달할 계정 아이디', max_length=200)
    password = ui.TextInput(label='비밀번호', placeholder='전달할 비밀번호', max_length=200)
    message = ui.TextInput(label='메시지 (선택)', placeholder='구매자에게 함께 전달할 안내 문구',
                           style=discord.TextStyle.paragraph, required=False, max_length=600)

    def __init__(self, bot, order_id):
        super().__init__()
        self.bot = bot
        self.order_id = int(order_id)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message('관리자만 사용할 수 있습니다.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, result, dm_ok = await deliver_and_notify(
            self.bot, self.order_id, 'idpw',
            {'id': self.account_id.value, 'pw': self.password.value},
            self.message.value or '', interaction.user,
        )
        await report_delivery_result(interaction, ok, result, dm_ok)


class KeyModal(ui.Modal, title='상품 지급 · KEY'):
    key_value = ui.TextInput(label='KEY / 코드', placeholder='전달할 키 또는 교환 코드',
                             style=discord.TextStyle.paragraph, max_length=1000)
    message = ui.TextInput(label='메시지 (선택)', placeholder='구매자에게 함께 전달할 안내 문구',
                           style=discord.TextStyle.paragraph, required=False, max_length=600)

    def __init__(self, bot, order_id):
        super().__init__()
        self.bot = bot
        self.order_id = int(order_id)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message('관리자만 사용할 수 있습니다.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, result, dm_ok = await deliver_and_notify(
            self.bot, self.order_id, 'key',
            {'key': self.key_value.value},
            self.message.value or '', interaction.user,
        )
        await report_delivery_result(interaction, ok, result, dm_ok)


class NoteModal(ui.Modal, title='상품 지급 · 기타'):
    note = ui.TextInput(label='전달 내용', placeholder='링크, 안내문 등 자유롭게 입력',
                        style=discord.TextStyle.paragraph, max_length=1000)
    message = ui.TextInput(label='메시지 (선택)', placeholder='구매자에게 함께 전달할 안내 문구',
                           style=discord.TextStyle.paragraph, required=False, max_length=600)

    def __init__(self, bot, order_id):
        super().__init__()
        self.bot = bot
        self.order_id = int(order_id)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message('관리자만 사용할 수 있습니다.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, result, dm_ok = await deliver_and_notify(
            self.bot, self.order_id, 'note',
            {'note': self.note.value},
            self.message.value or '', interaction.user,
        )
        await report_delivery_result(interaction, ok, result, dm_ok)


async def show_my_info(interaction: discord.Interaction):
    user = interaction.user
    db.ensure_user(user.id, user.name)
    row = db.get_user(user.id) or {}
    summary = db.user_order_summary(user.id)
    recent = db.user_recent_orders(user.id, 3)

    embed = make_embed('내 정보', footer='APEX OTT · ACCOUNT')
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name='보유 포인트', value=f'**{money(row.get("balance"))}**', inline=True)
    embed.add_field(name='주문', value=f'{summary.get("total", 0)}건', inline=True)
    embed.add_field(name='지급 완료', value=f'{summary.get("delivered", 0)}건', inline=True)

    if recent:
        lines = []
        for order in recent:
            label = STATUS_META.get(order['status'], ('-', COLOR_GRAY))[0]
            lines.append(f'`{order["order_no"]}` · {order["product_name"]} · {label}')
        embed.add_field(name='최근 주문', value='\n'.join(lines)[:1024], inline=False)
    else:
        embed.add_field(name='최근 주문', value='아직 주문 내역이 없습니다.', inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


def charge_embed(charge, status='pending'):
    label = {'pending': '입금 대기', 'completed': '충전 완료', 'cancelled': '취소'}.get(status, status)
    color = {'pending': COLOR_GOLD, 'completed': COLOR_MINT, 'cancelled': COLOR_RED}.get(status, COLOR_GRAY)
    embed = make_embed(f'충전 신청 · {label}', footer='APEX OTT · CHARGE LOG', color=color)
    embed.add_field(name='신청자', value=f'<@{charge["user_id"]}>\n`{charge.get("username") or "-"}`', inline=True)
    embed.add_field(name='입금자명', value=charge['depositor_name'], inline=True)
    embed.add_field(name='금액', value=money(charge['amount']), inline=True)
    embed.add_field(name='신청 시각', value=discord_ts(charge['created_at']), inline=True)
    if charge.get('completed_at'):
        embed.add_field(name='처리 시각', value=discord_ts(charge['completed_at']), inline=True)
    return embed


async def notify_charge_completed(bot, charge):
    user = bot.get_user(int(charge['user_id']))
    if user is None:
        try:
            user = await bot.fetch_user(int(charge['user_id']))
        except Exception:
            user = None
    if user:
        embed = make_embed(
            '포인트 충전 완료',
            f'**{money(charge["amount"])}** 이 충전되었습니다.\n'
            f'현재 보유 포인트: **{money(db.get_balance(charge["user_id"]))}**',
            COLOR_MINT,
            footer='APEX OTT · CHARGE',
        )
        await try_dm(user, embed=embed)

    channel = await resolve_channel(bot, 'charge_log_channel_id')
    if channel:
        try:
            await channel.send(embed=charge_embed(charge, 'completed'))
        except Exception:
            pass


class ChargeModal(ui.Modal, title='포인트 충전 신청'):
    depositor = ui.TextInput(label='입금자명', placeholder='실제 입금자명과 동일하게 입력',
                             min_length=2, max_length=20)
    amount = ui.TextInput(label='충전 금액 (원)', placeholder='예: 10000', max_length=10)

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.amount.value).replace(',', '').replace('원', '').strip()
        if not raw.isdigit() or not (1000 <= int(raw) <= 1_000_000):
            await interaction.response.send_message('금액은 1,000원 이상 1,000,000원 이하로 입력해 주세요.', ephemeral=True)
            return

        user = interaction.user
        db.ensure_user(user.id, user.name)
        charge = db.create_charge(user.id, user.name, self.depositor.value, int(raw))

        bank_name = db.get_setting('bank_name', '') or '은행'
        bank_account = db.get_setting('bank_account', '') or '계좌 미설정'
        bank_holder = db.get_setting('bank_holder', '') or '-'
        user_embed = make_embed(
            '충전 신청이 접수되었습니다',
            f'{bank_name} **{bank_account}** ({bank_holder}) 로 입금해 주세요.\n'
            f'입금자명 **{charge["depositor_name"]}** · 금액 **{money(charge["amount"])}**\n'
            '-# 입금이 확인되면 자동으로 포인트가 지급됩니다.',
            COLOR_VIOLET,
            footer='APEX OTT · CHARGE',
        )
        await try_dm(user, embed=user_embed)

        channel = await resolve_channel(self.bot, 'charge_log_channel_id')
        if channel:
            try:
                await channel.send(view=charge_card(self.bot, charge, 'pending'))
            except Exception:
                pass

        embed = make_embed(
            '충전 신청 완료',
            f'**{money(charge["amount"])}** · 입금자명 `{charge["depositor_name"]}`\n'
            f'{bank_name} {bank_account} ({bank_holder}) 로 입금해 주세요.',
            COLOR_MINT,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ChargeGuideView(ui.LayoutView):
    def __init__(self, bot, bank_name, bank_account, bank_holder):
        super().__init__(timeout=300)
        self.add_item(ui.Container(
            ui.TextDisplay(
                '## 포인트 충전\n'
                f'**입금 계좌** {bank_name} `{bank_account}`\n'
                f'**예금주** {bank_holder or "-"}\n\n'
                '충전 신청서의 입금자명과 실제 입금자명이 같아야 자동 충전됩니다.\n'
                '금액이 일치하지 않으면 관리자가 직접 확인합니다.'
            ),
            ui.ActionRow(ChargeRequestButton(bot)),
            accent_colour=COLOR_VIOLET,
        ))


class ChargeRequestButton(ui.Button):
    def __init__(self, bot):
        self.bot = bot
        super().__init__(label='충전 신청', style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ChargeModal(self.bot))


async def show_charge_guide(interaction: discord.Interaction):
    bank_name = db.get_setting('bank_name', '') or ''
    bank_account = db.get_setting('bank_account', '') or ''
    bank_holder = db.get_setting('bank_holder', '') or ''

    if bank_account:
        await interaction.response.send_message(
            view=ChargeGuideView(interaction.client, bank_name, bank_account, bank_holder),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        view=CardView(
            '## 포인트 충전\n충전 계좌가 아직 설정되지 않았습니다. 관리자에게 문의해 주세요.',
            accent=COLOR_GOLD,
        ),
        ephemeral=True,
    )


def charge_text(charge, status='pending'):
    label = {'pending': '입금 대기', 'completed': '충전 완료', 'cancelled': '취소'}.get(status, status)
    lines = [
        f'## 충전 신청 · {label}',
        f'**신청자** <@{charge["user_id"]}> · `{charge.get("username") or "-"}`',
        f'**입금자명** {charge["depositor_name"]}',
        f'**금액** {money(charge["amount"])}',
        f'**신청 시각** {discord_ts(charge["created_at"])}',
    ]
    if charge.get('completed_at'):
        lines.append(f'**처리 시각** {discord_ts(charge["completed_at"])}')
    return '\n'.join(lines)


def charge_card(bot, charge, status='pending'):
    color = {'pending': COLOR_GOLD, 'completed': COLOR_MINT, 'cancelled': COLOR_RED}.get(status, COLOR_GRAY)
    rows = []
    if status == 'pending':
        rows.append(ui.ActionRow(
            ApproveChargeButton(bot, charge['id']),
            CancelChargeButton(bot, charge['id']),
        ))
    return CardView(charge_text(charge, status), rows=rows, accent=color)


class ChargeAdminView(ui.View):
    def __init__(self, bot, charge_id):
        super().__init__(timeout=None)
        self.add_item(ApproveChargeButton(bot, charge_id))
        self.add_item(CancelChargeButton(bot, charge_id))


class ApproveChargeButton(ui.Button):
    def __init__(self, bot, charge_id):
        self.bot = bot
        self.charge_id = int(charge_id)
        super().__init__(label='입금 확인', style=discord.ButtonStyle.success,
                         custom_id=f'ott:charge:approve:{self.charge_id}')

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message('관리자만 사용할 수 있습니다.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, result = db.approve_charge(self.charge_id, decided_by=f'{interaction.user} ({interaction.user.id})')
        if not ok:
            await interaction.followup.send(embed=make_embed('충전 실패', result, COLOR_RED), ephemeral=True)
            return
        charge = result
        await notify_charge_completed(self.bot, charge)
        try:
            await interaction.message.edit(view=charge_card(self.bot, charge, 'completed'))
        except Exception:
            pass
        embed = make_embed('충전 완료', f'<@{charge["user_id"]}> · {money(charge["amount"])} 지급', COLOR_MINT)
        await interaction.followup.send(embed=embed, ephemeral=True)


class CancelChargeButton(ui.Button):
    def __init__(self, bot, charge_id):
        self.bot = bot
        self.charge_id = int(charge_id)
        super().__init__(label='신청 취소', style=discord.ButtonStyle.danger,
                         custom_id=f'ott:charge:cancel:{self.charge_id}')

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message('관리자만 사용할 수 있습니다.', ephemeral=True)
            return
        charge = db.get_charge(self.charge_id)
        if not charge or charge['status'] != 'pending':
            await interaction.response.send_message('이미 처리된 신청입니다.', ephemeral=True)
            return
        db.cancel_charge(self.charge_id, decided_by=f'{interaction.user} ({interaction.user.id})')
        charge['status'] = 'cancelled'
        try:
            await interaction.message.edit(view=charge_card(self.bot, charge, 'cancelled'))
        except Exception:
            pass
        await interaction.response.send_message('충전 신청을 취소했습니다.', ephemeral=True)


def ticket_embed(order=None):
    if order:
        embed = make_embed(
            f'주문 문의 · {order["order_no"]}',
            '문의 내용을 이 채널에 남겨 주세요. 관리자가 확인 후 답변드립니다.',
            COLOR_VIOLET,
            footer='APEX OTT · TICKET',
        )
        embed.add_field(name='상품', value=order['product_name'], inline=True)
        embed.add_field(name='결제 금액', value=money(order['amount']), inline=True)
        embed.add_field(name='주문 상태', value=status_label(order), inline=True)
        embed.add_field(name='주문 시각', value=discord_ts(order['created_at']), inline=True)
        return embed
    return make_embed(
        '1:1 문의',
        '문의 내용을 이 채널에 남겨 주세요. 관리자가 확인 후 답변드립니다.',
        COLOR_VIOLET,
        footer='APEX OTT · TICKET',
    )


_ticket_inflight = set()


def ticket_text(order=None):
    if order:
        return (
            f'## 주문 문의 · `{order["order_no"]}`\n'
            f'**상품** {order["product_name"]}\n'
            f'**결제 금액** {money(order["amount"])}\n'
            f'**주문 상태** {status_label(order)}\n'
            f'**주문 시각** {discord_ts(order["created_at"])}\n\n'
            '문의 내용을 이 채널에 남겨 주세요. 관리자가 확인 후 답변드립니다.'
        )
    return (
        '## 1:1 문의\n'
        '문의 내용을 이 채널에 남겨 주세요. 관리자가 확인 후 답변드립니다.'
    )


async def create_ticket_channel(bot, interaction, order=None):
    await interaction.response.defer(ephemeral=True, thinking=True)

    user = interaction.user
    guild = interaction.guild
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if guild is None:
        for candidate in bot.guilds:
            found = candidate.get_member(user.id)
            if found is None:
                try:
                    found = await candidate.fetch_member(user.id)
                except Exception:
                    continue
            guild, member = candidate, found
            break
    if guild is None or member is None:
        await interaction.followup.send('서버에서만 티켓을 열 수 있습니다. 관리자에게 문의해 주세요.', ephemeral=True)
        return

    key = f'order:{order["id"]}' if order is not None else f'user:{user.id}'
    if key in _ticket_inflight:
        await interaction.followup.send('티켓을 준비하고 있습니다. 잠시만 기다려 주세요.', ephemeral=True)
        return

    _ticket_inflight.add(key)
    try:
        if order is not None:
            existing = db.find_open_ticket(order['id'])
            label = f'주문 `{order["order_no"]}`'
        else:
            existing = db.find_open_ticket_by_user(user.id)
            label = '1:1 문의'
        if existing:
            await interaction.followup.send(
                f'이미 열린 티켓이 있습니다: <#{existing["channel_id"]}>', ephemeral=True)
            return

        category = None
        raw_category = str(db.get_setting('ticket_category_id', '') or '').strip()
        if not raw_category.isdigit():
            await interaction.followup.send(
                '티켓 카테고리가 아직 설정되지 않았습니다. 관리자에게 문의해 주세요.', ephemeral=True)
            return
        found = guild.get_channel(int(raw_category))
        if not isinstance(found, discord.CategoryChannel):
            await interaction.followup.send(
                '티켓 카테고리가 올바르지 않습니다. 관리자에게 문의해 주세요.', ephemeral=True)
            return
        category = found

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                 read_message_history=True, attach_files=True,
                                                 embed_links=True),
        }
        bot_member = guild.me or guild.get_member(bot.user.id)
        if bot_member is not None:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True,
                read_message_history=True, attach_files=True, embed_links=True,
            )
        role_id = str(db.get_setting('ticket_ping_role_id', '') or '').strip()
        ping_role = guild.get_role(int(role_id)) if role_id.isdigit() else None
        if ping_role is not None:
            overwrites[ping_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                                read_message_history=True)

        name = f'문의-{order["order_no"].lower()}' if order else f'문의-{user.name}'[:90]
        try:
            channel = await guild.create_text_channel(
                name=name, category=category, overwrites=overwrites,
                topic=f'{label} · {user} ({user.id})',
                reason='APEX OTT 문의 티켓',
            )
        except Exception as exc:
            await interaction.followup.send(f'티켓 채널을 만들 수 없습니다: {exc}', ephemeral=True)
            return

        ping = f'{ping_role.mention} ' if ping_role is not None else ''
        try:
            await channel.send(
                view=CardView(
                    f'{member.mention} {ping}'.strip() + '\n\n' + ticket_text(order),
                    rows=[ui.ActionRow(TicketCloseButton())],
                    accent=COLOR_VIOLET,
                ),
                allowed_mentions=discord.AllowedMentions(roles=True, users=True),
            )
        except Exception as exc:
            logger.warning(f'[ticket] 안내 메시지 실패: {exc}')

        db.create_ticket(order['id'] if order else None, user.id, user.name, channel.id)
        await interaction.followup.send(
            f'티켓이 열렸습니다: {channel.mention}\n문의 내용을 남겨 주시면 관리자가 확인합니다.',
            ephemeral=True,
        )
    except Exception as exc:
        logger.error(f'[ticket] 생성 오류: {exc}')
        try:
            await interaction.followup.send('티켓을 만드는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.',
                                            ephemeral=True)
        except Exception:
            pass
    finally:
        _ticket_inflight.discard(key)


class TicketOpenView(ui.View):
    def __init__(self, bot, order_id, user_id):
        super().__init__(timeout=None)
        self.add_item(TicketOpenButton(bot, order_id, user_id))


class TicketOpenButton(ui.Button):
    def __init__(self, bot, order_id, user_id):
        self.bot = bot
        self.order_id = int(order_id)
        self.owner_id = int(user_id)
        super().__init__(label='티켓 열기', style=discord.ButtonStyle.secondary,
                         custom_id=f'ott:ticket:open:{self.order_id}')

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message('본인 주문에서만 티켓을 열 수 있습니다.', ephemeral=True)
            return
        order = db.get_order(self.order_id)
        if not order:
            await interaction.response.send_message('주문 정보를 찾을 수 없습니다.', ephemeral=True)
            return
        if order['status'] == 'cancelled':
            await interaction.response.send_message('취소된 주문입니다.', ephemeral=True)
            return

        if order['status'] == 'paid':
            created = to_dt(order['created_at'])
            deadline = created + timedelta(minutes=window_minutes()) if created else None
            if deadline and datetime.now(timezone.utc) < deadline:
                await interaction.response.send_message(
                    f'아직 전달 대기 시간입니다. <t:{int(deadline.timestamp())}:R> 이후에 티켓을 열 수 있습니다.',
                    ephemeral=True,
                )
                return

        await create_ticket_channel(self.bot, interaction, order)


class TicketCloseButton(ui.Button):
    def __init__(self):
        super().__init__(label='티켓 닫기', style=discord.ButtonStyle.danger,
                         custom_id='ott:ticket:close')

    async def callback(self, interaction: discord.Interaction):
        ticket = db.get_ticket_by_channel(interaction.channel_id)
        allowed = is_admin(interaction.user)
        if not allowed and isinstance(interaction.user, discord.Member):
            allowed = interaction.user.guild_permissions.manage_channels
        if not allowed:
            await interaction.response.send_message('관리자만 티켓을 닫을 수 있습니다.', ephemeral=True)
            return
        db.close_ticket(interaction.channel_id)
        await interaction.response.send_message('티켓을 닫습니다. 잠시 후 채널이 삭제됩니다.')
        await asyncio.sleep(3)
        try:
            await interaction.channel.delete(reason=f'티켓 종료 · {interaction.user}')
        except Exception:
            if ticket:
                logger.info(f'[ticket] 채널 삭제 실패: {ticket["channel_id"]}')


class TicketCloseView(ui.View):
    """재시작 후 티켓 닫기 버튼을 복원하기 위한 등록용 뷰."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketCloseButton())


class OrderActionsView(ui.View):
    def __init__(self, bot, order_id, user_id):
        super().__init__(timeout=None)
        self.add_item(TicketOpenButton(bot, order_id, user_id))
        self.add_item(ReviewButton(bot, order_id, user_id))


class ReviewButton(ui.Button):
    def __init__(self, bot, order_id, user_id):
        self.bot = bot
        self.order_id = int(order_id)
        self.owner_id = int(user_id)
        super().__init__(label='후기 남기기', style=discord.ButtonStyle.primary,
                         emoji='📝', custom_id=f'ott:review:{self.order_id}')

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message('본인 주문에만 후기를 남길 수 있습니다.', ephemeral=True)
            return
        order = db.get_order(self.order_id)
        if not order or order['status'] != 'delivered':
            await interaction.response.send_message('상품 지급이 완료된 주문에만 후기를 남길 수 있습니다.', ephemeral=True)
            return
        if db.has_review(self.order_id):
            await interaction.response.send_message('이미 후기를 남긴 주문입니다. 감사합니다.', ephemeral=True)
            return
        await interaction.response.send_message(
            view=RatingView(self.bot, self.order_id, self.owner_id), ephemeral=True)


class RatingView(ui.LayoutView):
    def __init__(self, bot, order_id, user_id):
        super().__init__(timeout=300)
        order = db.get_order(order_id)
        product = order['product_name'] if order else '상품'
        self.add_item(ui.Container(
            ui.TextDisplay(
                f'## 후기 작성\n**{product}**\n별점을 선택한 뒤 후기 내용을 작성해 주세요.'
            ),
            ui.ActionRow(*[RatingButton(bot, order_id, user_id, rating) for rating in range(1, 6)]),
            ui.ActionRow(RatingCancelButton()),
            accent_colour=COLOR_GOLD,
        ))


class RatingButton(ui.Button):
    def __init__(self, bot, order_id, user_id, rating):
        self.bot = bot
        self.order_id = int(order_id)
        self.owner_id = int(user_id)
        self.rating = int(rating)
        super().__init__(label=f'{rating}점', emoji='⭐', style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message('본인 주문에만 후기를 남길 수 있습니다.', ephemeral=True)
            return
        await interaction.response.send_modal(
            ReviewModal(self.bot, self.order_id, self.owner_id, self.rating))


class RatingCancelButton(ui.Button):
    def __init__(self):
        super().__init__(label='취소', style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=None)


class ReviewModal(ui.Modal):
    content = ui.TextInput(
        label='후기 내용',
        placeholder='상품은 어떠셨나요? 전달과 이용 과정을 남겨주세요.',
        style=discord.TextStyle.paragraph,
        min_length=2,
        max_length=500,
    )

    def __init__(self, bot, order_id, user_id, rating):
        super().__init__(title=f'후기 작성 · {rating}점')
        self.bot = bot
        self.order_id = int(order_id)
        self.owner_id = int(user_id)
        self.rating = int(rating)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message('본인 주문에만 후기를 남길 수 있습니다.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        order = db.get_order(self.order_id)
        if not order or order['status'] != 'delivered':
            await interaction.followup.send('상품 지급이 완료된 주문에만 후기를 남길 수 있습니다.', ephemeral=True)
            return
        review = db.create_review(
            self.order_id, self.owner_id, interaction.user.name,
            order.get('product_id'), order['product_name'], self.rating, self.content.value,
        )
        if review is None:
            await interaction.followup.send('이미 후기를 남긴 주문입니다.', ephemeral=True)
            return
        await post_review(self.bot, review)
        embed = make_embed(
            '후기가 등록되었습니다',
            '소중한 후기 감사합니다.',
            COLOR_MINT,
            footer='APEX OTT · REVIEW',
        )
        embed.add_field(name='별점', value=f'{"★" * self.rating}{"☆" * (5 - self.rating)} · {self.rating}점', inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def post_review(bot, review):
    channel = await resolve_channel(bot, 'review_channel_id')
    if channel is None:
        logger.info('[review] 후기 채널이 설정되지 않아 저장만 완료했습니다.')
        return False
    embed = make_embed(
        '새 후기',
        review.get('content') or '',
        COLOR_GOLD,
        footer='APEX OTT · REVIEW',
    )
    embed.add_field(name='상품', value=review.get('product_name') or '-', inline=True)
    embed.add_field(name='작성자', value=f'<@{review["user_id"]}>', inline=True)
    rating = int(review.get('rating') or 0)
    embed.add_field(name='별점', value=f'{"★" * rating}{"☆" * (5 - rating)} · {rating}점', inline=True)
    embed.add_field(name='작성 시각', value=discord_ts(review.get('created_at')), inline=False)
    try:
        await channel.send(embed=embed)
        return True
    except Exception as exc:
        logger.warning(f'[review] 후기 게시 실패: {exc}')
        return False


class InquiryView(ui.LayoutView):
    def __init__(self, bot):
        super().__init__(timeout=300)
        self.add_item(ui.Container(
            ui.TextDisplay(
                '## 1:1 문의\n'
                '상품, 결제, 전달 과정에서 도움이 필요하면 티켓을 열어 주세요.\n'
                '-# 주문 관련 문의는 구매 DM의 [티켓 열기] 버튼을 사용하면 주문 정보가 함께 전달됩니다.'
            ),
            ui.ActionRow(InquiryTicketButton(bot)),
            accent_colour=COLOR_VIOLET,
        ))


class InquiryTicketButton(ui.Button):
    def __init__(self, bot):
        self.bot = bot
        super().__init__(label='티켓 열기', style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        await create_ticket_channel(self.bot, interaction, None)


async def show_inquiry(interaction: discord.Interaction):
    existing = db.find_open_ticket_by_user(interaction.user.id)
    if existing:
        await interaction.response.send_message(
            f'이미 열린 문의 티켓이 있습니다: <#{existing["channel_id"]}>', ephemeral=True)
        return
    await interaction.response.send_message(
        view=InquiryView(interaction.client), ephemeral=True)


class ApexOttBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = False
        super().__init__(
            command_prefix='!',
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=True, users=True),
        )
        self.synced = False

    async def setup_hook(self):
        self.add_view(CatalogView(self))
        self.add_view(TicketCloseView())

        # 재시작 후에도 구매 로그/티켓 버튼이 동작하도록 복원
        for order in db.list_orders(status='paid', limit=300):
            self.add_view(order_log_card(self, order))
        for charge in db.list_charges(status='pending', limit=300):
            self.add_view(ChargeAdminView(self, charge['id']))
        for order in db.list_orders(limit=500):
            if order['status'] == 'paid':
                self.add_view(TicketOpenView(self, order['id'], order['user_id']))
            elif order['status'] == 'delivered':
                self.add_view(OrderActionsView(self, order['id'], order['user_id']))

        await self.add_cog(ApexOttCommands(self))
        self.loop.create_task(self.deadline_watch())

    async def on_ready(self):
        if not self.synced:
            try:
                guild_id = str(os.environ.get('GUILD_ID', '')).strip()
                if guild_id.isdigit():
                    guild = discord.Object(id=int(guild_id))
                    self.tree.copy_global_to(guild=guild)
                    await self.tree.sync(guild=guild)
                await self.tree.sync()
                self.synced = True
            except Exception as exc:
                logger.warning(f'[sync] 명령어 동기화 실패: {exc}')
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name='OTT 주문 · /자판기'),
            status=discord.Status.online,
        )
        logger.info(f'{self.user} 로그인 완료 · 대기 주문 {db.pending_order_count()}건')

    async def deadline_watch(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                minutes = window_minutes()
                for order in db.overdue_orders(minutes):
                    db.mark_deadline_notified(order['id'])
                    user = self.get_user(int(order['user_id']))
                    if user is None:
                        try:
                            user = await self.fetch_user(int(order['user_id']))
                        except Exception:
                            user = None
                    if user:
                        text = (
                            '## 전달이 지연되고 있습니다\n'
                            f'주문 `{order["order_no"]}` 의 상품 전달이 **{minutes}분**을 초과했습니다.\n'
                            '아래 [티켓 열기] 버튼으로 문의를 남겨 주세요.\n\n'
                            f'**상품** {order["product_name"]}\n'
                            f'**결제 금액** {money(order["amount"])}\n'
                            f'**주문 시각** {discord_ts(order["created_at"])}'
                        )
                        await try_dm(
                            user,
                            view=CardView(
                                text,
                                rows=[ui.ActionRow(TicketOpenButton(self, order['id'], order['user_id']))],
                                accent=COLOR_GOLD,
                            ),
                        )

                    channel = await resolve_channel(self, 'purchase_log_channel_id')
                    if channel:
                        try:
                            await channel.send(view=CardView(
                                f'<@{order["user_id"]}> 주문 `{order["order_no"]}` 이 {minutes}분을 초과했습니다.\n\n'
                                + order_log_text(order),
                                accent=COLOR_GOLD,
                            ))
                        except Exception:
                            pass
            except Exception as exc:
                logger.error(f'[deadline] 처리 오류: {exc}')
            await asyncio.sleep(60)


def admin_only():
    async def predicate(interaction: discord.Interaction):
        return is_admin(interaction.user)
    return app_commands.check(predicate)


class ApexOttCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CheckFailure):
            if not interaction.response.is_done():
                await interaction.response.send_message('관리자만 사용할 수 있습니다.', ephemeral=True)
            return
        logger.error(f'[command] {getattr(interaction.command, "name", "?")} 오류: {error}')
        if not interaction.response.is_done():
            await interaction.response.send_message('명령을 처리하는 중 오류가 발생했습니다.', ephemeral=True)

    # ---------- 자판기 ----------
    @admin_only()
    @app_commands.command(name='자판기', description='현재 채널에 상품 자판기 패널을 설치합니다.')
    async def panel_command(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message('서버 채널에서 사용해 주세요.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, message = await install_panel(self.bot, interaction.channel_id)
        await interaction.followup.send(message, ephemeral=True)

    # ---------- 상품 ----------
    @admin_only()
    @app_commands.command(name='상품추가', description='새 상품을 등록합니다.')
    @app_commands.describe(상품명='상품 이름', 가격='판매 가격(P)', 카테고리='카테고리 (예: 넷플릭스)',
                           재고='재고 수량 (-1 = 무제한)', 설명='상품 설명')
    async def add_product(self, interaction: discord.Interaction, 상품명: str, 가격: int,
                          카테고리: str = '기타', 재고: int = -1, 설명: str = ''):
        if 가격 <= 0 or 재고 < -1:
            await interaction.response.send_message('가격과 재고를 확인해 주세요. (재고 -1 = 무제한)', ephemeral=True)
            return
        pid = db.add_product(상품명, 가격, 카테고리, 설명, 재고)
        embed = make_embed('상품 등록 완료',
                           f'**{상품명}** · {money(가격)} · 재고 {stock_label(재고)}', COLOR_MINT)
        embed.add_field(name='상품 ID', value=str(pid), inline=True)
        embed.add_field(name='카테고리', value=카테고리 or '기타', inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin_only()
    @app_commands.command(name='상품수정', description='상품 정보를 수정합니다.')
    @app_commands.describe(상품_id='수정할 상품 ID', 가격='새 가격(P)', 재고='새 재고 (-1 = 무제한)',
                           카테고리='새 카테고리', 설명='새 설명', 판매='판매 여부')
    async def edit_product(self, interaction: discord.Interaction, 상품_id: int,
                           가격: int = None, 재고: int = None, 카테고리: str = None,
                           설명: str = None, 판매: bool = None):
        product = db.get_product(상품_id)
        if not product:
            await interaction.response.send_message('해당 ID의 상품이 없습니다.', ephemeral=True)
            return
        if 재고 is not None and 재고 < -1:
            await interaction.response.send_message('재고는 -1(무제한) 또는 0 이상이어야 합니다.', ephemeral=True)
            return
        fields = {}
        if 가격 is not None:
            fields['price'] = int(가격)
        if 재고 is not None:
            fields['stock'] = int(재고)
        if 카테고리 is not None:
            fields['category'] = 카테고리
        if 설명 is not None:
            fields['description'] = 설명
        if 판매 is not None:
            fields['active'] = 1 if 판매 else 0
        if not fields:
            await interaction.response.send_message('수정할 항목을 하나 이상 입력해 주세요.', ephemeral=True)
            return
        db.update_product(상품_id, **fields)
        await interaction.response.send_message(
            embed=make_embed('상품 수정 완료', f'상품 `{상품_id}` 정보를 변경했습니다.', COLOR_MINT), ephemeral=True)

    @admin_only()
    @app_commands.command(name='상품삭제', description='상품을 삭제합니다.')
    @app_commands.describe(상품_id='삭제할 상품 ID')
    async def remove_product(self, interaction: discord.Interaction, 상품_id: int):
        product = db.get_product(상품_id)
        if not product:
            await interaction.response.send_message('해당 ID의 상품이 없습니다.', ephemeral=True)
            return
        db.delete_product(상품_id)
        await interaction.response.send_message(
            embed=make_embed('상품 삭제 완료', f'**{product["name"]}** 을 삭제했습니다.', COLOR_RED), ephemeral=True)

    @admin_only()
    @app_commands.command(name='재고추가', description='상품 재고를 추가합니다.')
    @app_commands.describe(상품_id='상품 ID', 수량='추가할 수량')
    async def add_stock(self, interaction: discord.Interaction, 상품_id: int, 수량: int):
        product = db.get_product(상품_id)
        if not product:
            await interaction.response.send_message('해당 ID의 상품이 없습니다.', ephemeral=True)
            return
        if 수량 <= 0:
            await interaction.response.send_message('수량은 1 이상이어야 합니다.', ephemeral=True)
            return
        if int(product['stock']) < 0:
            await interaction.response.send_message(
                '무제한 상품입니다. 재고를 유한하게 바꾸려면 `/상품수정`에서 재고를 입력해 주세요.', ephemeral=True)
            return
        db.add_stock(상품_id, 수량)
        updated = db.get_product(상품_id)
        await interaction.response.send_message(
            embed=make_embed('재고 추가', f'**{updated["name"]}** 재고: {stock_label(updated["stock"])}', COLOR_MINT),
            ephemeral=True)

    @admin_only()
    @app_commands.command(name='상품목록', description='등록된 상품과 재고를 확인합니다.')
    async def product_list(self, interaction: discord.Interaction):
        products = db.list_products()
        if not products:
            await interaction.response.send_message('등록된 상품이 없습니다.', ephemeral=True)
            return
        lines = []
        for product in products[:40]:
            stock = int(product['stock'])
            state = stock_label(stock)
            if not int(product['active']):
                state = '판매 중지'
            lines.append(f'`{product["id"]:>3}` · {product["name"]} · {money(product["price"])} · {state}')
        embed = make_embed('상품 목록', '\n'.join(lines)[:4000],
                           footer=f'APEX OTT · ADMIN · 총 {len(products)}개')
        await interaction.response.send_message(embed=embed, ephemeral=True)


    # ---------- 주문 ----------
    @admin_only()
    @app_commands.command(name='주문목록', description='주문 목록을 확인합니다.')
    @app_commands.describe(상태='조회할 주문 상태')
    @app_commands.choices(상태=[
        app_commands.Choice(name='지급 대기', value='paid'),
        app_commands.Choice(name='지급 완료', value='delivered'),
        app_commands.Choice(name='취소', value='cancelled'),
        app_commands.Choice(name='전체', value='all'),
    ])
    async def order_list(self, interaction: discord.Interaction, 상태: app_commands.Choice[str] = None):
        status = 상태.value if 상태 else 'paid'
        orders = db.list_orders(status=status, limit=15)
        if not orders:
            await interaction.response.send_message('해당 상태의 주문이 없습니다.', ephemeral=True)
            return
        lines = []
        for order in orders:
            label = STATUS_META.get(order['status'], ('-', COLOR_GRAY))[0]
            lines.append(
                f'`{order["order_no"]}` · {order["product_name"]} · {money(order["amount"])} · {label}\n'
                f'-# {order.get("username") or order["user_id"]} · {discord_ts(order["created_at"])}'
            )
        embed = make_embed(
            '주문 목록',
            '\n'.join(lines)[:4000],
            footer=f'APEX OTT · ADMIN · {window_minutes()}분 기준',
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin_only()
    @app_commands.command(name='지급', description='주문번호로 상품을 지급합니다.')
    @app_commands.describe(주문번호='예: OTT-261117-0001')
    async def deliver_command(self, interaction: discord.Interaction, 주문번호: str):
        order = db.get_order_by_no(주문번호)
        if not order:
            await interaction.response.send_message('해당 주문번호의 주문이 없습니다.', ephemeral=True)
            return
        if order['status'] != 'paid':
            await interaction.response.send_message('이미 처리된 주문입니다.', ephemeral=True)
            return
        await interaction.response.send_message(
            view=DeliveryTypeView(self.bot, order['id']), ephemeral=True)

    @admin_only()
    @app_commands.command(name='주문취소', description='지급 대기 주문을 취소하고 포인트를 복구합니다.')
    @app_commands.describe(주문번호='예: OTT-261117-0001')
    async def cancel_command(self, interaction: discord.Interaction, 주문번호: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        order = db.get_order_by_no(주문번호)
        if not order:
            await interaction.followup.send('해당 주문번호의 주문이 없습니다.', ephemeral=True)
            return
        ok, result = db.cancel_order(order['id'], decided_by=f'{interaction.user} ({interaction.user.id})')
        if not ok:
            await interaction.followup.send(result, ephemeral=True)
            return
        user = self.bot.get_user(int(result['user_id']))
        if user:
            await try_dm(user, embed=make_embed(
                '주문이 취소되었습니다',
                f'주문 `{result["order_no"]}` 이 취소되어 **{money(result["amount"])}** 이 복구되었습니다.',
                COLOR_GOLD,
            ))
        await edit_order_log(self.bot, result)
        await interaction.followup.send(
            embed=make_embed('주문 취소 완료', f'`{result["order_no"]}` · {money(result["amount"])} 복구', COLOR_MINT),
            ephemeral=True,
        )

    @admin_only()
    @app_commands.command(name='재전송', description='지급 완료된 상품을 구매자 DM으로 다시 보냅니다.')
    @app_commands.describe(주문번호='예: OTT-261117-0001')
    async def resend_command(self, interaction: discord.Interaction, 주문번호: str):
        order = db.get_order_by_no(주문번호)
        if not order:
            await interaction.response.send_message('해당 주문번호의 주문이 없습니다.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, message = await resend_delivery(self.bot, order)
        embed = make_embed('재전송 완료' if ok else '재전송 실패', message, COLOR_MINT if ok else COLOR_RED)
        await interaction.followup.send(embed=embed, ephemeral=True)


    # ---------- 포인트 ----------
    @admin_only()
    @app_commands.command(name='포인트지급', description='유저에게 포인트를 지급합니다.')
    @app_commands.describe(유저='대상 유저', 금액='지급할 포인트', 사유='지급 사유')
    async def give_points(self, interaction: discord.Interaction, 유저: discord.User,
                          금액: int, 사유: str = '관리자 지급'):
        if 금액 <= 0:
            await interaction.response.send_message('금액은 1 이상이어야 합니다.', ephemeral=True)
            return
        db.ensure_user(유저.id, 유저.name)
        ok, balance = db.add_balance(유저.id, 금액, 'admin_add', f'{사유} · {interaction.user}', 유저.name)
        embed = make_embed('포인트 지급', f'{유저.mention} · **{money(금액)}**\n잔액: {money(balance)}', COLOR_MINT)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await try_dm(유저, embed=make_embed(
            '포인트가 지급되었습니다',
            f'**{money(금액)}** 이 지급되었습니다.\n현재 보유 포인트: **{money(balance)}**',
            COLOR_MINT,
        ))

    @admin_only()
    @app_commands.command(name='포인트차감', description='유저의 포인트를 차감합니다.')
    @app_commands.describe(유저='대상 유저', 금액='차감할 포인트', 사유='차감 사유')
    async def take_points(self, interaction: discord.Interaction, 유저: discord.User,
                          금액: int, 사유: str = '관리자 차감'):
        if 금액 <= 0:
            await interaction.response.send_message('금액은 1 이상이어야 합니다.', ephemeral=True)
            return
        ok, balance = db.add_balance(유저.id, -금액, 'admin_remove', f'{사유} · {interaction.user}', 유저.name)
        if not ok:
            await interaction.response.send_message(f'잔액이 부족합니다. 현재 잔액: {money(balance)}', ephemeral=True)
            return
        embed = make_embed('포인트 차감', f'{유저.mention} · **{money(금액)}**\n잔액: {money(balance)}', COLOR_GOLD)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin_only()
    @app_commands.command(name='잔액', description='유저의 보유 포인트를 확인합니다.')
    @app_commands.describe(유저='확인할 유저')
    async def balance_command(self, interaction: discord.Interaction, 유저: discord.User = None):
        target = 유저 or interaction.user
        user = db.get_user(target.id) or {}
        embed = make_embed('보유 포인트', f'{target.mention} · **{money(user.get("balance"))}**', COLOR_VIOLET)
        await interaction.response.send_message(embed=embed, ephemeral=True)


    # ---------- 티켓 / 공지 ----------
    @admin_only()
    @app_commands.command(name='티켓목록', description='열려 있는 문의 티켓을 확인합니다.')
    async def ticket_list(self, interaction: discord.Interaction):
        tickets = db.list_open_tickets()
        if not tickets:
            await interaction.response.send_message('열려 있는 티켓이 없습니다.', ephemeral=True)
            return
        lines = [
            f'<#{ticket["channel_id"]}> · {ticket.get("username") or ticket["user_id"]}\n'
            f'-# {discord_ts(ticket["created_at"])}'
            for ticket in tickets[:15]
        ]
        await interaction.response.send_message(
            embed=make_embed('열린 티켓', '\n'.join(lines)[:4000],
                             footer=f'APEX OTT · ADMIN · {len(tickets)}건'),
            ephemeral=True,
        )

    @admin_only()
    @app_commands.command(name='공지', description='공지 채널에 안내를 게시합니다.')
    @app_commands.describe(내용='공지 내용', 제목='공지 제목')
    async def notice(self, interaction: discord.Interaction, 내용: str, 제목: str = '공지'):
        channel = await resolve_channel(self.bot, 'notice_channel_id')
        if channel is None:
            await interaction.response.send_message(
                '공지 채널이 설정되지 않았습니다. 대시보드 설정에서 채널 ID를 등록해 주세요.', ephemeral=True)
            return
        embed = make_embed(제목, 내용, COLOR_VIOLET, footer='APEX OTT · NOTICE')
        await channel.send(embed=embed)
        await interaction.response.send_message('공지를 게시했습니다.', ephemeral=True)

    # ---------- 관리자 / 설정 ----------
    @admin_only()
    @app_commands.command(name='관리자추가', description='봇 관리자 목록에 유저를 추가합니다.')
    @app_commands.describe(유저='관리자로 추가할 유저')
    async def add_admin_command(self, interaction: discord.Interaction, 유저: discord.User):
        db.add_admin(유저.id, 유저.name)
        await interaction.response.send_message(
            embed=make_embed('관리자 추가', f'{유저.mention} 을 관리자로 등록했습니다.', COLOR_MINT), ephemeral=True)

    @admin_only()
    @app_commands.command(name='관리자삭제', description='봇 관리자 목록에서 유저를 제거합니다.')
    @app_commands.describe(유저='제거할 유저')
    async def remove_admin_command(self, interaction: discord.Interaction, 유저: discord.User):
        db.remove_admin(유저.id)
        await interaction.response.send_message(
            embed=make_embed('관리자 제거', f'{유저.mention} 을 관리자 목록에서 제거했습니다.', COLOR_GOLD), ephemeral=True)

    @admin_only()
    @app_commands.command(name='설정확인', description='현재 봇 설정을 확인합니다.')
    async def show_settings(self, interaction: discord.Interaction):
        settings = db.get_settings()
        embed = make_embed('설정 확인', footer='APEX OTT · ADMIN')
        log_channel = str(settings.get('purchase_log_channel_id', ''))
        charge_channel = str(settings.get('charge_log_channel_id', ''))
        ticket_category = str(settings.get('ticket_category_id', ''))
        review_channel = str(settings.get('review_channel_id', ''))
        embed.add_field(name='구매 로그 채널', value=f'<#{log_channel}>' if log_channel.isdigit() else '미설정', inline=True)
        embed.add_field(name='충전 로그 채널', value=f'<#{charge_channel}>' if charge_channel.isdigit() else '미설정', inline=True)
        embed.add_field(name='티켓 카테고리', value=f'<#{ticket_category}>' if ticket_category.isdigit() else '미설정', inline=True)
        embed.add_field(name='후기 채널', value=f'<#{review_channel}>' if review_channel.isdigit() else '미설정', inline=True)
        embed.add_field(name='전달 제한 시간', value=f'{window_minutes()}분', inline=True)
        embed.add_field(name='입금 계좌', value=f'{settings.get("bank_name") or "-"} '
                                                 f'{settings.get("bank_account") or "미설정"}', inline=True)
        embed.add_field(name='카드 이미지', value='사용' if str(settings.get('card_image_enabled', 'true')).lower() != 'false' else '사용 안 함', inline=True)
        embed.add_field(name='패널 배너', value='설정됨' if str(settings.get('panel_banner_url', '') or '').strip() else '없음', inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin_only()
    @app_commands.command(name='채널설정', description='로그·티켓·후기·공지 채널을 설정합니다.')
    @app_commands.describe(구매로그='구매 로그 채널', 충전로그='충전 로그 채널',
                           티켓카테고리='티켓 채널을 만들 카테고리', 후기채널='후기 게시 채널',
                           공지채널='공지 채널', 티켓알림역할='티켓 생성 시 멘션할 역할')
    async def channel_settings(self, interaction: discord.Interaction,
                               구매로그: discord.TextChannel = None,
                               충전로그: discord.TextChannel = None,
                               티켓카테고리: discord.CategoryChannel = None,
                               후기채널: discord.TextChannel = None,
                               공지채널: discord.TextChannel = None,
                               티켓알림역할: discord.Role = None):
        fields = {}
        if 구매로그 is not None:
            fields['purchase_log_channel_id'] = str(구매로그.id)
        if 충전로그 is not None:
            fields['charge_log_channel_id'] = str(충전로그.id)
        if 티켓카테고리 is not None:
            fields['ticket_category_id'] = str(티켓카테고리.id)
        if 후기채널 is not None:
            fields['review_channel_id'] = str(후기채널.id)
        if 공지채널 is not None:
            fields['notice_channel_id'] = str(공지채널.id)
        if 티켓알림역할 is not None:
            fields['ticket_ping_role_id'] = str(티켓알림역할.id)
        if not fields:
            await interaction.response.send_message('변경할 채널을 하나 이상 선택해 주세요.', ephemeral=True)
            return
        db.update_settings(fields)
        embed = make_embed('채널 설정 완료', '', COLOR_MINT, footer='APEX OTT · ADMIN')
        for key, value in fields.items():
            label = {
                'purchase_log_channel_id': '구매 로그',
                'charge_log_channel_id': '충전 로그',
                'ticket_category_id': '티켓 카테고리',
                'review_channel_id': '후기 채널',
                'notice_channel_id': '공지 채널',
                'ticket_ping_role_id': '티켓 알림 역할',
            }.get(key, key)
            embed.add_field(name=label, value=f'<#{value}>' if key != 'ticket_ping_role_id' else f'<@&{value}>',
                            inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin_only()
    @app_commands.command(name='패널설치', description='자판기 패널을 설치하거나 새로고침합니다.')
    @app_commands.describe(채널='설치할 채널 (비우면 현재 채널)', 배너='배너 이미지 URL (비우면 기존 설정 유지)')
    async def panel_install_command(self, interaction: discord.Interaction,
                                    채널: discord.TextChannel = None,
                                    배너: str = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        target = 채널 or interaction.channel
        if target is None:
            await interaction.followup.send('채널을 선택해 주세요.', ephemeral=True)
            return
        ok, message = await install_panel(self.bot, target.id, 배너)
        await interaction.followup.send(message, ephemeral=True)

    @admin_only()
    @app_commands.command(name='패널배너', description='자판기 패널 배너 이미지를 변경합니다.')
    @app_commands.describe(url='이미지 주소 (비우려면 "없음" 입력)')
    async def panel_banner_command(self, interaction: discord.Interaction, url: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        value = '' if url.strip() in ('없음', 'none', '삭제') else url.strip()
        if value and not (value.startswith('https://') or value.startswith('http://')):
            await interaction.followup.send('http(s) 이미지 주소를 입력해 주세요.', ephemeral=True)
            return
        channel_id = db.get_setting('panel_channel_id', '')
        if channel_id.isdigit():
            await install_panel(self.bot, int(channel_id), value)
            await interaction.followup.send('패널 배너를 변경했습니다.', ephemeral=True)
            return
        db.set_setting('panel_banner_url', value)
        await interaction.followup.send('배너를 저장했습니다. 패널을 설치하면 적용됩니다.', ephemeral=True)

    @admin_only()
    @app_commands.command(name='db보내기', description='데이터베이스 백업 파일을 전송합니다.')
    @app_commands.describe(채널='전송할 채널 (비우면 나에게만 전송)')
    async def db_send(self, interaction: discord.Interaction, 채널: discord.TextChannel = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await asyncio.to_thread(db.backup_bytes)
        filename = f"APEX-OTT-{datetime.now(KST).strftime('%Y%m%d-%H%M')}.db"
        file = discord.File(io.BytesIO(data), filename=filename)
        if 채널 is not None:
            await 채널.send(file=file)
            await interaction.followup.send(f'{채널.mention} 로 DB 백업을 전송했습니다.', ephemeral=True)
            return
        await interaction.followup.send(file=file, ephemeral=True)

    @admin_only()
    @app_commands.command(name='동기화', description='슬래시 명령어를 다시 동기화합니다.')
    async def sync_commands(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.bot.tree.sync()
            await interaction.followup.send('명령어 동기화를 완료했습니다.', ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f'동기화 실패: {exc}', ephemeral=True)


def create_bot():
    return ApexOttBot()


def run_bot():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    token = os.environ.get('DISCORD_TOKEN', '').strip()
    if not token:
        raise RuntimeError('DISCORD_TOKEN 환경변수가 비어 있습니다.')
    db.init_db()
    create_bot().run(token)

















