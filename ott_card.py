# -*- coding: utf-8 -*-
"""APEX OTT — 상품 전달 / 주문 접수 카드 렌더러 (Pillow)

- 다크 카드 + 민트 포인트의 미니멀 레이아웃
- 폰트는 Pretendard 를 1회 내려받아 data/fonts 에 캐시하고,
  실패하면 시스템 한글 폰트로 대체한다.
- Pillow/폰트를 사용할 수 없으면 None 을 반환하고 호출부는 임베드로 폴백한다.
"""
import os
import re
import threading
from datetime import datetime, timedelta, timezone

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_OK = True
except Exception as _exc:  # pragma: no cover
    _PIL_OK = False
    print(f'[card] Pillow 사용 불가: {_exc}')

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

W = 1080
PAD = 54
RADIUS = 26

BG_TOP = (10, 12, 17)
BG_BOTTOM = (17, 22, 31)
CARD_BG = (18, 22, 30)
CARD_EDGE = (36, 43, 56)
PANEL_BG = (24, 29, 39)
PANEL_EDGE = (38, 46, 60)
TXT = (238, 242, 249)
SOFT = (204, 213, 226)
MUTED = (138, 150, 166)
FAINT = (99, 110, 126)
LINE = (33, 39, 50)
MINT = (61, 220, 151)
MINT_DARK = (10, 43, 32)
GOLD = (240, 180, 41)
VIOLET = (124, 124, 255)

_BASE = 'https://raw.githubusercontent.com/orioncactus/pretendard/main/packages/pretendard/dist/public/static/'
_NANUM = 'https://raw.githubusercontent.com/google/fonts/main/ofl/nanumgothic/'
_FONT_SOURCES = {
    'regular': [('Pretendard-Regular.otf', _BASE + 'Pretendard-Regular.otf'),
                ('NanumGothic-Regular.ttf', _NANUM + 'NanumGothic-Regular.ttf')],
    'medium': [('Pretendard-Medium.otf', _BASE + 'Pretendard-Medium.otf'),
               ('NanumGothic-Regular.ttf', _NANUM + 'NanumGothic-Regular.ttf')],
    'semibold': [('Pretendard-SemiBold.otf', _BASE + 'Pretendard-SemiBold.otf'),
                 ('NanumGothic-Bold.ttf', _NANUM + 'NanumGothic-Bold.ttf')],
    'bold': [('Pretendard-Bold.otf', _BASE + 'Pretendard-Bold.otf'),
             ('NanumGothic-Bold.ttf', _NANUM + 'NanumGothic-Bold.ttf')],
    'extra': [('Pretendard-ExtraBold.otf', _BASE + 'Pretendard-ExtraBold.otf'),
              ('NanumGothic-Bold.ttf', _NANUM + 'NanumGothic-Bold.ttf')],
}
_SYSTEM_FONTS = {
    'regular': [r'C:\Windows\Fonts\malgun.ttf', '/usr/share/fonts/truetype/nanum/NanumGothic.ttf',
                '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'],
    'bold': [r'C:\Windows\Fonts\malgunbd.ttf', '/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf',
             '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'],
}

_DL_LOCK = threading.Lock()
_FONT_CACHE = {}
_EMOJI_RE = re.compile(
    '[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE00-\uFE0F\u200D\u20E3'
    '\U0001F1E6-\U0001F1FF]')


def _clean(value):
    """카드에는 컬러 이모지를 그릴 수 없으므로 텍스트에서만 제거한다."""
    return _EMOJI_RE.sub('', str(value or '')).strip()



def _font_dir():
    path = os.path.join(DATA_DIR, 'fonts')
    os.makedirs(path, exist_ok=True)
    return path


def _download(url, dest):
    try:
        import requests
        res = requests.get(url, timeout=30)
        if res.status_code != 200 or len(res.content) < 5000:
            return False
        tmp = dest + '.tmp'
        with open(tmp, 'wb') as handle:
            handle.write(res.content)
        os.replace(tmp, dest)
        return True
    except Exception:
        return False


def _font_path(weight):
    weight = weight if weight in _FONT_SOURCES else 'regular'
    directory = _font_dir()
    for name, url in _FONT_SOURCES[weight]:
        path = os.path.join(directory, name)
        if os.path.exists(path) and os.path.getsize(path) > 5000:
            return path
        with _DL_LOCK:
            if os.path.exists(path) and os.path.getsize(path) > 5000:
                return path
            if _download(url, path):
                return path
    for candidate in _SYSTEM_FONTS.get('bold' if weight in ('bold', 'extra', 'semibold') else 'regular'):
        if os.path.exists(candidate):
            return candidate
    return None


def _font(weight, size):
    key = (weight, size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    if not _PIL_OK:
        return None
    path = _font_path(weight)
    try:
        font = ImageFont.truetype(path, size) if path else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _text_width(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _wrap(draw, text, font, max_width):
    """공백 기준 줄바꿈 (한글은 글자 단위 폴백)"""
    text = ' '.join(str(text or '').split())
    if not text:
        return ['']
    words = text.split(' ')
    lines, current = [], ''
    for word in words:
        candidate = (current + ' ' + word).strip()
        if _text_width(draw, candidate, font) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ''
        if _text_width(draw, word, font) <= max_width:
            current = word
            continue
        chunk = ''
        for char in word:
            if _text_width(draw, chunk + char, font) <= max_width:
                chunk += char
            else:
                lines.append(chunk)
                chunk = char
        current = chunk
    if current:
        lines.append(current)
    return lines or ['']


def _rounded(draw, box, radius, fill=None, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _kx_clock(value=None):
    base = value or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base.astimezone(timezone(timedelta(hours=9))).strftime('%Y.%m.%d %H:%M')


def _new_canvas(height):
    image = Image.new('RGB', (W, height), BG_TOP)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * ratio) for i in range(3))
        draw.line([(0, y), (W, y)], fill=color)
    return image, draw


def _draw_header(draw, status_text='DELIVERED', accent=MINT, status_text_color=MINT):
    x = PAD + 20
    logo_y = 76
    _rounded(draw, (x, logo_y, x + 52, logo_y + 52), 15, fill=accent)
    font_logo = _font('extra', 30)
    draw.text((x + 26, logo_y + 26), 'A', font=font_logo, fill=(9, 14, 18), anchor='mm')

    draw.text((x + 72, logo_y + 6), 'APEX OTT', font=_font('bold', 29), fill=TXT)
    draw.text((x + 72, logo_y + 42), 'PREMIUM OTT STORE', font=_font('medium', 12), fill=FAINT)

    font_status = _font('semibold', 13)
    width = _text_width(draw, status_text, font_status)
    box_right = W - PAD - 20
    box_left = box_right - width - 34
    _rounded(draw, (box_left, logo_y + 10, box_right, logo_y + 44), 17,
             fill=MINT_DARK, outline=accent, width=1)
    draw.text(((box_left + box_right) / 2, logo_y + 27), status_text,
              font=font_status, fill=status_text_color, anchor='mm')
    draw.line([(x, 166), (W - PAD - 20, 166)], fill=LINE, width=1)


def _draw_label(draw, x, y, text):
    draw.text((x, y), text, font=_font('semibold', 12), fill=FAINT)


def _draw_credential_panel(draw, x, y, width, rows, accent, label='DELIVERY CONTENTS'):
    pad_x, pad_y = 28, 24
    label_h = 18
    line_h = 36
    row_gap = 22

    body = 0
    for _, lines in rows:
        body += label_h + 7 + (line_h * len(lines)) + row_gap
    body = body - row_gap if rows else 0
    height = pad_y * 2 + 32 + body

    _rounded(draw, (x, y, x + width, y + height), 18, fill=PANEL_BG, outline=PANEL_EDGE, width=1)
    draw.rectangle((x, y + 18, x + 4, y + height - 18), fill=accent)
    draw.text((x + pad_x, y + pad_y), label, font=_font('semibold', 11), fill=FAINT)

    cursor = y + pad_y + 30
    for label, lines in rows:
        draw.text((x + pad_x, cursor), label, font=_font('medium', 13), fill=MUTED)
        cursor += label_h + 5
        for line in lines:
            draw.text((x + pad_x, cursor), line, font=_font('semibold', 28), fill=TXT)
            cursor += line_h
        cursor += row_gap
    return height


def _draw_message_panel(draw, x, y, width, lines):
    if not lines:
        return 0
    pad_x, pad_y = 26, 22
    line_h = 31
    height = pad_y * 2 + 30 + line_h * len(lines)
    _rounded(draw, (x, y, x + width, y + height), 16, fill=(21, 26, 34), outline=PANEL_EDGE, width=1)
    draw.rectangle((x, y + 18, x + 4, y + height - 18), fill=GOLD)
    draw.text((x + pad_x, y + pad_y), 'MESSAGE', font=_font('semibold', 11), fill=FAINT)
    cursor = y + pad_y + 28
    for line in lines:
        draw.text((x + pad_x, cursor), line, font=_font('medium', 20), fill=SOFT)
        cursor += line_h
    return height


def _draw_footer(draw, height, left_text, right_text='APEX OTT'):
    y = height - 96
    draw.line([(PAD + 20, y), (W - PAD - 20, y)], fill=LINE, width=1)
    draw.text((PAD + 20, y + 28), left_text, font=_font('medium', 15), fill=FAINT)
    width = _text_width(draw, right_text, _font('semibold', 14))
    draw.text((W - PAD - 20 - width, y + 29), right_text, font=_font('semibold', 14), fill=MUTED)


def _value_rows(delivery_type, values):
    values = values or {}
    if delivery_type == 'idpw':
        return [('아이디', str(values.get('id') or '')),
                ('비밀번호', str(values.get('pw') or ''))]
    if delivery_type == 'key':
        return [('KEY', str(values.get('key') or ''))]
    return [('전달 내용', str(values.get('note') or ''))]


def render_delivery_card(product, order_no, category, delivery_type, values,
                         message='', buyer='', delivered_at=None):
    """상품 전달 카드 PNG bytes (실패 시 None)"""
    if not _PIL_OK:
        return None
    try:
        inner = W - (PAD + 20) * 2
        max_width = inner - 8
        probe = Image.new('RGB', (10, 10))
        measure = ImageDraw.Draw(probe)

        product_lines = _wrap(measure, _clean(product), _font('bold', 40), max_width)[:2]
        message_lines = _wrap(measure, _clean(message), _font('medium', 20), max_width - 56) if message else []

        rows = []
        for label, value in _value_rows(delivery_type, values):
            lines = _wrap(measure, value, _font('semibold', 28), max_width - 56)[:4]
            rows.append((label, lines))

        cursor = 200 + 22 + (46 * len(product_lines)) + 52
        panel_height = 24 * 2 + 32
        for _, lines in rows:
            panel_height += 18 + 7 + (36 * len(lines)) + 22
        if rows:
            panel_height -= 22
        cursor += panel_height + 18
        if message_lines:
            cursor += (22 * 2 + 30 + 31 * len(message_lines)) + 18
        height = max(660, int(cursor + 116))

        image, draw = _new_canvas(height)
        _rounded(draw, (24, 24, W - 24, height - 24), RADIUS, fill=CARD_BG, outline=CARD_EDGE, width=1)
        _draw_header(draw, 'DELIVERED', MINT, MINT)

        x = PAD + 20
        cursor = 200
        _draw_label(draw, x, cursor, 'PRODUCT')
        cursor += 22
        for line in product_lines:
            draw.text((x, cursor), line, font=_font('bold', 40), fill=TXT)
            cursor += 46
        meta = ' · '.join(part for part in [_clean(category), _clean(order_no)] if part)
        draw.text((x, cursor + 4), meta, font=_font('medium', 18), fill=MUTED)
        cursor += 26 + 26

        cursor += _draw_credential_panel(draw, x, cursor, inner, rows, MINT) + 18
        if message_lines:
            cursor += _draw_message_panel(draw, x, cursor, inner, message_lines) + 18

        receiver = f'수신 {_clean(buyer)} · ' if buyer else ''
        _draw_footer(draw, height, f'{receiver}전달 완료 {_kx_clock(delivered_at)} KST')

        import io
        buffer = io.BytesIO()
        image.save(buffer, format='PNG', optimize=True)
        return buffer.getvalue()
    except Exception as exc:
        print(f'[card] 전달 카드 렌더 실패: {exc}')
        return None


def render_order_card(product, order_no, category, amount, buyer, balance_after,
                      paid_at=None, window_minutes=30):
    """구매 접수(영수증) 카드 PNG bytes (실패 시 None)"""
    if not _PIL_OK:
        return None
    try:
        inner = W - (PAD + 20) * 2
        max_width = inner - 8
        probe = Image.new('RGB', (10, 10))
        measure = ImageDraw.Draw(probe)
        product_lines = _wrap(measure, _clean(product), _font('bold', 40), max_width)[:2]

        panel_height = 24 * 2 + 32 + ((18 + 7 + 36 + 22) * 3) - 22
        height = 200 + 22 + (46 * len(product_lines)) + 52 + panel_height + 18 + 116
        image, draw = _new_canvas(height)
        _rounded(draw, (24, 24, W - 24, height - 24), RADIUS, fill=CARD_BG, outline=CARD_EDGE, width=1)
        _draw_header(draw, 'PAYMENT RECEIVED', VIOLET, (196, 196, 255))

        x = PAD + 20
        cursor = 200
        _draw_label(draw, x, cursor, 'PRODUCT')
        cursor += 22
        for line in product_lines:
            draw.text((x, cursor), line, font=_font('bold', 40), fill=TXT)
            cursor += 46
        meta = ' · '.join(part for part in [_clean(category), _clean(order_no)] if part)
        draw.text((x, cursor + 4), meta, font=_font('medium', 18), fill=MUTED)
        cursor += 26 + 26

        rows = [
            ('결제 금액', [f'{int(amount):,}P']),
            ('결제 후 잔액', [f'{int(balance_after):,}P']),
            ('전달 예정', [f'{int(window_minutes)}분 이내 DM 전송']),
        ]
        cursor += _draw_credential_panel(draw, x, cursor, inner, rows, VIOLET, 'ORDER SUMMARY') + 18

        _draw_footer(draw, height, f'구매자 {_clean(buyer)} · 결제 {_kx_clock(paid_at)} KST')

        import io
        buffer = io.BytesIO()
        image.save(buffer, format='PNG', optimize=True)
        return buffer.getvalue()
    except Exception as exc:
        print(f'[card] 주문 카드 렌더 실패: {exc}')
        return None


