/* frontend/_selftest.js —— 无浏览器 DOM 桩自测（零依赖，node _selftest.js）
 * 用法：node frontend/_selftest.js
 * 说明：手写最小 DOM + fetch 桩，加载真实 app.js，覆盖视图切换/对话/审批/审计/技能/外部服务/设置/窄屏。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

let PASS = 0, FAIL = 0;
const FAILS = [];
const SCROLLS = [];
function ok(name, cond, extra) {
  if (cond) { PASS++; console.log('  PASS  ' + name); }
  else { FAIL++; FAILS.push(name + (extra ? ' -> ' + extra : '')); console.log('  FAIL  ' + name + (extra ? ' -> ' + extra : '')); }
}
function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function enc(s) { return new Uint8Array(Buffer.from(s, 'utf8')); }

// ===================== 最小 DOM =====================
const VOID_TAGS = { meta: 1, link: 1, br: 1, input: 1, img: 1, hr: 1, source: 1, area: 1, base: 1, col: 1, embed: 1, param: 1, track: 1, wbr: 1 };

class ClassList {
  constructor(el) { this.el = el; }
  _list() { return (this.el.getAttribute('class') || '').split(/\s+/).filter(Boolean); }
  _set(a) { this.el.setAttribute('class', a.join(' ')); }
  add(...names) { const a = this._list(); names.forEach(function (n) { if (a.indexOf(n) === -1) a.push(n); }); this._set(a); }
  remove(...names) { const a = this._list().filter(function (n) { return names.indexOf(n) === -1; }); this._set(a); }
  contains(n) { return this._list().indexOf(n) !== -1; }
  toggle(n, force) {
    const has = this.contains(n);
    const want = force === undefined ? !has : !!force;
    if (want) this.add(n); else this.remove(n);
    return want;
  }
}

class Style {
  constructor() { this._p = {}; }
  setProperty(k, v) { this._p[k] = v; }
  removeProperty(k) { delete this._p[k]; }
  get cssText() { return Object.keys(this._p).map(k => k + ':' + this._p[k]).join(';'); }
  set cssText(v) { this._p = {}; }
}

class Node {
  constructor(tag, doc) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.nodeType = 1;
    this.nodeName = this.tagName;
    this.ownerDocument = doc || null;
    this.childNodes = [];
    this.parentNode = null;
    this._attrs = {};
    this._text = '';
    this._listeners = {};
    this.classList = new ClassList(this);
    this.style = new Style();
    this.offsetWidth = 0;
    this.offsetHeight = 0;
    this.scrollHeight = 0;
    this.scrollTop = 0;
  }
  get classList() { return this._classList || (this._classList = new ClassList(this)); }
  set classList(v) { this._classList = v; }
  get children() { return this.childNodes.filter(function (n) { return n.nodeType === 1; }); }
  get firstChild() { return this.childNodes[0] || null; }
  get isConnected() { return !!this.parentNode; }
  get parentElement() { return this.parentNode; }
  getAttribute(n) { return Object.prototype.hasOwnProperty.call(this._attrs, n) ? this._attrs[n] : null; }
  setAttribute(n, v) { this._attrs[n] = String(v); }
  removeAttribute(n) { delete this._attrs[n]; }
  hasAttribute(n) { return Object.prototype.hasOwnProperty.call(this._attrs, n); }
  get id() { return this.getAttribute('id') || ''; }
  set id(v) { this.setAttribute('id', v); }
  get className() { return this.getAttribute('class') || ''; }
  set className(v) { this.setAttribute('class', v); }
  get hidden() { return this.hasAttribute('hidden'); }
  set hidden(v) { if (v) this.setAttribute('hidden', ''); else this.removeAttribute('hidden'); }
  get disabled() { return this.hasAttribute('disabled'); }
  set disabled(v) { if (v) this.setAttribute('disabled', ''); else this.removeAttribute('disabled'); }
  get value() { return this._value === undefined ? (this.getAttribute('value') || '') : this._value; }
  set value(v) { this._value = String(v); }
  get title() { return this.getAttribute('title') || ''; }
  set title(v) { this.setAttribute('title', v); }
  get type() { return this.getAttribute('type') || ''; }
  set type(v) { this.setAttribute('type', v); }
  get textContent() {
    if (this.nodeType === 3) return this._text;
    return this.childNodes.map(function (n) { return n.nodeType === 3 ? n._text : n.textContent; }).join('');
  }
  set textContent(v) {
    if (this.nodeType === 3) { this._text = String(v); return; }
    this.childNodes = [];
    const t = new Node('#text', this.ownerDocument);
    t.nodeType = 3;
    t._text = String(v);
    t.parentNode = this;
    this.childNodes.push(t);
  }
  get innerHTML() { return serialize(this); }
  set innerHTML(html) {
    this.childNodes = [];
    const nodes = parseFragment(String(html), this.ownerDocument);
    const self = this;
    nodes.forEach(function (n) { n.parentNode = self; self.childNodes.push(n); });
  }
  appendChild(n) {
    if (n.parentNode) n.parentNode.removeChild(n);
    n.parentNode = this;
    this.childNodes.push(n);
    return n;
  }
  insertBefore(n, ref) {
    const i = this.childNodes.indexOf(ref);
    if (i === -1) return this.appendChild(n);
    if (n.parentNode) n.parentNode.removeChild(n);
    n.parentNode = this;
    this.childNodes.splice(i, 0, n);
    return n;
  }
  removeChild(n) {
    const i = this.childNodes.indexOf(n);
    if (i !== -1) this.childNodes.splice(i, 1);
    n.parentNode = null;
    return n;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  insertAdjacentHTML(pos, html) {
    const nodes = parseFragment(String(html), this.ownerDocument);
    const self = this;
    nodes.forEach(function (n) { n.parentNode = self; });
    if (pos === 'beforeend') nodes.forEach(function (n) { self.childNodes.push(n); });
  }
  contains(n) {
    let p = n;
    while (p) { if (p === this) return true; p = p.parentNode; }
    return false;
  }
  matches(sel) { return matchesSelector(this, sel); }
  closest(sel) {
    let p = this;
    while (p && p.nodeType === 1) {
      if (matchesSelector(p, sel)) return p;
      p = p.parentNode;
    }
    return null;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const out = [];
    const root = this;
    const sels = String(sel).split(',').map(function (s) { return s.trim(); }).filter(Boolean);
    walk(root, function (el) {
      if (el === root || el.nodeType !== 1) return;
      if (sels.some(function (s) { return matchComplex(el, s); })) out.push(el);
    });
    return out;
  }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  removeEventListener(type, fn) {
    const l = this._listeners[type];
    if (l) this._listeners[type] = l.filter(function (f) { return f !== fn; });
  }
  dispatchEvent(ev) {
    ev.target = ev.target || this;
    let node = this;
    while (node) {
      const list = node._listeners[ev.type];
      if (list) {
        ev.currentTarget = node;
        list.slice().forEach(function (fn) { fn.call(node, ev); });
      }
      if (ev._stop) break;
      node = node.parentNode;
    }
    return !ev._prevented;
  }
  click() { this.dispatchEvent(makeEvent('click')); }
  focus() { this.ownerDocument && (this.ownerDocument.activeElement = this); }
  blur() {}
  select() {}
  setSelectionRange() {}
}

function walk(node, fn) {
  fn.call(node, node);
  node.childNodes.forEach(function (c) { if (c.nodeType === 1) walk(c, fn); });
}

function makeEvent(type, extra) {
  const ev = {
    type: type, target: null, currentTarget: null, _prevented: false, _stop: false,
    preventDefault: function () { ev._prevented = true; }, stopPropagation: function () { ev._stop = true; }
  };
  Object.keys(extra || {}).forEach(function (k) { ev[k] = extra[k]; });
  return ev;
}

function matchComplex(el, sel) {
  const parts = String(sel).trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return false;
  if (!matchesSelector(el, parts[parts.length - 1])) return false;
  let idx = parts.length - 2;
  let p = el.parentNode;
  while (idx >= 0 && p && p.nodeType === 1) {
    if (matchesSelector(p, parts[idx])) idx -= 1;
    p = p.parentNode;
  }
  return idx < 0;
}

function matchesSelector(el, sel) {
  if (!el || el.nodeType !== 1) return false;
  let s = String(sel).trim();
  let m = s.match(/^([a-zA-Z][\w-]*)?((?:[.#][\w-]+|\[[\w-]+(?:=["'][^"']*["'])?\])*)$/);
  if (!m) return false;
  const tag = m[1];
  const rest = m[2] || '';
  if (tag && el.tagName !== tag.toUpperCase()) return false;
  const parts = rest.match(/[.#][\w-]+|\[[\w-]+(?:=["'][^"']*["'])?\]/g) || [];
  return parts.every(function (p) {
    if (p[0] === '.') return el.classList.contains(p.slice(1));
    if (p[0] === '#') return el.getAttribute('id') === p.slice(1);
    const am = p.match(/^\[([\w-]+)(?:=["']([^"']*)["'])?\]$/);
    if (!am) return false;
    if (am[2] === undefined) return el.hasAttribute(am[1]);
    return el.getAttribute(am[1]) === am[2];
  });
}

function serialize(node) {
  if (node.nodeType === 3) return node._text;
  const attrs = Object.keys(node._attrs).map(function (k) { return node._attrs[k] === '' ? ' ' + k : ' ' + k + '="' + node._attrs[k] + '"'; }).join('');
  const inner = node.childNodes.map(serialize).join('');
  return '<' + node.tagName.toLowerCase() + attrs + '>' + inner + '</' + node.tagName.toLowerCase() + '>';
}

function parseFragment(html, doc) {
  const nodes = [];
  const stack = [];
  let i = 0;
  const top = function () { return stack.length ? stack[stack.length - 1] : null; };
  while (i < html.length) {
    if (html.charAt(i) === '<') {
      if (html.startsWith('<!--', i)) { i = html.indexOf('-->', i) + 3; continue; }
      const end = html.indexOf('>', i);
      if (end === -1) break;
      let raw = html.slice(i + 1, end);
      if (raw.charAt(0) === '/') {
        const tagName = raw.slice(1).trim().toUpperCase();
        for (let k = stack.length - 1; k >= 0; k--) {
          if (stack[k].tagName === tagName) { stack.length = k; break; }
        }
        i = end + 1;
        continue;
      }
      const selfClose = raw.endsWith('/');
      if (selfClose) raw = raw.slice(0, -1);
      const tm = raw.match(/^([a-zA-Z][\w-]*)/);
      if (!tm) { i = end + 1; continue; }
      const el = new Node(tm[1], doc);
      const attrRe = /([\w:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
      const rest = raw.slice(tm[1].length);
      let am;
      while ((am = attrRe.exec(rest))) {
        const val = am[2] !== undefined ? am[2] : (am[3] !== undefined ? am[3] : (am[4] !== undefined ? am[4] : ''));
        el.setAttribute(am[1], val);
      }
      const parent = top();
      if (parent) { el.parentNode = parent; parent.childNodes.push(el); }
      else nodes.push(el);
      i = end + 1;
      if (!VOID_TAGS[el.tagName.toLowerCase()] && !selfClose) stack.push(el);
      if (el.tagName === 'SCRIPT' || el.tagName === 'STYLE') {
        const closeTag = '</' + el.tagName.toLowerCase() + '>';
        const closeIdx = html.toLowerCase().indexOf(closeTag, i);
        if (closeIdx !== -1) {
          el.textContent = html.slice(i, closeIdx);
          i = closeIdx + closeTag.length;
          stack.pop();
        }
      }
      continue;
    }
    let next = html.indexOf('<', i);
    if (next === -1) next = html.length;
    const text = html.slice(i, next);
    if (text.trim()) {
      const parent = top();
      const t = new Node('#text', doc);
      t.nodeType = 3;
      t._text = text;
      if (parent) { t.parentNode = parent; parent.childNodes.push(t); }
      else nodes.push(t);
    }
    i = next;
  }
  return nodes;
}

const htmlPath = path.join(__dirname, 'index.html');
const appPath = path.join(__dirname, 'app.js');
const html = fs.readFileSync(htmlPath, 'utf8');
const appCode = fs.readFileSync(appPath, 'utf8');

const doc = {
  nodeType: 9, readyState: 'complete', activeElement: null,
  _listeners: {},
  createElement: function (t) { return new Node(t, doc); },
  createTextNode: function (t) { const n = new Node('#text', doc); n.nodeType = 3; n._text = String(t); return n; },
  getElementById: function (id) { let found = null; walk(doc.body, function (el) { if (!found && el.getAttribute && el.getAttribute('id') === id) found = el; }); return found; },
  querySelector: function (s) { return doc.querySelectorAll(s)[0] || null; },
  querySelectorAll: function (s) {
    const out = [];
    const sels = String(s).split(',').map(function (x) { return x.trim(); }).filter(Boolean);
    walk(doc.body, function (el) { if (el.nodeType !== 1) return; if (sels.some(function (x) { return matchComplex(el, x); })) out.push(el); });
    return out;
  },
  addEventListener: function (t, fn) { (doc._listeners[t] = doc._listeners[t] || []).push(fn); },
  removeEventListener: function (t, fn) {
    const l = doc._listeners[t];
    if (l) doc._listeners[t] = l.filter(function (f) { return f !== fn; });
  },
  dispatchEvent: function (ev) {
    ev.target = ev.target || doc;
    (doc._listeners[ev.type] || []).slice().forEach(function (fn) { ev.currentTarget = doc; fn.call(doc, ev); });
    return !ev._prevented;
  },
  execCommand: function () { return true; }
};
const body = new Node('body', doc);
doc.documentElement = new Node('html', doc);
doc.documentElement.parentNode = doc;
doc.documentElement.appendChild(body);
doc.body = body;
parseFragment(html, doc).forEach(function (n) { n.parentNode = body; body.childNodes.push(n); });

// ===================== fetch 桩 =====================
const FIX = {
  demoSms: { ok: true, user_id: 'u001', code: '654321', seconds_left: 120, ttl: 120 },
  demoFace: { ok: true, user_id: 'u001', token: 'fc_abc123def456', seconds_left: 60, ttl: 60 },
  session: {
    session_id: 'sess_test_001',
    user: { user_id: 'u001', name: '张伟', risk_level: '稳健型' },
    accounts: [
      { account_id: 'a1', type: 'checking', name: '工资卡', number_masked: '**** 8821', balance_cents: 1284450, currency: 'CNY' },
      { account_id: 'a2', type: 'credit', name: '信用卡', number_masked: '**** 6712', balance_cents: -843200, currency: 'CNY' },
      { account_id: 'a3', type: 'savings', name: '储蓄', number_masked: '**** 3390', balance_cents: 4500000, currency: 'CNY' }
    ],
    llm_mode: 'auto', demo_mode: true
  },
  tasksAll: [
    { id: 't01', title: '查本月支出前三类', category: '账单', user_input: '查一下这个月支出最多的三类' },
    { id: 't02', title: '本月账单汇总', category: '账单', user_input: '看看我这个月的账单' },
    { id: 't03', title: '餐饮支出分类', category: '账单', user_input: '这个月餐饮花了多少' },
    { id: 't04', title: '近三个月账单汇总', category: '账单', user_input: '近三个月的账单汇总' },
    { id: 't05', title: '预算超支分析', category: '账单', user_input: '帮我看看预算超了没有' },
    { id: 't06', title: '查工资卡余额', category: '账户', user_input: '我工资卡还有多少钱' },
    { id: 't07', title: '转账 800 元给妈妈', category: '转账', user_input: '给妈妈转 800 元' },
    { id: 't08', title: '大额转账 2000 元给李明', category: '转账', user_input: '给李明转 2000 元' },
    { id: 't09', title: '申购 1000 元稳利宝', category: '理财', user_input: '帮我申购 1000 元稳利宝' },
    { id: 't10', title: '查理财持仓收益', category: '理财', user_input: '我的理财收益怎么样' },
    { id: 't11', title: '查询银行卡列表', category: '卡片', user_input: '我有哪些银行卡' },
    { id: 't12', title: '冻结信用卡', category: '卡片', user_input: '把我的信用卡冻结' },
    { id: 't13', title: '申请虚拟卡', category: '卡片', user_input: '帮我申请一张虚拟卡' },
    { id: 't14', title: '查询订阅', category: '订阅', user_input: '我订了哪些会员' },
    { id: 't15', title: '取消爱奇艺会员', category: '订阅', user_input: '取消爱奇艺会员' },
    { id: 't16', title: '订阅月支出统计', category: '订阅', user_input: '我每月订阅一共花多少钱' },
    { id: 't17', title: '工资到账分账', category: '组合', user_input: '发工资了，3000 存到储蓄卡，2000 买稳利宝，1000 还信用卡' },
    { id: 't18', title: '旅行预算测算', category: '组合', user_input: '去日本旅行要花多少钱，帮我做个预算' }
  ],
  llm: { mode: 'auto', effective: 'rule', enabled: true, provider: 'deepseek', model: 'deepseek-chat', base_host: 'api.deepseek.com', key_masked: 'sk-****', last_error: null, last_error_code: null, last_error_hint: null, degraded: false, breaker: { open: false, failures: 0, threshold: 3, cooldown_s: 60, retry_after_s: 0 }, stats: { calls: 4, ok: 3, fail: 1, retries: 1, timeouts: 1, circuit_skipped: 0, last_latency_ms: 412, last_usage: { prompt_tokens: 9, completion_tokens: 6, total_tokens: 15 }, last_cost_yuan: 0.0009, total_cost_yuan: 0.0031, total_tokens: 530 } },
  llmTest: { ok: true, latency_ms: 412, model: 'deepseek-chat', provider: 'deepseek', usage: { prompt_tokens: 9, completion_tokens: 6, total_tokens: 15 }, cost_yuan: 0.0009 },
  llmTestReject: false, llmTestManual: false, llmTestResolve: null,
  skills: { ok: true, skills: [
    { id: 'transfer', name: '转账', tool: 'create_transfer', risk: 'high', description: '向收款人转账，需动态验证码' },
    { id: 'bills', name: '账单分析', tool: 'get_bill_summary', risk: 'low', description: '汇总支出分类' },
    { id: 'wealth', name: '理财', tool: 'buy_wealth', risk: 'high', description: '购买理财产品' },
    { id: 'cards', name: '卡片', tool: 'card_ops', risk: 'medium', description: '挂失与限额' },
    { id: 'subscriptions', name: '订阅', tool: 'list_subscriptions', risk: 'low', description: '列出自动续费' },
    { id: 'cross', name: '跨境', tool: 'fx_quote', risk: 'high', description: '汇率换算' }
  ] },
  services: { ok: true, enabled: true, servers: [
    { name: 'fx', url: 'http://127.0.0.1:9101/mcp', status: 'ok', latency_ms: 12, last_error: null },
    { name: 'risk', url: 'http://127.0.0.1:9102/mcp', status: 'down', latency_ms: null, last_error: 'connection refused' }
  ] },
  channel: { ok: true, enabled: true, port: 9100, token_configured: true, sessions: 3 },
  health: { status: 'ok', app: 'zerostep-bank', version: '0.2.0', llm_mode: 'auto', demo_mode: true, seed_version: 3 },
  audit: { entries: [], chain: { valid: true, checked: 3, broken_at: null, reason: null } }
};
const AUDIT_ROWS = [
  { seq: 1, ts: '2026-10-26T12:41:58+08:00', actor: 'system', event_type: 'tool_result', tool: 'get_bill_summary', args_hash: 'a3', result_hash: 'b3', risk_level: 'low', approval_id: null, prev_hash: '0000', hash: 'da1c9edc5e6f7081' },
  { seq: 2, ts: '2026-10-26T12:41:59+08:00', actor: 'system', event_type: 'response', tool: 'respond', args_hash: 'a2', result_hash: 'b2', risk_level: 'low', approval_id: null, prev_hash: '1111', hash: 'b759efeb1a2b3c4d' },
  { seq: 3, ts: '2026-10-26T12:42:00+08:00', actor: 'system', event_type: 'approval_request', tool: 'policy_gate', args_hash: 'a1', result_hash: 'b1', risk_level: 'high', approval_id: 'ap_1', prev_hash: '0000', hash: '3f8a1b2c9d4e5f60' }
];
FIX.audit.entries = AUDIT_ROWS;

// ---- v0.7.0 专区：仪表盘 / 分析 固定数据 ----
const SPARK_OUT = [6100, 8200, 5400, 9100, 7300, 10400, 8600, 7800, 11200, 9400, 8900, 12100, 10200, 11500];
const SPARK_IN = [0, 0, 25000, 0, 0, 0, 0, 0, 0, 0, 0, 25000, 0, 0];
FIX.dashboard = {
  as_of: '2026-10-29T13:20:00+08:00',
  window: { kind: '30d', days: 30, from: '2026-09-30', to: '2026-10-29' },
  kpis: {
    net_worth_cents: 7942850, net_worth_display: '¥79,428.50',
    available_cents: 5786050, available_display: '¥57,860.50',
    holdings_cents: 3000000, holdings_display: '¥30,000.00',
    credit_due_cents: 843200, credit_due_display: '¥8,432.00',
    month_out_cents: 2443271, month_out_display: '¥24,432.71',
    month_in_cents: 2500001, month_in_display: '¥25,000.01',
    net_flow_cents: 56730, net_flow_display: '¥567.30',
    month: '2026-10',
    deltas: { month_out_pct: 47, month_in_pct: 0, net_flow_pct: -93 },
    prev: { month: '2026-09', out_cents: 1663000, in_cents: 2500001, net_cents: 837001 },
    sparklines: { out_cents: SPARK_OUT, in_cents: SPARK_IN }
  },
  trend: {
    unit: 'cents',
    months: [
      { month: '2026-05', in_cents: 25000000, out_cents: 17300000, net_cents: 7700000 },
      { month: '2026-06', in_cents: 25000000, out_cents: 17200000, net_cents: 7800000 },
      { month: '2026-07', in_cents: 25000000, out_cents: 17900000, net_cents: 7100000 },
      { month: '2026-08', in_cents: 25000000, out_cents: 17900000, net_cents: 7100000 },
      { month: '2026-09', in_cents: 25000000, out_cents: 16600000, net_cents: 8400000 },
      { month: '2026-10', in_cents: 25000001, out_cents: 2443271, net_cents: 56730 }
    ]
  },
  budget: [
    { category: '购物', limit_cents: 200000, used_cents: 1150000, remaining_cents: -950000, pct: 575, state: 'over' },
    { category: '居住', limit_cents: 500000, used_cents: 480000, remaining_cents: 20000, pct: 96, state: 'warn' },
    { category: '订阅', limit_cents: 30000, used_cents: 25500, remaining_cents: 4500, pct: 85, state: 'warn' },
    { category: '餐饮', limit_cents: 300000, used_cents: 213000, remaining_cents: 87000, pct: 71, state: 'ok' },
    { category: '娱乐', limit_cents: 100000, used_cents: 58000, remaining_cents: 42000, pct: 58, state: 'ok' },
    { category: '交通', limit_cents: 80000, used_cents: 8000, remaining_cents: 72000, pct: 10, state: 'ok' }
  ],
  composition: {
    total_out_cents: 2443271,
    by_category: [
      { category: '购物', cents: 1150000, display: '¥11,500.00', pct: 47.1 },
      { category: '居住', cents: 478600, display: '¥4,786.00', pct: 19.6 },
      { category: '还款', cents: 300000, display: '¥3,000.00', pct: 12.3 },
      { category: '餐饮', cents: 212300, display: '¥2,123.00', pct: 8.7 },
      { category: '转账', cents: 200000, display: '¥2,000.00', pct: 8.2 }
    ],
    by_account: [
      { account_id: 'a1', name: '工资卡', kind: 'checking', number_masked: '**** 8821', balance_cents: 1284450, balance_display: '¥12,844.50' },
      { account_id: 'a2', name: '信用卡', kind: 'credit', number_masked: '**** 6712', balance_cents: -843200, balance_display: '-¥8,432.00' },
      { account_id: 'a3', name: '储蓄', kind: 'savings', number_masked: '**** 3390', balance_cents: 4500000, balance_display: '¥45,000.00' }
    ],
    holdings_cents: 3000000, holdings_display: '¥30,000.00'
  },
  todo: {
    rows: [
      { kind: 'subscription', label: '未来 3 天有 2 笔自动扣费', count: 2, detail: '爱奇艺 ¥25.00、网易云 ¥15.00', target: 'analytics' },
      { kind: 'anomaly', label: '发现 3 笔异常消费', count: 3, detail: '单笔金额超过近 90 天分位', target: 'analytics' }
    ],
    pending_approvals: 0, anomaly_count: 3, subscriptions_due: 2, scheduled_pending: 1,
    chain: { valid: true, checked: 12 }
  },
  recent: [
    { txn_id: 'x1', ts: '2026-10-29T12:40:00+08:00', direction: 'out', amount_cents: 3800, amount_display: '¥38.00', category: '餐饮', counterparty: '麦当劳', account_last4: '**** 8821', account_name: '工资卡' },
    { txn_id: 'x2', ts: '2026-10-28T19:02:00+08:00', direction: 'out', amount_cents: 25900, amount_display: '¥259.00', category: '购物', counterparty: '京东', account_last4: '**** 8821', account_name: '工资卡' },
    { txn_id: 'x3', ts: '2026-10-28T08:12:00+08:00', direction: 'in', amount_cents: 2500000, amount_display: '¥25,000.00', category: '工资', counterparty: '公司代发', account_last4: '**** 8821', account_name: '工资卡' },
    { txn_id: 'x4', ts: '2026-10-27T21:30:00+08:00', direction: 'out', amount_cents: 450000, amount_display: '¥4,500.00', category: '居住', counterparty: '房东', account_last4: '**** 8821', account_name: '工资卡' },
    { txn_id: 'x5', ts: '2026-10-26T10:00:00+08:00', direction: 'out', amount_cents: 843200, amount_display: '¥8,432.00', category: '还款', counterparty: '信用卡还款', account_last4: '**** 8821', account_name: '工资卡' },
    { txn_id: 'x6', ts: '2026-10-25T14:00:00+08:00', direction: 'out', amount_cents: 6600, amount_display: '¥66.00', category: '交通', counterparty: '地铁', account_last4: '**** 8821', account_name: '工资卡' },
    { txn_id: 'x7', ts: '2026-10-24T20:00:00+08:00', direction: 'out', amount_cents: 12800, amount_display: '¥128.00', category: '娱乐', counterparty: '影院', account_last4: '**** 8821', account_name: '工资卡' },
    { txn_id: 'x8', ts: '2026-10-23T11:00:00+08:00', direction: 'out', amount_cents: 19900, amount_display: '¥199.00', category: '订阅', counterparty: '健身私教', account_last4: '**** 8821', account_name: '工资卡' }
  ]
};

const ANA_TX = [];
for (let i = 0; i < 25; i++) {
  ANA_TX.push({
    txn_id: 'tx' + i, ts: '2026-10-' + String(28 - i).padStart(2, '0') + 'T12:00:00+08:00',
    direction: i % 3 === 0 ? 'in' : 'out', amount_cents: 1000 + i * 500,
    amount_display: '¥' + ((1000 + i * 500) / 100).toFixed(2),
    category: i % 3 === 0 ? '工资' : '餐饮', counterparty: '商户' + i,
    account_last4: '**** 8821'
  });
}
FIX.analytics = {
  as_of: '2026-10-29T13:20:00+08:00',
  range: { start: '2026-05-01', end: '2026-10-29', span_days: 182, granularity: 'month', buckets: 6, prev: { start: '2025-11-01', end: '2026-04-30' } },
  totals: {
    in_cents: 15000001, in_display: '¥150,000.01',
    out_cents: 11125135, out_display: '¥111,251.35',
    net_cents: 3874866, net_display: '¥38,748.66',
    txn_count: 353, avg_day_out_cents: 61127, avg_day_out_display: '¥611.27',
    prev_in_cents: 14900000, prev_out_cents: 11000000, out_delta_pct: 1.1, in_delta_pct: 0.7
  },
  series: [
    { bucket: '2026-05', in_cents: 25000000, out_cents: 17300000, net_cents: 7700000, count: 61 },
    { bucket: '2026-06', in_cents: 25000000, out_cents: 17200000, net_cents: 7800000, count: 58 },
    { bucket: '2026-07', in_cents: 25000000, out_cents: 17900000, net_cents: 7100000, count: 60 },
    { bucket: '2026-08', in_cents: 25000000, out_cents: 17900000, net_cents: 7100000, count: 59 },
    { bucket: '2026-09', in_cents: 25000000, out_cents: 16600000, net_cents: 8400000, count: 62 },
    { bucket: '2026-10', in_cents: 25000001, out_cents: 2443271, net_cents: 56730, count: 53 }
  ],
  categories: [
    { category: '购物', cents: 5200000, display: '¥52,000.00', pct: 40.0, prev_cents: 4000000, delta_pct: 30, budget_cents: 200000, budget_pct: 2600, over_budget: true },
    { category: '居住', cents: 2700000, display: '¥27,000.00', pct: 46.7, prev_cents: 2900000, delta_pct: -6.9, budget_cents: 500000, budget_pct: 540, over_budget: true },
    { category: '餐饮', cents: 1200000, display: '¥12,000.00', pct: 13.3, prev_cents: 1400000, delta_pct: -14.3, budget_cents: 300000, budget_pct: 400, over_budget: true }
  ],
  merchants: [
    { merchant: '京东', cents: 1800000, display: '¥18,000.00', pct: 16.2, count: 12 },
    { merchant: '淘宝', cents: 1400000, display: '¥14,000.00', pct: 12.6, count: 18 },
    { merchant: '房东', cents: 1300000, display: '¥13,000.00', pct: 11.7, count: 3 },
    { merchant: '美团', cents: 900000, display: '¥9,000.00', pct: 8.1, count: 25 },
    { merchant: '山姆会员店', cents: 800000, display: '¥8,000.00', pct: 7.2, count: 4 },
    { merchant: '滴滴出行', cents: 600000, display: '¥6,000.00', pct: 5.4, count: 30 },
    { merchant: '星巴克', cents: 400000, display: '¥4,000.00', pct: 3.6, count: 22 },
    { merchant: '国家电网', cents: 300000, display: '¥3,000.00', pct: 2.7, count: 3 },
    { merchant: '中国移动', cents: 200000, display: '¥2,000.00', pct: 1.8, count: 3 },
    { merchant: '健身房', cents: 100000, display: '¥1,000.00', pct: 0.9, count: 2 }
  ],
  anomalies: [
    { txn_id: 'an1', ts: '2026-10-18T21:00:00+08:00', amount_cents: 388000, amount_display: '¥3,880.00', counterparty: '某某珠宝', category: '购物', rule: 'amount_p99', level: 'high', detail: '单笔金额超过近 90 天 P99（¥3,120.00）' },
    { txn_id: 'an2', ts: '2026-10-11T03:00:00+08:00', amount_cents: 199900, amount_display: '¥1,999.00', counterparty: '海外商户', category: '购物', rule: 'odd_hour', level: 'warn', detail: '凌晨 3 点发生大额支出' }
  ],
  subscriptions: [
    { sub_id: 's1', name: '爱奇艺', cycle: '每月', amount_cents: 2500, amount_display: '¥25.00', next_charge: '2026-10-31', days_left: 2 },
    { sub_id: 's2', name: '健身私教', cycle: '每月', amount_cents: 19900, amount_display: '¥199.00', next_charge: '2026-11-08', days_left: 10 }
  ],
  savings: { potential_cents: 58000, potential_display: '¥580.00', savings_rate: 0.23, suggestions: ['把 2 个低频订阅降级为季付，预计每月省 ¥58.00', '本月外卖支出比上月高 32%，可设为每周两次'] },
  transactions: ANA_TX
};

FIX.plugins = [
  { id: 'policy', name: '权限分级与审批', kind: 'core', version: '0.2.0', enabled: true, status: 'enabled', toggleable: false,
    locked_reason: '核心能力不可停用',
    description: '对转账、还款等敏感动作做风险分级、二次确认与 TOTP 强验证，是资金安全的第一道闸门。',
    provides: { tools: [], skills: [], routes: ['/api/approval', '/api/demo/totp'] },
    requires: [], detail: { how: '按 policy 规则给出风险等级与审批单，敏感动作必须二次确认；不允许停用。', env: [], endpoints: ['/api/approval', '/api/demo/totp'] } },
  { id: 'audit', name: '哈希链审计', kind: 'core', version: '0.2.0', enabled: true, status: 'enabled', toggleable: false,
    locked_reason: '核心能力不可停用',
    description: '每次工具调用、审批与回复都写入 SHA-256 哈希链，可校验、可回放、可导出。',
    provides: { tools: [], skills: [], routes: ['/api/audit', '/api/events'] },
    requires: [], detail: { how: '所有决策与工具调用写审计链，verify 可发现任何篡改；不允许停用。', env: [], endpoints: ['/api/audit', '/api/events'] } },
  { id: 'mcp-fx', name: '汇率服务', kind: 'mcp', version: '0.2.0', enabled: true, status: 'enabled', toggleable: true,
    locked_reason: null,
    description: '通过 MCP 调用外部汇率服务，只用于查询与展示；不可用时回退内置参考价，绝不影响资金动作。',
    provides: { tools: ['fx_convert'], skills: [], routes: ['/api/mcp/status'] },
    requires: [], detail: { how: '启用时优先调用外部 fx_convert 拿实时参考汇率；停用后按内置参考价估算。', env: ['MCP_ENABLED', 'MCP_FX_URL', 'MCP_TIMEOUT_MS', 'MCP_FX_FALLBACK_RATE'], endpoints: ['/api/mcp/status'] } },
  { id: 'mcp-risk', name: '反欺诈风控', kind: 'mcp', version: '0.2.0', enabled: true, status: 'degraded', toggleable: true,
    locked_reason: null,
    description: '转账前调用外部风险名单与评分，只加严不放松；探测失败自动降级。',
    provides: { tools: ['risk_screen'], skills: [], routes: [] },
    requires: [], detail: { how: '启用时对 create_transfer 做外部风险筛查；探测失败降级为本地规则。', env: ['MCP_ENABLED', 'MCP_RISK_URL', 'MCP_RISK_BLOCK_SCORE'], endpoints: [] } },
  { id: 'intent-llm', name: '模型意图通道', kind: 'model', version: '0.2.0', enabled: true, status: 'enabled', toggleable: true,
    locked_reason: null,
    description: '用大模型解析自然语言意图；未配置 API Key 或停用时自动回退到规则解析。',
    provides: { tools: ['llm.parse'], skills: [], routes: ['/api/llm/mode', '/api/llm/status'] },
    requires: [], detail: { how: '启用且配置 Key 时由模型解析意图；停用后强制走规则解析。', env: ['DEEPSEEK_API_KEY', 'DEEPSEEK_MODEL', 'DEEPSEEK_BASE_URL'], endpoints: ['/api/llm/mode', '/api/llm/status'] } },
  { id: 'skill-pack', name: '技能卡包', kind: 'skill', version: '0.2.0', enabled: true, status: 'enabled', toggleable: true,
    locked_reason: null,
    description: '六张领域技能卡，作为模型提示与前端能力目录。',
    provides: { tools: [], skills: ['bills', 'cards', 'cross', 'subscriptions', 'transfer', 'wealth'], routes: ['/api/skills'] },
    requires: [], detail: { how: '启用时在 /api/skills 展示六张技能卡；停用后技能列表返回空。', env: [], endpoints: ['/api/skills'] } },
  { id: 'channel-im', name: 'IM 渠道', kind: 'channel', version: '0.2.0', enabled: false, status: 'disabled', toggleable: true,
    locked_reason: null,
    description: '把 IM / 短信网关 webhook 适配成 /api/chat 调用，两步确认，且绝不向渠道回传内部凭据。',
    provides: { tools: [], skills: [], routes: ['/api/channel/status'] },
    requires: [], detail: { how: '启用时渠道服务可转发消息并做幂等；停用后 webhook 返回 503。', env: ['CHANNEL_TOKEN', 'CHANNEL_PORT', 'CHANNEL_ENABLED'], endpoints: ['/api/channel/status'] } }
];
FIX.pluginsResponse = function () {
  return {
    ok: true, count: 7, enabled_count: 6, version: '0.2.0',
    kinds: [
      { id: 'core', name: '核心能力' }, { id: 'mcp', name: '外部服务' },
      { id: 'model', name: '模型通道' }, { id: 'skill', name: '技能包' },
      { id: 'channel', name: '渠道' }
    ],
    plugins: FIX.plugins
  };
};

const CALLS = [];
const PUSH_CALLS = [];
let pushMode = 'delivered';
let tasksMode = 'paged';
let zoneFail = false;
let chatScript = null;
let chatFailAfter = null;
function sseChunks(events) {
  const parts = events.map(function (e) { return 'event: ' + e.event + '\ndata: ' + JSON.stringify(e.data) + '\n\n'; });
  return parts;
}

// ---- v0.7.1 账户页 fixtures ----
const ACC_MONTH = (function () {
  const d = new Date();
  const m = String(d.getMonth() + 1);
  return d.getFullYear() + '-' + (m.length < 2 ? '0' + m : m);
})();
const ACC_FIX = {
  accounts: {
    accounts: [
      { account_id: 'a1', kind: 'checking', name: '工资卡', number_masked: '**** 8821', balance_cents: 1284450,
        credit_limit_cents: 0, currency: 'CNY', balance_display: '¥12,844.50',
        card_id: 'c1', card_kind: 'debit', card_status: 'active', single_limit_cents: 2000000, daily_limit_cents: 2000000 },
      { account_id: 'a2', kind: 'credit', name: '信用卡', number_masked: '**** 6712', balance_cents: -843200,
        credit_limit_cents: 3000000, currency: 'CNY', balance_display: '¥-8,432.00',
        card_id: 'c2', card_kind: 'credit', card_status: 'active', single_limit_cents: 3000000, daily_limit_cents: 3000000 },
      { account_id: 'a3', kind: 'savings', name: '储蓄', number_masked: '**** 3390', balance_cents: 4500000,
        credit_limit_cents: 0, currency: 'CNY', balance_display: '¥45,000.00',
        card_id: '', card_kind: '', card_status: '', single_limit_cents: 0, daily_limit_cents: 0 }
    ],
    cards: [
      { card_id: 'c1', kind: 'debit', number_masked: '**** 8821', status: 'active', single_limit_cents: 2000000, daily_limit_cents: 2000000 },
      { card_id: 'c2', kind: 'credit', number_masked: '**** 6712', status: 'active', single_limit_cents: 3000000, daily_limit_cents: 3000000 }
    ],
    total_cents: 4941250
  },
  txns: (function () {
    const rows = [];
    const cats = ['餐饮', '购物', '交通', '娱乐'];
    for (let i = 0; i < 26; i++) {
      const credit = i % 3 === 0;
      const day = String(28 - i).length < 2 ? '0' + (28 - i) : String(28 - i);
      rows.push({
        txn_id: 'tx_acc_' + i,
        ts: ACC_MONTH + '-' + day + 'T10:' + (String(i).length < 2 ? '0' + i : i) + ':00',
        direction: i % 7 === 0 ? 'in' : 'out',
        amount_cents: (i % 7 === 0 ? 250000 : 10000 + i * 137),
        amount_display: '¥' + ((i % 7 === 0 ? 250000 : 10000 + i * 137) / 100).toFixed(2),
        category: cats[i % cats.length],
        counterparty: (credit ? '优衣库' : '星巴克') + ' #' + i,
        account_id: credit ? 'a2' : 'a1'
      });
    }
    return rows;
  })()
};

function makeFetch() {
  return function (url, init) {
    CALLS.push({ url: String(url), method: (init && init.method) || 'GET', body: init && init.body });
    const u = String(url);
    const json = function (data, status) {
      return Promise.resolve({
        ok: (status || 200) < 400, status: status || 200,
        headers: { get: function () { return 'application/json'; } },
        json: function () { return Promise.resolve(data); },
        text: function () { return Promise.resolve(JSON.stringify(data)); }
      });
    };
    const sse = function (chunks) {
      let idx = 0;
      return Promise.resolve({
        ok: true, status: 200,
        headers: { get: function () { return 'text/event-stream'; } },
        body: {
          getReader: function () {
            return {
              read: function () {
                if (chatFailAfter != null && idx >= chatFailAfter) return Promise.reject(new Error('network down'));
                if (idx >= chunks.length) return Promise.resolve({ done: true });
                return Promise.resolve({ done: false, value: enc(chunks[idx++]) });
              }
            };
          }
        },
        json: function () { return Promise.resolve({}); }
      });
    };
    if (u.indexOf('/api/session') === 0 && u === '/api/session') return json(FIX.session);
    if (u.indexOf('/api/session/') === 0) return json(Object.assign({ pending_approval: null }, FIX.session));
    if (u.indexOf('/api/tasks') === 0) {
      const qi = u.indexOf('?');
      const q = {};
      if (qi >= 0) {
        u.slice(qi + 1).split('&').forEach(function (pair) {
          const kv = pair.split('=');
          if (kv[0]) q[kv[0]] = kv[1];
        });
      }
      const all = tasksMode === 'single' ? FIX.tasksAll.slice(0, 3) : FIX.tasksAll;
      const lim = Number(q.limit || 6) || 6;
      const off = Number(q.offset || 0) || 0;
      const page = all.slice(off, off + lim);
      return json({ tasks: page, total: all.length, offset: off, limit: lim, has_more: off + lim < all.length });
    }
    if (u.indexOf('/api/llm/test') === 0) {
      if (FIX.llmTestReject) return Promise.reject(new Error('network down'));
      if (FIX.llmTestManual) return new Promise(function (resolve) { FIX.llmTestResolve = function () { resolve(json(FIX.llmTest)); }; });
      return json(FIX.llmTest);
    }
    if (u.indexOf('/api/llm/status') === 0) return json(FIX.llm);
    if (u.indexOf('/api/llm/mode') === 0) {
      let m = 'auto';
      try { m = JSON.parse(init.body).mode; } catch (e) {}
      return json(Object.assign({}, FIX.llm, { mode: m, effective: m === 'rule' ? 'rule' : 'rule', switched: true }));
    }
    if (u.indexOf('/api/skills') === 0) return json(FIX.skills);
    if (u === '/api/plugins') return json(FIX.pluginsResponse());
    if (u.indexOf('/api/plugins/') === 0 && u.indexOf('/toggle') > 0) {
      let body = {};
      try { body = JSON.parse(init && init.body); } catch (e) {}
      const pid = decodeURIComponent(u.replace('/api/plugins/', '').replace(/\/toggle$/, ''));
      const plug = FIX.plugins.filter(function (x) { return x.id === pid; })[0];
      if (!plug) return json({ error: { code: 'PLUGIN_NOT_FOUND', message: '没有这个插件' } }, 404);
      plug.enabled = !!body.enabled;
      plug.status = plug.enabled ? (plug.id === 'mcp-risk' ? 'degraded' : 'enabled') : 'disabled';
      return json({ ok: true, plugin: plug, counts: { total: FIX.plugins.length, enabled: FIX.plugins.filter(function (x) { return x.enabled; }).length, disabled: FIX.plugins.filter(function (x) { return !x.enabled; }).length } });
    }
    if (u.indexOf('/api/mcp/status') === 0) return json(FIX.services);
    if (u.indexOf('/api/channel/status') === 0) return json(FIX.channel);
    if (u.indexOf('/api/health') === 0) return json(FIX.health);
    if (u.indexOf('/api/audit') === 0) return json(FIX.audit);
    if (u.indexOf('/api/turn/cancel') === 0) return json({ cancelled: true, emitted_chunks: 1 });
    if (u.indexOf('/api/chat') === 0) return sse(sseChunks(chatScript || []));
    if (u.indexOf('/api/approval') === 0) return json(FIX.approvalResult || { status: 'ok', reply: '已完成。' });
    if (u.indexOf('/api/demo/totp') === 0) return json({ code: '123456', expires_in: 30 });
    if (u.indexOf('/api/demo/sms') === 0) return json(FIX.demoSms);
    if (u.indexOf('/api/demo/face') === 0) return json(FIX.demoFace);
    if (u.indexOf('/api/undo') === 0) return json({ undone: true, status: 'ok', action_id: 'act_1', message: '已撤销' });
    if (u.indexOf('/api/admin/reset') === 0) return json({ reset: true, seed_version: 4 });
    if (u.indexOf('/api/push') === 0) {
      let body = {};
      try { body = JSON.parse(init && init.body); } catch (e) {}
      PUSH_CALLS.push(body);
      if (pushMode === 'reject') return Promise.reject(new Error('network down'));
      if (pushMode === 'fail') return json({ error: { code: 'PUSH_FAILED', message: '渠道返回 502' } }, 502);
      const delivered = pushMode === 'delivered';
      const pushed = pushMode !== 'nopush';
      return json({
        ok: true, kind: body.kind,
        title: body.kind === 'large_txn' ? '大额支出提醒' : body.kind === 'repayment' ? '还款到期提醒' : '订阅扣费提醒',
        text: '演示推送正文：' + (body.kind || 'subscription'),
        pushed: pushed, delivered: delivered,
        channel: {
          configured: delivered,
          url_masked: 'https://hooks.example.com/…/abc123',
          status: delivered ? 'ok' : 'unconfigured',
          latency_ms: 42,
          error: null
        },
        ts: '2026-10-27T22:40:00+08:00'
      });
    }
    if (u.indexOf('/api/dashboard') === 0) {
      if (zoneFail) return Promise.reject(new Error('dashboard 502'));
      return json(FIX.dashboard);
    }
    if (u.indexOf('/api/analytics') === 0) {
      if (zoneFail) return Promise.reject(new Error('analytics 502'));
      return json(FIX.analytics);
    }

    if (u.indexOf('/api/accounts') === 0) {
      if (zoneFail) return Promise.reject(new Error('accounts 502'));
      return json(ACC_FIX.accounts);
    }
    if (u.indexOf('/api/transactions') === 0) {
      if (zoneFail) return Promise.reject(new Error('transactions 502'));
      const qi = u.indexOf('?');
      const q = {};
      if (qi >= 0) {
        u.slice(qi + 1).split('&').forEach(function (pair) {
          const kv = pair.split('=');
          if (kv[0]) q[decodeURIComponent(kv[0])] = decodeURIComponent(kv[1] || '');
        });
      }
      const aid = q.account_id || '';
      const rows = aid ? ACC_FIX.txns.filter(function (t) { return t.account_id === aid; }) : ACC_FIX.txns;
      if (q.limit) return json({ transactions: rows.slice(0, Number(q.limit) || 20), account_id: aid || null });
      return json({ transactions: rows, account_id: aid || null });
    }
    return json({ error: 'NOT_FOUND' }, 404);
  };
}

const LS_STORE = {};
const sandbox = {
  console: console, document: doc, navigator: { clipboard: undefined },
  localStorage: {
    getItem: function (k) { return Object.prototype.hasOwnProperty.call(LS_STORE, k) ? LS_STORE[k] : null; },
    setItem: function (k, v) { LS_STORE[k] = String(v); },
    removeItem: function (k) { delete LS_STORE[k]; }
  },
  fetch: makeFetch(),
  setTimeout: setTimeout, clearTimeout: clearTimeout, setInterval: setInterval, clearInterval: clearInterval,
  TextDecoder: TextDecoder, TextEncoder: TextEncoder,
  Blob: function () {}, URL: { createObjectURL: function () { return 'blob:x'; }, revokeObjectURL: function () {} },
  matchMedia: function () { return { matches: false, addEventListener: function () {} }; },
  SpeechRecognition: undefined, webkitSpeechRecognition: undefined,
  requestAnimationFrame: function (fn) { return setTimeout(fn, 16); },
  location: { href: 'http://127.0.0.1:8011/' },
  innerWidth: 1600, innerHeight: 1000,
  scrollTo: function (x, y) { SCROLLS.push([x, y]); },
  addEventListener: function () {}
};
sandbox.window = sandbox;
sandbox.self = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

function getEls(sel) { return doc.querySelectorAll(sel); }
function one(sel) { return doc.querySelector(sel); }

(async function main() {
  console.log('== 加载 app.js ==');
  try { vm.runInContext(appCode, sandbox, { filename: 'app.js' }); }
  catch (e) { console.log('  FAIL  app.js 执行异常: ' + e.message); FAIL++; }
  await sleep(80);
  const ZS = sandbox.window.__ZS;
  ok('app.js 暴露测试钩子 __ZS', !!ZS);
  if (!ZS) { report(); return; }

  console.log('== 启动 / 会话 ==');
  ok('会话已建立', ZS.state.sessionId === 'sess_test_001', String(ZS.state.sessionId));
  ok('余额拆成 9 个数字位', getEls('.digits .dg').length >= 8, String(getEls('.digits .dg').length));
  ok('账户行 = 3', getEls('#acctList .acct__row').length === 3, String(getEls('#acctList .acct__row').length));
  ok('资产构成三段', getEls('#acctMix i').length === 3, String(getEls('#acctMix i').length));
  ok('快捷指令首批 6 个', getEls('#chips .chip').length === 6, String(getEls('#chips .chip').length));
  ok('换一批按钮文案 1/3', /换一批 · 1\/3/.test(one('#btnTasksRefresh').textContent), one('#btnTasksRefresh').textContent);
  ok('首页为空态视图', ZS.state.view === 'home' && one('#view-home').classList.contains('is-on'));
  ok('模式徽章写实际模式', /实际：/.test(one('#modeBadge .effective__text').textContent), one('#modeBadge .effective__text').textContent);
  ok('模式按钮 title 含 provider', /deepseek/.test(one('#modeBadge').title), one('#modeBadge').title);
  console.log('== 资金流向与余额刷新（kind 字段 / 理财持仓）==');
  var mixBoot = getEls('#acctMix i').map(function (el) { return parseFloat(String(el.getAttribute('style') || '').replace(/[^0-9.]/g, '')) || 0; });
  ok('启动时理财段仅占最小宽度', mixBoot.length === 3 && mixBoot[1] <= 2.1, JSON.stringify(mixBoot));
  ok('启动时无理财持仓行', getEls('#acctList .acct__row--wealth').length === 0);
  // 模拟一次理财申购后服务端快照：账户用 kind 字段（真实接口返回的是 kind），并带 wealth.holding_total_cents
  FIX.session.accounts = [
    { account_id: 'a1', kind: 'checking', name: '工资卡', number_masked: '**** 8821', balance_cents: 284450, currency: 'CNY' },
    { account_id: 'a2', kind: 'credit', name: '信用卡', number_masked: '**** 6712', balance_cents: -843200, currency: 'CNY' },
    { account_id: 'a3', kind: 'savings', name: '储蓄', number_masked: '**** 3390', balance_cents: 4500000, currency: 'CNY' }
  ];
  FIX.session.wealth = { holding_total_cents: 1000000, holding_total_display: '¥10,000.00', holdings: [], products: [] };
  await ZS.refreshAccountSnapshot();
  await sleep(30);
  ok('刷新后工资卡余额跟随服务端', ZS.state.accounts[0].balance_cents === 284450, String(ZS.state.accounts[0].balance_cents));
  ok('信用卡待还写进账户脚注', /信用卡待还 ¥8,432.00/.test(one('#acctDelta').textContent || one('#acctDelta').innerHTML), one('#acctDelta').textContent);
  ok('多出理财持仓行', getEls('#acctList .acct__row').length === 4, String(getEls('#acctList .acct__row').length));
  ok('理财持仓行写明金额', /理财 · 持仓/.test(getEls('#acctList .acct__row--wealth')[0].textContent) && /¥10,000.00/.test(getEls('#acctList .acct__row--wealth')[0].textContent), getEls('#acctList .acct__row--wealth')[0].textContent);
  var mixAfter = getEls('#acctMix i').map(function (el) { return parseFloat(String(el.getAttribute('style') || '').replace(/[^0-9.]/g, '')) || 0; });
  ok('理财段按持仓放宽', mixAfter.length === 3 && mixAfter[1] > 5, JSON.stringify(mixAfter));
  // 回合结束时应自动再拉一次快照：模拟第二次申购后的服务端状态
  FIX.session.accounts[0].balance_cents = 184450;
  FIX.session.wealth.holding_total_cents = 2000000;
  ZS.finishTurn('ok', {});
  await sleep(60);
  ok('回合结束自动刷新余额', ZS.state.accounts[0].balance_cents === 184450, String(ZS.state.accounts[0].balance_cents));
  ok('回合结束自动刷新持仓', /¥20,000.00/.test(getEls('#acctList .acct__row--wealth')[0].textContent), getEls('#acctList .acct__row--wealth')[0].textContent);

  console.log('== 视图切换 ==');
  ZS.showView('skills'); await sleep(30);
  ok('技能页 6 张卡', getEls('#skillsGrid .scard').length === 6, String(getEls('#skillsGrid .scard').length));
  ZS.showView('services'); await sleep(30);
  ok('外部服务 2 张卡', getEls('#servicesGrid .scard').length === 2, String(getEls('#servicesGrid .scard').length));
  ok('外部服务降级条出现', one('#servicesInfoBar').hidden === false);
  ZS.showView('settings'); await sleep(40);
  ok('渠道端口写入', one('#channelPort').textContent === '9100', one('#channelPort').textContent);
  ok('令牌打码', /已配置/.test(one('#channelToken').textContent), one('#channelToken').textContent);
  ok('健康版本写入', /0\.2\.0/.test(one('#healthVersion').textContent), one('#healthVersion').textContent);
  ZS.showView('home');
  ok('顶栏导航高亮回到工作台', one('[data-nav="home"]').classList.contains('is-on'));

  console.log('== 快捷指令：换一批 ==');
  const chipsBefore = getEls('#chips .chip').map(function (c) { return c.textContent; }).join('|');
  const callsBeforeSwap = CALLS.length;
  const swapP = ZS.nextTaskPage();
  await sleep(30);
  ok('换一批时 chips 淡出', one('#chips').classList.contains('chips--swap'));
  ok('换一批时按钮禁用', one('#btnTasksRefresh').disabled === true);
  await swapP;
  await sleep(40);
  const chipsAfter = getEls('#chips .chip').map(function (c) { return c.textContent; }).join('|');
  ok('换一批后 chips 内容变化', chipsBefore !== chipsAfter, chipsAfter.slice(0, 40));
  ok('换一批后按钮文案 2/3', /换一批 · 2\/3/.test(one('#btnTasksRefresh').textContent), one('#btnTasksRefresh').textContent);
  ok('换一批后淡出类移除', one('#chips').classList.contains('chips--swap') === false);
  ok('换一批请求带分页参数', CALLS.slice(callsBeforeSwap).some(function (c) { return c.url.indexOf('/api/tasks?limit=6&offset=6') === 0; }), JSON.stringify(CALLS.slice(callsBeforeSwap).map(function (c) { return c.url; })));
  await ZS.loadTasksPage(2);
  ok('第三批 6 个且文案 3/3', getEls('#chips .chip').length === 6 && /换一批 · 3\/3/.test(one('#btnTasksRefresh').textContent), one('#btnTasksRefresh').textContent);
  await ZS.nextTaskPage();
  await sleep(40);
  ok('末批再换一批回绕到首批', /换一批 · 1\/3/.test(one('#btnTasksRefresh').textContent), one('#btnTasksRefresh').textContent);

  tasksMode = 'single';
  await ZS.loadTasks();
  const singleChipCount = getEls('#chips .chip').length;
  ok('只有一批时文案 1/1', /换一批 · 1\/1/.test(one('#btnTasksRefresh').textContent), one('#btnTasksRefresh').textContent);
  await ZS.nextTaskPage();
  const singleToast = getEls('.toast .toast__text').map(function (e) { return e.textContent; }).join('|');
  ok('只有一批时提示已是最新一批', /已经是这一批了/.test(singleToast), singleToast);
  ok('只有一批时 chips 不变', getEls('#chips .chip').length === singleChipCount);
  tasksMode = 'paged';
  await ZS.loadTasks();

  console.log('== 插件页 ==');
  ZS.showView('plugins');
  await sleep(90);
  ok('插件页 7 张卡', getEls('#pluginsGrid .plug').length === 7, String(getEls('#pluginsGrid .plug').length));
  ok('插件汇总计数', /共 7 个插件/.test(one('#pluginsSummary').textContent) && /已启用 6/.test(one('#pluginsSummary').textContent) && /已停用 1/.test(one('#pluginsSummary').textContent) && /降级 1/.test(one('#pluginsSummary').textContent), one('#pluginsSummary').textContent);
  ok('三种状态徽章齐全', getEls('#pluginsGrid .plug__status--on').length === 5 && getEls('#pluginsGrid .plug__status--off').length === 1 && getEls('#pluginsGrid .plug__status--degraded').length === 1,
    'on=' + getEls('#pluginsGrid .plug__status--on').length + ' off=' + getEls('#pluginsGrid .plug__status--off').length + ' degraded=' + getEls('#pluginsGrid .plug__status--degraded').length);
  ok('核心插件（policy/audit）开关锁定', one('#pluginsGrid .plug[data-plugin="policy"] [data-toggle]').disabled === true && one('#pluginsGrid .plug[data-plugin="audit"] [data-toggle]').disabled === true);
  ok('锁定卡使用后端 locked_reason 文案', /核心能力不可停用/.test(one('#pluginsGrid .plug[data-plugin="policy"] .plug__note').textContent), one('#pluginsGrid .plug[data-plugin="policy"] .plug__note').textContent);
  ok('停用插件卡 is-off', one('#pluginsGrid .plug[data-plugin="channel-im"]').classList.contains('is-off'));
  ok('provides 对象渲染成能力 chip', /fx_convert/.test(one('#pluginsGrid .plug[data-plugin="mcp-fx"] .plug__provides').textContent), one('#pluginsGrid .plug[data-plugin="mcp-fx"] .plug__provides').textContent);
  ok('能力 chip 超过 4 个折叠计数', getEls('#pluginsGrid .plug[data-plugin="skill-pack"] .plug__provides .plug__chip').length === 4 && /\+3/.test(one('#pluginsGrid .plug[data-plugin="skill-pack"] .plug__more').textContent),
    one('#pluginsGrid .plug[data-plugin="skill-pack"] .plug__provides').textContent);
  one('#pluginsGrid .plug[data-plugin="intent-llm"] .plug__head').click();
  ok('点击卡头展开详情', one('#pluginsGrid .plug[data-plugin="intent-llm"]').classList.contains('is-open') && one('#pluginsGrid .plug[data-plugin="intent-llm"] .plug__detail').hidden === false);
  one('#pluginsGrid .plug[data-plugin="skill-pack"] .plug__head').click();
  ok('手风琴互斥（前一张收起）', one('#pluginsGrid .plug[data-plugin="intent-llm"]').classList.contains('is-open') === false && one('#pluginsGrid .plug[data-plugin="skill-pack"]').classList.contains('is-open'));
  one('#pluginsGrid .plug[data-plugin="skill-pack"] .plug__head').click();
  ok('再次点击收起详情', one('#pluginsGrid .plug[data-plugin="skill-pack"] .plug__detail').hidden === true);
  const callsBeforeCore = CALLS.length;
  await ZS.togglePlugin('policy');
  ok('锁定插件不发切换请求', CALLS.slice(callsBeforeCore).every(function (c) { return c.url.indexOf('/toggle') === -1; }));
  ok('锁定插件给出提示文案', /核心能力不可停用/.test(getEls('.toast .toast__text').map(function (e) { return e.textContent; }).join('|')));
  const callsBeforeToggle = CALLS.length;
  await ZS.togglePlugin('channel-im');
  await sleep(90);
  ok('切换成功写回卡片（渠道启用）', one('#pluginsGrid .plug[data-plugin="channel-im"]').classList.contains('is-off') === false && one('#pluginsGrid .plug[data-plugin="channel-im"]').getAttribute('data-status') === 'enabled',
    one('#pluginsGrid .plug[data-plugin="channel-im"]').getAttribute('data-status'));
  ok('切换成功更新计数', /已启用 7/.test(one('#pluginsSummary').textContent) && /已停用 0/.test(one('#pluginsSummary').textContent), one('#pluginsSummary').textContent);
  ok('切换走后端 toggle（POST）', CALLS.slice(callsBeforeToggle).some(function (c) { return c.url === '/api/plugins/channel-im/toggle' && c.method === 'POST'; }),
    JSON.stringify(CALLS.slice(callsBeforeToggle).map(function (c) { return c.method + ' ' + c.url; })));
  ok('切换后联动刷新渠道状态', CALLS.slice(callsBeforeToggle).some(function (c) { return c.url.indexOf('/api/channel/status') === 0; }));
  ok('切换后刷新健康检查', CALLS.slice(callsBeforeToggle).some(function (c) { return c.url.indexOf('/api/health') === 0; }));
  const callsBeforeMcp = CALLS.length;
  await ZS.togglePlugin('mcp-fx');
  await sleep(90);
  ok('停用外部服务插件后卡片 is-off', one('#pluginsGrid .plug[data-plugin="mcp-fx"]').classList.contains('is-off') && one('#pluginsGrid .plug[data-plugin="mcp-fx"]').getAttribute('data-status') === 'disabled',
    one('#pluginsGrid .plug[data-plugin="mcp-fx"]').getAttribute('data-status'));
  ok('停用 MCP 插件联动刷新外部服务', CALLS.slice(callsBeforeMcp).some(function (c) { return c.url.indexOf('/api/mcp/status') === 0; }));
  const realFetch2 = sandbox.fetch;
  sandbox.fetch = function (url, init) {
    if (String(url).indexOf('/api/plugins/') === 0) return Promise.reject(new Error('boom'));
    return realFetch2(url, init);
  };
  await ZS.togglePlugin('skill-pack');
  sandbox.fetch = realFetch2;
  ok('切换失败保持原状态', (function () { const pl = ZS.state.plugins.filter(function (x) { return x.id === 'skill-pack'; })[0]; return !!pl && pl.enabled === true; })());
  ok('切换失败显示错误条', one('#pluginsError').hidden === false && /切换失败/.test(one('#pluginsErrorText').textContent), one('#pluginsErrorText').textContent);
  ok('切换失败按钮恢复可用', one('#pluginsGrid .plug[data-plugin="skill-pack"] [data-toggle]').disabled === false);
  one('#pluginsErrorClose').click();
  ok('错误条可关闭', one('#pluginsError').hidden === true);
  ZS.showView('home');
  await sleep(30);

  console.log('== 视图切换回归：插件页不再叠在工作台下方 ==');
  const cssText = fs.readFileSync(path.join(__dirname, 'styles.css'), 'utf8');
  const hideRules = cssText.match(/[^{}]*\{[^}]*display:\s*none[^}]*\}/g) || [];
  ok('CSS 存在 body[data-view="plugins"] .shell 隐藏规则', hideRules.some(function (r) { return /body\[data-view="plugins"\]\s*\.shell/.test(r); }),
    JSON.stringify(hideRules.filter(function (r) { return r.indexOf('data-view') >= 0; })));
  const scrollRules = cssText.match(/[^{}]*\{[^}]*overflow-y:\s*auto[^}]*\}/g) || [];
  ok('CSS 让 plugins 视图可整页滚动', scrollRules.some(function (r) { return /body\[data-view="plugins"\]/.test(r); }));
  ok('切换 home 时 .shell 不被隐藏（无 home/chat 隐藏规则）', !/body\[data-view="(home|chat)"\]\s*\.shell/.test(cssText));
  const scrollBefore = SCROLLS.length;
  ZS.showView('plugins');
  await sleep(60);
  ok('showView("plugins") 后 body[data-view]=plugins', doc.body.getAttribute('data-view') === 'plugins', String(doc.body.getAttribute('data-view')));
  ok('#scr-plugins 处于 is-on', one('#scr-plugins').classList.contains('is-on'));
  ok('scr-skills/scr-services/scr-settings 均无 is-on', ['scr-skills', 'scr-services', 'scr-settings'].every(function (id) { return !one('#' + id).classList.contains('is-on'); }));
  ok('工作台视图已让位（#view-home 无 is-on）', !one('#view-home').classList.contains('is-on'));
  ok('切到整页视图会 window.scrollTo(0,0)', SCROLLS.length > scrollBefore && SCROLLS[SCROLLS.length - 1][0] === 0 && SCROLLS[SCROLLS.length - 1][1] === 0, JSON.stringify(SCROLLS.slice(-3)));
  ZS.showView('skills');
  await sleep(40);
  ok('切到技能页 data-view=skills', doc.body.getAttribute('data-view') === 'skills');
  const scrollHome = SCROLLS.length;
  ZS.showView('home');
  await sleep(40);
  ok('切回 home 后 data-view=home 且 #view-home 恢复', doc.body.getAttribute('data-view') === 'home' && one('#view-home').classList.contains('is-on'));
  ok('切回 home 不额外滚到顶部（工作台自身滚动容器负责）', SCROLLS.length === scrollHome, JSON.stringify(SCROLLS.slice(-3)));
  ok('切回 home 后技能页已关闭', !one('#scr-skills').classList.contains('is-on'));

  console.log('== 设置页：模型接入 ==');
  ZS.showView('settings');
  await sleep(90);
  ok('模型接入：厂商', one('#llmProvider').textContent === 'deepseek', one('#llmProvider').textContent);
  ok('模型接入：模型名', one('#llmModel').textContent === 'deepseek-chat', one('#llmModel').textContent);
  ok('模型接入：接口域名', one('#llmHost').textContent === 'api.deepseek.com', one('#llmHost').textContent);
  ok('模型接入：Key 已配置且打码', /已配置/.test(one('#llmKey').textContent) && /sk-\*\*\*\*/.test(one('#llmKey').textContent), one('#llmKey').textContent);
  ok('模型接入：实际解析方式', /规则解析/.test(one('#llmEffective').textContent), one('#llmEffective').textContent);
  ok('模型接入：熔断状态正常', one('#llmBreaker').textContent === '正常', one('#llmBreaker').textContent);
  ok('模型接入：卡片状态徽章', /已配置/.test(one('#llmTag').textContent) && one('#llmTag').className.indexOf('tag--ok') >= 0, one('#llmTag').className + '|' + one('#llmTag').textContent);
  ok('用量统计行显示且有 stats', one('#llmStats').hidden === false && /调用 4 次/.test(one('#llmStatsText').textContent), one('#llmStatsText').textContent);
  ok('用量统计含成功/失败/重试/最近耗时/累计成本',
    /成功 3/.test(one('#llmStatsText').textContent) && /失败 1/.test(one('#llmStatsText').textContent) && /重试 1/.test(one('#llmStatsText').textContent) && /最近 412 ms/.test(one('#llmStatsText').textContent) && /累计约 ¥0\.0031/.test(one('#llmStatsText').textContent),
    one('#llmStatsText').textContent);
  const LLM_STATS = JSON.parse(JSON.stringify(FIX.llm.stats));
  delete FIX.llm.stats;
  await ZS.loadLlmStatus();
  await sleep(40);
  ok('无 stats 时整行隐藏且不报错', one('#llmStats').hidden === true, String(one('#llmStats').hidden));
  FIX.llm.stats = LLM_STATS;
  FIX.llm.degraded = true;
  FIX.llm.breaker = { open: true, failures: 3, threshold: 3, cooldown_s: 60, retry_after_s: 23 };
  await ZS.loadLlmStatus();
  await sleep(40);
  ok('degraded：熔断状态展示剩余秒数', /已熔断/.test(one('#llmBreaker').textContent) && /剩余 23s/.test(one('#llmBreaker').textContent), one('#llmBreaker').textContent);
  ok('degraded：顶栏徽章追加熔断', /实际：规则 · 熔断/.test(one('#modeBadge .effective__text').textContent), one('#modeBadge .effective__text').textContent);
  ok('degraded：卡片徽章转熔断回退', /熔断回退/.test(one('#llmTag').textContent), one('#llmTag').textContent);
  FIX.llm.degraded = false;
  FIX.llm.breaker = { open: false, failures: 0, threshold: 3, cooldown_s: 60, retry_after_s: 0 };
  await ZS.loadLlmStatus();
  await sleep(40);
  ok('恢复正常后徽章去掉熔断', !/熔断/.test(one('#modeBadge .effective__text').textContent), one('#modeBadge .effective__text').textContent);
  FIX.llmTest = { ok: true, latency_ms: 412, model: 'deepseek-chat', provider: 'deepseek', usage: { prompt_tokens: 9, completion_tokens: 6, total_tokens: 15 }, cost_yuan: 0.0009 };
  one('#btnLlmTest').click();
  await sleep(120);
  ok('测试连接成功文案含耗时与 token 与费用',
    /连接正常/.test(one('#llmTestResult').textContent) && /耗时 412 ms/.test(one('#llmTestResult').textContent) && /输入 9/.test(one('#llmTestResult').textContent) && /输出 6/.test(one('#llmTestResult').textContent) && /¥0\.0009/.test(one('#llmTestResult').textContent),
    one('#llmTestResult').textContent);
  ok('测试成功后按钮复原', one('#btnLlmTest').disabled === false && one('#btnLlmTest').textContent === '测试连接', one('#btnLlmTest').textContent);
  FIX.llmTest = { ok: false, error: '鉴权失败（401）', hint: '请检查 DEEPSEEK_API_KEY 是否正确', code: 'LLM_AUTH' };
  one('#btnLlmTest').click();
  await sleep(120);
  ok('测试失败显示中文 error + hint',
    /鉴权失败/.test(one('#llmTestResult').textContent) && /DEEPSEEK_API_KEY/.test(one('#llmTestResult').textContent),
    one('#llmTestResult').textContent);
  ok('测试失败用 inline 错误样式（非红色大块）', one('#llmTestResult').className.indexOf('llmtest__result--err') >= 0, one('#llmTestResult').className);
  ok('测试失败后按钮仍可用', one('#btnLlmTest').disabled === false);
  FIX.llmTestReject = true;
  one('#btnLlmTest').click();
  await sleep(120);
  ok('测试网络异常提示确认服务', /请求失败，请确认服务在运行/.test(one('#llmTestResult').textContent), one('#llmTestResult').textContent);
  FIX.llmTestReject = false;
  FIX.llmTest = { ok: true, latency_ms: 100, usage: { prompt_tokens: 1, completion_tokens: 1 }, cost_yuan: 0.00001 };
  FIX.llmTestManual = true;
  one('#btnLlmTest').click();
  await sleep(40);
  ok('测试中按钮禁用并显示测试中', one('#btnLlmTest').disabled === true && one('#btnLlmTest').textContent === '测试中…', one('#btnLlmTest').textContent);
  FIX.llmTestResolve();
  await sleep(80);
  FIX.llmTestManual = false;
  ok('请求返回后按钮 loading 复原', one('#btnLlmTest').disabled === false && one('#btnLlmTest').textContent === '测试连接', one('#btnLlmTest').textContent);
  ZS.showView('home');
  await sleep(30);

  console.log('== 对话：查询类（low，自动执行）==');
  chatScript = [
    { event: 'session', data: { session_id: 'sess_test_001', thread_id: 'th_1' } },
    { event: 'intent', data: { name: 'query_bills', confidence: 0.9, source: 'rule', slots: { months: 1 } } },
    { event: 'plan', data: { steps: [{ step_id: 's1', title: '汇总近 1 个月账单', tool: 'get_bill_summary', risk: 'low' }], edges: [] } },
    { event: 'tool_start', data: { step_id: 's1', tool: 'get_bill_summary', args_preview: { months: 1 } } },
    { event: 'tool_end', data: { step_id: 's1', tool: 'get_bill_summary', ok: true, latency_ms: 8, result: { total_cents: 1671231 } } },
    { event: 'message', data: { delta: '近 1 个月你一共支出 **¥16,712.31**，' } },
    { event: 'message', data: { delta: '最多的是「居住」。' } },
    { event: 'audit', data: { seq: 4, event_type: 'tool_result', tool: 'get_bill_summary', risk_level: 'low', hash: 'abcd1234' } },
    { event: 'done', data: { status: 'ok', thread_id: 'th_1' } }
  ];
  await ZS.sendText('查一下这个月支出最多的三类');
  await sleep(120);
  ok('切到对话视图', ZS.state.view === 'chat' && one('#view-chat').classList.contains('is-on'));
  ok('用户消息右对齐气泡', getEls('.chat__me').length === 1, String(getEls('.chat__me').length));
  ok('意图卡存在', getEls('.card--intent').length === 1, String(getEls('.card--intent').length));
  ok('计划卡 1 步', getEls('.card--plan .plan__step').length === 1);
  ok('计划步已标完成', getEls('.card--plan .plan__step.is-ok').length === 1);
  ok('轨迹 1 行已完成', getEls('#traceList .tl__row').length === 1 && getEls('#traceList .tl__row.is-ok').length === 1);
  ok('轨迹头部同步为已完成', /已完成/.test(one('#traceMeta').textContent), one('#traceMeta').textContent);
  const resultHtml = one('.result') ? one('.result').innerHTML : '';
  ok('回复中 markdown 加粗被解析（无字面 **）', resultHtml.indexOf('<strong>') !== -1 && resultHtml.indexOf('**') === -1, resultHtml.slice(0, 120));
  ok('回复含金额', /16,712\.31/.test(one('.result').textContent), one('.result').textContent.slice(0, 60));
  ok('审计小条出现', getEls('.turn__audit').length === 1);
  ok('查询类无撤销按钮', getEls('.toast__action').length === 0);

  console.log('== 对话：转账（medium，需确认）==');
  chatScript = [
    { event: 'intent', data: { name: 'transfer', confidence: 0.86, source: 'llm', model: 'deepseek-chat', slots: { amount_cents: 800, payee: '妈妈' } } },
    { event: 'plan', data: { steps: [{ step_id: 's1', title: '校验收款人', tool: 'get_payees', risk: 'low' }, { step_id: 's2', title: '向「妈妈」转账', tool: 'create_transfer', risk: 'medium' }], edges: [] } },
    { event: 'tool_start', data: { step_id: 's1', tool: 'get_payees', args_preview: {} } },
    { event: 'tool_end', data: { step_id: 's1', tool: 'get_payees', ok: true, latency_ms: 3, result: { payees: [] } } },
    { event: 'message', data: { delta: '已生成待授权操作。' } },
    { event: 'approval_required', data: {
      approval_id: 'ap_test_1', nonce: 'n1', expires_at: new Date(Date.now() + 300000).toISOString(),
      risk_level: 'medium', challenge: { type: 'confirm' },
      reasons: ['按银行权限分级，该操作需要用户确认后执行'],
      hits: [
        { rule: 'amount_single_limit', dimension: 'amount', level: 'medium', detail: '单笔 ¥8.00 超过自动执行阈值 ¥0.00，需要确认', value: 800, threshold: 0 },
        { rule: 'payee_new', dimension: 'payee', level: 'low', detail: '收款人「妈妈」近 30 天无转账记录', value: 0, threshold: 30 },
        { rule: 'daily_cumulative', dimension: 'amount', level: 'low', detail: '当日累计 ¥8.00，未超过 ¥20,000.00', value: 800, threshold: 2000000 },
        { rule: 'channel_risk', dimension: 'channel', level: 'low', detail: '渠道为 App 直连', value: 0, threshold: 0 }
      ],
      title: '向「妈妈」转账 ¥8.00', preview: '执行后「工资卡」余额将从 ¥12,844.50 变为 ¥12,836.50',
      amount_cents: 800, currency: 'CNY', counterparty: '妈妈', counterparty_account_masked: '6214 **** 6621',
      payer_account_masked: '**** 8821', source: 'llm', source_detail: '模型抽取金额与收款人', conflicts: []
    } },
    { event: 'done', data: { status: 'awaiting_approval', thread_id: 'th_2' } }
  ];
  await ZS.sendText('给妈妈转 8 元');
  await sleep(120);
  ok('审批弹层打开', one('#approvalSheet').hidden === false);
  ok('风险徽章=中风险', /中风险/.test(one('#apRisk').textContent), one('#apRisk').textContent);
  ok('金额大字 ¥8.00', /¥/.test(one('#apAmount').innerHTML) && /8\.00/.test(one('#apAmount').textContent), one('#apAmount').textContent);
  ok('倒计时为 mm:ss', /^\d\d:\d\d$/.test(one('#apCountdown').textContent), one('#apCountdown').textContent);
  ok('规则行最多 3 行', getEls('#apReasons .rule').length === 3, String(getEls('#apReasons .rule').length));
  ok('规则名 mono（amount_single_limit 在首位）', /amount_single_limit/.test(one('#apReasons .rule__name').textContent), one('#apReasons .rule__name').textContent);
  ok('剩余折叠为 +1 条', /\+1 条/.test(one('#apReasons .rule__more').textContent), one('#apReasons .rule__more') ? one('#apReasons .rule__more').textContent : 'none');
  ok('confirm 型不显示验证码块', one('#apOtp').hidden === true);
  ok('预览后果写入', /12,844\.50/.test(one('#apPreview').textContent));
  ok('来源显示模型', /模型/.test(one('#apSource').textContent), one('#apSource').textContent);
  ok('待授权胶囊在弹层下隐藏', one('#pendingPill').hidden === true);

  FIX.approvalResult = {
    status: 'ok',
    reply: '已完成：向「妈妈」转账 **¥8.00**，可在 60 秒内撤销。',
    executed: [{ step_id: 's2', tool: 'create_transfer', ok: true, result: { action_id: 'act_123', transfer: { action_id: 'act_123' } } }],
    audit: [{ seq: 6, event_type: 'approval_granted', tool: 'policy_gate', risk_level: 'medium', hash: 'ff00' }]
  };
  ZS.submitApproval('approve');
  await sleep(60);
  ok('确认后进入盖章态', one('#approvalPanel').classList.contains('is-stamping') || one('#approvalPanel').classList.contains('is-stamped'));
  await sleep(900);
  ok('盖章后弹层关闭', one('#approvalSheet').hidden === true);
  ok('执行后出现撤销按钮', getEls('.toast__action').length >= 1, String(getEls('.toast__action').length));
  ok('执行结果写入对话', /12,836\.50|已完成/.test(one('#chat').textContent));
  ok('计划第二步标完成', getEls('.card--plan .plan__step.is-ok').length >= 1);

  console.log('== 审批：高风险 TOTP ==');
  chatScript = [
    { event: 'intent', data: { name: 'transfer', confidence: 0.9, source: 'rule', slots: { amount_cents: 800000 } } },
    { event: 'approval_required', data: { approval_id: 'ap_high', nonce: 'n2', expires_at: new Date(Date.now() + 300000).toISOString(), risk_level: 'high', challenge: { type: 'totp' }, reasons: ['大额转账需动态验证码'], hits: [{ rule: 'amount_large', dimension: 'amount', level: 'high', detail: '单笔 ¥8,000.00 超过 ¥5,000.00 上限', value: 800000, threshold: 500000 }], title: '向「妈妈」转账 ¥8,000.00', preview: '执行后余额减少 ¥8,000.00', amount_cents: 800000, counterparty: '妈妈', source: 'rule' } },
    { event: 'done', data: { status: 'awaiting_approval' } }
  ];
  await ZS.sendText('给妈妈转 8000 元');
  await sleep(100);
  ok('高风险弹层打开', one('#approvalSheet').hidden === false);
  ok('风险徽章=高风险', /高风险/.test(one('#apRisk').textContent), one('#apRisk').textContent);
  ok('显示 6 位验证码块', one('#apOtp').hidden === false);
  ok('确认按钮文案含验证', /验证/.test(one('#btnApprove').textContent), one('#btnApprove').textContent);
  ok('空码提交被本地拦截', (function () { ZS.submitApproval('approve'); return /6 位/.test(one('#apError').textContent); })(), one('#apError').textContent);
  one('#btnDemoTotp').click();
  await sleep(60);
  const otpVals = getEls('#otpCells .otp__cell').map(function (c) { return c.value; }).join('');
  ok('演示验证码自动填入 6 位', otpVals === '123456', otpVals);
  FIX.approvalResult = { status: 'ok', reply: '大额转账已完成。', executed: [{ step_id: 's1', tool: 'create_transfer', ok: true, result: { action_id: 'act_456' } }] };
  ZS.submitApproval('approve');
  await sleep(900);
  ok('TOTP 确认后弹层关闭', one('#approvalSheet').hidden === true);

  console.log('== 审计抽屉 ==');
  ZS.openAudit();
  await sleep(80);
  ok('审计抽屉打开', one('#auditSheet').hidden === false);
  ok('审计行 = 3', getEls('#auditList .arow').length === 3, String(getEls('#auditList .arow').length));
  ok('倒序最新在首', /#3/.test(one('#auditList .arow .arow__seq').textContent));
  await sleep(800);
  ok('全部行逐位锁定完成', getEls('#auditList .arow.is-verified').length === 3, String(getEls('#auditList .arow.is-verified').length));
  ok('链校验徽章通过', /链校验通过/.test(one('#auditValid').textContent), one('#auditValid').textContent);
  ok('哈希显示前 8 位', one('#auditList .arow__hash').textContent.length === 8, one('#auditList .arow__hash').textContent);
  one('#auditList .arow').click();
  ok('点击行展开完整哈希', one('#auditList .arow').classList.contains('is-open') && one('#auditList .arow .arow__detail').hidden === false);
  ZS.closeAudit();
  ok('关闭审计抽屉', one('#auditSheet').hidden === true);

  console.log('== 更多菜单 / 窄屏 ==');
  one('#btnMore').click();
  ok('更多菜单打开', one('#moreMenu').hidden === false);
  one('#moreMenu .menu__item--nav').click();
  await sleep(30);
  ok('菜单项可切视图', getEls('.screen.is-on').length === 1, String(getEls('.screen.is-on').length));
  ZS.showView('home');
  sandbox.innerWidth = 390;
  ZS.showView('chat');
  await sleep(30);
  ok('390 宽下仍可渲染对话（无异常）', one('#view-chat').classList.contains('is-on') && getEls('.chat__me').length >= 1);
  ZS.showView('skills'); await sleep(30);
  ok('390 宽下技能页可渲染', getEls('#skillsGrid .scard').length === 6);
  ZS.showView('home');
  sandbox.innerWidth = 1600;

  console.log('== 错误降级 ==');
  const origFetch = sandbox.fetch;
  sandbox.fetch = function () { return Promise.reject(new Error('network down')); };
  ZS.state.pages.services = false;
  ZS.showView('services');
  await sleep(60);
  ok('外部服务失败降级为「服务暂时不可用」', /服务暂时不可用/.test(one('#servicesGrid').textContent), one('#servicesGrid').textContent.slice(0, 40));
  ok('失败不白屏（页面仍在）', one('#scr-services').classList.contains('is-on'));
  sandbox.fetch = origFetch;
  ZS.showView('home');

  console.log('== v0.3.2 PWA ==');
  const flat = cssText.replace(/\s+/g, ' ');
  ok('index.html 含 manifest 链接', /<link rel="manifest" href="\/manifest.webmanifest">/.test(html));
  ok('index.html 含 theme-color', /<meta name="theme-color" content="#0B5FD0">/.test(html));
  ok('index.html 含 apple-touch-icon', /rel="apple-touch-icon" href="\/assets\/icons\/apple-touch-icon.png"/.test(html));
  ok('index.html 末尾加载 app.js（defer）', /<script src="app.js" defer><\/script>/.test(html));
  const maniRaw = fs.readFileSync(path.join(__dirname, 'manifest.webmanifest'), 'utf8');
  let mani = null;
  try { mani = JSON.parse(maniRaw); } catch (e) { mani = null; }
  ok('manifest 可解析', !!mani);
  ok('manifest name / short_name', !!mani && mani.name === 'ZeroStep 零步银行' && mani.short_name === 'ZeroStep', mani && mani.name);
  ok('manifest start_url 与 scope 均为 /', !!mani && mani.start_url === '/' && mani.scope === '/');
  ok('manifest display standalone', !!mani && mani.display === 'standalone', mani && mani.display);
  ok('manifest 4 个图标（2 个 maskable）', !!mani && mani.icons.length === 4 && mani.icons.filter(function (i) { return i.purpose === 'maskable'; }).length === 2);
  ok('manifest 配色 #F3F5F8 / #0B5FD0', !!mani && mani.background_color === '#F3F5F8' && mani.theme_color === '#0B5FD0');
  const swCode = fs.readFileSync(path.join(__dirname, 'sw.js'), 'utf8');
  ok('sw.js 预缓存外壳四件', ['/index.html', '/styles.css', '/app.js', '/manifest.webmanifest'].every(function (u) { return swCode.indexOf(u) >= 0; }));
  ok('sw.js /api/ 走网络不缓存', /isApi/.test(swCode) && swCode.indexOf('/api/') >= 0 && !/caches\.match\(request\)[\s\S]{0,40}api/.test(swCode));
  ok('sw.js 有版本常量与旧缓存清理', /VERSION/.test(swCode) && /caches\.delete/.test(swCode));
  const swCalls = [];
  sandbox.navigator.serviceWorker = { register: function (u) { swCalls.push(u); return Promise.resolve({ scope: '/' }); } };
  ZS.registerServiceWorker();
  ok('注册 Service Worker 指向 /sw.js', swCalls.length === 1 && swCalls[0] === '/sw.js', String(swCalls));
  sandbox.navigator.serviceWorker = { register: function () { return Promise.reject(new Error('sw blocked')); } };
  let swThrew = false;
  try { ZS.registerServiceWorker(); } catch (e) { swThrew = true; }
  await sleep(30);
  ok('SW 注册失败静默降级（不抛异常）', swThrew === false);
  sandbox.navigator.serviceWorker = undefined;

  console.log('== v0.3.2 手机收口 ==');
  ok('底部 tab 六个入口', getEls('#tabbar .tabbar__btn').length === 6, String(getEls('#tabbar .tabbar__btn').length));
  const tabNavs = getEls('#tabbar .tabbar__btn').map(function (b) { return b.getAttribute('data-nav'); }).join(',');
  ok('tab 顺序 = 工作台/账户/专区/技能/插件/设置', tabNavs === 'home,accounts,zone,skills,plugins,settings', tabNavs);
  sandbox.innerWidth = 390;
  one('#tabbar .tabbar__btn[data-nav="plugins"]').click();
  await sleep(60);
  ok('点 tab 切到插件页', ZS.state.view === 'plugins' && one('#scr-plugins').classList.contains('is-on'), ZS.state.view);
  ok('插件 tab 高亮', one('#tabbar .tabbar__btn[data-nav="plugins"]').classList.contains('is-on'));
  ok('顶栏当前页名 = 插件', one('#pageTitle').textContent === '插件', one('#pageTitle').textContent);
  one('#tabbar .tabbar__btn[data-nav="settings"]').click();
  await sleep(60);
  ok('点 tab 切到设置页', ZS.state.view === 'settings' && one('#scr-settings').classList.contains('is-on'));
  ok('设置 tab 高亮且其它不亮', one('#tabbar .tabbar__btn[data-nav="settings"]').classList.contains('is-on') && !one('#tabbar .tabbar__btn[data-nav="home"]').classList.contains('is-on'));
  ok('顶栏当前页名 = 设置', one('#pageTitle').textContent === '设置', one('#pageTitle').textContent);
  ok('CSS：tabbar 固定底部并留安全区', /\.tabbar\{ position:fixed;left:0;right:0;bottom:0/.test(flat));
  ok('CSS：主内容为 tabbar 预留底部空间', /body\{\s*padding-bottom:calc\(56px \+ env\(safe-area-inset-bottom\)\)/.test(flat));
  ok('CSS：桌面隐藏 tabbar / 页名 / 菜单模式段', /\.tabbar\{display:none\}/.test(flat) && /\.topbar__page\{ display:none/.test(flat) && /\.menu__modewrap\{display:none\}/.test(flat));
  ok('CSS：手机隐藏审计与头像按钮', /\.topbar__right \.btn-text\{display:none\}/.test(flat) && /\.topbar__right \.avatar\{display:none\}/.test(flat));
  ok('CSS：手机隐藏顶栏三段、菜单内展开', /\.topbar__mid\{display:none\}/.test(flat) && /\.menu__modewrap\{display:block\}/.test(flat));
  ok('CSS：审批弹层手机全屏宽且限高', /\.dlg\{width:100%;max-height:calc\(100vh - 32px\)/.test(flat) && /\.dlg\{max-height:calc\(100dvh - 24px\)/.test(flat));
  ok('CSS：手机验证码格加高', /\.otp__cell\{height:44px\}/.test(flat));
  one('#btnMore').click();
  ok('更多抽屉含模式三段（手机可切模式）', getEls('#menuModeWrap [data-mode]').length === 3, String(getEls('#menuModeWrap [data-mode]').length));
  one('#menuModeWrap [data-mode="rule"]').click();
  await sleep(80);
  ok('手机抽屉里切到规则模式', ZS.state.llmMode === 'rule', ZS.state.llmMode);
  ok('切模式后抽屉收起', one('#moreMenu').hidden === true);
  one('#btnMore').click();
  one('#menuModeWrap [data-mode="auto"]').click();
  await sleep(80);
  ok('切回自动模式', ZS.state.llmMode === 'auto', ZS.state.llmMode);
  ok('视口高度接口可调用（键盘场景）', (function () { try { ZS.applyViewportHeight(); return ZS.isMobileViewport() === true; } catch (e) { return false; } })());

  console.log('== v0.3.2 模拟推送 ==');
  ZS.showView('settings');
  await sleep(60);
  ok('推送卡三个类型按钮', getEls('#pushKinds [data-push]').length === 3, String(getEls('#pushKinds [data-push]').length));
  ok('默认选中订阅扣费', one('#pushKinds [data-push="subscription"]').classList.contains('is-on'));
  ok('结果区初始隐藏', one('#pushResult').hidden === true);
  const pushBefore = PUSH_CALLS.length;
  one('#btnPushPreview').click();
  await sleep(120);
  const lastPush = PUSH_CALLS[PUSH_CALLS.length - 1] || {};
  ok('POST /api/push 带 kind 与 session_id', PUSH_CALLS.length === pushBefore + 1 && lastPush.kind === 'subscription' && lastPush.session_id === 'sess_test_001', JSON.stringify(lastPush));
  ok('结果区展示标题与正文', one('#pushResult').hidden === false && one('#pushTitle').textContent.length > 0 && one('#pushText').textContent.length > 0, one('#pushTitle').textContent);
  ok('已推送到渠道状态', /已推送/.test(one('#pushTag').textContent) && /已推送/.test(one('#pushStatus').textContent), one('#pushTag').textContent);
  ok('推送后按钮复原', one('#btnPushPreview').disabled === false && one('#btnPushPreview').textContent === '模拟推送');
  pushMode = 'nopush';
  one('#pushKinds [data-push="large_txn"]').click();
  ok('切换类型高亮', one('#pushKinds [data-push="large_txn"]').classList.contains('is-on') && !one('#pushKinds [data-push="subscription"]').classList.contains('is-on'));
  one('#btnPushPreview').click();
  await sleep(120);
  ok('大额支出 kind 正确', (PUSH_CALLS[PUSH_CALLS.length - 1] || {}).kind === 'large_txn', JSON.stringify(PUSH_CALLS[PUSH_CALLS.length - 1]));
  ok('未配置地址时提示已生成未配置', /未配置/.test(one('#pushTag').textContent) && /未配置/.test(one('#pushStatus').textContent), one('#pushTag').textContent);
  pushMode = 'fail';
  one('#pushKinds [data-push="repayment"]').click();
  one('#btnPushPreview').click();
  await sleep(120);
  ok('渠道失败显示错误', /失败/.test(one('#pushTag').textContent) && /502|失败/.test(one('#pushStatus').textContent), one('#pushTag').textContent + ' / ' + one('#pushStatus').textContent);
  ok('失败后按钮复原', one('#btnPushPreview').disabled === false);
  pushMode = 'reject';
  one('#btnPushPreview').click();
  await sleep(120);
  ok('网络失败降级为服务暂时不可用', /服务暂时不可用/.test(one('#pushStatus').textContent + one('#pushText').textContent), one('#pushStatus').textContent);
  pushMode = 'delivered';
  sandbox.innerWidth = 1600;

  console.log('== v0.4.0 新工具中文名与能力入口 ==');
  var NEW_TOOL_MAP = { redeem_wealth: '赎回理财', set_card_limit: '调整卡片限额', card_spend: '卡片消费', scan_anomalies: '异常交易扫描', annual_report: '年度账单报告', create_scheduled_transfer: '创建预约转账', list_scheduled_transfers: '查看预约转账', cancel_scheduled_transfer: '取消预约转账' };
  var mapBad = Object.keys(NEW_TOOL_MAP).filter(function (k) { return ZS.toolLabel(k) !== NEW_TOOL_MAP[k]; });
  ok('8 个新工具映射为中文', mapBad.length === 0, mapBad.join(','));
  ok('未知工具回落原名', ZS.toolLabel('unknown_tool_xyz') === 'unknown_tool_xyz', ZS.toolLabel('unknown_tool_xyz'));
  ok('空工具名回落 tool', ZS.toolLabel('') === 'tool', ZS.toolLabel(''));
  ok('旧工具同样中文化（账单汇总）', ZS.toolLabel('get_bill_summary') === '账单汇总', ZS.toolLabel('get_bill_summary'));
  ZS.showView('home');
  await sleep(30);
  var sampleTexts = getEls('#samples .sample').map(function (el) { return el.textContent; });
  ok('首页示例 6 条', sampleTexts.length === 6, String(sampleTexts.length));
  ok('示例含赎回 / 年度账单 / 预约转账', ['赎回 500 元稳利宝', '看一下今年的年度账单', '3 天后给妈妈转 800 元'].every(function (t) { return sampleTexts.indexOf(t) >= 0; }), sampleTexts.join(' | '));
  ZS.state.currentTurn = null;
  ZS.handleEvent({ event: 'plan', data: { steps: [
    { step_id: 's1', title: '赎回「稳利宝」500 元', tool: 'redeem_wealth', risk: 'high' },
    { step_id: 's2', title: '创建 3 天后的预约转账', tool: 'create_scheduled_transfer', risk: 'high' }
  ] } });
  await sleep(30);
  var planTurn = ZS.state.currentTurn;
  var planTools = [];
  if (planTurn && planTurn.planEl) {
    Array.prototype.forEach.call(planTurn.planEl.querySelectorAll('.plan__step .plan__tool'), function (el) { planTools.push(el.textContent); });
  }
  ok('计划卡工具名已中文化', planTools.indexOf('赎回理财') >= 0 && planTools.indexOf('创建预约转账') >= 0, planTools.join(','));
  ok('计划卡不再出现英文工具名', !/[a-z]_[a-z]/.test(planTools.join(' ')), planTools.join(','));

  console.log('== v0.4.1 审批多因子 ==');
  var highAp = {
    approval_id: 'ap_mf_1', nonce: 'n3', expires_at: new Date(Date.now() + 300000).toISOString(),
    risk_level: 'high', challenge: { type: 'totp' }, factors: ['totp', 'sms', 'face'],
    reasons: ['大额转账需动态验证码'],
    hits: [{ rule: 'amount_large', dimension: 'amount', level: 'high', detail: '单笔 ¥8,000.00 超过 ¥5,000.00 上限', value: 800000, threshold: 500000 }],
    title: '向「妈妈」转账 ¥8,000.00', preview: '执行后余额减少 ¥8,000.00',
    amount_cents: 800000, counterparty: '妈妈', source: 'rule'
  };
  ZS.openApproval(highAp);
  await sleep(80);
  ok('高风险显示三种校验方式', one('#dlgFactors').hidden === false && getEls('#dlgFactors .factor').length === 3, String(getEls('#dlgFactors .factor').length));
  ok('默认选中动态验证码且显示验证码块', one('#dlgFactorTotp').classList.contains('is-on') && one('#apOtp').hidden === false);
  ok('校验方式区块有标题', /本次校验方式/.test(one('#dlgFactors').textContent), one('#dlgFactors').textContent.slice(0, 40));
  one('#dlgFactorSms').click();
  await sleep(60);
  ok('切短信后隐藏 TOTP 演示链接', one('#btnDemoTotp').hidden === true && one('#dlgSmsSend').hidden === false);
  one('#dlgSmsSend').click();
  await sleep(90);
  ok('短信演示码展示并计时', /654321/.test(one('#dlgSmsCode').textContent) && /秒/.test(one('#dlgSmsCode').textContent), one('#dlgSmsCode').textContent);
  ok('验证码标题变为短信验证码', /短信/.test(one('#apOtpLabel').textContent), one('#apOtpLabel').textContent);
  one('#dlgSmsCode').click();
  await sleep(20);
  var smsVals = getEls('#otpCells .otp__cell').map(function (c) { return c.value; }).join('');
  ok('点击演示码自动填入 6 格', smsVals === '654321', smsVals);
  FIX.approvalResult = { status: 'ok', reply: '短信校验通过，转账已完成。', executed: [] };
  var smsCall = (function () { ZS.submitApproval('approve'); return CALLS[CALLS.length - 1] || {}; })();
  var smsBody = {};
  try { smsBody = JSON.parse(smsCall.body); } catch (e) {}
  ok('短信提交带 method=sms 与验证码', /\/api\/approval/.test(smsCall.url || '') && smsBody.method === 'sms' && smsBody.code === '654321', JSON.stringify(smsBody));
  await sleep(900);
  ZS.openApproval(highAp);
  await sleep(60);
  one('#dlgFactorFace').click();
  await sleep(50);
  ok('人脸方式隐藏验证码键盘', one('#apOtp').hidden === true && one('#dlgFace').hidden === false);
  one('#dlgFaceScan').click();
  await sleep(90);
  ok('人脸确认后显示已完成扫描', /已完成扫描/.test(one('#dlgFaceState').textContent) && one('#dlgFaceToken').hidden === false, one('#dlgFaceState').textContent);
  FIX.approvalResult = { status: 'ok', reply: '人脸校验通过，转账已完成。', executed: [] };
  var faceCall = (function () { ZS.submitApproval('approve'); return CALLS[CALLS.length - 1] || {}; })();
  var faceBody = {};
  try { faceBody = JSON.parse(faceCall.body); } catch (e) {}
  ok('人脸提交带 method=face 与 token', /\/api\/approval/.test(faceCall.url || '') && faceBody.method === 'face' && faceBody.code === 'fc_abc123def456', JSON.stringify(faceBody));
  await sleep(900);
  ZS.openApproval({ approval_id: 'ap_mf_2', nonce: 'n4', expires_at: new Date(Date.now() + 300000).toISOString(), risk_level: 'medium', challenge: { type: 'confirm' }, reasons: ['该操作需要用户确认后执行'], title: '向「妈妈」转账 ¥8.00', amount_cents: 800, counterparty: '妈妈', source: 'rule' });
  await sleep(50);
  ok('中风险不显示因子选择', one('#dlgFactors').hidden === true && one('#apOtp').hidden === true);
  ZS.submitApproval('reject');
  await sleep(120);
  ok('拒绝后弹层关闭', one('#approvalSheet').hidden === true);

  console.log('== v0.4.1 会话锁定 ==');
  FIX.approvalResult = { status: 'ok', reply: '已完成。', executed: [] };
  chatScript = [{ event: 'done', data: { status: 'locked', code: 'SESSION_LOCKED', locked_until: new Date(Date.now() + 1200).toISOString() } }];
  await ZS.sendText('给妈妈转 100000 元');
  await sleep(150);
  ok('锁定 InfoBar 显示并给出剩余秒', one('#lockBar').hidden === false && /会话已临时锁定/.test(one('#lockBarText').textContent) && /剩余/.test(one('#lockBarText').textContent), one('#lockBarText').textContent);
  ok('锁定期间输入与发送禁用', one('#input').disabled === true && one('#btnSend').disabled === true && ZS.lockedNow() === true);
  await sleep(1700);
  ok('倒计时结束自动恢复', one('#lockBar').hidden === true && one('#input').disabled === false && one('#btnSend').disabled === false && ZS.lockedNow() === false, one('#lockBarText').textContent);

  console.log('== v0.5.0 逐字流式回答 ==');
  ZS.state.currentTurn = null;
  ZS.setBusy(false);
  ZS.handleEvent({ event: 'answer_start', data: { turn_id: 'tr_9', thread_id: 'th_9', chars: 42 } });
  await sleep(10);
  function lastStream() { const els = getEls('.result--stream'); return els[els.length - 1] || null; }
  let bubble = lastStream();
  ok('answer_start 建占位气泡并带 is-typing', !!bubble && bubble.classList.contains('is-typing'));
  ok('占位气泡尾部有闪烁光标', !!(bubble && bubble.querySelector('.answer__caret')));
  ok('answer_start 后按钮变「停止」', one('#btnSend').textContent === '停止' && one('#btnSend').classList.contains('is-stop'), one('#btnSend').textContent);
  ok('流式期间停止按钮可点击', one('#btnSend').disabled === false);
  ZS.state.busy = true;
  ZS.handleEvent({ event: 'answer_delta', data: { turn_id: 'tr_9', i: 0, text: '近 1 个月你一共支出 ' } });
  ZS.handleEvent({ event: 'answer_delta', data: { turn_id: 'tr_9', i: 2, text: '，最多的是「居住」。' } });
  await sleep(10);
  ok('先到的 i=2 不会插到 i=1 前面', bubble.querySelector('.answer__body').textContent === '近 1 个月你一共支出 ', bubble.textContent);
  ZS.handleEvent({ event: 'answer_delta', data: { turn_id: 'tr_9', i: 1, text: '**¥16,712.31**' } });
  await sleep(10);
  const joined = bubble.querySelector('.answer__body').textContent;
  ok('按 i 顺序累积全部分块', joined === '近 1 个月你一共支出 **¥16,712.31**，最多的是「居住」。', joined);
  ZS.handleEvent({ event: 'answer_end', data: { turn_id: 'tr_9', chunks: 3, elapsed_ms: 36, cancelled: false } });
  await sleep(10);
  ok('answer_end 去掉 is-typing 与光标', !bubble.classList.contains('is-typing') && !bubble.querySelector('.answer__caret'), bubble.className);
  ok('流式收尾后 markdown 加粗被解析（无字面 **）', bubble.innerHTML.indexOf('<strong>') >= 0 && bubble.innerHTML.indexOf('**') === -1, bubble.innerHTML.slice(0, 100));
  ok('answer_end 后按钮恢复「发送」', one('#btnSend').textContent === '发送' && !one('#btnSend').classList.contains('is-stop'), one('#btnSend').textContent);
  ok('answer_end 记录分块数与耗时', bubble.getAttribute('data-chunks') === '3' && bubble.getAttribute('data-elapsed') === '36');

  console.log('== v0.5.0 停止按钮 / 取消 ==');
  ZS.state.currentTurn = null;
  ZS.setBusy(true);
  ZS.handleEvent({ event: 'answer_start', data: { turn_id: 'tr_11', thread_id: 'th_11' } });
  ZS.handleEvent({ event: 'answer_delta', data: { turn_id: 'tr_11', i: 0, text: '第一段' } });
  await sleep(10);
  ok('流式中按钮文案=停止且未禁用', one('#btnSend').textContent === '停止' && one('#btnSend').disabled === false, one('#btnSend').textContent);
  one('#btnSend').click();
  await sleep(60);
  const cancelCall = CALLS[CALLS.length - 1] || {};
  ok('点击停止 POST /api/turn/cancel 带 thread_id', /\/api\/turn\/cancel/.test(cancelCall.url || '') && /th_11/.test(cancelCall.body || ''), (cancelCall.url || '') + ' ' + (cancelCall.body || ''));
  ZS.handleEvent({ event: 'answer_end', data: { turn_id: 'tr_11', chunks: 1, elapsed_ms: 18, cancelled: true } });
  await sleep(10);
  bubble = lastStream();
  ok('取消后气泡标 is-cancelled 并注「已停止输出」', bubble.classList.contains('is-cancelled') && /已停止输出/.test(bubble.textContent), bubble.textContent);

  ZS.handleEvent({ event: 'note', data: { text: '已停止输出' } });
  await sleep(10);
  ok('取消后补发 note「已停止输出」只渲染一次', (bubble.textContent.match(/已停止输出/g) || []).length === 1 && !ZS.state.currentTurn.noteEl, '残留 ' + (bubble.textContent.match(/已停止输出/g) || []).length + ' 次');

  console.log('== v0.5.0 note 正常渲染（非取消）==');
  ZS.state.currentTurn = null;
  ZS.handleEvent({ event: 'intent', data: { name: 'query_bills', confidence: 0.9, source: 'rule', slots: {} } });
  ZS.handleEvent({ event: 'note', data: { text: '这是一条普通提示' } });
  await sleep(10);
  ok('普通 note 正常渲染为 turn__note', !!ZS.state.currentTurn.noteEl && ZS.state.currentTurn.noteEl.textContent === '这是一条普通提示', ZS.state.currentTurn.noteEl ? ZS.state.currentTurn.noteEl.textContent : 'null');
  ZS.setBusy(false);
  await sleep(10);
  ok('取消收尾后按钮恢复发送', one('#btnSend').textContent === '发送' && !one('#btnSend').classList.contains('is-stop'), one('#btnSend').textContent);

  console.log('== v0.5.0 降级路径（无 answer_delta）==');
  ZS.state.currentTurn = null;
  ZS.setBusy(false);
  ZS.handleEvent({ event: 'answer_start', data: { turn_id: 'tr_10', chars: 20 } });
  await sleep(10);
  ZS.handleEvent({ event: 'message', data: { delta: '未收到分块时的整段回答：本月支出 **¥16,712.31**。' } });
  ZS.handleEvent({ event: 'answer_end', data: { turn_id: 'tr_10', chunks: 0, elapsed_ms: 0, cancelled: false } });
  await sleep(10);
  bubble = lastStream();
  ok('降级：整段 message 显示在同一气泡', /本月支出/.test(bubble.textContent) && /16,712\.31/.test(bubble.textContent), bubble.textContent.slice(0, 80));
  ok('降级：整段仍是加粗解析', bubble.innerHTML.indexOf('<strong>') >= 0);
  ok('降级：本轮只有一个回复气泡（无重复）', ZS.state.currentTurn.root.querySelectorAll('.result').length === 1, String(ZS.state.currentTurn.root.querySelectorAll('.result').length));

  console.log('== v0.5.0 消费洞察卡 ==');
  ZS.state.currentTurn = null;
  ZS.handleEvent({ event: 'insight_card', data: {
    as_of: '2026-10-28', window: { months: 1 },
    totals: { out_cents: 1671231, in_cents: 2500001, prev_out_cents: 1490000, delta_pct: 12, potential_save_monthly_cents: 5800 },
    top_categories: [{ category: '居住', cents: 478600, pct: 29, prev_cents: 460000, delta_pct: 4 }],
    savings: [
      { id: 'sv1', kind: 'subscription', title: '视频会员自动续费', monthly_cents: 3800, yearly_cents: 45600, detail: '连续 4 个月扣费，可取消', action: { kind: 'subscription_cancel', merchant: '爱奇艺' } },
      { id: 'sv2', kind: 'category', title: '餐饮支出偏高', monthly_cents: 2000, yearly_cents: 24000, detail: '比上月多 ¥300.00', action: { kind: 'query_bills', category: '餐饮' } }
    ] } });
  await sleep(20);
  let card = getEls('.insight')[getEls('.insight').length - 1];
  ok('洞察卡标题「本月的三件事」', !!card && /本月的三件事/.test(card.textContent), card ? card.textContent.slice(0, 60) : 'null');
  ok('三个数字：环比 +12% / 最大分类 居住 / 可省 ¥58.00', !!card && /\+12%/.test(card.textContent) && /居住/.test(card.textContent) && /¥58\.00/.test(card.textContent), card ? card.textContent.slice(0, 160) : 'null');
  ok('savings 两条 + 每条去处理按钮', !!card && card.querySelectorAll('.insight__save').length === 2 && card.querySelectorAll('.insight__go').length === 2);
  ok('洞察卡标注只读', !!card && /只读洞察/.test(card.textContent));

  console.log('== v0.5.0 洞察卡 XSS 转义 ==');
  ZS.state.currentTurn = null;
  ZS.handleEvent({ event: 'insight_card', data: {
    totals: { delta_pct: -3, potential_save_monthly_cents: 100 },
    top_categories: [{ category: '<img src=x onerror=alert(1)>', pct: 10 }],
    savings: [{ id: 'sv9', title: '<script>alert(2)</script>', monthly_cents: 100, detail: '"><b>x</b>', action: { kind: 'subscription_cancel', merchant: '<svg onload=alert(3)>' } }]
  } });
  await sleep(20);
  const xssCard = getEls('.insight')[getEls('.insight').length - 1];
  ok('XSS：分类名被转义为文本（无真实 img 节点）', xssCard.querySelectorAll('img').length === 0 && xssCard.innerHTML.indexOf('&lt;img') >= 0 && xssCard.textContent.indexOf('onerror') >= 0, xssCard.innerHTML.slice(0, 160));
  ok('XSS：savings 标题与 detail 被转义', xssCard.innerHTML.indexOf('<script') === -1 && xssCard.innerHTML.indexOf('<b>x</b>') === -1);
  ok('XSS：去处理文案兜底为纯文本', xssCard.querySelectorAll('.insight__go').length === 0 || xssCard.innerHTML.indexOf('<svg') === -1);

  console.log('== v0.5.0 洞察卡一键处理 ==');
  ZS.state.currentTurn = null;
  ZS.handleEvent({ event: 'insight_card', data: {
    totals: { delta_pct: 5, potential_save_monthly_cents: 3800 },
    top_categories: [{ category: '居住', pct: 29 }],
    savings: [{ id: 'sv1', title: '视频会员自动续费', monthly_cents: 3800, detail: '可取消', action: { kind: 'subscription_cancel', merchant: '爱奇艺' } }]
  } });
  await sleep(20);
  card = getEls('.insight')[getEls('.insight').length - 1];
  chatScript = [{ event: 'message', data: { delta: '已进入取消订阅流程。' } }, { event: 'done', data: { status: 'ok' } }];
  card.querySelector('.insight__go').click();
  await sleep(150);
  const goCalls = CALLS.filter(function (c) { return c.url.indexOf('/api/chat') === 0; });
  const goCall = goCalls[goCalls.length - 1] || {};
  let goBody = {};
  try { goBody = JSON.parse(goCall.body); } catch (e) {}
  ok('点「去处理」发出「取消 爱奇艺 订阅」', /\/api\/chat/.test(goCall.url || '') && goBody.text === '取消 爱奇艺 订阅', JSON.stringify(goBody));
  const meEls = getEls('.chat__me');
  ok('一键处理复用对话流（用户消息可见）', /取消 爱奇艺 订阅/.test(meEls[meEls.length - 1].textContent), meEls[meEls.length - 1].textContent);
  ok('洞察卡在对话视图内', ZS.state.view === 'chat' && one('#view-chat').classList.contains('is-on'));
  ZS.showView('home');
  await sleep(20);

  console.log('== v0.5.0 流式中断（网络错误）==');
  ZS.state.currentTurn = null;
  chatFailAfter = 2;
  chatScript = [
    { event: 'answer_start', data: { turn_id: 't99', thread_id: 'th_99', chars: 20 } },
    { event: 'answer_delta', data: { turn_id: 't99', i: 0, text: '先输出一半' } }
  ];
  ZS.sendText('测试流式中断');
  await sleep(260);
  chatFailAfter = null;
  const brokenEls = getEls('.result--stream');
  const broken = brokenEls[brokenEls.length - 1];
  ok('流式中断：去掉 is-typing 与光标', !!broken && !broken.classList.contains('is-typing') && !broken.querySelector('.answer__caret'));
  ok('流式中断：已输出文本保留', !!broken && /先输出一半/.test(broken.textContent), broken ? broken.textContent.slice(0, 80) : 'null');
  ok('流式中断：按钮恢复「发送」且可点', ZS.dom.btnSend.textContent === '发送' && !ZS.dom.btnSend.classList.contains('is-stop') && ZS.dom.btnSend.disabled === false);

  // ===================== v0.7.0 专区 / 仪表盘 / 分析 =====================
  console.log('== v0.7.0 专区：入口与路由 ==');
  ZS.state.sessionId = ZS.state.sessionId || 'sess_test_001';
  const navBtnsAll = getEls('.topnav__btn');
  ok('顶栏第三项是「专区」', navBtnsAll[2] && navBtnsAll[2].getAttribute('data-nav') === 'zone', navBtnsAll.map(function (b) { return b.getAttribute('data-nav'); }).join(','));
  ok('顶栏第二项是「账户」', navBtnsAll[1] && navBtnsAll[1].getAttribute('data-nav') === 'accounts', navBtnsAll.map(function (b) { return b.getAttribute('data-nav'); }).join(','));
  ok('更多菜单也带专区入口', !!one('#moreMenu [data-nav="zone"]'));
  ZS.state.zone.dash = null;
  ZS.state.zone.dashAt = 0;
  one('.topnav__btn[data-nav="zone"]').click();
  ok('点专区切到 zone 视图', ZS.state.view === 'zone' && one('#scr-zone').classList.contains('is-on'), ZS.state.view);
  ok('顶栏标题 = 专区', one('#pageTitle').textContent === '专区', one('#pageTitle').textContent);
  ok('专区 H1 = 钱在哪、花在哪、要不要动手', one('#zoneH1').textContent === '钱在哪、花在哪、要不要动手', one('#zoneH1').textContent);
  const dashTab = one('[data-zone="dashboard"]');
  ok('默认 tab 为仪表盘且 aria-selected', dashTab.classList.contains('is-on') && dashTab.getAttribute('aria-selected') === 'true');
  ok('加载中先标「读取中」', /读取中/.test(one('#zoneStamp').textContent), one('#zoneStamp').textContent);
  await sleep(150);
  ok('哈希解析：#/zone/analytics?preset=90d', (function () {
    const r = ZS.zoneParseHash('#/zone/analytics?preset=90d&from=2026-05-01&to=2026-06-30&gran=week');
    return r && r.view === 'zone' && r.zone === 'analytics' && r.params.preset === '90d' && r.params.gran === 'week';
  })());
  ok('哈希解析：#/zone/dashboard 无参数', (function () {
    const r = ZS.zoneParseHash('#/zone/dashboard');
    return r && r.view === 'zone' && r.zone === 'dashboard' && Object.keys(r.params).length === 0;
  })());
  ok('哈希解析：普通页面 #/skills', (function () { const r = ZS.zoneParseHash('#/skills'); return r && r.view === 'skills'; })());

  console.log('== v0.7.0 仪表盘渲染 ==');
  ok('KPI 六张卡', getEls('#zoneBody .zkpi').length === 6, String(getEls('#zoneBody .zkpi').length));
  const kpiText = getEls('#zoneBody .zkpi').map(function (el) { return el.textContent; }).join(' | ');
  ok('净资产 KPI = ¥79,428.50', kpiText.indexOf('¥79,428.50') >= 0, kpiText.slice(0, 60));
  ok('信用卡待还 KPI = ¥8,432.00', kpiText.indexOf('¥8,432.00') >= 0);
  ok('本月支出带环比徽章', getEls('#zoneBody .zkpi__delta').length >= 2, String(getEls('#zoneBody .zkpi__delta').length));
  ok('两张 KPI 带 14 点 sparkline', getEls('#zoneBody .zspark').length === 2, String(getEls('#zoneBody .zspark').length));
  ok('趋势图 6 根支出柱', getEls('#zoneBody .zchart__rect--out').length === 6, String(getEls('#zoneBody .zchart__rect--out').length));
  ok('趋势图有净额折线', getEls('#zoneBody .zchart__net').length === 1);
  ok('柱可聚焦且有 aria-label', (function () {
    const bar = one('#zoneBody .zchart__bar');
    return !!bar && bar.getAttribute('tabindex') === '0' && /支出/.test(bar.getAttribute('aria-label') || '');
  })());
  ok('图例三项（支出/收入/净流入）', (function () {
    const t = one('#zoneBody .zlegend').textContent;
    return /支出/.test(t) && /收入/.test(t) && /净流入/.test(t);
  })());
  ok('趋势数据表可展开且 6 行', (function () {
    const d = one('#zoneBody .zdetails');
    return !!d && /查看数据表/.test(d.querySelector('summary').textContent) && d.querySelectorAll('tbody tr').length === 6;
  })());
  ok('预算 6 行', getEls('#zoneBody .zbudget__row').length === 6, String(getEls('#zoneBody .zbudget__row').length));
  ok('超支行标红（购物 575%）', getEls('#zoneBody .zbudget__row--over').length === 1 && /575%/.test(one('#zoneBody .zbudget__row--over').textContent));
  ok('支出构成 5 段堆叠', getEls('#zoneBody .zstack__seg').length === 5, String(getEls('#zoneBody .zstack__seg').length));
  const compLegend = getEls('#zoneBody .zlegend').filter(function (el) { return !el.classList.contains('zlegend--inline'); })[0];
  ok('构成图例首项购物 47.1%', !!compLegend && /购物/.test(compLegend.textContent) && /47.1%/.test(compLegend.textContent), compLegend ? compLegend.textContent.slice(0, 80) : 'null');
  ok('待办 2 行', getEls('#zoneBody .ztodo__btn').length === 2, String(getEls('#zoneBody .ztodo__btn').length));
  ok('审计链徽章 valid', /审计链/.test(one('#zoneBody .zchain').textContent) && one('#zoneBody .zchain').classList.contains('zchain--ok'));
  ok('最近流水 8 行', getEls('#zoneBody .zcard--wide .ztable tbody tr').length >= 8);
  ok('数据时间戳标注只读', /更新于/.test(one('#zoneStamp').textContent) && /只读/.test(one('#zoneStamp').textContent), one('#zoneStamp').textContent);

  console.log('== v0.7.0 分析页渲染 ==');
  one('[data-zone="analytics"]').click();
  await sleep(150);
  ok('切到分析 tab', ZS.state.view === 'zone' && ZS.state.zone.view === 'analytics' && one('[data-zone="analytics"]').classList.contains('is-on'));
  ok('分析 H1 = 花在哪里，和上一期比变了多少', one('#zoneH1').textContent === '花在哪里，和上一期比变了多少', one('#zoneH1').textContent);
  ok('分析 KPI 四张卡', getEls('#zoneBody .zkpi').length === 4, String(getEls('#zoneBody .zkpi').length));
  ok('控制条 4 个区间预设 + 3 个粒度', getEls('#zoneBody [data-zpreset]').length === 4 && getEls('#zoneBody [data-zgran]').length === 3);
  ok('控制条回填起止日期', one('#zoneFrom').value.length === 10 && one('#zoneTo').value.length === 10, one('#zoneFrom').value + '→' + one('#zoneTo').value);
  ok('序列数据表 6 行', (function () {
    const d = one('#zoneBody .zdetails');
    return d.querySelectorAll('tbody tr').length === 6;
  })());
  ok('类别表默认按金额降序（购物在前）', /购物/.test(one('#zoneCats tbody tr').textContent), one('#zoneCats tbody tr').textContent.slice(0, 40));
  ok('点「占比」表头改为按占比排序（居住在前）', (function () {
    one('#zoneCats [data-zsort="pct"]').click();
    return /居住/.test(one('#zoneCats tbody tr').textContent);
  })(), one('#zoneCats tbody tr').textContent.slice(0, 40));
  ok('商户 TOP 10 横条', getEls('#zoneBody .zhbars__row').length === 10, String(getEls('#zoneBody .zhbars__row').length));
  ok('异常消费 2 条且标注等级', getEls('#zoneBody .zlist__badge').length >= 2 && /高/.test(one('#zoneBody .zlist__badge').textContent));
  ok('订阅显示剩余天数', /2 天/.test(one('#zoneBody').textContent));
  ok('储蓄建议显示可优化金额', /¥580.00/.test(one('#zoneBody').textContent));
  ok('明细首屏 20 笔 + 分页信息', (function () {
    const info = getEls('#zoneBody .zpager__info');
    return /共 25 笔 · 第 1 \/ 2 页/.test(info[info.length - 1].textContent);
  })(), getEls('#zoneBody .zpager__info').map(function (e) { return e.textContent; }).join(' | '));
  ok('明细上一页在首屏禁用', getEls('#zoneBody [data-zpage="prev"]').filter(function (b) { return b.disabled; }).length === 1);
  one('#zoneBody [data-zpage="next"]').click();
  ok('点下一页到第 2 页（5 笔）', /第 2 \/ 2 页/.test(getEls('#zoneBody .zpager__info').slice(-1)[0].textContent));
  ok('导出 CSV 触发下载提示', (function () {
    one('#zoneBody #zoneCsv').click();
    return getEls('#toasts .toast').some(function (t) { return /已导出/.test(t.textContent); });
  })());

  console.log('== v0.7.0 外观与命令面板 ==');
  ok('主题按钮存在', !!one('#btnTheme'));
  ZS.themeApply('dark');
  ok('切深色写入 body[data-theme]', doc.body.getAttribute('data-theme') === 'dark');
  ok('深色记忆写入 localStorage', sandbox.localStorage.getItem('zs-theme') === 'dark', String(sandbox.localStorage.getItem('zs-theme')));
  ok('主题按钮 aria-label 跟随', /深色/.test(one('#btnTheme').getAttribute('aria-label') || ''), one('#btnTheme').getAttribute('aria-label'));
  ZS.themeToggle();
  ok('再点一次 → 跟随系统（matchMedia 返回浅色）', doc.body.getAttribute('data-theme') === 'light' && sandbox.localStorage.getItem('zs-theme') === 'system');
  ok('命令面板动作 8 条', ZS.paletteActions().length === 8, String(ZS.paletteActions().length));
  doc.dispatchEvent(makeEvent('keydown', { key: 'k', ctrlKey: true, metaKey: false }));
  ok('Ctrl+K 打开命令面板', one('#palette').hidden === false);
  ok('面板列出 8 条动作', getEls('#paletteList [data-palette]').length === 8, String(getEls('#paletteList [data-palette]').length));
  one('#paletteInput').value = '分析';
  one('#paletteInput').dispatchEvent(makeEvent('input'));
  ok('输入「分析」过滤到 1 条', getEls('#paletteList [data-palette]').length === 1, String(getEls('#paletteList [data-palette]').length));
  doc.dispatchEvent(makeEvent('keydown', { key: 'Escape' }));
  ok('Esc 关闭命令面板', one('#palette').hidden === true);

  console.log('== v0.7.0 空态与失败降级 ==');
  ok('仪表盘空数据 → 空态块', /zempty/.test(ZS.zoneRenderDashboard(null)));
  ok('分析页空数据 → 空态块', /zempty/.test(ZS.zoneRenderAnalytics(null)));
  ok('趋势图空数据 → 空态', /zempty/.test(ZS.zoneFlowSvg([])));
  zoneFail = true;
  one('#btnZoneRefresh').click();
  await sleep(150);
  ok('接口失败 → 错误块带重试按钮', !!one('#zoneBody .zerror') && /重试/.test(one('#zoneBody').textContent));
  ok('失败时时间戳改为读取失败', /读取失败/.test(one('#zoneStamp').textContent), one('#zoneStamp').textContent);
  zoneFail = false;
  ZS.showView('home');
  await sleep(20);


  console.log('== v0.7.1 账户页 ==');
  const navNames = getEls('#topNav .topnav__btn').map(function (b) { return b.getAttribute('data-nav'); }).join(',');
  ok('顶栏七个入口且含账户', navNames === 'home,accounts,zone,skills,plugins,services,settings', navNames);
  ok('更多菜单含账户入口', !!one('#moreMenu [data-nav="accounts"]'));
  ZS.showView('accounts');
  await sleep(120);
  ok('切到账户页', ZS.state.view === 'accounts' && one('#scr-accounts').classList.contains('is-on'), ZS.state.view);
  ok('顶栏标题为账户', one('#pageTitle').textContent === '账户', one('#pageTitle').textContent);
  ok('账户页标题', /账本|账户/.test(one('#accH1').textContent), one('#accH1').textContent);
  const accKpis = getEls('#accBody .zkpi');
  ok('四张账户 KPI', accKpis.length === 4, String(accKpis.length));
  ok('KPI 账户余额 ¥57,844.50', /¥57,844\.50/.test(one('#accBody').textContent), one('#accBody').textContent.slice(0, 120));
  ok('KPI 信用卡待还 ¥8,432.00', /¥8,432\.00/.test(one('#accBody').textContent));
  ok('KPI 额度使用率 28.1%', /28\.1%/.test(one('#accBody').textContent));
  const acards = getEls('#accBody .acard');
  ok('三张账户卡', acards.length === 3, String(acards.length));
  ok('账户卡带卡片状态徽章', getEls('#accBody .acard .abadge--ok').length >= 2, String(getEls('#accBody .acard .abadge--ok').length));
  ok('信用卡卡显示额度占比', /已用 28\.1% 额度/.test(one('#accBody').textContent));
  ok('账户卡有余额条', getEls('#accBody .abar').length >= 3, String(getEls('#accBody .abar').length));
  ok('信用卡区有还款日 25 日信息', /还款日/.test(one('#accBody').textContent) && /25/.test(ZS.accDueInfo().text), ZS.accDueInfo().text);
  ok('信用卡区有本期消费', /本期消费/.test(one('#accBody').textContent));
  const txRows = getEls('#accBody .ztable tbody tr');
  ok('流水默认每页 20 行', txRows.length === 20, String(txRows.length));
  ok('流水分页信息与总数', /共 26 笔/.test(one('#accBody').textContent), one('#accBody .zpager') ? one('#accBody .zpager').textContent : 'no pager');
  const txFilterCredit = one('#accBody [data-acc-filter="a2"]');
  ok('流水区有信用卡筛选按钮', !!txFilterCredit);
  txFilterCredit.click();
  await sleep(120);
  const creditRows = getEls('#accBody .ztable tbody tr');
  ok('筛信用卡后只剩该账户行', creditRows.length > 0 && creditRows.length < 20, String(creditRows.length));
  ok('筛信用卡后仍显示筛选态', one('#accBody .aseg__btn[data-acc-filter="a2"]').classList.contains('is-on'));
  one('#accBody .aseg__btn[data-acc-filter="all"]').click();
  await sleep(120);
  ok('切回全部后有 26 笔', /共 26 笔/.test(one('#accBody').textContent), one('#accBody').textContent.slice(0, 60));
  ZS.accSetPage(1);
  await sleep(40);
  ok('翻到第 2 页', /第 2\//.test(one('#accBody').textContent));
  ok('第 2 页只剩 6 行', getEls('#accBody .ztable tbody tr').length === 6, String(getEls('#accBody .ztable tbody tr').length));
  ZS.accSetPage(0);
  await sleep(40);
  ZS.accExportCsv();
  await sleep(40);
  ok('导出 CSV 有提示', /已导出/.test(one('#toasts').textContent), one('#toasts').textContent.slice(0, 80));
  const prefillBtn = one('#accBody [data-acc-text]');
  ok('账户页有预填对话按钮', !!prefillBtn);
  prefillBtn.click();
  await sleep(40);
  ok('预填后回到工作台', ZS.state.view === 'home', ZS.state.view);
  ok('预填内容进入输入框', /还信用卡/.test(one('#input').value), one('#input').value);
  const accRoute = ZS.zoneParseHash('#/accounts');
  ok('hash #/accounts 解析', accRoute && accRoute.view === 'accounts', JSON.stringify(accRoute));
  const accRoute2 = ZS.zoneParseHash('#/zone/dashboard');
  ok('hash 仍能解析专区', accRoute2 && accRoute2.view === 'zone' && accRoute2.zone === 'dashboard', JSON.stringify(accRoute2));
  zoneFail = true;
  ZS.state.acc.at = 0;
  ZS.showView('accounts');
  await sleep(150);
  ok('账户接口失败 → 错误块带重试', !!one('#accBody .zerror') && /重试/.test(one('#accBody').textContent));
  ok('失败时时间戳改为读取失败', /读取失败/.test(one('#accStamp').textContent), one('#accStamp').textContent);
  zoneFail = false;
  one('#input').value = '';
  ZS.state.acc = null;
  ZS.showView('home');
  await sleep(20);

  if (process.env.ZS_DUMP_CLASSES) {
    const seen = {};
    walk(doc.body, function (el) {
      const cls = el.getAttribute ? el.getAttribute('class') : null;
      if (cls) cls.split(/\s+/).forEach(function (c) { if (c) seen[c] = 1; });
    });
    require('fs').writeFileSync(process.env.ZS_DUMP_CLASSES, Object.keys(seen).sort().join('\n'));
  }
  console.log('== 汇总 ==');
  report();
})();

function report() {
  console.log('');
  console.log('PASS = ' + PASS + '   FAIL = ' + FAIL);
  if (FAILS.length) {
    console.log('失败项：');
    FAILS.forEach(function (f) { console.log('  - ' + f); });
  }
  process.exitCode = FAIL ? 1 : 0;
}