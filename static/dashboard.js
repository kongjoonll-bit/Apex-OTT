'use strict';

const state = {
  view: 'overview',
  products: [],
  orders: [],
  orderStatus: 'paid',
  users: [],
  charges: [],
  chargeStatus: 'pending',
  settings: {},
  discordOptions: { text: [], categories: [], roles: [] },
  deliverOrderId: null,
  deliverType: 'idpw',
  productEditId: null,
  pointsUserId: null,
};

const TITLES = {
  overview: ['개요', '주문과 지급 현황을 한눈에 확인합니다.'],
  orders: ['주문', '지급 대기 주문을 확인하고 상품을 전달합니다.'],
  products: ['상품', '판매 상품과 재고를 관리합니다.'],
  users: ['유저', '보유 포인트를 조회하고 조정합니다.'],
  charges: ['충전', '입금 신청을 확인하고 포인트를 지급합니다.'],
  reviews: ['후기', '구매자 후기를 확인합니다.'],
  settings: ['설정', '채널, 계좌, 웹훅, 자판기 패널을 설정합니다.'],
};

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function money(value) {
  return Number(value || 0).toLocaleString('ko-KR') + 'P';
}

function fmtDate(value) {
  if (!value) return '-';
  const iso = String(value).replace(' ', 'T');
  const date = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
  if (isNaN(date.getTime())) return '-';
  return date.toLocaleString('ko-KR', {
    timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  });
}

function relative(value) {
  if (!value) return '-';
  const iso = String(value).replace(' ', 'T');
  const date = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
  const diff = Math.floor((Date.now() - date.getTime()) / 1000);
  if (isNaN(diff) || diff < 0) return fmtDate(value);
  if (diff < 60) return diff + '초 전';
  if (diff < 3600) return Math.floor(diff / 60) + '분 전';
  if (diff < 86400) return Math.floor(diff / 3600) + '시간 전';
  return Math.floor(diff / 86400) + '일 전';
}

function badge(status) {
  const labels = { paid: '구매 완료', delivered: '지급 완료', cancelled: '취소' };
  return '<span class="badge ' + status + '">' + (labels[status] || status) + '</span>';
}

async function api(path, options) {
  const response = await fetch(path, Object.assign({
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
  }, options || {}));
  let data = null;
  try { data = await response.json(); } catch (err) { data = null; }
  if (response.status === 401) {
    window.location.href = '/login';
    throw new Error('로그인이 필요합니다.');
  }
  if (!response.ok || (data && data.ok === false)) {
    throw new Error((data && (data.error || data.message)) || '요청을 처리하지 못했습니다.');
  }
  return data || {};
}

function toast(message, type) {
  const wrap = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + (type || '');
  el.textContent = message;
  wrap.appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transition = 'opacity .2s';
    setTimeout(() => el.remove(), 220);
  }, 3200);
}

function openModal(id) { document.getElementById(id).classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }

function confirmAction(title, text, callback) {
  document.getElementById('confirm-title').textContent = title;
  document.getElementById('confirm-text').textContent = text;
  const button = document.getElementById('confirm-ok');
  const clone = button.cloneNode(true);
  button.parentNode.replaceChild(clone, button);
  clone.addEventListener('click', async () => {
    closeModal('confirm-modal');
    await callback();
  });
  openModal('confirm-modal');
}

function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text);
  }
  const area = document.createElement('textarea');
  area.value = text;
  document.body.appendChild(area);
  area.select();
  document.execCommand('copy');
  area.remove();
  return Promise.resolve();
}

function setBotStatus(online) {
  const dot = document.getElementById('bot-dot');
  const topDot = document.getElementById('top-dot');
  const label = document.getElementById('bot-status');
  const topLabel = document.getElementById('top-status');
  dot.className = 'dot ' + (online ? 'on' : 'off');
  topDot.className = 'dot ' + (online ? 'on' : 'off');
  label.textContent = online ? '봇 온라인' : '봇 오프라인';
  topLabel.textContent = online ? '봇 정상 작동' : '봇 연결 안 됨';
}

function switchView(view, sub) {
  state.view = view;
  document.querySelectorAll('.view').forEach((el) => {
    el.classList.toggle('active', el.id === 'view-' + view);
  });
  document.querySelectorAll('.nav button').forEach((el) => {
    el.classList.toggle('active', el.dataset.view === view);
  });
  const meta = TITLES[view] || TITLES.overview;
  document.getElementById('view-title').textContent = meta[0];
  document.getElementById('view-sub').textContent = meta[1];

  if (view === 'orders') {
    if (sub) {
      state.orderStatus = sub;
      document.querySelectorAll('#order-tabs button').forEach((el) => {
        el.classList.toggle('active', el.dataset.status === sub);
      });
    }
    loadOrders();
  }
  if (view === 'products') loadProducts();
  if (view === 'users') loadUsers();
  if (view === 'charges') loadCharges();
  if (view === 'reviews') loadReviews();
  if (view === 'settings') loadSettings();
}

// ---------- 개요 ----------
async function loadOverview() {
  try {
    const data = await api('/api/overview');
    const stats = data.stats || {};
    window.__pendingCache = data.pending || [];
    setBotStatus(!!stats.bot_online);
    document.getElementById('stat-pending').textContent = stats.pending || 0;
    document.getElementById('stat-today').textContent = stats.today_delivered || 0;
    document.getElementById('stat-today-sub').textContent = '오늘 매출 ' + money(stats.today_amount || 0);
    document.getElementById('stat-revenue').textContent = money(stats.revenue || 0);
    document.getElementById('stat-users').textContent = stats.users || 0;
    document.getElementById('stat-points').textContent = '보유 포인트 합계 ' + money(stats.total_points || 0);
    document.getElementById('nav-pending').textContent = stats.pending || 0;
    document.getElementById('nav-charges').textContent = stats.pending_charges || 0;
    document.getElementById('pending-hint').textContent = stats.pending ? '총 ' + stats.pending + '건' : '';

    renderPending(data.pending || []);
    renderRecent(data.recent || []);
    renderOverviewCharges(data.charges || []);
  } catch (err) {
    toast(err.message, 'err');
  }
}

function renderPending(orders) {
  const body = document.getElementById('pending-body');
  if (!orders.length) {
    body.innerHTML = '<tr><td colspan="6" class="empty">지급 대기 주문이 없습니다.</td></tr>';
    return;
  }
  body.innerHTML = orders.map((order) => `
    <tr>
      <td><span class="mono">${esc(order.order_no)}</span></td>
      <td>${esc(order.product_name)}<div class="sub">${esc(order.category || '기타')}</div></td>
      <td>${esc(order.username || '-')}<div class="sub">${esc(order.user_id)}</div></td>
      <td class="amount">${money(order.amount)}</td>
      <td>${relative(order.created_at)}</td>
      <td><button class="btn primary small" onclick="openDeliver(${order.id})">지급</button></td>
    </tr>`).join('');
}

function renderRecent(orders) {
  const list = document.getElementById('recent-list');
  if (!orders.length) {
    list.innerHTML = '<div class="empty">최근 지급 내역이 없습니다.</div>';
    return;
  }
  list.innerHTML = orders.map((order) => `
    <div class="list-item">
      <div class="grow">
        <div>${esc(order.product_name)}</div>
        <div class="sub">${esc(order.order_no)} · ${esc(order.username || order.user_id)}</div>
      </div>
      <div style="text-align:right">
        <div class="amount">${money(order.amount)}</div>
        <div class="sub">${relative(order.delivered_at || order.created_at)}</div>
      </div>
    </div>`).join('');
}

function renderOverviewCharges(charges) {
  const list = document.getElementById('charge-list');
  if (!charges.length) {
    list.innerHTML = '<div class="empty">대기 중인 충전 신청이 없습니다.</div>';
    return;
  }
  list.innerHTML = charges.map((charge) => `
    <div class="list-item">
      <div class="grow">
        <div>${esc(charge.depositor_name)}</div>
        <div class="sub">${esc(charge.username || charge.user_id)} · ${relative(charge.created_at)}</div>
      </div>
      <div style="text-align:right">
        <div class="amount plus">+${money(charge.amount)}</div>
        <button class="btn small" style="margin-top:5px" onclick="approveCharge(${charge.id})">승인</button>
      </div>
    </div>`).join('');
}

// ---------- 주문 ----------
async function loadOrders() {
  try {
    const data = await api('/api/orders?status=' + encodeURIComponent(state.orderStatus));
    state.orders = data.orders || [];
    renderOrders();
  } catch (err) {
    toast(err.message, 'err');
  }
}

function renderOrders() {
  const body = document.getElementById('orders-body');
  if (!state.orders.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty">해당 상태의 주문이 없습니다.</td></tr>';
    return;
  }
  body.innerHTML = state.orders.map((order) => {
    let actions = '';
    if (order.status === 'paid') {
      actions = `<button class="btn primary small" onclick="openDeliver(${order.id})">지급</button>
                 <button class="btn danger small" onclick="cancelOrder(${order.id}, '${esc(order.order_no)}')">취소</button>`;
    } else if (order.status === 'delivered') {
      actions = `<button class="btn small" onclick="resendOrder(${order.id}, '${esc(order.order_no)}')">재전송</button>`;
    } else {
      actions = '<span class="sub">-</span>';
    }
    const time = order.status === 'delivered' ? order.delivered_at
      : order.status === 'cancelled' ? order.cancelled_at : order.created_at;
    return `
      <tr>
        <td><span class="mono">${esc(order.order_no)}</span></td>
        <td>${esc(order.product_name)}<div class="sub">${esc(order.category || '기타')}</div></td>
        <td>${esc(order.username || '-')}<div class="sub">${esc(order.user_id)}</div></td>
        <td class="amount">${money(order.amount)}</td>
        <td>${badge(order.status)}</td>
        <td>${relative(time)}</td>
        <td><div class="row-actions">${actions}</div></td>
      </tr>`;
  }).join('');
}

async function cancelOrder(id, orderNo) {
  confirmAction('주문 취소', orderNo + ' 주문을 취소하면 포인트와 재고가 복구됩니다.', async () => {
    try {
      await api('/api/orders/' + id + '/cancel', { method: 'POST' });
      toast('주문을 취소했습니다.', 'ok');
      loadOrders();
      loadOverview();
    } catch (err) {
      toast(err.message, 'err');
    }
  });
}

async function resendOrder(id, orderNo) {
  confirmAction('상품 재전송', orderNo + ' 지급 정보를 구매자 DM으로 다시 전송합니다.', async () => {
    try {
      await api('/api/orders/' + id + '/resend', { method: 'POST' });
      toast('재전송했습니다.', 'ok');
    } catch (err) {
      toast(err.message, 'err');
    }
  });
}

// ---------- 지급 ----------
function openDeliver(orderId) {
  const cache = [...state.orders, ...(window.__pendingCache || [])];
  const order = cache.find((o) => o.id === orderId);
  state.deliverOrderId = orderId;
  state.deliverType = 'idpw';
  document.querySelectorAll('#deliver-types button').forEach((el) => {
    el.classList.toggle('active', el.dataset.type === 'idpw');
  });
  ['deliver-id', 'deliver-pw', 'deliver-key-value', 'deliver-note-value', 'deliver-message']
    .forEach((id) => { document.getElementById(id).value = ''; });
  document.getElementById('deliver-idpw').classList.remove('hidden');
  document.getElementById('deliver-key').classList.add('hidden');
  document.getElementById('deliver-note').classList.add('hidden');
  document.getElementById('deliver-sub').textContent = order
    ? `${order.order_no} · ${order.product_name} · ${order.username || order.user_id}`
    : '전달할 정보를 입력하면 구매자 DM으로 전송됩니다.';
  document.getElementById('deliver-submit').disabled = false;
  openModal('deliver-modal');
  setTimeout(() => document.getElementById('deliver-id').focus(), 80);
}

async function submitDeliver() {
  if (!state.deliverOrderId) return;
  const payload = {
    type: state.deliverType,
    message: document.getElementById('deliver-message').value.trim(),
  };
  if (state.deliverType === 'idpw') {
    payload.id = document.getElementById('deliver-id').value.trim();
    payload.pw = document.getElementById('deliver-pw').value.trim();
    if (!payload.id || !payload.pw) { toast('아이디와 비밀번호를 입력해 주세요.', 'err'); return; }
  } else if (state.deliverType === 'key') {
    payload.key = document.getElementById('deliver-key-value').value.trim();
    if (!payload.key) { toast('KEY를 입력해 주세요.', 'err'); return; }
  } else {
    payload.note = document.getElementById('deliver-note-value').value.trim();
    if (!payload.note) { toast('전달 내용을 입력해 주세요.', 'err'); return; }
  }

  const button = document.getElementById('deliver-submit');
  button.disabled = true;
  button.textContent = '전송 중…';
  try {
    const data = await api('/api/orders/' + state.deliverOrderId + '/deliver', {
      method: 'POST', body: JSON.stringify(payload),
    });
    closeModal('deliver-modal');
    toast(data.dm_sent ? '지급 완료 · 구매자 DM으로 전송했습니다.' : '지급 처리했지만 DM 전송에 실패했습니다.',
      data.dm_sent ? 'ok' : 'err');
    loadOrders();
    loadOverview();
  } catch (err) {
    toast(err.message, 'err');
  } finally {
    button.disabled = false;
    button.textContent = '지급 전송';
  }
}

// ---------- 상품 ----------
async function loadProducts() {
  try {
    const data = await api('/api/products');
    state.products = data.products || [];
    renderProducts();
  } catch (err) {
    toast(err.message, 'err');
  }
}

function renderProducts() {
  const body = document.getElementById('products-body');
  const query = (document.getElementById('product-search').value || '').trim().toLowerCase();
  const items = state.products.filter((p) =>
    !query || p.name.toLowerCase().includes(query) || (p.category || '').toLowerCase().includes(query));
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="6" class="empty">등록된 상품이 없습니다.</td></tr>';
    return;
  }
  body.innerHTML = items.map((product) => `
    <tr>
      <td>${esc(product.name)}<div class="sub">${esc(product.description || '')}</div></td>
      <td><span class="badge muted">${esc(product.category || '기타')}</span></td>
      <td class="amount">${money(product.price)}</td>
      <td>${product.stock < 0 ? '<span class="badge delivered">무제한</span>' : (product.stock > 0 ? product.stock + '개' : '<span class="badge cancelled">품절</span>')}</td>
      <td>${product.active ? '<span class="badge delivered">판매 중</span>' : '<span class="badge muted">중지</span>'}</td>
      <td>
        <div class="row-actions">
          <button class="btn small" onclick="openProductModal(${product.id})">수정</button>
          <button class="btn small" onclick="toggleProduct(${product.id}, ${product.active ? 0 : 1})">
            ${product.active ? '중지' : '판매'}
          </button>
          <button class="btn danger small" onclick="deleteProduct(${product.id})">삭제</button>
        </div>
      </td>
    </tr>`).join('');
}

function openProductModal(id) {
  state.productEditId = id || null;
  const product = id ? state.products.find((p) => p.id === id) : null;
  document.getElementById('product-modal-title').textContent = product ? '상품 수정' : '상품 추가';
  document.getElementById('product-name').value = product ? product.name : '';
  document.getElementById('product-category').value = product ? (product.category || '') : '';
  document.getElementById('product-price').value = product ? product.price : '';
  const unlimited = product ? Number(product.stock) < 0 : true;
  document.getElementById('product-unlimited').checked = unlimited;
  const stockInput = document.getElementById('product-stock');
  stockInput.value = product && !unlimited ? product.stock : 0;
  stockInput.disabled = unlimited;
  document.getElementById('product-active').checked = product ? !!product.active : true;
  document.getElementById('product-description').value = product ? (product.description || '') : '';
  openModal('product-modal');
  setTimeout(() => document.getElementById('product-name').focus(), 80);
}

async function submitProduct() {
  const unlimited = document.getElementById('product-unlimited').checked;
  const payload = {
    name: document.getElementById('product-name').value.trim(),
    category: document.getElementById('product-category').value.trim() || '기타',
    price: Number(document.getElementById('product-price').value || 0),
    stock: unlimited ? -1 : Number(document.getElementById('product-stock').value || 0),
    active: document.getElementById('product-active').checked ? 1 : 0,
    description: document.getElementById('product-description').value.trim(),
  };
  if (!payload.name || payload.price <= 0) {
    toast('상품명과 가격을 확인해 주세요.', 'err');
    return;
  }
  if (!unlimited && payload.stock < 0) {
    toast('재고는 0 이상으로 입력해 주세요.', 'err');
    return;
  }
  try {
    if (state.productEditId) {
      await api('/api/products/' + state.productEditId, { method: 'PUT', body: JSON.stringify(payload) });
    } else {
      await api('/api/products', { method: 'POST', body: JSON.stringify(payload) });
    }
    closeModal('product-modal');
    toast('상품을 저장했습니다.', 'ok');
    loadProducts();
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function toggleProduct(id, active) {
  try {
    await api('/api/products/' + id, { method: 'PUT', body: JSON.stringify({ active }) });
    loadProducts();
  } catch (err) {
    toast(err.message, 'err');
  }
}

function deleteProduct(id) {
  const product = state.products.find((p) => p.id === id);
  const name = product ? product.name : '선택한';
  confirmAction('상품 삭제', name + ' 상품을 삭제합니다. 되돌릴 수 없습니다.', async () => {
    try {
      await api('/api/products/' + id, { method: 'DELETE' });
      toast('상품을 삭제했습니다.', 'ok');
      loadProducts();
    } catch (err) {
      toast(err.message, 'err');
    }
  });
}

// ---------- 유저 ----------
let userTimer = null;
function debouncedUsers() {
  clearTimeout(userTimer);
  userTimer = setTimeout(loadUsers, 320);
}

async function loadUsers() {
  try {
    const query = (document.getElementById('user-search').value || '').trim();
    const data = await api('/api/users?q=' + encodeURIComponent(query));
    state.users = data.users || [];
    renderUsers();
  } catch (err) {
    toast(err.message, 'err');
  }
}

function renderUsers() {
  const body = document.getElementById('users-body');
  document.getElementById('user-count').textContent = state.users.length + '명';
  if (!state.users.length) {
    body.innerHTML = '<tr><td colspan="6" class="empty">조건에 맞는 유저가 없습니다.</td></tr>';
    return;
  }
  body.innerHTML = state.users.map((user) => `
    <tr>
      <td>${esc(user.username || '-')}</td>
      <td><span class="mono">${esc(user.user_id)}</span></td>
      <td class="amount">${money(user.balance)}</td>
      <td class="amount">${money(user.total_spent)}</td>
      <td>${fmtDate(user.created_at)}</td>
      <td><button class="btn small" onclick="openPoints('${esc(user.user_id)}')">포인트</button></td>
    </tr>`).join('');
}

function openPoints(userId) {
  const user = state.users.find((u) => String(u.user_id) === String(userId));
  state.pointsUserId = userId;
  document.getElementById('points-sub').textContent =
    `${user ? (user.username || user.user_id) : userId} · ${userId}`;
  document.getElementById('points-amount').value = '';
  document.getElementById('points-reason').value = '';
  openModal('points-modal');
  setTimeout(() => document.getElementById('points-amount').focus(), 80);
}

async function submitPoints() {
  const amount = Number(document.getElementById('points-amount').value || 0);
  if (!amount) { toast('금액을 입력해 주세요.', 'err'); return; }
  try {
    const data = await api('/api/users/' + state.pointsUserId + '/points', {
      method: 'POST',
      body: JSON.stringify({
        amount: amount,
        reason: document.getElementById('points-reason').value.trim(),
      }),
    });
    closeModal('points-modal');
    toast('포인트를 조정했습니다. 잔액 ' + money(data.balance), 'ok');
    loadUsers();
    loadOverview();
  } catch (err) {
    toast(err.message, 'err');
  }
}

// ---------- 충전 ----------
async function loadCharges() {
  try {
    const data = await api('/api/charges?status=' + encodeURIComponent(state.chargeStatus));
    state.charges = data.charges || [];
    renderCharges();
  } catch (err) {
    toast(err.message, 'err');
  }
}

function renderCharges() {
  const body = document.getElementById('charges-body');
  if (!state.charges.length) {
    body.innerHTML = '<tr><td colspan="6" class="empty">해당 상태의 충전 신청이 없습니다.</td></tr>';
    return;
  }
  const labels = { pending: '입금 대기', completed: '충전 완료', cancelled: '취소' };
  const badges = { pending: 'paid', completed: 'delivered', cancelled: 'cancelled' };
  body.innerHTML = state.charges.map((charge) => {
    let actions = '<span class="sub">-</span>';
    if (charge.status === 'pending') {
      actions = `<div class="row-actions">
        <button class="btn primary small" onclick="approveCharge(${charge.id})">승인</button>
        <button class="btn danger small" onclick="cancelCharge(${charge.id})">취소</button>
      </div>`;
    }
    return `
      <tr>
        <td>${esc(charge.username || '-')}<div class="sub">${esc(charge.user_id)}</div></td>
        <td>${esc(charge.depositor_name)}</td>
        <td class="amount plus">+${money(charge.amount)}</td>
        <td><span class="badge ${badges[charge.status] || 'muted'}">${labels[charge.status] || charge.status}</span></td>
        <td>${relative(charge.created_at)}</td>
        <td>${actions}</td>
      </tr>`;
  }).join('');
}

async function approveCharge(id) {
  confirmAction('충전 승인', '입금을 확인하고 포인트를 지급합니다.', async () => {
    try {
      await api('/api/charges/' + id + '/approve', { method: 'POST' });
      toast('충전을 승인했습니다.', 'ok');
      loadCharges();
      loadOverview();
    } catch (err) {
      toast(err.message, 'err');
    }
  });
}

async function cancelCharge(id) {
  confirmAction('충전 취소', '충전 신청을 취소 처리합니다.', async () => {
    try {
      await api('/api/charges/' + id + '/cancel', { method: 'POST' });
      toast('충전 신청을 취소했습니다.', 'ok');
      loadCharges();
      loadOverview();
    } catch (err) {
      toast(err.message, 'err');
    }
  });
}

// ---------- 후기 ----------
async function loadReviews() {
  try {
    const data = await api('/api/reviews');
    renderReviews(data.reviews || [], data.summary || {});
  } catch (err) {
    toast(err.message, 'err');
  }
}

function renderReviews(reviews, summary) {
  const body = document.getElementById('reviews-body');
  const summaryEl = document.getElementById('review-summary');
  const average = Number(summary.average || 0);
  summaryEl.textContent = `총 ${summary.count || 0}개 · 평균 ★ ${average.toFixed(1)}`;
  if (!reviews.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty">아직 등록된 후기가 없습니다.</td></tr>';
    return;
  }
  body.innerHTML = reviews.map((review) => {
    const rating = Number(review.rating || 0);
    const stars = '★'.repeat(rating) + '☆'.repeat(Math.max(0, 5 - rating));
    return `
      <tr>
        <td>${esc(review.product_name || '-')}</td>
        <td>${esc(review.username || review.user_id)}<div class="sub">${esc(review.user_id)}</div></td>
        <td><span style="color:var(--gold); letter-spacing:1px;">${stars}</span><div class="sub">${rating}점</div></td>
        <td style="max-width:420px;">${esc(review.content || '')}</td>
        <td>${fmtDate(review.created_at)}</td>
      </tr>`;
  }).join('');
}

// ---------- 설정 ----------
const SETTING_FIELDS = [
  'purchase_log_channel_id', 'charge_log_channel_id', 'ticket_category_id',
  'review_channel_id', 'notice_channel_id', 'ticket_ping_role_id',
  'delivery_window_minutes', 'bank_name', 'bank_account', 'bank_holder',
];

function fillSelect(id, items, current, emptyLabel) {
  const element = document.getElementById(id);
  if (!element) return;
  const map = new Map();
  (items || []).forEach((item) => map.set(String(item.id), item.name));
  if (current && !map.has(String(current))) {
    map.set(String(current), `현재 설정 (${current})`);
  }
  const options = [`<option value="">${esc(emptyLabel)}</option>`];
  map.forEach((name, value) => {
    options.push(`<option value="${esc(value)}">${esc(name)}</option>`);
  });
  element.innerHTML = options.join('');
  element.value = current ? String(current) : '';
}

function updateBannerPreview() {
  const input = document.getElementById('panel-banner-url');
  const image = document.getElementById('panel-banner-preview');
  if (!input || !image) return;
  const url = input.value.trim();
  if (url) {
    image.src = url;
    image.style.display = 'block';
  } else {
    image.removeAttribute('src');
    image.style.display = 'none';
  }
}

async function loadSettings() {
  try {
    const [data, options] = await Promise.all([api('/api/settings'), api('/api/discord/channels')]);
    const settings = data.settings || {};
    state.settings = settings;
    state.discordOptions = options;
    fillSelect('set-purchase_log_channel_id', options.text, settings.purchase_log_channel_id, '채널 선택 안 함');
    fillSelect('set-charge_log_channel_id', options.text, settings.charge_log_channel_id, '채널 선택 안 함');
    fillSelect('set-ticket_category_id', options.categories, settings.ticket_category_id, '카테고리 선택 안 함');
    fillSelect('set-review_channel_id', options.text, settings.review_channel_id, '채널 선택 안 함');
    fillSelect('set-notice_channel_id', options.text, settings.notice_channel_id, '채널 선택 안 함');
    fillSelect('set-ticket_ping_role_id', options.roles, settings.ticket_ping_role_id, '알림 역할 없음');
    fillSelect('panel-channel-select', options.text, settings.panel_channel_id, '패널 채널 선택 안 함');
    SETTING_FIELDS.forEach((key) => {
      const manual = document.getElementById('id-' + key);
      if (manual) manual.value = '';
    });
    const panelManual = document.getElementById('id-panel_channel_id');
    if (panelManual) panelManual.value = '';

    document.getElementById('set-delivery_window_minutes').value =
      settings.delivery_window_minutes || '30';
    document.getElementById('set-bank_name').value = settings.bank_name || '';
    document.getElementById('set-bank_account').value = settings.bank_account || '';
    document.getElementById('set-bank_holder').value = settings.bank_holder || '';
    document.getElementById('set-card_image_enabled').checked =
      String(settings.card_image_enabled || 'true') !== 'false';
    document.getElementById('panel-banner-url').value = settings.panel_banner_url || '';
    updateBannerPreview();
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function saveSettings() {
  const payload = {};
  for (const key of SETTING_FIELDS) {
    const manual = document.getElementById('id-' + key);
    const manualValue = manual ? manual.value.trim() : '';
    if (manualValue) {
      if (!/^\d+$/.test(manualValue)) {
        toast('직접 입력한 채널 ID는 숫자여야 합니다.', 'err');
        return;
      }
      payload[key] = manualValue;
    } else {
      const element = document.getElementById('set-' + key);
      if (element) payload[key] = element.value.trim();
    }
  }
  const panelManual = document.getElementById('id-panel_channel_id').value.trim();
  if (panelManual && !/^\d+$/.test(panelManual)) {
    toast('패널 채널 ID는 숫자여야 합니다.', 'err');
    return;
  }
  payload.card_image_enabled = document.getElementById('set-card_image_enabled').checked ? 'true' : 'false';
  payload.panel_banner_url = document.getElementById('panel-banner-url').value.trim();
  payload.panel_channel_id = panelManual || document.getElementById('panel-channel-select').value;
  try {
    await api('/api/settings', { method: 'POST', body: JSON.stringify(payload) });
    toast('설정을 저장했습니다.', 'ok');
    loadSettings();
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function installPanel() {
  const panelManual = document.getElementById('id-panel_channel_id').value.trim();
  if (panelManual && !/^\d+$/.test(panelManual)) {
    toast('패널 채널 ID는 숫자여야 합니다.', 'err');
    return;
  }
  const channelId = panelManual || document.getElementById('panel-channel-select').value;
  const banner = document.getElementById('panel-banner-url').value.trim();
  if (!channelId) {
    toast('패널을 설치할 채널을 선택하거나 ID를 입력해 주세요.', 'err');
    return;
  }
  try {
    const data = await api('/api/panel/install', {
      method: 'POST',
      body: JSON.stringify({ channel_id: channelId, banner_url: banner }),
    });
    toast(data.message || '패널을 설치했습니다.', 'ok');
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function changePassword() {
  const value = document.getElementById('set-new-password').value.trim();
  if (value.length < 6) { toast('비밀번호는 6자 이상이어야 합니다.', 'err'); return; }
  try {
    await api('/api/settings', { method: 'POST', body: JSON.stringify({ new_password: value }) });
    document.getElementById('set-new-password').value = '';
    toast('비밀번호를 변경했습니다.', 'ok');
  } catch (err) {
    toast(err.message, 'err');
  }
}

function copyWebhook() {
  const value = document.getElementById('webhook-url').value;
  copyText(value).then(() => toast('웹훅 URL을 복사했습니다.', 'ok'));
}

function rotateWebhook() {
  confirmAction('웹훅 키 재발급', '기존 키는 즉시 사용할 수 없게 됩니다. Tasker 설정도 함께 변경해야 합니다.', async () => {
    try {
      const data = await api('/api/settings/rotate-webhook', { method: 'POST' });
      const url = window.location.origin + '/api/deposit-webhook?key=' + data.webhook_key;
      document.getElementById('webhook-url').value = url;
      toast('새 키를 발급했습니다.', 'ok');
    } catch (err) {
      toast(err.message, 'err');
    }
  });
}

async function testWebhook() {
  try {
    const data = await api('/api/deposit-webhook/test', {
      method: 'POST',
      body: JSON.stringify({ title: '토스뱅크', body: '5,000원 입금 홍길동' }),
    });
    toast(data.parsed
      ? `파싱 성공 · ${data.depositor} · ${Number(data.amount).toLocaleString('ko-KR')}원`
      : '파싱하지 못했습니다. 알림 형식을 확인해 주세요.', data.parsed ? 'ok' : 'err');
  } catch (err) {
    toast(err.message, 'err');
  }
}

// ---------- 초기화 ----------
document.querySelectorAll('.nav button').forEach((button) => {
  button.addEventListener('click', () => switchView(button.dataset.view));
});

document.querySelectorAll('#order-tabs button').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelectorAll('#order-tabs button').forEach((el) => el.classList.remove('active'));
    button.classList.add('active');
    state.orderStatus = button.dataset.status;
    loadOrders();
  });
});

document.querySelectorAll('#charge-tabs button').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelectorAll('#charge-tabs button').forEach((el) => el.classList.remove('active'));
    button.classList.add('active');
    state.chargeStatus = button.dataset.status;
    loadCharges();
  });
});

document.querySelectorAll('#deliver-types button').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelectorAll('#deliver-types button').forEach((el) => el.classList.remove('active'));
    button.classList.add('active');
    state.deliverType = button.dataset.type;
    document.getElementById('deliver-idpw').classList.toggle('hidden', state.deliverType !== 'idpw');
    document.getElementById('deliver-key').classList.toggle('hidden', state.deliverType !== 'key');
    document.getElementById('deliver-note').classList.toggle('hidden', state.deliverType !== 'note');
  });
});

const unlimitedInput = document.getElementById('product-unlimited');
if (unlimitedInput) {
  unlimitedInput.addEventListener('change', () => {
    const stockInput = document.getElementById('product-stock');
    stockInput.disabled = unlimitedInput.checked;
    if (unlimitedInput.checked) stockInput.value = 0;
  });
}

const bannerInput = document.getElementById('panel-banner-url');
if (bannerInput) {
  bannerInput.addEventListener('input', updateBannerPreview);
}

document.querySelectorAll('.overlay').forEach((overlay) => {
  overlay.addEventListener('click', (event) => {
    if (event.target === overlay) overlay.classList.remove('open');
  });
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    document.querySelectorAll('.overlay.open').forEach((overlay) => overlay.classList.remove('open'));
  }
});

async function refreshStatus() {
  try {
    const response = await fetch('/ping', { credentials: 'same-origin' });
    const data = await response.json();
    setBotStatus(!!data.bot_online);
  } catch (err) {
    setBotStatus(false);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  loadOverview();
  loadSettings();
  setInterval(refreshStatus, 60000);
  setInterval(() => {
    if (state.view === 'overview') loadOverview();
    if (state.view === 'orders' && state.orderStatus === 'paid') loadOrders();
  }, 45000);
});





