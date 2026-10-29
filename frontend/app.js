/* ZeroStep 前端 app.js —— 零依赖，原生 ES2020。
 * 结构：常量 / state / 工具函数 / api / 视图 / 账户 / 任务 / 对话渲染 / 轨迹 / 审批 / 撤销 / 审计 / 技能与服务 / 模式 / 菜单 / 事件 / 启动
 */
(function () {
  'use strict';

  // ===================== 常量 =====================
  var RISK_META = {
    low: { label: '低风险 · 自动执行', short: '低', cls: 'risk--low' },
    medium: { label: '中风险 · 需确认', short: '中', cls: '' },
    high: { label: '高风险 · 需动态码', short: '高', cls: 'risk--high' }
  };
  var SOURCE_LABEL = { rule: '规则', llm: '模型', fallback: '规则回落' };
  var MODE_LABEL = { rule: '规则', auto: '自动', llm: '模型' };
  var MODE_HINT = {
    rule: '只走确定性规则，结果可复现',
    auto: '有 Key 走模型，无 Key 回落规则',
    llm: '强制模型解析意图与槽位'
  };
  var EVENT_LABEL = {
    tool_call: '工具调用', tool_result: '工具返回', approval_request: '审批请求',
    approval_granted: '审批通过', approval_rejected: '审批拒绝', approval_consumed: '审批单次核销',
    undo: '撤销执行', response: '应答生成', session_start: '会话建立',
    intent: '意图识别', plan: '执行计划', blocked: '风控拦截', policy_gate: '风控判定'
  };
  var TOOL_LABEL = {
    get_accounts: '账户查询', get_recent_transactions: '近期流水', get_bill_summary: '账单汇总',
    budget_analysis: '预算分析', get_payees: '收款人校验', create_transfer: '发起转账',
    purchase_wealth: '申购理财', list_wealth: '理财持仓', list_cards: '卡片列表',
    apply_virtual_card: '申请虚拟卡', set_card_status: '冻结/解冻卡', list_subscriptions: '订阅查询',
    cancel_subscription: '取消订阅', create_reminder: '创建提醒', repay_credit: '信用卡还款',
    fx_convert: '汇率换算',
    redeem_wealth: '赎回理财', set_card_limit: '调整卡片限额', card_spend: '卡片消费',
    scan_anomalies: '异常交易扫描', annual_report: '年度账单报告',
    create_scheduled_transfer: '创建预约转账', list_scheduled_transfers: '查看预约转账',
    cancel_scheduled_transfer: '取消预约转账'
  };
  var DEFAULT_TASKS = [
    { id: 't01', title: '查本月支出前三类', category: '账单', user_input: '查一下这个月支出最多的三类' },
    { id: 't02', title: '餐饮支出明细', category: '账单', user_input: '这个月餐饮花了多少' },
    { id: 't03', title: '近三个月账单汇总', category: '账单', user_input: '汇总近三个月的账单' },
    { id: 't04', title: '预算超支检查', category: '账单', user_input: '帮我看看预算超了没有' },
    { id: 't05', title: '工资卡余额', category: '账户', user_input: '查一下工资卡余额' },
    { id: 't06', title: '转 800 元给妈妈', category: '转账', user_input: '给妈妈转 800 元' },
    { id: 't07', title: '转 2000 元给李明', category: '转账', user_input: '给李明转 2000 元' },
    { id: 't08', title: '申购 1000 元稳利宝', category: '理财', user_input: '帮我申购 1000 元稳利宝' },
    { id: 't09', title: '理财持仓与收益', category: '理财', user_input: '我的理财收益怎么样' },
    { id: 't10', title: '银行卡列表', category: '卡片', user_input: '我有哪些银行卡' },
    { id: 't11', title: '冻结信用卡', category: '卡片', user_input: '把我的信用卡冻结' },
    { id: 't12', title: '申请虚拟卡', category: '卡片', user_input: '帮我申请一张虚拟卡' },
    { id: 't13', title: '查自动续费订阅', category: '订阅', user_input: '我订了哪些会员' },
    { id: 't14', title: '取消爱奇艺会员', category: '订阅', user_input: '取消爱奇艺会员' },
    { id: 't15', title: '订阅月支出统计', category: '订阅', user_input: '我每月订阅一共花多少钱' },
    { id: 't16', title: '工资到账分账', category: '组合', user_input: '发工资了，3000 存到储蓄卡，2000 买稳利宝，1000 还信用卡' },
    { id: 't17', title: '生日转账并提醒', category: '组合', user_input: '下周三我妈生日，转 800 给她，再定个提醒' },
    { id: 't18', title: '旅行预算测算', category: '组合', user_input: '去日本旅行要花多少钱，帮我做个预算' },
    { id: 't19', title: '赎回 500 元稳利宝', category: '理财', user_input: '赎回 500 元稳利宝' },
    { id: 't20', title: '今年的年度账单', category: '账单', user_input: '看一下今年的年度账单' },
    { id: 't21', title: '3 天后给妈妈转 800 元', category: '转账', user_input: '3 天后给妈妈转 800 元' },
    { id: 't22', title: '还信用卡 8,432 元', category: '卡片', user_input: '还信用卡 8432 元' }
  ];
  var SAMPLES = [
    '查一下这个月支出最多的三类',
    '给妈妈转 8 元',
    '看看有没有正在自动续费的订阅',
    '赎回 500 元稳利宝',
    '看一下今年的年度账单',
    '3 天后给妈妈转 800 元'
  ];
  var PAGE_TITLES = { home: '工作台', chat: '工作台', zone: '专区', skills: '技能', plugins: '插件', services: '外部服务', settings: '设置' };
  var PUSH_KIND = {
    subscription: { label: '订阅扣费', hint: '会员即将自动续费' },
    large_txn: { label: '大额支出', hint: '单笔支出超过阈值' },
    repayment: { label: '还款到期', hint: '信用卡还款日临近' }
  };

  // ===================== 状态 =====================
  var state = {
    sessionId: null, threadId: null, user: null,
    accounts: [], tasks: [], sampleUsed: false, acctRefreshing: false,
    llmMode: 'auto', llmStatus: null, modeBusy: false, llmTesting: false,
    busy: false, lastUserText: '', lastIntentSource: '',
    view: 'home',
    traceRows: 0, traceStatus: 'idle',
    currentTurn: null,
    approval: null, approvalTimer: null, approvalExpiresAt: 0, approvalSubmitting: false,
    auditEntries: [], auditHash: '', auditLocked: false, auditTimers: [],
    undo: null, retry: null,
    pages: { skills: false, plugins: false, services: false, settings: false },
    tasksAll: [], taskPage: 0, taskTotal: 0, taskBusy: false,
    plugins: [], pluginKinds: null, pluginOpenId: null, pluginBusy: false,
    pushKind: 'subscription', pushBusy: false,
    approvalFactor: 'totp', approvalSms: { code: '', expiresAt: 0, busy: false },
    approvalFace: { token: '', expiresAt: 0, busy: false, error: '' },
    lockUntil: 0, lockTimer: null, lockActive: false,
    lastAction: null,
    streaming: false, streamCancelRequested: false, streamTurnId: null,
    hashReady: false, theme: 'light',
    zone: {
      view: 'dashboard', dash: null, dashAt: 0, dashBusy: false,
      ana: null, anaAt: 0, anaBusy: false,
      preset: '180d', from: '', to: '', granularity: 'month',
      sortKey: 'cents', sortDir: -1, txPage: 0, txPer: 20,
      pollTimer: null, ignoreHash: false, sessionWait: null,
      paletteActions: [], paletteQuery: '', paletteIndex: 0
    }
  };
  var dom = {};

  // ===================== 工具函数 =====================
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function fmtRich(text) {
    var h = esc(text);
    h = h.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    h = h.replace(/`([^`]+)`/g, '<code>$1</code>');
    return h.replace(/\n/g, '<br>');
  }
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function fmtMoney(cents) {
    if (cents == null || isNaN(Number(cents))) return '¥0.00';
    var v = Number(cents) / 100;
    var neg = v < 0;
    var abs = Math.abs(v).toFixed(2);
    var parts = abs.split('.');
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return (neg ? '-¥' : '¥') + parts[0] + '.' + parts[1];
  }
  function fmtMoneyPlain(cents) {
    var s = fmtMoney(cents);
    return s.indexOf('¥') === 0 ? s.slice(1) : s;
  }
  function fmtTime(iso) {
    var d = iso ? new Date(iso) : new Date();
    if (isNaN(d.getTime())) d = new Date();
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }
  function fmtCountdown(sec) {
    if (!isFinite(sec) || sec < 0) sec = 0;
    var s = Math.floor(sec);
    var m = Math.floor(s / 60);
    return pad2(m) + ':' + pad2(s % 60);
  }
  function truncate(s, n) {
    s = String(s == null ? '' : s);
    return s.length > n ? s.slice(0, n) + '…' : s;
  }
  function riskMeta(level) { return RISK_META[level] || RISK_META.medium; }
  function sourceLabel(src) { return SOURCE_LABEL[src] || '规则'; }
  function eventLabel(type) { return EVENT_LABEL[type] || (type || '事件'); }
  function toolLabel(tool) { return TOOL_LABEL[tool] || tool || 'tool'; }
  function modeLabel(m) { return MODE_LABEL[m] || '自动'; }
  function parseApiError(data) {
    if (!data) return null;
    if (typeof data === 'string') return data;
    if (data.message) return data.message;
    if (data.error) {
      if (typeof data.error === 'string') return data.error;
      return data.error.message || data.error.code || null;
    }
    return null;
  }
  function errCode(e) { return (e && (e.code || e.name)) || 'ERROR'; }

  // ===================== DOM 缓存 =====================
  function cacheDom() {
    var ids = [
      'brand', 'topNav', 'modeSeg', 'modeRule', 'modeAuto', 'modeLlm', 'modeHint', 'modeBadge',
      'btnAudit', 'btnMore', 'userName', 'device',
      'acctTag', 'acctTotal', 'acctDelta', 'acctList', 'acctMix', 'btnTasksRefresh', 'chips',
      'stageScroll', 'samples', 'chat', 'chatEmpty',
      'errorBar', 'errorText', 'errorRetry', 'errorClose',
      'input', 'btnMic', 'btnSend', 'lockBar', 'lockBarText',
      'tracePanel', 'btnTraceToggle', 'traceMeta', 'traceBody', 'traceList',
      'skillsGrid', 'skillsInfoBar', 'btnSkillsRefresh',
      'servicesGrid', 'servicesInfoBar', 'btnServicesRefresh',
      'pluginsGrid', 'pluginsInfoBar', 'btnPluginsRefresh', 'pluginsSummary',
      'pluginsError', 'pluginsErrorText', 'pluginsErrorClose',
      'btnChannelRefresh', 'channelTag', 'channelEnabled', 'channelPort', 'channelToken', 'channelSessions', 'channelNote',
      'healthTag', 'healthVersion', 'healthDemo', 'healthMode', 'healthSession',
      'llmCard', 'llmTag', 'llmProvider', 'llmModel', 'llmHost', 'llmKey', 'llmEffective', 'llmBreaker',
      'llmStats', 'llmStatsText', 'btnLlmTest', 'llmTestResult', 'llmNote',
      'approvalSheet', 'approvalPanel', 'apRisk', 'apCountdown', 'apAmount', 'apTitle', 'apRows',
      'apSource', 'apConflicts', 'apConflictList', 'dlgFactors', 'dlgFactorTotp', 'dlgFactorSms', 'dlgFactorFace',
      'apOtp', 'apOtpLabel', 'otpCells', 'otpKeypad', 'btnDemoTotp', 'dlgSmsSend', 'dlgSmsCode',
      'dlgFace', 'dlgFaceScan', 'dlgFaceState', 'dlgFaceToken',
      'apError', 'apReasons', 'apPreview', 'btnReject', 'btnApprove',
      'auditSheet', 'auditTitle', 'btnAuditExport', 'btnAuditRefresh', 'btnAuditClose',
      'auditValid', 'auditMeta', 'auditList',
      'moreMenu', 'modal', 'modalTitle', 'modalBody', 'modalActions',
      'pendingPill', 'pendingPillTime', 'toasts',
      'pageTitle', 'tabbar', 'menuModeWrap',
      'btnTheme', 'btnZoneRefresh', 'zoneBody', 'zoneStamp', 'zoneH1', 'zoneSub',
      'palette', 'paletteInput', 'paletteList',
      'pushCard', 'pushTag', 'pushKinds', 'btnPushPreview', 'pushStatus',
      'pushResult', 'pushTitle', 'pushText', 'pushMeta', 'pushNote'
    ];
    ids.forEach(function (id) { dom[id] = document.getElementById(id); });
    dom.otpInputs = dom.otpCells ? Array.prototype.slice.call(dom.otpCells.querySelectorAll('.otp__cell')) : [];
    dom.modeBtns = Array.prototype.slice.call(document.querySelectorAll('[data-mode]'));
    dom.navBtns = Array.prototype.slice.call(document.querySelectorAll('[data-nav]'));
  }

  // ===================== API =====================
  function parseErrorMessage(data, res) {
    var m = parseApiError(data);
    if (m) return m;
    return '请求失败（HTTP ' + res.status + '）';
  }
  async function request(path, options) {
    options = options || {};
    var init = { method: options.method || 'GET', headers: {} };
    if (options.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(options.body);
    }
    var res = await fetch(path, init);
    var data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      var err = new Error(parseErrorMessage(data, res));
      err.code = (data && data.error && (data.error.code || data.error)) || ('HTTP_' + res.status);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }
  function getJson(path) { return request(path); }
  function postJson(path, body) { return request(path, { method: 'POST', body: body === undefined ? {} : body }); }

  // --- SSE ---
  function dispatchBlock(raw) {
    var event = 'message', dataLines = [];
    raw.split(/\r?\n/).forEach(function (line) {
      if (!line || line.charAt(0) === ':') return;
      var i = line.indexOf(':');
      var field = i === -1 ? line : line.slice(0, i);
      var value = i === -1 ? '' : line.slice(i + 1);
      if (value.charAt(0) === ' ') value = value.slice(1);
      if (field === 'event') event = value;
      else if (field === 'data') dataLines.push(value);
    });
    if (!dataLines.length) return null;
    var payload = dataLines.join('\n');
    var data = null;
    try { data = JSON.parse(payload); } catch (e) { data = payload; }
    return { event: event, data: data };
  }
  async function streamChat(path, body, onEvent) {
    var res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
      body: JSON.stringify(body)
    });
    if (!res.ok) {
      var data = null;
      try { data = await res.json(); } catch (e) { data = null; }
      var err = new Error(parseErrorMessage(data, res));
      err.code = (data && data.error && (data.error.code || data.error)) || ('HTTP_' + res.status);
      err.status = res.status;
      throw err;
    }
    var ct = res.headers.get('content-type') || '';
    if (ct.indexOf('text/event-stream') === -1 || !res.body) {
      var full = await res.json();
      onEvent({ event: '__result__', data: full });
      return;
    }
    var reader = res.body.getReader();
    var decoder = new TextDecoder('utf-8');
    var buf = '';
    for (;;) {
      var chunk = await reader.read();
      if (chunk.done) break;
      buf += decoder.decode(chunk.value, { stream: true });
      var idx;
      while ((idx = buf.indexOf('\n\n')) !== -1) {
        var raw = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        var ev = dispatchBlock(raw);
        if (ev) onEvent(ev);
      }
    }
    buf += decoder.decode();
    if (buf.trim()) {
      var last = dispatchBlock(buf);
      if (last) onEvent(last);
    }
  }

  // ===================== 轻提示 / 弹层 =====================
  function toast(message, opts) {
    opts = opts || {};
    if (!dom.toasts) return { close: function () {} };
    var el = document.createElement('div');
    el.className = 'toast' + (opts.type ? ' toast--' + opts.type : '');
    var html = '<button class="toast__close" type="button" aria-label="关闭">×</button>' +
      '<div class="toast__text">' + esc(message) + '</div>';
    if (opts.sub) html += '<div class="toast__sub">' + esc(opts.sub) + '</div>';
    if (opts.actionLabel) html += '<div class="toast__actions"><button class="toast__action" type="button">' + esc(opts.actionLabel) + '</button></div>';
    if (opts.duration) html += '<span class="toast__bar" style="animation-duration:' + opts.duration + 'ms"></span>';
    el.innerHTML = html;
    dom.toasts.appendChild(el);
    var timer = null;
    function close() {
      if (!el.parentNode) return;
      if (timer) clearTimeout(timer);
      el.parentNode.removeChild(el);
    }
    function arm(ms) {
      if (timer) clearTimeout(timer);
      timer = setTimeout(close, ms);
    }
    el.querySelector('.toast__close').addEventListener('click', close);
    var actionEl = el.querySelector('.toast__action');
    if (actionEl) actionEl.addEventListener('click', function () { if (opts.onAction) opts.onAction(); });
    if (opts.duration) arm(opts.duration);
    else if (opts.type !== 'error') arm(5200);
    return {
      el: el, close: close, arm: arm,
      setText: function (t) { var n = el.querySelector('.toast__text'); if (n) n.textContent = t; },
      setSub: function (t) { var n = el.querySelector('.toast__sub'); if (n) n.textContent = t; },
      disableAction: function () { if (actionEl) { actionEl.disabled = true; actionEl.textContent = '处理中…'; } },
      enableAction: function (t) { if (actionEl) { actionEl.disabled = false; actionEl.textContent = t || '撤销'; } }
    };
  }
  function showModal(title, bodyHtml, actions) {
    if (!dom.modal) return;
    dom.modalTitle.textContent = title;
    dom.modalBody.innerHTML = bodyHtml;
    dom.modalActions.innerHTML = '';
    (actions || []).forEach(function (a) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = a.primary ? 'btn-primary' : 'btn-secondary';
      btn.textContent = a.label;
      btn.addEventListener('click', function () { closeModal(); if (a.onClick) a.onClick(); });
      dom.modalActions.appendChild(btn);
    });
    dom.modal.hidden = false;
  }
  function closeModal() { if (dom.modal) dom.modal.hidden = true; }
  function closeMenu() { if (dom.moreMenu) dom.moreMenu.hidden = true; }
  function anyLayerOpen() {
    return (dom.approvalSheet && !dom.approvalSheet.hidden) ||
      (dom.auditSheet && !dom.auditSheet.hidden) ||
      (dom.modal && !dom.modal.hidden) ||
      (dom.moreMenu && !dom.moreMenu.hidden);
  }
  function closeTopLayer() {
    if (dom.moreMenu && !dom.moreMenu.hidden) { closeMenu(); return true; }
    if (dom.modal && !dom.modal.hidden) { closeModal(); return true; }
    if (dom.approvalSheet && !dom.approvalSheet.hidden) { closeApprovalSheet(); return true; }
    if (dom.auditSheet && !dom.auditSheet.hidden) { closeAudit(); return true; }
    return false;
  }

  // ===================== 错误条 =====================
  function showError(message, retryFn) {
    if (!dom.errorBar) return;
    dom.errorText.textContent = message || '网络异常，请稍后重试';
    dom.errorBar.hidden = false;
    state.retry = retryFn || null;
    dom.errorRetry.style.display = retryFn ? '' : 'none';
  }
  function hideError() {
    if (dom.errorBar) dom.errorBar.hidden = true;
    state.retry = null;
  }

  // ===================== 视图切换 =====================
  function showView(name) {
    if (!name) name = 'home';
    state.view = name;
    document.body.setAttribute('data-view', name);
    var isHome = name === 'home' || name === 'chat';
    var homeEl = document.getElementById('view-home');
    var chatEl = document.getElementById('view-chat');
    if (homeEl) homeEl.classList.toggle('is-on', name === 'home');
    if (chatEl) chatEl.classList.toggle('is-on', name === 'chat');
    var screens = { zone: 'scr-zone', skills: 'scr-skills', plugins: 'scr-plugins', services: 'scr-services', settings: 'scr-settings' };
    Object.keys(screens).forEach(function (key) {
      var el = document.getElementById(screens[key]);
      if (el) el.classList.toggle('is-on', name === key);
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-nav]'), function (btn) {
      btn.classList.toggle('is-on', btn.getAttribute('data-nav') === (isHome ? 'home' : name));
    });
    if (dom.topNav) dom.topNav.classList.toggle('is-on', false);
    if (dom.pageTitle) dom.pageTitle.textContent = PAGE_TITLES[name] || '工作台';
    closeMenu();
    if (name === 'zone') zoneEnter();
    else if (name === 'skills') loadSkills();
    else if (name === 'plugins') loadPlugins();
    else if (name === 'services') loadServices();
    else if (name === 'settings') loadSettings();
    else zoneStopPoll();
    if (state.hashReady) {
      if (name === 'zone') zoneSyncHash();
      else zoneSetHash('#/' + (isHome ? 'home' : name));
    }
    if (dom.stageScroll) dom.stageScroll.scrollTop = 0;
    // 整页视图（技能/插件/外部服务/设置）不走 .shell 滚动容器，需要把页面本身拉回顶部，
    // 否则从工作台下滚后切到插件页会停在中间。
    if (!isHome && typeof window.scrollTo === 'function') {
      try { window.scrollTo(0, 0); } catch (e) { /* 忽略无法滚动的环境 */ }
    }
  }

  // ===================== 账户 / 任务 =====================
  function renderBalance(total) {
    if (!dom.acctTotal) return;
    var text = fmtMoneyPlain(total);
    var html = '';
    for (var i = 0; i < text.length; i++) {
      var ch = text.charAt(i);
      html += '<span class="dg" style="--i:' + i + '">' + esc(ch) + '</span>';
    }
    dom.acctTotal.innerHTML = html;
    dom.acctTotal.classList.remove('is-in');
    void dom.acctTotal.offsetWidth;
    dom.acctTotal.classList.add('is-in');
  }
  // 后端返回的账户类型字段叫 kind（不是 type），旧代码按 type 判断会全部落到「其它」，
  // 导致信用卡被算进存款、资产构成条永远没有理财段。这里统一取 kind，兼容 type。
  function acctKind(a) { return (a && (a.kind || a.type)) || ''; }
  function renderAccounts(session) {
    var accounts = (session && session.accounts) || [];
    state.accounts = accounts;
    if (session && session.user && dom.userName) {
      dom.userName.textContent = (session.user.name || '张').slice(0, 1);
      dom.userName.title = (session.user.name || '') + (session.user.risk_level ? ' · ' + session.user.risk_level : '');
    }
    var total = 0, credit = 0;
    accounts.forEach(function (a) {
      total += Number(a.balance_cents || 0);
      if (acctKind(a) === 'credit') credit += Math.abs(Number(a.balance_cents || 0));
    });
    renderBalance(total);
    if (dom.acctTag) dom.acctTag.textContent = '演示账户';
    if (dom.acctDelta) {
      var parts = [accounts.length + ' 个账户'];
      if (credit > 0) parts.push('信用卡待还 ' + fmtMoney(credit));
      dom.acctDelta.innerHTML = parts.map(function (p) { return '<span>' + esc(p) + '</span>'; }).join('<i class="acct__dot"></i>');
    }
    if (dom.acctList) {
      var typeName = { checking: '活期', savings: '储蓄', wealth: '理财', credit: '信用卡' };
      var rows = accounts.map(function (a) {
        var bal = Number(a.balance_cents || 0);
        return '<div class="acct__row"><span>' + esc(typeName[acctKind(a)] || a.name || '账户') + ' · ' + esc(a.number_masked || '') + '</span>' +
          '<span class="v">' + esc(fmtMoney(bal)) + '</span></div>';
      });
      // 理财持仓不是银行账户行，但它是「钱去哪了」的一部分，单独补一行，避免申购后看不到去向
      var holdings = (session && session.wealth && session.wealth.holding_total_cents) || 0;
      if (holdings > 0) {
        rows.push('<div class="acct__row acct__row--wealth"><span>理财 · 持仓</span>' +
          '<span class="v">' + esc(fmtMoney(holdings)) + '</span></div>');
      }
      dom.acctList.innerHTML = rows.join('');
    }
    renderMix(accounts, (session && session.wealth && session.wealth.holding_total_cents) || 0);
  }
  function renderMix(accounts, holdingsCents) {
    if (!dom.acctMix) return;
    var dep = 0, wealth = Number(holdingsCents || 0), credit = 0;
    accounts.forEach(function (a) {
      var v = Math.abs(Number(a.balance_cents || 0));
      var kind = acctKind(a);
      if (kind === 'credit') credit += v;
      else if (kind === 'wealth') wealth += v;
      else dep += v;
    });
    var sum = dep + wealth + credit;
    if (!sum) { dom.acctMix.innerHTML = ''; return; }
    var w1 = Math.max(2, Math.round(dep / sum * 1000) / 10);
    var w2 = Math.max(2, Math.round(wealth / sum * 1000) / 10);
    var w3 = Math.max(2, Math.round(credit / sum * 1000) / 10);
    dom.acctMix.innerHTML = '<i style="width:' + w1 + '%"></i><i style="width:' + w2 + '%"></i><i style="width:' + w3 + '%"></i>';
  }
  // 资金变动后重新拉一次会话快照，让左侧「可用余额 / 账户 / 资产构成」跟着操作走。
  // 只读接口，不触发任何写操作；失败时保持旧数字并提示，不阻塞对话。
  async function refreshAccountSnapshot() {
    if (!state.sessionId || state.acctRefreshing) return;
    state.acctRefreshing = true;
    try {
      var s = await getJson('/api/session/' + encodeURIComponent(state.sessionId));
      if (s && s.accounts) renderAccounts(s);
    } catch (e) {
      // 静默失败：下一次回合结束会再试
    } finally {
      state.acctRefreshing = false;
    }
  }
  function renderChips(tasks) {
    if (!dom.chips) return;
    var list = (tasks && tasks.length ? tasks : DEFAULT_TASKS).slice(0, 6);
    dom.chips.innerHTML = list.map(function (t) {
      var text = t.user_input || t.prompt || t.title || '';
      return '<button class="chip" type="button" data-text="' + esc(text) + '">' + esc(truncate(t.title || text, 16)) + '</button>';
    }).join('');
  }
  function renderSamples() {
    if (!dom.samples || state.sampleUsed) return;
    var label = dom.samples.querySelector('.samples__label');
    var html = '';
    SAMPLES.forEach(function (s, i) {
      html += '<button class="sample" type="button" data-text="' + esc(s) + '" style="--d:' + (i * 6) + 'ms">' + esc(s) + '</button>';
    });
    dom.samples.innerHTML = '';
    if (label) dom.samples.appendChild(label);
    dom.samples.insertAdjacentHTML('beforeend', html);
    state.sampleUsed = true;
  }

  // ===================== 对话渲染 =====================
  function scrollChat() {
    if (dom.stageScroll) dom.stageScroll.scrollTop = dom.stageScroll.scrollHeight;
  }
  function removeEmptyState() {
    if (dom.chatEmpty && dom.chatEmpty.parentNode) {
      dom.chatEmpty.parentNode.removeChild(dom.chatEmpty);
      dom.chatEmpty = null;
    }
  }
  function makeThinking() {
    var el = document.createElement('div');
    el.className = 'thinking';
    el.innerHTML = '<span class="thinking__dots"><i></i><i></i><i></i></span><span class="thinking__label">正在解析指令…</span>';
    return el;
  }
  function beginTurn() {
    removeEmptyState();
    var root = document.createElement('div');
    root.className = 'turn';
    var head = document.createElement('div');
    head.className = 'turn__head';
    head.innerHTML = '<span class="turn__brand">ZeroStep</span><span class="turn__time">' + fmtTime() + '</span>' +
      '<span class="turn__tags"></span>';
    root.appendChild(head);
    var thinking = makeThinking();
    root.appendChild(thinking);
    if (dom.chat) dom.chat.appendChild(root);
    var turn = {
      root: root, head: head, tags: head.querySelector('.turn__tags'),
      thinking: thinking, intentEl: null, planEl: null, planSteps: {},
      msgEl: null, typer: null, auditEl: null, noteEl: null,
      status: 'running', done: false, replyText: '',
      auditCount: 0, actions: [], latency: null,
      answerEl: null, answerBody: null, answerCaret: null, answerRaw: '', answerChunks: {},
      answerNext: 0, answerSeen: false, answerGotDelta: false, insightEl: null
    };
    state.currentTurn = turn;
    traceReset('已接收指令 · 正在解析');
    scrollChat();
    return turn;
  }
  function dropThinking(turn) {
    if (turn && turn.thinking && turn.thinking.parentNode) {
      turn.thinking.parentNode.removeChild(turn.thinking);
      turn.thinking = null;
    }
  }
  function ensureTurn() { return state.currentTurn || beginTurn(); }
  function appendCard(turn, cls, html) {
    var card = document.createElement('div');
    card.className = 'card' + (cls ? ' ' + cls : '');
    card.innerHTML = html;
    if (turn.msgEl && turn.msgEl.parentNode === turn.root) turn.root.insertBefore(card, turn.msgEl);
    else turn.root.appendChild(card);
    return card;
  }
  function slotValue(v) {
    if (v === null || v === undefined || v === '') return '';
    if (Array.isArray(v)) return v.join(' / ');
    if (typeof v === 'object') {
      try { return JSON.stringify(v); } catch (e) { return String(v); }
    }
    return String(v);
  }
  function renderIntent(turn, intent) {
    if (!intent) return;
    dropThinking(turn);
    state.lastIntentSource = intent.source || 'rule';
    if (turn.tags) {
      turn.tags.innerHTML = '<span class="tag ' + (intent.source === 'llm' ? '' : 'tag--rule') + '">' + esc(sourceLabel(intent.source)) + '</span>';
    }
    var conf = null;
    if (intent.confidence != null) conf = Math.round(Number(intent.confidence) * (Number(intent.confidence) <= 1 ? 100 : 1));
    var slotsHtml = '';
    var slots = intent.slots || {};
    Object.keys(slots).forEach(function (k) {
      var val = slotValue(slots[k]);
      if (!val) return;
      slotsHtml += '<span class="slot">' + esc(k) + ' <b>' + esc(truncate(val, 40)) + '</b></span>';
    });
    var html = '<div class="card__head"><span class="card__title">意图</span>' +
      '<span class="card__meta">' + esc(sourceLabel(intent.source)) + (conf == null ? '' : ' · ' + conf + '%') + '</span></div>' +
      '<div class="intent__body"><div class="intent__line"><span class="intent__name">' + esc(intent.name || '未识别') + '</span>' +
      (intent.source === 'llm' ? '<span class="tag">模型</span>' : '<span class="tag tag--rule">规则</span>') +
      (intent.model ? '<span class="tag tag--muted tag--mono">' + esc(intent.model) + '</span>' : '') +
      '</div>' + (slotsHtml ? '<div class="slots">' + slotsHtml + '</div>' : '') + '</div>';
    if (!turn.intentEl) turn.intentEl = appendCard(turn, 'card--intent', html);
    else turn.intentEl.innerHTML = html;
    scrollChat();
  }
  function renderPlan(turn, plan) {
    if (!plan || !plan.steps) return;
    dropThinking(turn);
    var steps = plan.steps || [];
    var html = '<div class="card__head"><span class="card__title">执行计划</span>' +
      '<span class="card__meta">' + steps.length + ' 步</span></div><div class="plan__steps">';
    steps.forEach(function (s, i) {
      html += '<div class="plan__step" data-step="' + esc(s.step_id) + '">' +
        '<span class="plan__no">' + (i + 1) + '</span>' +
        '<div class="plan__body"><div class="plan__t">' + esc(s.title || '') + '</div>' +
        '<div class="plan__meta"><span class="plan__tool">' + esc(toolLabel(s.tool)) + '</span>' +
        '<span class="plan__state">待执行</span></div></div></div>';
    });
    html += '</div>';
    if (!turn.planEl) turn.planEl = appendCard(turn, 'card--plan', html);
    else turn.planEl.innerHTML = html;
    turn.planSteps = {};
    Array.prototype.forEach.call(turn.planEl.querySelectorAll('.plan__step'), function (el) {
      turn.planSteps[el.getAttribute('data-step')] = el;
    });
    traceReset('计划 ' + steps.length + ' 步 · 准备执行');
    scrollChat();
  }
  function setStepState(turn, stepId, stateName, text) {
    if (!turn) return;
    var el = turn.planSteps[stepId];
    if (!el) return;
    el.classList.remove('is-ok', 'is-fail');
    if (stateName === 'ok') el.classList.add('is-ok');
    if (stateName === 'fail') el.classList.add('is-fail');
    var st = el.querySelector('.plan__state');
    if (st) {
      st.className = 'plan__state' + (stateName === 'ok' ? ' plan__ok' : stateName === 'fail' ? ' plan__fail' : stateName === 'run' ? ' plan__run' : '');
      st.textContent = text || '';
    }
  }
  function createTyper(el) {
    var buf = '', shown = '', running = false, timer = null;
    function paint() { el.innerHTML = fmtRich(shown); }
    function pump() {
      if (!buf) { running = false; return; }
      var step = Math.min(3, buf.length);
      shown += buf.slice(0, step);
      buf = buf.slice(step);
      paint();
      scrollChat();
      timer = setTimeout(pump, 18);
    }
    return {
      push: function (text) {
        if (!text) return;
        buf += text;
        if (!running) { running = true; pump(); }
      },
      flush: function () {
        if (timer) clearTimeout(timer);
        shown += buf;
        buf = '';
        running = false;
        paint();
        scrollChat();
      },
      text: function () { return shown + buf; }
    };
  }
  function ensureMessage(turn) {
    if (turn.msgEl) return turn.msgEl;
    dropThinking(turn);
    var el = document.createElement('div');
    el.className = 'result';
    turn.root.appendChild(el);
    turn.msgEl = el;
    turn.typer = createTyper(el);
    return el;
  }
  function renderDelta(text) {
    var turn = ensureTurn();
    // 后端未发 answer_delta（关闭流式 / 老服务）：整段 message 仍显示在同一个气泡里
    if (turn.answerEl && !turn.answerGotDelta) {
      turn.replyText += text;
      turn.answerRaw += text;
      paintAnswer(turn);
      return;
    }
    ensureMessage(turn);
    turn.replyText += text;
    turn.typer.push(text);
    scrollChat();
  }

  // ---- v0.5.0：逐字流式回答（answer_start / answer_delta / answer_end）----
  function setSendMode(mode) {
    if (!dom.btnSend) return;
    var stop = mode === 'stop';
    dom.btnSend.classList.toggle('is-stop', stop);
    dom.btnSend.disabled = lockedNow() || mode === 'busy';
    dom.btnSend.textContent = stop ? '停止' : (mode === 'busy' ? '处理中…' : '发送');
    dom.btnSend.setAttribute('aria-label', stop ? '停止输出' : '发送');
  }
  function ensureAnswerBubble(turn) {
    if (!turn) return null;
    if (turn.answerEl) return turn.answerEl;
    dropThinking(turn);
    var el = document.createElement('div');
    el.className = 'result result--stream is-typing';
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    var body = document.createElement('span');
    body.className = 'answer__body';
    el.appendChild(body);
    var caret = document.createElement('i');
    caret.className = 'answer__caret';
    caret.setAttribute('aria-hidden', 'true');
    el.appendChild(caret);
    turn.root.appendChild(el);
    turn.answerEl = el;
    turn.answerBody = body;
    turn.answerCaret = caret;
    return el;
  }
  function paintAnswer(turn) {
    if (!turn || !turn.answerBody) return;
    turn.answerBody.textContent = turn.answerRaw;
    scrollChat();
  }
  function renderAnswerStart(data) {
    var turn = ensureTurn();
    data = data || {};
    ensureAnswerBubble(turn);
    if (!turn.answerSeen) {
      turn.answerSeen = true;
      turn.answerRaw = '';
      turn.answerChunks = {};
      turn.answerNext = 0;
    }
    turn.answerEl.classList.add('is-typing');
    if (data.turn_id) { state.streamTurnId = data.turn_id; turn.streamTurnId = data.turn_id; }
    if (data.thread_id) state.threadId = data.thread_id;
    state.streaming = true;
    state.streamCancelRequested = false;
    setSendMode('stop');
  }
  function renderAnswerDelta(data) {
    var turn = ensureTurn();
    data = data || {};
    if (!turn.answerSeen) renderAnswerStart({ turn_id: data.turn_id, thread_id: data.thread_id });
    var i = Number(data.i);
    if (!isFinite(i) || i < 0) i = turn.answerNext;
    var text = data.text != null ? data.text : (data.delta || '');
    turn.answerChunks[i] = String(text);
    turn.answerGotDelta = true;
    while (Object.prototype.hasOwnProperty.call(turn.answerChunks, turn.answerNext)) {
      turn.answerRaw += turn.answerChunks[turn.answerNext];
      delete turn.answerChunks[turn.answerNext];
      turn.answerNext += 1;
    }
    paintAnswer(turn);
  }
  function finishAnswerBubble(turn, data) {
    if (!turn || !turn.answerEl) return;
    var rest = Object.keys(turn.answerChunks || {});
    if (rest.length) {
      rest.sort(function (a, b) { return Number(a) - Number(b); });
      rest.forEach(function (k) { turn.answerRaw += turn.answerChunks[k]; });
      turn.answerChunks = {};
    }
    turn.answerEl.classList.remove('is-typing');
    if (turn.answerCaret && turn.answerCaret.parentNode) turn.answerCaret.parentNode.removeChild(turn.answerCaret);
    turn.answerCaret = null;
    if (turn.answerBody) turn.answerBody.innerHTML = fmtRich(turn.answerRaw);
    if (data && data.cancelled) {
      turn.answerEl.classList.add('is-cancelled');
      var stopped = document.createElement('span');
      stopped.className = 'answer__stopped';
      stopped.textContent = '已停止输出';
      turn.answerEl.appendChild(stopped);
    }
    if (data && data.elapsed_ms != null) turn.answerEl.setAttribute('data-elapsed', String(data.elapsed_ms));
    if (data && data.chunks != null) turn.answerEl.setAttribute('data-chunks', String(data.chunks));
  }
  function renderAnswerEnd(data) {
    data = data || {};
    state.streaming = false;
    finishAnswerBubble(state.currentTurn, data);
    setSendMode('idle');
    scrollChat();
  }
  async function cancelTurn() {
    if (!state.streaming || state.streamCancelRequested) return;
    state.streamCancelRequested = true;
    if (dom.btnSend) { dom.btnSend.disabled = true; dom.btnSend.textContent = '停止中…'; }
    try {
      var res = await postJson('/api/turn/cancel', { thread_id: state.threadId || '' });
      if (!res || res.cancelled !== true) {
        state.streaming = false;
        setSendMode(state.busy ? 'busy' : 'idle');
      }
    } catch (e) {
      state.streamCancelRequested = false;
      setSendMode('stop');
      toast('停止失败，请稍后重试', { type: 'error', sub: e.message || '' });
    }
  }
  // ---- v0.5.0：消费洞察卡 ----
  function fmtPct(n) {
    var v = Number(n);
    if (!isFinite(v)) return '—';
    var r = Math.round(v * 10) / 10;
    var s = String(Math.abs(r));
    if (s.indexOf('.') !== -1) s = s.replace(/0+$/, '').replace(/\.$/, '');
    return (r > 0 ? '+' : (r < 0 ? '-' : '')) + s + '%';
  }
  function insightSentence(action) {
    var a = action || {};
    if (a.kind === 'subscription_cancel' && a.merchant) return '取消 ' + a.merchant + ' 订阅';
    if (a.kind === 'query_bills' && a.category) return '查一下 ' + a.category + ' 分类的账单';
    return '';
  }
  function renderInsightCard(turn, data) {
    var ins = data && (data.insight || data.card) ? (data.insight || data.card) : data;
    if (!ins || !(ins.totals || ins.savings || ins.top_categories)) return;
    turn = turn || ensureTurn();
    dropThinking(turn);
    if (turn.insightEl && turn.insightEl.parentNode) turn.insightEl.parentNode.removeChild(turn.insightEl);
    var totals = ins.totals || {};
    var top = (ins.top_categories || [])[0] || {};
    var save = totals.potential_save_monthly_cents;
    var stats = [
      { label: '环比', value: fmtPct(totals.delta_pct), sub: '较上月支出' },
      { label: '最大分类', value: top.category || '—', sub: top.pct == null ? '本月最大支出' : fmtPct(top.pct).replace('+', '') + ' 占比' },
      { label: '可省', value: save == null ? '—' : fmtMoney(save), sub: '每月可优化' }
    ];
    var html = '<div class="insight__head"><span class="insight__title">本月的三件事</span>' +
      '<span class="insight__tag">只读洞察</span></div><div class="insight__stats">' +
      stats.map(function (s) {
        return '<div class="insight__stat"><span class="insight__stat-k">' + esc(s.label) + '</span>' +
          '<span class="insight__stat-v">' + esc(s.value) + '</span>' +
          '<span class="insight__stat-s">' + esc(s.sub) + '</span></div>';
      }).join('') + '</div>';
    var savings = ins.savings || [];
    if (savings.length) {
      html += '<div class="insight__save-head">还能省下的钱</div><ul class="insight__saves">' +
        savings.map(function (s) {
          var sentence = insightSentence(s.action);
          var btn = sentence
            ? '<button class="insight__go" type="button" data-insight-text="' + esc(sentence) + '">去处理</button>'
            : '';
          return '<li class="insight__save"><span class="insight__save-main">' +
            '<span class="insight__save-title">' + esc(s.title || s.detail || '可优化项') + '</span>' +
            '<span class="insight__save-note">' + esc(s.detail || '') + '</span></span>' +
            '<span class="insight__save-money">' + esc(fmtMoney(s.monthly_cents)) + '<i>/月</i></span>' + btn + '</li>';
        }).join('') + '</ul>';
    }
    var el = document.createElement('div');
    el.className = 'insight';
    el.innerHTML = html;
    turn.root.appendChild(el);
    turn.insightEl = el;
    scrollChat();
  }
  function renderAuditChip(data) {
    var turn = state.currentTurn;
    if (!turn) return;
    turn.auditCount += 1;
    if (!turn.auditEl) {
      turn.auditEl = document.createElement('div');
      turn.auditEl.className = 'turn__audit';
      turn.root.appendChild(turn.auditEl);
    }
    turn.auditEl.innerHTML = '<span class="auditdot"></span>已写入审计链 · <b>#' + esc(String(data.seq == null ? '' : data.seq)) + '</b>' +
      ' · 本次 ' + turn.auditCount + ' 条事件';
  }
  function renderNote(text) {
    var turn = state.currentTurn;
    if (!turn || !text) return;
    if (!turn.noteEl) {
      turn.noteEl = document.createElement('div');
      turn.noteEl.className = 'turn__note';
      turn.root.appendChild(turn.noteEl);
    }
    turn.noteEl.textContent = text;
  }
  function renderBlocked(turn, reasons) {
    dropThinking(turn);
    var list = (reasons && reasons.length ? reasons : ['该请求被风控拦截，未发生任何资金变动。']);
    var el = document.createElement('div');
    el.className = 'blocked';
    el.innerHTML = '<div class="blocked__title">' +
      '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l9 16H3l9-16zM12 9v5M12 17h.01"/></svg>操作已被拦截</div>' +
      '<ul>' + list.map(function (r) { return '<li>' + esc(r) + '</li>'; }).join('') + '</ul>' +
      '<div class="blocked__foot">未发生任何资金变动，审计链已记录拦截事件。</div>';
    turn.root.appendChild(el);
    scrollChat();
  }
  function renderErrorLine(message) {
    var turn = ensureTurn();
    var el = document.createElement('div');
    el.className = 'turn__error';
    el.textContent = message;
    turn.root.appendChild(el);
  }
  // ===================== 轨迹 =====================
  function traceReset(label) {
    state.traceRows = 0;
    state.traceStatus = 'running';
    state.traceMap = {};
    if (dom.traceList) dom.traceList.innerHTML = '';
    if (dom.traceMeta) dom.traceMeta.innerHTML = '<b>进行中</b> · ' + esc(label || '等待指令');
  }
  function traceIdle() {
    state.traceStatus = 'idle';
    state.traceMap = {};
    if (dom.traceList) dom.traceList.innerHTML = '<div class="trace__empty">发出指令后，每一步工具调用逐条显示，并同步写入审计链。</div>';
    if (dom.traceMeta) dom.traceMeta.textContent = '还没有执行记录';
  }
  function traceStart(stepId, title, tool) {
    if (!dom.traceList) return;
    var empty = dom.traceList.querySelector('.trace__empty');
    if (empty) empty.remove();
    state.traceRows += 1;
    var row = document.createElement('div');
    row.className = 'tl__row is-run';
    row.innerHTML = '<div class="tl__name"><span>' + esc(title) + '</span><span class="tl__time">—</span></div>' +
      '<div class="tl__sub"><span class="tl__tool">' + esc(toolLabel(tool)) + '</span><span class="tl__state tl__state--run">进行中</span></div>';
    dom.traceList.appendChild(row);
    state.traceMap[stepId] = row;
    if (dom.traceMeta) dom.traceMeta.innerHTML = '<b>进行中</b> · 第 ' + state.traceRows + ' 步';
  }
  function traceEnd(stepId, ok, latency) {
    var row = state.traceMap && state.traceMap[stepId];
    if (!row) return;
    row.classList.remove('is-run');
    row.classList.add(ok ? 'is-ok' : 'is-fail');
    var timeEl = row.querySelector('.tl__time');
    if (timeEl) {
      var n = Number(latency);
      timeEl.textContent = latency == null || isNaN(n) ? '' : (n >= 1000 ? (n / 1000).toFixed(1) + 's' : Math.round(n) + 'ms');
    }
    var stateEl = row.querySelector('.tl__state');
    if (stateEl) {
      stateEl.className = 'tl__state ' + (ok ? 'tl__state--ok' : 'tl__state--fail');
      stateEl.textContent = ok ? '已完成' : '失败';
    }
  }
  function traceDone(status) {
    state.traceStatus = status || 'done';
    if (!dom.traceMeta) return;
    if (status === 'awaiting') dom.traceMeta.innerHTML = '<b>等待授权</b> · 已生成待授权操作';
    else if (status === 'blocked') dom.traceMeta.innerHTML = '<b>已拦截</b> · 未执行资金操作';
    else if (status === 'error') dom.traceMeta.innerHTML = '<b>执行失败</b> · 请查看对话提示';
    else dom.traceMeta.innerHTML = '<b>已完成</b> · ' + state.traceRows + ' 步工具调用';
  }
  // ===================== 撤销 =====================
  function detectAction(executed) {
    var found = null;
    (executed || []).forEach(function (step) {
      if (found) return;
      var r = (step && step.result) || {};
      var aid = r.action_id || (r.transfer && r.transfer.action_id) || (r.order && r.order.action_id) || (r.payment && r.payment.action_id);
      if (aid) found = { action_id: aid, tool: step.tool, amount_cents: r.amount_cents || (r.transfer && r.transfer.amount_cents) || null };
    });
    return found;
  }
  function clearUndo() {
    if (state.undo && state.undo.handle) { try { state.undo.handle.close(); } catch (e) {} }
    state.undo = null;
  }
  function maybeUndo(action) {
    if (!action || !action.action_id) return;
    clearUndo();
    var handle = toast('已执行，60 秒内可撤销', {
      type: 'success',
      sub: '撤销后资金原路退回，并追加一条撤销审计事件',
      actionLabel: '撤销',
      duration: 60000,
      onAction: function () { doUndo(action, handle); }
    });
    state.undo = { action: action, handle: handle };
  }
  async function doUndo(action, handle) {
    if (!state.sessionId) return;
    if (handle) { handle.disableAction(); handle.setSub('正在撤销…'); }
    try {
      var res = await postJson('/api/undo', { session_id: state.sessionId, action_id: action.action_id });
      if (handle) handle.close();
      state.undo = null;
      toast('已撤销这笔操作', { type: 'success', sub: (res && res.message) || '资金已原路退回' });
      renderNote('撤销完成 · 已追加撤销审计事件');
      loadAudit(true);
      refreshAccountSnapshot();
    } catch (e) {
      if (handle) { handle.enableAction('重试'); handle.setSub(e.message || '撤销失败'); }
      toast(e.message || '撤销失败', { type: 'error' });
    }
  }
  // ===================== SSE 事件 =====================
  function handleEvent(ev) {
    var name = ev.event, data = ev.data;
    if (name === 'session') {
      if (data && data.session_id) state.sessionId = data.session_id;
      if (data && data.thread_id) state.threadId = data.thread_id;
      return;
    }
    if (name === 'intent') { renderIntent(ensureTurn(), data); return; }
    if (name === 'plan') { renderPlan(ensureTurn(), data); return; }
    if (name === 'tool_start') {
      var turn = ensureTurn();
      setStepState(turn, data.step_id, 'run', '执行中');
      traceStart(data.step_id, data.title || data.tool || '工具调用', data.tool);
      return;
    }
    if (name === 'tool_end') {
      var t2 = ensureTurn();
      setStepState(t2, data.step_id, data.ok ? 'ok' : 'fail', data.ok ? ('已完成 · ' + (data.latency_ms == null ? '' : Math.round(data.latency_ms) + 'ms')) : '失败');
      traceEnd(data.step_id, data.ok, data.latency_ms);
      var act = detectAction([{ tool: data.tool, result: data.result }]);
      if (act) t2.actions.push(act);
      return;
    }
    if (name === 'approval_required') {
      openApproval(data);
      traceDone('awaiting');
      renderNote('已生成待授权操作，请在审批单中确认。');
      return;
    }
    if (name === 'message') {
      var delta = typeof data === 'string' ? data : (data && data.delta) || '';
      var msgTurn = ensureTurn();
      // 已经走逐字流式（收到过 answer_delta）时忽略整段 message，避免重复显示
      if (msgTurn && msgTurn.answerGotDelta) return;
      if (delta) renderDelta(delta);
      return;
    }
    if (name === 'answer_start') { renderAnswerStart(data); return; }
    if (name === 'answer_delta') { renderAnswerDelta(data); return; }
    if (name === 'answer_end') { renderAnswerEnd(data); return; }
    if (name === 'insight_card') { renderInsightCard(ensureTurn(), data); return; }
    if (name === 'note') {
      var noteText = (data && (data.text || data.note)) || '已停止输出';
      var canceledTurn = state.currentTurn;
      var alreadyMarked = !!(canceledTurn && canceledTurn.answerEl && canceledTurn.answerEl.classList.contains('is-cancelled'));
      if (alreadyMarked && noteText === '已停止输出') return;
      renderNote(noteText);
      return;
    }
    if (name === 'audit') { renderAuditChip(data || {}); return; }
    if (name === 'done') { finishTurn(data && data.status, data); return; }
    if (name === 'error') {
      renderErrorLine(parseApiError(data) || '服务返回错误');
      traceDone('error');
      return;
    }
    if (name === '__result__') { applyFullResult(data); return; }
  }
  function finishTurn(status, data) {
    var turn = state.currentTurn;
    if (turn) {
      turn.status = status || 'ok';
      turn.done = true;
      dropThinking(turn);
      if (turn.answerSeen && turn.answerCaret) finishAnswerBubble(turn, null);
      if (turn.typer && status !== 'awaiting_approval') turn.typer.flush();
      if (turn.tags) {
        if (status === 'blocked') turn.tags.insertAdjacentHTML('beforeend', '<span class="tag tag--err">已拦截</span>');
        else if (status === 'awaiting_approval') turn.tags.insertAdjacentHTML('beforeend', '<span class="tag tag--warn">待授权</span>');
      }
      if (status === 'ok' && turn.actions.length) maybeUndo(turn.actions[0]);
    }
    if (status === 'locked') {
      if (turn && turn.tags) turn.tags.insertAdjacentHTML('beforeend', '<span class="tag tag--err">会话锁定</span>');
      renderNote('会话已临时锁定，倒计时结束后自动恢复。');
      applyLocked(data && data.locked_until);
      traceDone('blocked');
      return;
    }
    if (status === 'awaiting_approval') traceDone('awaiting');
    else if (status === 'blocked') traceDone('blocked');
    else traceDone('done');
    if (status === 'ok' || status === 'blocked') { loadAudit(true); refreshAccountSnapshot(); }
    state.streaming = false;
    state.streamCancelRequested = false;
    setSendMode('idle');
  }
  function applyFullResult(data) {
    if (!data) return;
    if (data.session_id) state.sessionId = data.session_id;
    if (data.thread_id) state.threadId = data.thread_id;
    var turn = ensureTurn();
    if (data.intent) renderIntent(turn, data.intent);
    if (data.plan) renderPlan(turn, data.plan);
    (data.audit || []).forEach(function (a) { renderAuditChip(a); });
    if (data.approval) { openApproval(data.approval); traceDone('awaiting'); }
    if (data.insight) renderInsightCard(turn, data.insight);
    if (data.reply) {
      ensureMessage(turn);
      turn.replyText = data.reply;
      turn.typer.push(data.reply);
      turn.typer.flush();
    }
    (data.executed || []).forEach(function (step) {
      setStepState(turn, step.step_id, step.ok ? 'ok' : 'fail', step.ok ? '已完成' : '失败');
      var act = detectAction([step]);
      if (act) turn.actions.push(act);
    });
    if (data.status === 'blocked') renderBlocked(turn, collectBlockedReasons(data));
    finishTurn(data.status || 'ok', data);
  }
  function collectBlockedReasons(data) {
    var out = [];
    (data && data.executed || []).forEach(function (step) {
      var r = (step && step.result) || {};
      if (r.reason) out.push(r.reason);
      if (r.message) out.push(r.message);
      if (r.blocked_reason) out.push(r.blocked_reason);
      if (Array.isArray(r.reasons)) out = out.concat(r.reasons);
    });
    if (data && data.reply && !out.length) out.push(data.reply);
    return out;
  }

  // ===================== 审批弹层 =====================
  function getOtpInputs() { return (dom.otpInputs || []).filter(function (x) { return !!x; }); }
  var FACTOR_LABEL = { totp: '动态验证码', sms: '短信验证码', face: '人脸确认' };
  var FACTOR_OTP_LABEL = { totp: '6 位动态验证码', sms: '6 位短信验证码' };
  function isHighApproval(data) {
    var a = data || state.approval;
    if (!a) return false;
    return a.risk_level === 'high' || !!(a.challenge && a.challenge.type === 'totp');
  }
  function approvalFactorList(data) {
    var a = data || state.approval;
    var list = a && Array.isArray(a.factors) ? a.factors.filter(function (f) { return !!FACTOR_LABEL[f]; }) : [];
    if (!list.length) list = ['totp', 'sms', 'face'];
    return list;
  }
  function needsFactor() { return isHighApproval(); }
  function isOtpRequired() { return isHighApproval() && state.approvalFactor !== 'face'; }
  function readOtp() {
    return getOtpInputs().map(function (i) { return (i.value || '').replace(/\D/g, ''); }).join('');
  }
  function focusOtpCell(idx) {
    var list = getOtpInputs();
    if (!list.length) return;
    var i = Math.max(0, Math.min(list.length - 1, idx));
    try { list[i].focus(); } catch (e) {}
    if (!isMobileViewport()) return;
    applyViewportHeight();
    var cell = list[i];
    if (cell && typeof cell.scrollIntoView === 'function') {
      try { cell.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e2) { /* 不支持时忽略 */ }
    }
  }
  function resetOtp() {
    getOtpInputs().forEach(function (i) { i.value = ''; i.classList.remove('is-error'); });
    var box = dom.otpCells;
    if (box) box.classList.remove('is-shake');
  }
  function fillOtp(code) {
    var digits = String(code || '').replace(/\D/g, '').slice(0, 6);
    var list = getOtpInputs();
    list.forEach(function (i, idx) { i.value = digits.charAt(idx) || ''; i.classList.remove('is-error'); });
    focusOtpCell(Math.min(digits.length, list.length - 1));
  }
  function shakeOtp() {
    var box = dom.otpCells;
    if (!box) return;
    box.classList.remove('is-shake');
    void box.offsetWidth;
    box.classList.add('is-shake');
    getOtpInputs().forEach(function (i) { i.classList.add('is-error'); });
  }
  function showFieldError(msg) {
    if (!dom.apError) return;
    dom.apError.textContent = msg;
    dom.apError.hidden = false;
  }
  function clearFieldError() {
    if (!dom.apError) return;
    dom.apError.textContent = '';
    dom.apError.hidden = true;
  }
  function stopApprovalTimer() {
    if (state.approvalTimer) { clearInterval(state.approvalTimer); state.approvalTimer = null; }
  }
  function markApprovalExpired() {
    stopApprovalTimer();
    if (dom.apCountdown) {
      dom.apCountdown.textContent = '00:00';
      dom.apCountdown.classList.add('is-expired');
    }
    if (dom.btnApprove) dom.btnApprove.disabled = true;
    showFieldError('审批已过期，请重新发起指令');
    if (dom.pendingPillTime) dom.pendingPillTime.textContent = '已过期';
  }
  function startApprovalCountdown(expiresAt) {
    stopApprovalTimer();
    var ms = expiresAt ? new Date(expiresAt).getTime() : NaN;
    state.approvalExpiresAt = isNaN(ms) ? Date.now() + 300000 : ms;
    function tick() {
      var left = Math.max(0, Math.round((state.approvalExpiresAt - Date.now()) / 1000));
      if (dom.apCountdown) {
        dom.apCountdown.textContent = fmtCountdown(left);
        if (left > 0) dom.apCountdown.classList.remove('is-expired');
      }
      if (dom.pendingPillTime) dom.pendingPillTime.textContent = fmtCountdown(left);
      updateFactorCountdowns();
      if (left <= 0) markApprovalExpired();
    }
    tick();
    state.approvalTimer = setInterval(tick, 500);
  }
  function setApprovalEnabled(on) {
    if (dom.btnApprove) dom.btnApprove.disabled = !on;
    if (dom.btnReject) dom.btnReject.disabled = !on;
    getOtpInputs().forEach(function (i) { i.disabled = !on; });
    if (dom.otpKeypad) {
      Array.prototype.forEach.call(dom.otpKeypad.querySelectorAll('.keypad__key'), function (b) { b.disabled = !on; });
    }
    if (dom.btnDemoTotp) dom.btnDemoTotp.disabled = !on;
    if (dom.dlgSmsSend) dom.dlgSmsSend.disabled = !on;
    if (dom.dlgFaceScan) dom.dlgFaceScan.disabled = !on;
  }
  function setApprovalBusy(busy, label) {
    if (!dom.btnApprove) return;
    dom.btnApprove.disabled = busy;
    if (dom.btnReject) dom.btnReject.disabled = busy;
    dom.btnApprove.textContent = busy ? (label || '处理中…') : (needsFactor() ? '确认并验证' : '确认执行');
  }
  function syncPendingPill() {
    if (!dom.pendingPill) return;
    var has = !!state.approval;
    dom.pendingPill.hidden = !has;
    if (has && dom.approvalSheet && !dom.approvalSheet.hidden) dom.pendingPill.hidden = true;
  }
  // ---- 审批多因子：动态码 / 短信 / 人脸 ----
  function factorBtn(m) { return m === 'totp' ? dom.dlgFactorTotp : (m === 'sms' ? dom.dlgFactorSms : dom.dlgFactorFace); }
  function setApprovalFactor(method) {
    var avail = approvalFactorList();
    if (avail.indexOf(method) < 0) method = avail[0] || 'totp';
    state.approvalFactor = method;
    ['totp', 'sms', 'face'].forEach(function (m) {
      var b = factorBtn(m);
      if (!b) return;
      var ok = avail.indexOf(m) >= 0;
      b.hidden = !ok;
      b.disabled = !ok;
      var on = ok && m === method;
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    var isFace = method === 'face';
    if (dom.apOtp) dom.apOtp.hidden = isFace;
    if (dom.apOtpLabel) dom.apOtpLabel.textContent = FACTOR_OTP_LABEL[method] || FACTOR_OTP_LABEL.totp;
    if (dom.btnDemoTotp) dom.btnDemoTotp.hidden = method !== 'totp';
    if (dom.dlgSmsSend) {
      dom.dlgSmsSend.hidden = method !== 'sms';
      dom.dlgSmsSend.textContent = state.approvalSms && state.approvalSms.code ? '重新获取' : '获取短信验证码';
    }
    if (dom.dlgFace) dom.dlgFace.hidden = !isFace;
    resetOtp();
    clearFieldError();
    if (isFace) renderFaceState();
    else renderSmsCode();
    if (!isFace) setTimeout(function () { focusOtpCell(0); }, 40);
    else if (dom.dlgFaceScan) { try { dom.dlgFaceScan.focus(); } catch (e) {} }
  }
  function renderSmsCode() {
    if (!dom.dlgSmsCode) return;
    var sms = state.approvalSms || { code: '', expiresAt: 0 };
    if (!sms.code) { dom.dlgSmsCode.hidden = true; dom.dlgSmsCode.textContent = ''; return; }
    var left = Math.max(0, Math.round((sms.expiresAt - Date.now()) / 1000));
    dom.dlgSmsCode.hidden = state.approvalFactor !== 'sms';
    if (left <= 0) dom.dlgSmsCode.textContent = '短信验证码已过期，请重新获取';
    else dom.dlgSmsCode.textContent = '演示码 ' + sms.code + ' · ' + left + ' 秒内有效（点击填入）';
  }
  async function sendDemoSms() {
    if (!state.approval || !state.approvalSms || state.approvalSms.busy) return;
    state.approvalSms.busy = true;
    if (dom.dlgSmsSend) { dom.dlgSmsSend.disabled = true; dom.dlgSmsSend.textContent = '发送中…'; }
    try {
      var uid = (state.user && state.user.user_id) || 'u001';
      var res = await getJson('/api/demo/sms?session_id=' + encodeURIComponent(state.sessionId || '') + '&user_id=' + encodeURIComponent(uid));
      if (!res || !res.code) throw new Error('短信验证码不可用');
      var ttl = Number(res.ttl || res.seconds_left || 120);
      state.approvalSms.code = String(res.code);
      state.approvalSms.expiresAt = Date.now() + (ttl > 0 ? ttl : 120) * 1000;
      renderSmsCode();
      clearFieldError();
      toast('演示短信验证码已发送', { sub: '仅演示环境可用，真实环境由短信下发' });
    } catch (e) {
      if (dom.dlgSmsCode) { dom.dlgSmsCode.hidden = false; dom.dlgSmsCode.textContent = (e && e.message) || '服务暂时不可用'; }
      toast((e && e.message) || '短信验证码不可用', { type: 'error', sub: '演示模式关闭时该接口不可用' });
    } finally {
      state.approvalSms.busy = false;
      if (dom.dlgSmsSend) { dom.dlgSmsSend.disabled = false; dom.dlgSmsSend.textContent = state.approvalSms.code ? '重新获取' : '获取短信验证码'; }
      if (dom.dlgSmsSend) dom.dlgSmsSend.hidden = state.approvalFactor !== 'sms';
    }
  }
  function renderFaceState() {
    var face = state.approvalFace || { token: '', expiresAt: 0, error: '' };
    var left = Math.max(0, Math.round((face.expiresAt - Date.now()) / 1000));
    if (face.token && left <= 0) { face.token = ''; face.expiresAt = 0; face.error = '人脸确认已过期，请重新发起'; }
    if (dom.dlgFaceToken) {
      if (face.token) { dom.dlgFaceToken.hidden = false; dom.dlgFaceToken.textContent = 'token ' + truncate(face.token, 6) + '… · ' + left + ' 秒内有效'; }
      else { dom.dlgFaceToken.hidden = true; dom.dlgFaceToken.textContent = ''; }
    }
    if (dom.dlgFaceState) {
      if (face.token) dom.dlgFaceState.textContent = '已完成扫描 · ' + left + ' 秒内有效';
      else dom.dlgFaceState.textContent = face.error || '点击开始，模拟一次人脸确认';
    }
    if (dom.dlgFaceScan) dom.dlgFaceScan.textContent = face.token ? '重新确认' : '开始人脸确认';
  }
  async function startFaceScan() {
    if (!state.approval || !state.approvalFace || state.approvalFace.busy) return;
    state.approvalFace.busy = true;
    state.approvalFace.error = '';
    if (dom.dlgFaceScan) { dom.dlgFaceScan.disabled = true; dom.dlgFaceScan.textContent = '扫描中…'; }
    if (dom.dlgFaceState) dom.dlgFaceState.textContent = '正在确认…请保持面部在框内';
    try {
      var uid = (state.user && state.user.user_id) || 'u001';
      var res = await postJson('/api/demo/face', { session_id: state.sessionId, user_id: uid });
      if (!res || !res.token) throw new Error('人脸确认不可用');
      var ttl = Number(res.ttl || res.seconds_left || 60);
      state.approvalFace.token = String(res.token);
      state.approvalFace.expiresAt = Date.now() + (ttl > 0 ? ttl : 60) * 1000;
      clearFieldError();
    } catch (e) {
      state.approvalFace.token = '';
      state.approvalFace.expiresAt = 0;
      state.approvalFace.error = (e && e.message) || '服务暂时不可用';
      toast((e && e.message) || '人脸确认不可用', { type: 'error' });
    } finally {
      state.approvalFace.busy = false;
      if (dom.dlgFaceScan) dom.dlgFaceScan.disabled = false;
      renderFaceState();
    }
  }
  function updateFactorCountdowns() {
    if (state.approvalFactor === 'sms') renderSmsCode();
    else if (state.approvalFactor === 'face') renderFaceState();
  }
  var HIT_LEVEL_ORDER = { high: 0, medium: 1, low: 2 };
  function approvalRulesHtml(data) {
    var html = '<h4>为什么需要你确认</h4>';
    var hits = Array.isArray(data.hits) ? data.hits.slice() : [];
    if (hits.length) {
      hits.sort(function (a, b) {
        var la = HIT_LEVEL_ORDER[a.level] == null ? 3 : HIT_LEVEL_ORDER[a.level];
        var lb = HIT_LEVEL_ORDER[b.level] == null ? 3 : HIT_LEVEL_ORDER[b.level];
        return la - lb;
      });
      var top = hits.slice(0, 3);
      top.forEach(function (h) {
        html += '<div class="rule"><span class="rule__name">' + esc(h.rule || 'rule') + '</span>' +
          '<span class="rule__detail">' + esc(h.detail || '') + '</span></div>';
      });
      var rest = hits.length - top.length;
      if (rest > 0) html += '<span class="rule__more">+' + rest + ' 条判定依据</span>';
      return html;
    }
    var reasons = Array.isArray(data.reasons) ? data.reasons : [];
    if (!reasons.length) return '';
    reasons.slice(0, 4).forEach(function (r) {
      html += '<div class="rule"><span class="rule__detail">' + esc(typeof r === 'string' ? r : (r.detail || r.reason || '')) + '</span></div>';
    });
    if (reasons.length > 4) html += '<span class="rule__more">+' + (reasons.length - 4) + ' 条判定依据</span>';
    return html;
  }
  function approvalRowsHtml(data) {
    var rows = [];
    if (data.counterparty) rows.push(['收款人', data.counterparty, false]);
    if (data.counterparty_account_masked) rows.push(['收款账户', data.counterparty_account_masked, true]);
    if (data.payer_account_masked) rows.push(['付款账户', data.payer_account_masked, true]);
    if (isHighApproval(data)) {
      var fnames = approvalFactorList(data).map(function (m) { return FACTOR_LABEL[m] || m; }).join(' / ');
      rows.push(['校验方式', fnames + '（任选其一）', false]);
    } else rows.push(['验证方式', '确认后执行', false]);
    rows.push(['时间', '提交后即时执行', false]);
    return rows.map(function (r) {
      return '<div class="dlg__row"><span class="k">' + esc(r[0]) + '</span>' +
        '<span class="v' + (r[2] ? ' mono' : '') + '">' + esc(r[1]) + '</span></div>';
    }).join('');
  }
  function openApproval(data) {
    if (!data) return;
    state.approval = data;
    var level = data.risk_level || 'medium';
    var meta = riskMeta(level);
    if (dom.apRisk) {
      dom.apRisk.className = 'risk ' + (meta.cls || '');
      dom.apRisk.textContent = meta.label;
    }
    if (dom.apAmount) {
      if (data.amount_cents == null) { dom.apAmount.hidden = true; }
      else {
        dom.apAmount.hidden = false;
        dom.apAmount.innerHTML = '<span class="cur">¥</span>' + esc(fmtMoneyPlain(data.amount_cents));
      }
    }
    if (dom.apTitle) {
      var title = data.title || '';
      if (!title && data.counterparty) title = '向「' + data.counterparty + '」操作';
      dom.apTitle.innerHTML = fmtRich(title || '操作授权');
    }
    if (dom.apRows) dom.apRows.innerHTML = approvalRowsHtml(data);
    if (dom.apSource) {
      var st = '来源：<b>' + esc(sourceLabel(data.source)) + '</b>';
      if (data.source_detail) st += ' · ' + esc(data.source_detail);
      dom.apSource.innerHTML = st;
    }
    var conflicts = Array.isArray(data.conflicts) ? data.conflicts : [];
    if (dom.apConflicts && dom.apConflictList) {
      if (conflicts.length) {
        dom.apConflicts.hidden = false;
        dom.apConflictList.innerHTML = conflicts.map(function (c) {
          return '<li>' + esc(typeof c === 'string' ? c : (c.message || c.detail || '')) + '</li>';
        }).join('');
      } else dom.apConflicts.hidden = true;
    }
    if (dom.apReasons) dom.apReasons.innerHTML = approvalRulesHtml(data);
    if (dom.apPreview) {
      if (data.preview) { dom.apPreview.hidden = false; dom.apPreview.innerHTML = fmtRich(data.preview); }
      else dom.apPreview.hidden = true;
    }
    var high = isHighApproval(data);
    state.approvalFactor = 'totp';
    state.approvalSms = { code: '', expiresAt: 0, busy: false };
    state.approvalFace = { token: '', expiresAt: 0, busy: false, error: '' };
    if (dom.dlgFactors) dom.dlgFactors.hidden = !high;
    resetOtp();
    clearFieldError();
    if (dom.btnApprove) dom.btnApprove.textContent = high ? '确认并验证' : '确认执行';
    setApprovalEnabled(true);
    if (high) setApprovalFactor('totp');
    else {
      if (dom.apOtp) dom.apOtp.hidden = true;
      if (dom.dlgFace) dom.dlgFace.hidden = true;
      if (dom.dlgSmsCode) { dom.dlgSmsCode.hidden = true; dom.dlgSmsCode.textContent = ''; }
    }
    if (dom.approvalSheet) dom.approvalSheet.hidden = false;
    applyViewportHeight();
    startApprovalCountdown(data.expires_at);
    syncPendingPill();
    if (high) setTimeout(function () { focusOtpCell(0); }, 60);
  }
  function clearApproval() {
    state.approval = null;
    stopApprovalTimer();
    if (dom.pendingPill) dom.pendingPill.hidden = true;
  }
  function closeApprovalSheet(silent) {
    if (dom.approvalSheet) dom.approvalSheet.hidden = true;
    if (dom.approvalPanel) dom.approvalPanel.classList.remove('is-stamping', 'is-stamped');
    stopApprovalTimer();
    clearFieldError();
    if (!silent) { syncPendingPill(); }
  }
  function playStamp(cb) {
    var dlg = dom.approvalPanel;
    if (!dlg) { if (cb) cb(); return; }
    dlg.classList.add('is-stamping');
    if (dom.btnApprove) { dom.btnApprove.disabled = true; dom.btnApprove.textContent = '已执行'; }
    if (dom.apCountdown) dom.apCountdown.textContent = '已执行';
    setTimeout(function () {
      dlg.classList.add('is-stamped');
      setTimeout(function () { if (cb) cb(); }, 380);
    }, 260);
  }
  async function submitApproval(decision) {
    var data = state.approval;
    if (!data || state.approvalSubmitting) return;
    var verify = needsFactor();
    var method = verify ? state.approvalFactor : '';
    var code = '';
    if (decision === 'approve' && verify) {
      if (method === 'face') {
        if (!state.approvalFace || !state.approvalFace.token) { showFieldError('请先完成人脸确认'); return; }
        code = state.approvalFace.token;
      } else {
        code = readOtp();
        if (!/^\d{6}$/.test(code)) {
          showFieldError(method === 'sms' ? '请输入 6 位短信验证码' : '请输入 6 位动态验证码');
          shakeOtp();
          focusOtpCell(code.length);
          return;
        }
        if (method === 'sms' && !(state.approvalSms && state.approvalSms.code)) {
          showFieldError('请先获取短信验证码');
          return;
        }
      }
    }
    clearFieldError();
    state.approvalSubmitting = true;
    setApprovalBusy(true);
    var body = { session_id: state.sessionId, approval_id: data.approval_id, decision: decision };
    if (decision === 'approve' && verify) { body.method = method; body.code = code; }
    try {
      var res = await postJson('/api/approval', body);
      state.approvalSubmitting = false;
      if (decision === 'reject') {
        closeApprovalSheet();
        clearApproval();
        setApprovalBusy(false);
        renderNote('已拒绝该操作，未发生任何资金变动。');
        if (state.currentTurn) {
          state.currentTurn.status = 'rejected';
          state.currentTurn.done = true;
          if (state.currentTurn.tags) state.currentTurn.tags.insertAdjacentHTML('beforeend', '<span class="tag tag--muted">已拒绝</span>');
        }
        traceDone('done');
        toast('已拒绝该操作', { type: 'success', sub: '资金未变动，已记录审批拒绝事件' });
        loadAudit(true);
        if (res) applyApprovalResult(res);
        return;
      }
      playStamp(function () {
        closeApprovalSheet();
        clearApproval();
        applyApprovalResult(res);
      });
    } catch (e) {
      state.approvalSubmitting = false;
      setApprovalBusy(false);
      var c = errCode(e);
      if (c === 'INVALID_SMS') {
        showFieldError(e.message || '短信验证码不正确或已过期，请重新获取');
        shakeOtp();
        resetOtp();
        state.approvalSms.code = '';
        state.approvalSms.expiresAt = 0;
        renderSmsCode();
        if (dom.dlgSmsSend) dom.dlgSmsSend.textContent = '获取短信验证码';
        focusOtpCell(0);
      } else if (c === 'INVALID_FACE') {
        state.approvalFace.token = '';
        state.approvalFace.expiresAt = 0;
        state.approvalFace.error = '人脸确认已过期，请重新发起';
        renderFaceState();
        showFieldError(e.message || '人脸确认已过期，请重新发起');
      } else if (c === 'INVALID_METHOD') {
        showFieldError('不支持的校验方式');
      } else if (c === 'INVALID_OTP' || /验证码/.test(e.message || '')) {
        showFieldError('验证码不正确，请重新输入');
        shakeOtp();
        resetOtp();
        focusOtpCell(0);
      } else if (c === 'APPROVAL_EXPIRED' || c === 'APPROVAL_CONSUMED' || e.status === 410) {
        markApprovalExpired();
        showFieldError(e.message || '审批已过期，请重新发起指令');
      } else {
        showFieldError(e.message || '提交失败，请重试');
      }
    }
  }
  function applyApprovalResult(res) {
    if (!res) return;
    var turn = state.currentTurn;
    (res.audit || []).forEach(function (a) { renderAuditChip(a); });
    (res.executed || []).forEach(function (step) {
      setStepState(turn, step.step_id, step.ok ? 'ok' : 'fail', step.ok ? '已完成' : '失败');
      var act = detectAction([step]);
      if (act && turn) turn.actions.push(act);
    });
    if (res.reply) {
      var t = ensureTurn();
      ensureMessage(t);
      t.replyText += res.reply;
      t.typer.push(res.reply);
      t.typer.flush();
    }
    if (res.status === 'blocked') renderBlocked(turn, collectBlockedReasons(res));
    finishTurn(res.status || 'ok', res);
  }
  async function fetchDemoTotp() {
    if (!state.sessionId && !state.user) return;
    var uid = (state.user && state.user.user_id) || 'u001';
    if (dom.btnDemoTotp) dom.btnDemoTotp.disabled = true;
    try {
      var res = await getJson('/api/demo/totp?session_id=' + encodeURIComponent(state.sessionId || '') + '&user_id=' + encodeURIComponent(uid));
      if (res && res.code) {
        fillOtp(res.code);
        clearFieldError();
        toast('演示验证码已填入', { sub: '仅演示环境可用，真实环境由身份验证器生成' });
      } else toast('暂时取不到演示验证码', { type: 'error' });
    } catch (e) {
      toast(e.message || '演示验证码不可用', { type: 'error', sub: '演示模式关闭时该接口不可用' });
    } finally {
      if (dom.btnDemoTotp) dom.btnDemoTotp.disabled = false;
    }
  }
  async function reopenPendingApproval() {
    if (state.approval) {
      if (dom.approvalSheet) dom.approvalSheet.hidden = false;
      syncPendingPill();
      if (isOtpRequired()) setTimeout(function () { focusOtpCell(0); }, 60);
      return;
    }
    if (!state.sessionId) return;
    try {
      var s = await getJson('/api/session/' + encodeURIComponent(state.sessionId));
      if (s && s.pending_approval) openApproval(s.pending_approval);
      else if (dom.pendingPill) dom.pendingPill.hidden = true;
    } catch (e) {
      if (dom.pendingPill) dom.pendingPill.hidden = true;
      toast(e.message || '读取待授权操作失败', { type: 'error' });
    }
  }
  function setupOtp() {
    var list = getOtpInputs();
    if (!list.length) return;
    list.forEach(function (cell, idx) {
      cell.addEventListener('input', function () {
        var v = (cell.value || '').replace(/\D/g, '');
        if (v.length > 1) {
          fillOtp(v);
          return;
        }
        cell.value = v;
        cell.classList.remove('is-error');
        clearFieldError();
        if (v && idx < list.length - 1) focusOtpCell(idx + 1);
        if (readOtp().length === 6) {
          list[list.length - 1].blur();
          if (dom.btnApprove && !dom.btnApprove.disabled) dom.btnApprove.focus();
        }
      });
      cell.addEventListener('keydown', function (ev) {
        if (ev.key === 'Backspace') {
          if (!cell.value && idx > 0) { focusOtpCell(idx - 1); list[idx - 1].value = ''; ev.preventDefault(); }
        } else if (ev.key === 'ArrowLeft' && idx > 0) { focusOtpCell(idx - 1); ev.preventDefault(); }
        else if (ev.key === 'ArrowRight' && idx < list.length - 1) { focusOtpCell(idx + 1); ev.preventDefault(); }
        else if (ev.key === 'Enter') { ev.preventDefault(); submitApproval('approve'); }
      });
      cell.addEventListener('paste', function (ev) {
        var text = (ev.clipboardData && ev.clipboardData.getData('text')) || '';
        text = text.replace(/\D/g, '');
        if (!text) return;
        ev.preventDefault();
        fillOtp(text);
        clearFieldError();
      });
      cell.addEventListener('focus', function () { try { cell.select(); } catch (e) {} });
    });
    if (dom.otpKeypad) {
      dom.otpKeypad.addEventListener('click', function (ev) {
        var btn = ev.target && ev.target.closest ? ev.target.closest('[data-key]') : null;
        if (!btn) return;
        var key = btn.getAttribute('data-key');
        if (state.approvalSubmitting) return;
        var cells = getOtpInputs();
        var cur = 0;
        for (var i = 0; i < cells.length; i++) { if (!cells[i].value) { cur = i; break; } cur = i + 1; }
        if (cur > cells.length - 1) cur = cells.length - 1;
        if (key === 'clear') { resetOtp(); focusOtpCell(0); clearFieldError(); return; }
        if (key === 'del') {
          for (var j = cells.length - 1; j >= 0; j--) {
            if (cells[j].value) { cells[j].value = ''; focusOtpCell(j); break; }
          }
          clearFieldError();
          return;
        }
        if (/^[0-9]$/.test(key)) {
          cells[cur].value = key;
          cells[cur].classList.remove('is-error');
          clearFieldError();
          focusOtpCell(cur + 1);
          if (readOtp().length === 6) cells[cells.length - 1].blur();
        }
      });
    }
  }

  // ===================== 审计抽屉 =====================
  function randHex(n) {
    var s = '', chars = '0123456789abcdef';
    for (var i = 0; i < n; i++) s += chars.charAt(Math.floor(Math.random() * 16));
    return s;
  }
  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }
  function reduceMotion() {
    try { return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); }
    catch (e) { return false; }
  }
  function hashlineHtml(rows) {
    return rows.map(function (r) {
      return '<span class="hashline__k">' + esc(r[0]) + '</span><span class="hashline__v">' + esc(r[1] || '—') + '</span>';
    }).join('');
  }
  function clearAuditTimers() {
    (state.auditTimers || []).forEach(function (t) { clearTimeout(t); });
    state.auditTimers = [];
  }
  function auditRowHtml(e) {
    var hash = e.hash || '';
    var risk = e.risk_level || 'low';
    var dotCls = risk === 'low' ? 'dot--ok' : (risk === 'high' ? 'dot--warn' : 'dot');
    var detail = hashlineHtml([
      ['prev_hash', e.prev_hash],
      ['hash', e.hash],
      ['args_hash', e.args_hash],
      ['result_hash', e.result_hash],
      ['actor · tool', (e.actor || '—') + ' · ' + (e.tool || '—')],
      ['审批', e.approval_id || '—']
    ]);
    return '<div class="arow" data-idx="' + esc(String(e.seq)) + '" data-hash="' + esc(hash) + '">' +
      '<span class="arow__bar" aria-hidden="true"></span>' +
      '<span class="arow__seq">#' + esc(String(e.seq == null ? '' : e.seq)) + '</span>' +
      '<div class="arow__main"><div class="arow__name"><i class="dot ' + dotCls + '"></i>' + esc(eventLabel(e.event_type)) + '</div>' +
      '<div class="arow__sub">' + esc(e.tool || '—') + ' · ' + (risk === 'low' ? '低风险' : risk === 'high' ? '高风险' : '中风险') + '</div></div>' +
      '<div class="arow__right"><span class="arow__hash" data-full="' + esc(hash) + '">' + esc(hash.slice(0, 8)) + '</span>' +
      '<button class="arow__copy" type="button" aria-label="复制完整哈希">复制</button></div>' +
      '<div class="arow__detail" hidden>' + detail + '</div>' +
    '</div>';
  }
  function renderAudit(payload) {
    var entries = (payload && (payload.entries || payload.audit)) || [];
    var chain = (payload && payload.chain) || {};
    var valid = chain.valid != null ? chain.valid : payload && payload.valid;
    var checked = chain.checked != null ? chain.checked : (payload && payload.checked);
    state.auditEntries = entries;
    if (dom.auditMeta) dom.auditMeta.textContent = '共 ' + entries.length + ' 条' + (valid ? '' : ' · 存在断点');
    if (dom.auditList) {
      var list = entries.slice().reverse();
      dom.auditList.innerHTML = list.map(auditRowHtml).join('') || '<div class="trace__empty">还没有审计事件。</div>';
    }
    clearAuditTimers();
    var rows = dom.auditList ? Array.prototype.slice.call(dom.auditList.querySelectorAll('.arow')) : [];
    var animate = !reduceMotion() && rows.length <= 24;
    if (dom.auditValid) {
      dom.auditValid.className = 'badge' + (valid ? '' : ' badge--err');
      if (animate && rows.length) {
        dom.auditValid.className = 'badge badge--wait';
        dom.auditValid.textContent = '校验中';
      } else {
        dom.auditValid.textContent = valid ? ('链校验通过 · ' + (checked || entries.length) + ' 条') : '链校验失败';
      }
    }
    if (!animate) {
      rows.forEach(function (r) { r.classList.add('is-verified'); });
      return;
    }
    var perRow = 32, perChar = 24;
    rows.forEach(function (row, i) {
      var hashEl = row.querySelector('.arow__hash');
      var full = (hashEl && hashEl.getAttribute('data-full')) || '';
      if (hashEl) hashEl.textContent = randHex(Math.min(8, full.length || 8));
      var startTimer = setTimeout(function () {
        var locked = 0;
        var timer = setInterval(function () {
          locked += 1;
          if (hashEl) hashEl.textContent = full.slice(0, locked) + randHex(Math.max(0, Math.min(8, full.length) - locked));
          if (locked >= Math.min(8, full.length)) {
            clearInterval(timer);
            if (hashEl) hashEl.textContent = full.slice(0, 8);
            row.classList.add('is-verified');
          }
        }, perChar);
        state.auditTimers.push(timer);
      }, i * perRow);
      state.auditTimers.push(startTimer);
    });
    var badgeTimer = setTimeout(function () {
      if (!dom.auditValid) return;
      dom.auditValid.className = 'badge' + (valid ? '' : ' badge--err');
      dom.auditValid.textContent = valid ? ('链校验通过 · ' + (checked || entries.length) + ' 条') : '链校验失败';
    }, rows.length * perRow + 8 * perChar + 90);
    state.auditTimers.push(badgeTimer);
  }
  function openAudit() {
    if (!dom.auditSheet) return;
    if (dom.auditValid) { dom.auditValid.className = 'badge badge--wait'; dom.auditValid.textContent = '校验中'; }
    if (dom.auditMeta) dom.auditMeta.textContent = '共 ' + (state.auditEntries.length || 0) + ' 条';
    dom.auditSheet.hidden = false;
    closeMenu();
    loadAudit(false);
  }
  function closeAudit() {
    if (dom.auditSheet) dom.auditSheet.hidden = true;
    clearAuditTimers();
  }
  async function loadAudit(quiet) {
    if (!state.sessionId) return;
    try {
      var res = await getJson('/api/audit?session_id=' + encodeURIComponent(state.sessionId) + '&limit=100');
      renderAudit(res || {});
    } catch (e) {
      if (dom.auditValid) { dom.auditValid.className = 'badge badge--err'; dom.auditValid.textContent = '校验失败'; }
      if (dom.auditList) dom.auditList.innerHTML = '<div class="trace__empty">' + esc(e.message || '审计数据暂时不可用') + '</div>';
      if (!quiet) toast(e.message || '审计数据暂时不可用', { type: 'error' });
    }
  }
  function copyText(text) {
    var t = String(text || '');
    if (!t) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(function () { toast('已复制', { sub: truncate(t, 40) }); }, function () { fallbackCopy(t); });
      return;
    }
    fallbackCopy(t);
  }
  function fallbackCopy(t) {
    try {
      var ta = document.createElement('textarea');
      ta.value = t;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      toast('已复制', { sub: truncate(t, 40) });
    } catch (e) { toast('复制失败，请手动选择', { type: 'error' }); }
  }
  function exportAudit() {
    if (!state.auditEntries.length) { toast('还没有可导出的审计事件', { type: 'error' }); return; }
    var lines = state.auditEntries.map(function (e) { return JSON.stringify(e); }).join('\n');
    try {
      var blob = new Blob([lines + '\n'], { type: 'application/x-ndjson' });
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download = 'audit.log';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
      toast('已导出 audit.log', { sub: state.auditEntries.length + ' 条事件（JSONL）' });
    } catch (e) { toast('导出失败', { type: 'error' }); }
  }

  // ===================== 插件 =====================
  var PLUGIN_KIND = {
    core: '核心', mcp: '外部服务', external: '外部服务', model: '模型通道',
    skill: '技能包', channel: '渠道', capability: '能力'
  };
  var PLUGIN_STATUS = {
    enabled: { text: '已启用', cls: 'plug__status--on' },
    disabled: { text: '已停用', cls: 'plug__status--off' },
    degraded: { text: '降级', cls: 'plug__status--degraded' }
  };
  function pluginId(p) { return (p && (p.id || p.name)) || ''; }
  function pluginKindName(kind) {
    var kinds = state.pluginKinds;
    if (Array.isArray(kinds)) {
      for (var i = 0; i < kinds.length; i++) {
        if (kinds[i] && kinds[i].id === kind) return kinds[i].name || PLUGIN_KIND[kind] || kind;
      }
    }
    return PLUGIN_KIND[kind] || kind || '插件';
  }
  function pluginStatusOf(p) {
    if (p && p.status) return p.status;
    return p && p.enabled ? 'enabled' : 'disabled';
  }
  function pluginLocked(p) {
    if (!p) return true;
    if (p.toggleable === false) return true;
    return p.kind === 'core' && p.toggleable !== true;
  }
  function pluginProvideList(provides) {
    var chips = [];
    if (Array.isArray(provides)) {
      provides.forEach(function (x) { if (x) chips.push(String(x)); });
    } else if (provides && typeof provides === 'object') {
      ['tools', 'skills', 'routes'].forEach(function (key) {
        var arr = provides[key];
        if (!Array.isArray(arr)) return;
        arr.forEach(function (x) { if (x) chips.push(String(x)); });
      });
    }
    return chips;
  }
  function pluginProvidesHtml(provides) {
    var chips = pluginProvideList(provides);
    if (!chips.length) return '<span class="plug__chip plug__chip--none">无对外能力</span>';
    var shown = chips.slice(0, 4).map(function (x) { return '<span class="plug__chip">' + esc(x) + '</span>'; }).join('');
    if (chips.length > 4) shown += '<span class="plug__more">+' + (chips.length - 4) + '</span>';
    return shown;
  }
  function pluginDetailHtml(p) {
    var detail = p.detail || {};
    var env = Array.isArray(detail.env) ? detail.env : [];
    var endpoints = Array.isArray(detail.endpoints) ? detail.endpoints : [];
    var requires = Array.isArray(p.requires) && p.requires.length ? p.requires.join('、') : '无';
    return '<div class="plug__dgrid">' +
      '<section class="plug__dsec"><h4>如何工作</h4><p>' + esc(detail.how || '服务端注册的能力项，启停即时生效。') + '</p></section>' +
      '<section class="plug__dsec"><h4>环境变量</h4>' +
        (env.length ? '<div class="plug__mono">' + env.map(function (e) { return '<span>' + esc(e) + '</span>'; }).join('') + '</div>' : '<p class="plug__dim">无需配置</p>') +
      '</section>' +
      '<section class="plug__dsec"><h4>接口</h4>' +
        (endpoints.length ? '<div class="plug__mono">' + endpoints.map(function (e) { return '<span>' + esc(e) + '</span>'; }).join('') + '</div>' : '<p class="plug__dim">无对外接口</p>') +
      '</section>' +
      '<section class="plug__dsec"><h4>依赖</h4><p>' + esc(requires) + '</p>' +
        (detail.last_error ? '<p class="plug__dim">最近错误：' + esc(detail.last_error) + '</p>' : '') +
      '</section>' +
    '</div>';
  }
  function pluginCardHtml(p) {
    var id = pluginId(p);
    var status = pluginStatusOf(p);
    var meta = PLUGIN_STATUS[status] || PLUGIN_STATUS.disabled;
    var locked = pluginLocked(p);
    var open = state.pluginOpenId === id;
    var on = !!p.enabled;
    var switchText = locked ? '已锁定' : (on ? '启用' : '停用');
    return '<article class="plug' + (on ? '' : ' is-off') + (open ? ' is-open' : '') + '" data-plugin="' + esc(id) + '" data-status="' + esc(status) + '">' +
      '<div class="plug__head" role="button" tabindex="0" aria-expanded="' + (open ? 'true' : 'false') + '">' +
        '<span class="plug__kind">' + esc(pluginKindName(p.kind)) + '</span>' +
        '<span class="plug__name">' + esc(p.name || id) + '</span>' +
        '<span class="plug__status ' + meta.cls + '"><i class="plug__statusdot" aria-hidden="true"></i>' + meta.text + '</span>' +
        (p.version ? '<span class="plug__ver">v' + esc(p.version) + '</span>' : '') +
      '</div>' +
      '<p class="plug__desc">' + esc(p.description || '') + '</p>' +
      '<div class="plug__provides">' + pluginProvidesHtml(p.provides) + '</div>' +
      '<div class="plug__foot">' +
        '<span class="plug__note">' + (locked
          ? '<svg class="plug__lock" viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>' + esc(p.locked_reason || '核心能力，不可停用')
          : '依赖：' + (Array.isArray(p.requires) && p.requires.length ? esc(p.requires.join('、')) : '无')) +
        '</span>' +
        '<span class="plug__switchwrap">' +
          '<button class="switch' + (on ? ' is-on' : '') + '" type="button" role="switch" aria-checked="' + (on ? 'true' : 'false') + '" data-toggle="' + esc(id) + '" aria-label="' + esc((on ? '停用 ' : '启用 ') + (p.name || id)) + '"' + (locked ? ' disabled' : '') + '><span class="switch__knob"></span></button>' +
          '<span class="plug__switchtext">' + switchText + '</span>' +
        '</span>' +
      '</div>' +
      '<div class="plug__detail"' + (open ? '' : ' hidden') + '>' + pluginDetailHtml(p) + '</div>' +
    '</article>';
  }
  function pluginCounts() {
    var on = 0, off = 0, degraded = 0;
    (state.plugins || []).forEach(function (p) {
      if (pluginStatusOf(p) === 'degraded') degraded += 1;
      if (p.enabled) on += 1; else off += 1;
    });
    return { total: (state.plugins || []).length, on: on, off: off, degraded: degraded };
  }
  function paintPluginCounts() {
    if (!dom.pluginsSummary) return;
    var c = pluginCounts();
    var bits = ['共 ' + c.total + ' 个插件', '已启用 ' + c.on, '已停用 ' + c.off];
    if (c.degraded) bits.push('降级 ' + c.degraded);
    dom.pluginsSummary.textContent = bits.join(' · ');
  }
  function renderPlugins(res) {
    if (res && Array.isArray(res.kinds)) state.pluginKinds = res.kinds;
    var plugins = (res && res.plugins) || [];
    state.plugins = plugins;
    paintPluginCounts();
    if (!dom.pluginsGrid) return;
    if (!plugins.length) {
      dom.pluginsGrid.innerHTML = '<div class="plug plug--empty">服务暂时不可用</div>';
      showInfoBar(dom.pluginsInfoBar, '插件清单暂时取不到，可点右上角「刷新」重试。', true);
      return;
    }
    showInfoBar(dom.pluginsInfoBar, '');
    dom.pluginsGrid.innerHTML = plugins.map(function (p) { return pluginCardHtml(p); }).join('');
  }
  async function loadPlugins(force) {
    if (!dom.pluginsGrid) return;
    if (state.pages.plugins && !force) return;
    state.pages.plugins = true;
    if (!(state.plugins || []).length) {
      dom.pluginsGrid.innerHTML = '<div class="plug plug--empty">正在读取插件…</div>';
    }
    try {
      var res = await getJson('/api/plugins');
      var plugins = (res && res.plugins) || [];
      if (!plugins.length) throw new Error('插件清单为空');
      renderPlugins(res);
    } catch (e) {
      state.pages.plugins = false;
      if (!(state.plugins || []).length) {
        dom.pluginsGrid.innerHTML = '<div class="plug plug--empty">服务暂时不可用</div>';
        paintPluginCounts();
      }
      showInfoBar(dom.pluginsInfoBar, '插件清单暂时取不到：' + (e.message || '服务暂时不可用'), true);
    }
  }
  function pluginsCardEl(id) {
    if (!dom.pluginsGrid) return null;
    var cards = dom.pluginsGrid.querySelectorAll('.plug');
    for (var i = 0; i < cards.length; i++) {
      if (cards[i].getAttribute('data-plugin') === id) return cards[i];
    }
    return null;
  }
  function paintPluginOpen() {
    if (!dom.pluginsGrid) return;
    var cards = dom.pluginsGrid.querySelectorAll('.plug');
    for (var i = 0; i < cards.length; i++) {
      var card = cards[i];
      var open = !!card.getAttribute('data-plugin') && card.getAttribute('data-plugin') === state.pluginOpenId;
      card.classList.toggle('is-open', open);
      var head = card.querySelector('.plug__head');
      if (head) head.setAttribute('aria-expanded', open ? 'true' : 'false');
      var detail = card.querySelector('.plug__detail');
      if (detail) detail.hidden = !open;
    }
  }
  function togglePluginOpen(id) {
    state.pluginOpenId = state.pluginOpenId === id ? null : id;
    paintPluginOpen();
  }
  function showPluginsError(text) {
    if (!dom.pluginsError) return;
    dom.pluginsError.hidden = false;
    if (dom.pluginsErrorText) dom.pluginsErrorText.textContent = text || '切换失败，请稍后重试';
  }
  function hidePluginsError() {
    if (dom.pluginsError) dom.pluginsError.hidden = true;
  }
  function linkageReload(plugin) {
    var id = pluginId(plugin);
    var kind = (plugin && plugin.kind) || '';
    if (kind === 'external' || id.indexOf('mcp') === 0) {
      state.pages.services = false;
      loadServices();
    }
    if (kind === 'channel' || id.indexOf('channel') >= 0) loadChannel();
    if (kind === 'model' || id.indexOf('llm') >= 0) loadLlmStatus();
    if (kind === 'skill' || id.indexOf('skill') >= 0) {
      state.pages.skills = false;
      loadSkills();
    }
    loadHealth();
  }
  async function togglePlugin(id) {
    if (state.pluginBusy) return;
    var plugin = null;
    (state.plugins || []).forEach(function (p) { if (pluginId(p) === id) plugin = p; });
    if (!plugin) return;
    if (pluginLocked(plugin)) {
      toast('核心能力不可停用', { sub: plugin.locked_reason || '风险分级与哈希链审计必须常驻' });
      return;
    }
    var next = !plugin.enabled;
    var card = pluginsCardEl(id);
    var sw = card ? card.querySelector('[data-toggle]') : null;
    var swText = card ? card.querySelector('.plug__switchtext') : null;
    state.pluginBusy = true;
    hidePluginsError();
    if (card) card.classList.add('is-busy');
    if (sw) { sw.disabled = true; sw.classList.add('is-busy'); }
    if (swText) swText.textContent = next ? '启用中…' : '停用中…';
    try {
      var res = await postJson('/api/plugins/' + encodeURIComponent(id) + '/toggle', { enabled: next });
      var updated = (res && res.plugin) || null;
      if (updated) {
        state.plugins = state.plugins.map(function (p) { return pluginId(p) === id ? updated : p; });
      } else {
        plugin.enabled = next;
        plugin.status = next ? 'enabled' : 'disabled';
      }
      renderPlugins({ plugins: state.plugins, kinds: state.pluginKinds });
      toast((next ? '已启用 · ' : '已停用 · ') + (plugin.name || id), { type: 'success', sub: '开关已保存，相关页面同步刷新' });
      linkageReload(updated || plugin);
    } catch (e) {
      // 悲观更新：请求失败时不改动任何状态，仅恢复按钮并提示
      if (card) card.classList.remove('is-busy');
      if (sw) { sw.disabled = false; sw.classList.remove('is-busy'); }
      if (swText) swText.textContent = plugin.enabled ? '启用' : '停用';
      var code = errCode(e);
      if (code === 'PLUGIN_CORE_LOCKED') {
        toast('核心能力不可停用', { sub: (e && e.message) || '风险分级与审计链必须常驻' });
      } else if (code === 'PLUGIN_NOT_FOUND') {
        toast('插件不存在', { sub: '清单可能已更新，点右上角「刷新」重新读取', type: 'error' });
      } else {
        showPluginsError('切换失败：' + ((e && e.message) || '请稍后重试'));
      }
    } finally {
      state.pluginBusy = false;
    }
  }

  // ===================== 技能 / 外部服务 / 设置 =====================
  function unavailableCard(text) {
    return '<div class="scard scard--empty"><div class="scard__empty">' + esc(text || '服务暂时不可用') + '</div></div>';
  }
  function showInfoBar(el, text, off) {
    if (!el) return;
    if (!text) { el.hidden = true; el.textContent = ''; return; }
    el.hidden = false;
    el.className = 'infobar' + (off ? ' infobar--off' : '');
    el.textContent = text;
  }
  async function loadSkills() {
    if (!dom.skillsGrid) return;
    if (state.pages.skills) return;
    state.pages.skills = true;
    dom.skillsGrid.innerHTML = '<div class="scard scard--empty"><div class="scard__empty">正在读取技能…</div></div>';
    try {
      var res = await getJson('/api/skills');
      var skills = (res && (res.skills || res.data)) || [];
      if (!skills.length) {
        dom.skillsGrid.innerHTML = unavailableCard('服务暂时不可用');
        showInfoBar(dom.skillsInfoBar, '技能清单暂时取不到，可稍后点右上角刷新重试。', true);
        return;
      }
      dom.skillsGrid.innerHTML = skills.map(function (s) {
        var risk = s.risk || 'low';
        return '<div class="scard"><div class="scard__top"><span class="scard__t">' + esc(s.name || s.id) + '</span>' +
          '<span class="tag ' + (risk === 'high' ? 'tag--err' : risk === 'medium' ? 'tag--warn' : '') + '">' + esc(riskMeta(risk).short) + '风险</span></div>' +
          '<div class="scard__tool">' + esc(s.tool || s.id || '') + '</div>' +
          '<div class="scard__d">' + esc(s.description || '') + '</div>' +
          (Array.isArray(s.examples) && s.examples.length ? '<div class="scard__meta">例：' + esc(s.examples[0]) + '</div>' : '') +
          '</div>';
      }).join('');
      showInfoBar(dom.skillsInfoBar, '');
    } catch (e) {
      state.pages.skills = false;
      dom.skillsGrid.innerHTML = unavailableCard('服务暂时不可用');
      showInfoBar(dom.skillsInfoBar, '技能清单暂时取不到：' + (e.message || '服务暂时不可用'), true);
    }
  }
  async function loadServices() {
    if (!dom.servicesGrid) return;
    if (state.pages.services) return;
    state.pages.services = true;
    dom.servicesGrid.innerHTML = '<div class="scard scard--empty"><div class="scard__empty">正在读取外部服务…</div></div>';
    try {
      var res = await getJson('/api/mcp/status');
      var servers = (res && res.servers) || [];
      if (!servers.length) {
        dom.servicesGrid.innerHTML = unavailableCard('服务暂时不可用');
        showInfoBar(dom.servicesInfoBar, '外部服务不可用，已降级为内置工具，六类业务仍可正常办理。', true);
        return;
      }
      var down = 0;
      dom.servicesGrid.innerHTML = servers.map(function (s) {
        var ok = s.status === 'ok';
        if (!ok) down += 1;
        return '<div class="scard"><div class="scard__top"><span class="scard__t">' + esc(s.name || 'service') + '</span>' +
          '<span class="tag ' + (ok ? 'tag--ok' : 'tag--err') + '">' + (ok ? '可用' : '降级') + '</span></div>' +
          '<div class="scard__tool">' + esc(s.url || '') + '</div>' +
          '<div class="scard__d">' + (ok ? '只读工具可用，作为增强数据源。' : '连接失败，已自动降级，业务不受影响。') + '</div>' +
          '<div class="scard__meta">延迟 ' + esc(String(s.latency_ms == null ? '—' : s.latency_ms)) + 'ms' +
          (s.last_error ? ' · ' + esc(truncate(s.last_error, 60)) : '') + '</div></div>';
      }).join('');
      if (res && res.enabled === false) showInfoBar(dom.servicesInfoBar, '外部服务未启用，全部使用内置工具。', true);
      else if (down > 0) showInfoBar(dom.servicesInfoBar, down + ' 个外部服务不可用，已降级处理。', true);
      else showInfoBar(dom.servicesInfoBar, '');
    } catch (e) {
      state.pages.services = false;
      dom.servicesGrid.innerHTML = unavailableCard('服务暂时不可用');
      showInfoBar(dom.servicesInfoBar, '外部服务状态取不到：' + (e.message || '服务暂时不可用') + '，已按降级处理。', true);
    }
  }
  async function loadChannel() {
    try {
      var res = await getJson('/api/channel/status');
      var enabled = !!(res && res.enabled);
      if (dom.channelTag) { dom.channelTag.className = 'tag ' + (enabled ? 'tag--ok' : 'tag--muted'); dom.channelTag.textContent = enabled ? '已启用' : '未启用'; }
      if (dom.channelEnabled) dom.channelEnabled.textContent = enabled ? '已启用' : '未启用';
      if (dom.channelPort) dom.channelPort.textContent = res && res.port != null ? String(res.port) : '—';
      if (dom.channelToken) dom.channelToken.textContent = res && res.token_configured ? '已配置（打码）' : '未配置';
      if (dom.channelSessions) dom.channelSessions.textContent = res && res.sessions != null ? String(res.sessions) : '0';
    } catch (e) {
      if (dom.channelTag) { dom.channelTag.className = 'tag tag--muted'; dom.channelTag.textContent = '不可用'; }
      if (dom.channelEnabled) dom.channelEnabled.textContent = '服务暂时不可用';
      if (dom.channelPort) dom.channelPort.textContent = '—';
      if (dom.channelToken) dom.channelToken.textContent = '—';
      if (dom.channelSessions) dom.channelSessions.textContent = '—';
    }
  }
  async function loadHealth() {
    try {
      var res = await getJson('/api/health');
      if (dom.healthTag) {
        dom.healthTag.className = 'tag ' + (res && res.status === 'ok' ? 'tag--ok' : 'tag--err');
        dom.healthTag.textContent = res && res.status === 'ok' ? '运行中' : '异常';
      }
      if (dom.healthVersion) dom.healthVersion.textContent = (res && (res.version || res.app)) || '—';
      if (dom.healthDemo) dom.healthDemo.textContent = res && res.demo_mode ? '开启' : '关闭';
      if (dom.healthMode) dom.healthMode.textContent = modeLabel(res && res.llm_mode);
      if (dom.healthSession) dom.healthSession.textContent = state.sessionId ? String(state.sessionId).slice(0, 12) : '—';
      return res;
    } catch (e) {
      if (dom.healthTag) { dom.healthTag.className = 'tag tag--err'; dom.healthTag.textContent = '不可用'; }
      if (dom.healthVersion) dom.healthVersion.textContent = '服务暂时不可用';
      return null;
    }
  }
  function loadSettings() {
    if (!state.pages.settings) state.pages.settings = true;
    loadChannel();
    loadHealth();
    loadLlmStatus();
  }
  function taskPageSize() { return 6; }
  function taskPageCount() { return Math.max(1, Math.ceil((state.taskTotal || 0) / taskPageSize())); }
  function updateTasksRefresh() {
    if (!dom.btnTasksRefresh) return;
    var pages = taskPageCount();
    var page = Math.min(state.taskPage + 1, pages);
    dom.btnTasksRefresh.textContent = '换一批 · ' + page + '/' + pages;
    dom.btnTasksRefresh.title = '切换到第 ' + page + ' / ' + pages + ' 批快捷指令';
  }
  async function loadTasksPage(page) {
    page = Math.max(0, page || 0);
    var size = taskPageSize();
    var remote = null;
    var tasks = [];
    try {
      remote = await getJson('/api/tasks?limit=' + size + '&offset=' + (page * size));
      tasks = (remote && remote.tasks) || [];
    } catch (e) {
      remote = null;
    }
    if (!tasks.length && remote && page > 0) {
      // 偏移越界（数据变化）：回到第一批，避免空白
      try {
        remote = await getJson('/api/tasks?limit=' + size + '&offset=0');
        tasks = (remote && remote.tasks) || [];
        page = 0;
      } catch (e2) { tasks = []; }
    }
    if (!tasks.length) {
      var totalLocal = DEFAULT_TASKS.length;
      if (page * size >= totalLocal) page = 0;
      tasks = DEFAULT_TASKS.slice(page * size, page * size + size);
      state.taskTotal = totalLocal;
      state.taskPage = page;
    } else {
      var total = remote && remote.total != null ? Number(remote.total) : (page * size + tasks.length);
      state.taskTotal = total > 0 ? total : tasks.length;
      var offset = remote && remote.offset != null ? Number(remote.offset) : page * size;
      state.taskPage = Math.max(0, Math.floor(offset / size));
    }
    state.tasksAll = tasks;
    state.tasks = tasks;
    renderChips(tasks);
    updateTasksRefresh();
    return { page: state.taskPage, pages: taskPageCount(), total: state.taskTotal, local: !remote };
  }
  async function loadTasks() {
    return loadTasksPage(0);
  }
  async function nextTaskPage() {
    if (state.taskBusy) return;
    var pages = taskPageCount();
    if (pages <= 1) {
      if (dom.chips) {
        dom.chips.classList.remove('is-shake');
        void dom.chips.offsetWidth;
        dom.chips.classList.add('is-shake');
        setTimeout(function () { if (dom.chips) dom.chips.classList.remove('is-shake'); }, 320);
      }
      toast('已经是这一批了', { sub: '快捷指令只有一批，也可以直接在下方描述业务' });
      return;
    }
    var btn = dom.btnTasksRefresh;
    var next = (state.taskPage + 1) % pages;
    state.taskBusy = true;
    if (btn) {
      btn.classList.add('is-busy');
      btn.disabled = true;
      btn.textContent = '加载中…';
    }
    if (dom.chips) dom.chips.classList.add('chips--swap');
    await sleep(reduceMotion() ? 1 : 160);
    try {
      await loadTasksPage(next);
    } finally {
      state.taskBusy = false;
      if (dom.chips) dom.chips.classList.remove('chips--swap');
      if (btn) {
        btn.classList.remove('is-busy');
        btn.disabled = false;
      }
      updateTasksRefresh();
    }
  }

  // ===================== 模式 =====================
  // 金额展示：模型调用成本通常很小，按量级自动选小数位
  function fmtCost(v) {
    var n = Number(v);
    if (!isFinite(n)) return '—';
    if (n === 0) return '¥0';
    var abs = Math.abs(n);
    if (abs < 0.0001) return '¥' + n.toFixed(6);
    if (abs < 0.01) return '¥' + n.toFixed(4);
    if (abs < 1) return '¥' + n.toFixed(3);
    return '¥' + n.toFixed(2);
  }
  function fmtMs(v) { return v == null ? '—' : (v + ' ms'); }

  // 设置页「模型接入」卡：全部字段容错，旧后端缺字段时降级为「—」/隐藏
  function renderLlmCard(status) {
    status = status || {};
    var eff = status.effective || (status.enabled ? 'llm' : 'rule');
    var configured = !!(status.key_masked || status.enabled);
    if (dom.llmProvider) dom.llmProvider.textContent = status.provider || '—';
    if (dom.llmModel) dom.llmModel.textContent = status.model || '—';
    if (dom.llmHost) dom.llmHost.textContent = status.base_host || status.base_url || '—';
    if (dom.llmKey) dom.llmKey.textContent = configured ? ('已配置 · ' + (status.key_masked || '打码')) : '未配置';
    if (dom.llmEffective) dom.llmEffective.textContent = eff === 'llm' ? '模型解析' : '规则解析（未走模型）';
    var breaker = status.breaker || {};
    var breakerText = '正常';
    if (status.degraded) {
      breakerText = '已熔断';
      if (breaker.retry_after_s != null) breakerText += ' · 剩余 ' + Math.max(0, Math.round(breaker.retry_after_s)) + 's';
      else if (breaker.cooldown_s != null) breakerText += ' · 冷却 ' + breaker.cooldown_s + 's';
    } else if (breaker.open) {
      breakerText = '已熔断';
    }
    if (dom.llmBreaker) dom.llmBreaker.textContent = breakerText;
    if (dom.llmTag) {
      var tagText = '已配置';
      var tagCls = 'tag tag--ok';
      if (status.degraded) { tagText = '熔断回退'; tagCls = 'tag tag--warn'; }
      else if (!configured) { tagText = '未配置 Key'; tagCls = 'tag tag--muted'; }
      dom.llmTag.className = tagCls;
      dom.llmTag.textContent = tagText;
    }
    // 用量统计：任一字段缺失都不报错，全部缺失则整行隐藏
    var st = status.stats || null;
    var parts = [];
    if (st) {
      if (st.calls != null) parts.push('调用 ' + st.calls + ' 次');
      if (st.ok != null) parts.push('成功 ' + st.ok);
      if (st.fail != null) parts.push('失败 ' + st.fail);
      if (st.retries) parts.push('重试 ' + st.retries);
      if (st.timeouts) parts.push('超时 ' + st.timeouts);
      if (st.circuit_skipped) parts.push('熔断跳过 ' + st.circuit_skipped);
      if (st.last_latency_ms != null && st.calls !== 0) parts.push('最近 ' + st.last_latency_ms + ' ms');
      if (st.total_cost_yuan != null) parts.push('累计约 ' + fmtCost(st.total_cost_yuan));
      else if (st.last_cost_yuan != null) parts.push('最近约 ' + fmtCost(st.last_cost_yuan));
    }
    if (dom.llmStats && dom.llmStatsText) {
      if (parts.length) {
        dom.llmStatsText.textContent = parts.join(' · ');
        dom.llmStats.hidden = false;
      } else {
        dom.llmStatsText.textContent = '—';
        dom.llmStats.hidden = true;
      }
    }
    if (dom.llmNote) {
      if (status.degraded && (breaker.failures != null || breaker.threshold != null)) {
        dom.llmNote.textContent = '连续失败已熔断，本次回退规则解析' + (breaker.failures != null && breaker.threshold != null ? '（' + breaker.failures + '/' + breaker.threshold + '）' : '') + '，冷却后自动重试。';
      } else if (status.last_error_hint || status.last_error) {
        dom.llmNote.textContent = '最近错误：' + (status.last_error_hint || status.last_error) + (status.last_error_code ? '（' + status.last_error_code + '）' : '');
      } else {
        dom.llmNote.textContent = '测试连接只发送一句极短提示词，消耗少量 token；Key 始终打码显示。';
      }
    }
  }

  function setLlmTestResult(text, kind) {
    if (!dom.llmTestResult) return;
    dom.llmTestResult.hidden = !text;
    dom.llmTestResult.className = 'llmtest__result' + (kind ? ' llmtest__result--' + kind : '');
    dom.llmTestResult.textContent = text || '';
  }

  async function testLlm() {
    if (state.llmTesting) return;
    state.llmTesting = true;
    var btn = dom.btnLlmTest;
    if (btn) { btn.disabled = true; btn.textContent = '测试中…'; }
    setLlmTestResult('正在请求模型…', null);
    try {
      var res = await postJson('/api/llm/test', {});
      if (res && res.ok) {
        var parts = ['连接正常'];
        if (res.latency_ms != null) parts.push('耗时 ' + res.latency_ms + ' ms');
        var u = res.usage || {};
        if (u.prompt_tokens != null || u.completion_tokens != null) {
          parts.push('输入 ' + (u.prompt_tokens == null ? '—' : u.prompt_tokens) + ' / 输出 ' + (u.completion_tokens == null ? '—' : u.completion_tokens) + ' tokens');
        }
        if (res.cost_yuan != null) parts.push('约 ' + fmtCost(res.cost_yuan));
        setLlmTestResult(parts.join(' · '), 'ok');
      } else {
        var msg = (res && (res.error || res.message)) || '连接失败';
        var hint = res && res.hint ? '（' + res.hint + '）' : '';
        setLlmTestResult(String(msg) + hint, 'err');
      }
      loadLlmStatus();
    } catch (e) {
      setLlmTestResult('请求失败，请确认服务在运行', 'err');
    } finally {
      state.llmTesting = false;
      if (btn) { btn.disabled = false; btn.textContent = '测试连接'; }
    }
  }

  function applyLlmStatus(status) {
    if (!status) return;
    state.llmStatus = status;
    var mode = status.mode || state.llmMode || 'auto';
    state.llmMode = mode;
    var eff = status.effective || (status.enabled ? 'llm' : 'rule');
    dom.modeBtns.forEach(function (btn) {
      var on = btn.getAttribute('data-mode') === mode;
      btn.classList.toggle('is-on', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      btn.disabled = !!state.modeBusy;
    });
    if (dom.modeHint) dom.modeHint.textContent = MODE_HINT[mode] || MODE_HINT.auto;
    if (dom.modeBadge) {
      var textEl = dom.modeBadge.querySelector('.effective__text');
      var label = '实际：' + (eff === 'llm' ? '模型' : '规则');
      if (status.degraded) label += ' · 熔断';
      if (textEl) textEl.textContent = label;
      else dom.modeBadge.textContent = label;
      dom.modeBadge.classList.toggle('is-llm', eff === 'llm');
      var bits = ['推理模式：' + (eff === 'llm' ? '模型' : '规则')];
      if (status.provider) bits.push('provider ' + status.provider);
      if (status.model) bits.push('model ' + status.model);
      if (status.key_masked) bits.push('key ' + status.key_masked);
      if (status.base_host) bits.push('host ' + status.base_host);
      if (status.last_error) bits.push('最近错误 ' + status.last_error);
      if (status.degraded) bits.push('熔断中，已回退规则解析');
      dom.modeBadge.title = bits.join(' · ');
    }
    renderLlmCard(status);
  }
  async function loadLlmStatus() {
    try { applyLlmStatus(await getJson('/api/llm/status')); } catch (e) { /* 静默降级 */ }
  }
  async function switchLlmMode(mode) {
    if (state.modeBusy || mode === state.llmMode) return;
    state.modeBusy = true;
    dom.modeBtns.forEach(function (b) { b.disabled = true; });
    try {
      var res = await postJson('/api/llm/mode', { mode: mode });
      state.modeBusy = false;
      applyLlmStatus(res);
      toast('已切换为「' + modeLabel(mode) + '」模式', { sub: MODE_HINT[mode] });
    } catch (e) {
      state.modeBusy = false;
      applyLlmStatus(state.llmStatus || { mode: state.llmMode });
      toast(e.message || '模式切换失败', { type: 'error' });
    }
  }

  // ===================== 菜单 / 弹窗 / 重置 =====================
  function showHealth() {
    closeMenu();
    getJson('/api/health').then(function (res) {
      var rows = [
        ['状态', res.status || '—'],
        ['应用', res.app || '—'],
        ['版本', res.version || '—'],
        ['解析模式', modeLabel(res.llm_mode)],
        ['演示模式', res.demo_mode ? '开启' : '关闭'],
        ['种子版本', res.seed_version == null ? '—' : String(res.seed_version)]
      ];
      showModal('API 健康', '<div class="kvlist">' + rows.map(function (r) {
        return '<div class="kvrow"><span class="k">' + esc(r[0]) + '</span><span class="v mono">' + esc(r[1]) + '</span></div>';
      }).join('') + '</div>', [{ label: '关闭' }]);
      loadHealth();
    }).catch(function (e) {
      showModal('API 健康', '<p class="muted">' + esc(e.message || '服务暂时不可用') + '</p>', [{ label: '关闭' }]);
    });
  }
  function showAbout() {
    closeMenu();
    showModal('关于 ZeroStep', '<p>ZeroStep 零步银行：说一句话，AI 规划并调用确定性工具完成银行业务。</p>' +
      '<p class="muted">三级风险分级：绿·自动执行 / 黄·需确认 / 红·需动态验证码。所有动作写入 SHA-256 哈希链审计，转账 60 秒内可撤销。</p>',
      [{ label: '知道了' }]);
  }
  function confirmReset() {
    closeMenu();
    showModal('重置演示数据', '<p>将清空当前会话与全部业务数据，恢复初始种子数据。</p><p class="muted">用于反复演示同一套流程，操作不可撤销。</p>', [
      { label: '取消' },
      { label: '确认重置', primary: true, onClick: function () { doReset(); } }
    ]);
  }
  async function doReset() {
    try {
      await postJson('/api/admin/reset', {});
    } catch (e) {
      toast(e.message || '重置失败', { type: 'error' });
      return;
    }
    clearConversation();
    clearApproval();
    closeApprovalSheet();
    state.pages = { skills: false, plugins: false, services: false, settings: false };
    state.plugins = [];
    state.pluginOpenId = null;
    state.taskPage = 0;
    resetPush();
    await bootSession();
    await loadTasks();
    loadLlmStatus();
    if (state.view === 'settings') loadSettings();
    toast('演示数据已重置', { type: 'success', sub: '已重新建立会话' });
    if (state.view !== 'home' && state.view !== 'chat') showView('home');
  }
  function clearConversation() {
    if (dom.chat) dom.chat.innerHTML = '<div class="chat__empty" id="chatEmpty">还没有对话。说一句话，例如「查一下这个月支出最多的三类」。</div>';
    dom.chatEmpty = document.getElementById('chatEmpty');
    state.currentTurn = null;
    traceIdle();
    state.sampleUsed = false;
    renderSamples();
  }
  // ===================== 模拟推送 =====================
  function setPushKind(kind) {
    if (!PUSH_KIND[kind]) return;
    state.pushKind = kind;
    if (!dom.pushKinds) return;
    Array.prototype.forEach.call(dom.pushKinds.querySelectorAll('[data-push]'), function (b) {
      var on = b.getAttribute('data-push') === kind;
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }
  function setPushStatus(text, tone) {
    if (!dom.pushStatus) return;
    if (!text) { dom.pushStatus.hidden = true; dom.pushStatus.textContent = ''; dom.pushStatus.className = 'push__status'; return; }
    dom.pushStatus.hidden = false;
    dom.pushStatus.textContent = text;
    dom.pushStatus.className = 'push__status' + (tone === 'err' ? ' push__status--err' : tone === 'ok' ? ' push__status--ok' : '');
  }
  function resetPush() {
    state.pushBusy = false;
    state.pushKind = 'subscription';
    setPushKind('subscription');
    if (dom.pushTag) { dom.pushTag.className = 'tag tag--muted'; dom.pushTag.textContent = '未触发'; }
    setPushStatus('');
    if (dom.pushResult) dom.pushResult.hidden = true;
    if (dom.pushTitle) dom.pushTitle.textContent = '';
    if (dom.pushText) dom.pushText.textContent = '';
    if (dom.pushMeta) dom.pushMeta.textContent = '';
    if (dom.btnPushPreview) { dom.btnPushPreview.disabled = false; dom.btnPushPreview.textContent = '模拟推送'; }
  }
  function renderPush(data, kind) {
    data = data || {};
    var k = PUSH_KIND[data.kind] ? data.kind : (PUSH_KIND[kind] ? kind : 'subscription');
    var failed = data.ok === false;
    var ch = data.channel || {};
    var tagText = '已生成';
    var tagCls = 'tag tag--ok';
    if (failed) { tagText = '投递失败'; tagCls = 'tag tag--err'; }
    else if (data.delivered) { tagText = '已推送到渠道'; tagCls = 'tag tag--ok'; }
    else if (data.pushed === false) { tagText = '未配置推送地址'; tagCls = 'tag tag--muted'; }
    if (dom.pushTag) { dom.pushTag.className = tagCls; dom.pushTag.textContent = tagText; }
    if (dom.pushTitle) dom.pushTitle.textContent = data.title || (PUSH_KIND[k].label + '提醒');
    if (dom.pushText) dom.pushText.textContent = data.text || (failed ? '服务暂时不可用，稍后再试。' : '已生成推送内容。');
    var bits = [];
    if (data.delivered) bits.push('已推送到渠道');
    else if (data.pushed === false) bits.push('已生成，未配置推送地址');
    if (ch.status) {
      var CH_STATUS = { ok: '渠道正常', configured: '渠道已配置', not_configured: '未配置推送地址', unconfigured: '未配置推送地址', down: '渠道不可用', disabled: '渠道已停用' };
      bits.push(CH_STATUS[ch.status] || ('渠道 ' + ch.status));
    }
    if (ch.latency_ms != null && isFinite(Number(ch.latency_ms))) bits.push(Number(ch.latency_ms) + 'ms');
    if (ch.url_masked) bits.push(String(ch.url_masked));
    if (data.error) bits.push('错误：' + data.error);
    else if (ch.error) bits.push('错误：' + ch.error);
    if (data.ts) bits.push(fmtTime(data.ts));
    if (dom.pushMeta) dom.pushMeta.textContent = bits.join(' · ');
    if (dom.pushResult) dom.pushResult.hidden = false;
    if (failed) setPushStatus((data.error || ch.error || '推送失败'), 'err');
    else if (data.delivered) setPushStatus('已推送到渠道', 'ok');
    else if (data.pushed === false) setPushStatus('已生成，未配置推送地址', 'mute');
    else setPushStatus('已生成', 'ok');
  }
  async function sendPush() {
    if (state.pushBusy) return;
    if (!state.sessionId) { toast('会话尚未就绪', { type: 'error', sub: '请稍候再试' }); return; }
    var kind = PUSH_KIND[state.pushKind] ? state.pushKind : 'subscription';
    state.pushBusy = true;
    if (dom.btnPushPreview) { dom.btnPushPreview.disabled = true; dom.btnPushPreview.textContent = '推送中…'; }
    setPushStatus('正在投递…', 'mute');
    try {
      var data = await postJson('/api/push', { session_id: state.sessionId, kind: kind });
      renderPush(data, kind);
    } catch (e) {
      var msg = (e && e.message) || '推送失败';
      if (!e || !e.status || e.status === 404 || e.status === 501) msg = '服务暂时不可用';
      renderPush({ ok: false, kind: kind, error: msg, title: PUSH_KIND[kind].label + '提醒', text: '推送通道暂时不可用，业务不受影响。' }, kind);
    } finally {
      state.pushBusy = false;
      if (dom.btnPushPreview) { dom.btnPushPreview.disabled = false; dom.btnPushPreview.textContent = '模拟推送'; }
    }
  }

  // ===================== 手机视口 / PWA =====================
  function isMobileViewport() {
    var w = window.innerWidth;
    return !!w && w <= 720;
  }
  function applyViewportHeight() {
    var panel = dom.approvalPanel;
    if (!panel) return;
    if (!isMobileViewport()) { panel.style.maxHeight = ''; return; }
    var vv = window.visualViewport;
    var h = vv && vv.height ? vv.height : window.innerHeight;
    if (!h || !isFinite(h)) { panel.style.maxHeight = ''; return; }
    panel.style.maxHeight = Math.max(260, Math.round(h) - 24) + 'px';
  }
  function watchViewport() {
    var vv = window.visualViewport;
    var onResize = function () { applyViewportHeight(); };
    if (vv && typeof vv.addEventListener === 'function') {
      vv.addEventListener('resize', onResize);
      vv.addEventListener('scroll', onResize);
    }
    if (typeof window.addEventListener === 'function') window.addEventListener('resize', onResize);
  }
  function registerServiceWorker() {
    try {
      if (typeof navigator === 'undefined' || !navigator.serviceWorker) return;
      if (typeof navigator.serviceWorker.register !== 'function') return;
      var p = navigator.serviceWorker.register('/sw.js');
      if (p && typeof p.catch === 'function') p.catch(function () { /* 注册失败静默降级，在线使用不受影响 */ });
    } catch (e) { /* 静默降级 */ }
  }

  // ===================== 发送 =====================
  function autoGrow() {
    if (!dom.input) return;
    dom.input.style.height = 'auto';
    var h = Math.min(160, Math.max(58, dom.input.scrollHeight));
    dom.input.style.height = h + 'px';
  }
  // ---- 会话锁定：倒计时自动恢复 ----
  function lockedNow() { return !!state.lockActive && state.lockUntil > Date.now(); }
  function paintLockUi(on, seconds) {
    if (dom.lockBar) dom.lockBar.hidden = !on;
    if (dom.lockBarText) dom.lockBarText.textContent = on ? ('会话已临时锁定，剩余 ' + Math.max(0, Math.round(seconds)) + ' 秒') : '';
    if (dom.input) dom.input.disabled = !!on;
    if (dom.btnSend) dom.btnSend.disabled = !!on || !!state.busy;
    if (on && dom.input) { try { dom.input.blur(); } catch (e) {} }
  }
  function stopLockTimer() { if (state.lockTimer) { clearInterval(state.lockTimer); state.lockTimer = null; } }
  function clearLocked(notify) {
    stopLockTimer();
    var was = state.lockActive;
    state.lockActive = false;
    state.lockUntil = 0;
    paintLockUi(false, 0);
    if (notify && was) toast('会话已恢复', { type: 'success', sub: '可以继续发送指令' });
  }
  function applyLocked(untilIso) {
    var ms = untilIso ? new Date(untilIso).getTime() : NaN;
    if (isNaN(ms) || ms <= Date.now()) ms = Date.now() + 30000;
    state.lockUntil = ms;
    state.lockActive = true;
    stopLockTimer();
    function tick() {
      var left = Math.max(0, Math.round((state.lockUntil - Date.now()) / 1000));
      if (left <= 0) { clearLocked(true); return; }
      paintLockUi(true, left);
    }
    tick();
    state.lockTimer = setInterval(tick, 500);
  }
  function setBusy(busy) {
    state.busy = busy;
    if (!busy) { state.streaming = false; state.streamCancelRequested = false; }
    if (dom.input) dom.input.disabled = lockedNow();
    var composer = dom.btnSend ? dom.btnSend.closest('.composer') : null;
    if (composer) composer.classList.toggle('is-busy', busy);
    setSendMode(busy ? (state.streaming ? 'stop' : 'busy') : 'idle');
  }
  function userMessage(text) {
    removeEmptyState();
    var el = document.createElement('div');
    el.className = 'chat__me';
    el.textContent = text;
    if (dom.chat) dom.chat.appendChild(el);
    scrollChat();
  }
  async function sendText(text, opts) {
    var t = String(text == null ? '' : text).trim();
    if (!t || state.busy) return;
    if (lockedNow()) { toast('会话已临时锁定，请稍后再试', { type: 'error', sub: '倒计时结束后自动恢复' }); return; }
    opts = opts || {};
    hideError();
    userMessage(t);
    if (dom.input) { dom.input.value = ''; autoGrow(); }
    state.lastUserText = t;
    showView('chat');
    beginTurn();
    setBusy(true);
    try {
      await streamChat('/api/chat', { session_id: state.sessionId, text: t, stream: true }, handleEvent);
      var turn = state.currentTurn;
      if (turn && !turn.done) finishTurn('ok', null);
    } catch (e) {
      var cur = state.currentTurn;
      if (cur) {
        dropThinking(cur);
        if (cur.answerSeen && !cur.done) finishAnswerBubble(cur, null);
        cur.done = true;
      }
      renderErrorLine(e.message || '请求失败，请重试');
      traceDone('error');
      showError(e.message || '网络异常，请求失败', function () { sendText(t); });
    } finally {
      state.streaming = false;
      state.streamCancelRequested = false;
      setBusy(false);
      if (!opts || !opts.keepFocus) {
        if (dom.input && !isOtpRequired() && !lockedNow()) { try { dom.input.focus(); } catch (e) {} }
      }
    }
  }
  function setupVoice() {
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!dom.btnMic) return;
    if (!SR) {
      dom.btnMic.addEventListener('click', function () { toast('当前浏览器不支持语音输入', { type: 'error', sub: '可以继续用键盘输入' }); });
      return;
    }
    var rec = new SR();
    rec.lang = 'zh-CN';
    rec.interimResults = false;
    rec.maxAlternatives = 1;
    var listening = false;
    rec.onresult = function (ev) {
      var res = ev.results && ev.results[0];
      var said = res && res[0] ? res[0].transcript : '';
      if (said && dom.input) { dom.input.value = said; autoGrow(); }
    };
    rec.onerror = function () {
      listening = false;
      if (dom.btnMic) dom.btnMic.classList.remove('is-listening');
    };
    rec.onend = function () {
      listening = false;
      if (dom.btnMic) dom.btnMic.classList.remove('is-listening');
    };
    dom.btnMic.addEventListener('click', function () {
      if (listening) { try { rec.stop(); } catch (e) {} return; }
      try {
        rec.start();
        listening = true;
        dom.btnMic.classList.add('is-listening');
        toast('请说话…', { sub: '识别结果会填入输入框' });
      } catch (e) {
        listening = false;
        dom.btnMic.classList.remove('is-listening');
      }
    });
  }
  // ===================== 事件绑定 =====================
  function bindEvents() {
    if (dom.btnSend) dom.btnSend.addEventListener('click', function () {
      if (dom.btnSend.classList.contains('is-stop')) { cancelTurn(); return; }
      sendText(dom.input ? dom.input.value : '');
    });
    if (dom.input) {
      dom.input.addEventListener('input', autoGrow);
      dom.input.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) {
          ev.preventDefault();
          sendText(dom.input.value);
        }
      });
    }
    if (dom.errorRetry) dom.errorRetry.addEventListener('click', function () { if (state.retry) state.retry(); });
    if (dom.errorClose) dom.errorClose.addEventListener('click', hideError);
    if (dom.btnTasksRefresh) dom.btnTasksRefresh.addEventListener('click', nextTaskPage);
    document.addEventListener('click', function (ev) {
      var nav = ev.target && ev.target.closest ? ev.target.closest('[data-nav]') : null;
      if (nav) { ev.preventDefault(); showView(nav.getAttribute('data-nav')); return; }
      var goBtn = ev.target && ev.target.closest ? ev.target.closest('[data-insight-text]') : null;
      if (goBtn) {
        ev.preventDefault();
        var sentence = goBtn.getAttribute('data-insight-text') || '';
        if (state.busy) { toast('请等当前指令执行完再处理', { type: 'error' }); return; }
        if (sentence) sendText(sentence);
        return;
      }
      var textEl = ev.target && ev.target.closest ? ev.target.closest('[data-text]') : null;
      if (textEl && (textEl.classList.contains('chip') || textEl.classList.contains('sample'))) {
        ev.preventDefault();
        sendText(textEl.getAttribute('data-text'));
        return;
      }
      var pushBtn = ev.target && ev.target.closest ? ev.target.closest('[data-push]') : null;
      if (pushBtn) { ev.preventDefault(); setPushKind(pushBtn.getAttribute('data-push')); return; }
      var act = ev.target && ev.target.closest ? ev.target.closest('[data-action]') : null;
      if (act) {
        var action = act.getAttribute('data-action');
        if (action === 'health') { showHealth(); return; }
        if (action === 'reset') { confirmReset(); return; }
        if (action === 'about') { showAbout(); return; }
      }
      if (dom.moreMenu && !dom.moreMenu.hidden) {
        if (!(ev.target === dom.btnMore || dom.moreMenu.contains(ev.target) || (dom.btnMore && dom.btnMore.contains(ev.target)))) closeMenu();
      }
      if (dom.modal && !dom.modal.hidden && ev.target === dom.modal) closeModal();
    });
    if (dom.btnMore) dom.btnMore.addEventListener('click', function (ev) { ev.stopPropagation(); if (dom.moreMenu) dom.moreMenu.hidden = !dom.moreMenu.hidden; });
    dom.modeBtns.forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (dom.moreMenu && dom.moreMenu.contains(btn)) closeMenu();
        switchLlmMode(btn.getAttribute('data-mode'));
      });
    });
    if (dom.modeBadge) dom.modeBadge.addEventListener('click', function () { toast(dom.modeBadge.title || '推理模式', { sub: '规则与模型只影响理解，执行与风控始终是确定性代码' }); });
    if (dom.btnAudit) dom.btnAudit.addEventListener('click', openAudit);
    if (dom.btnAuditClose) dom.btnAuditClose.addEventListener('click', closeAudit);
    if (dom.btnAuditRefresh) dom.btnAuditRefresh.addEventListener('click', function () { loadAudit(false); });
    if (dom.btnAuditExport) dom.btnAuditExport.addEventListener('click', exportAudit);
    if (dom.auditList) {
      dom.auditList.addEventListener('click', function (ev) {
        var copyBtn = ev.target && ev.target.closest ? ev.target.closest('.arow__copy') : null;
        if (copyBtn) {
          ev.stopPropagation();
          var row = copyBtn.closest('.arow');
          var hashEl = row ? row.querySelector('.arow__hash') : null;
          copyText(hashEl ? (hashEl.getAttribute('data-full') || hashEl.textContent) : '');
          return;
        }
        var r = ev.target && ev.target.closest ? ev.target.closest('.arow') : null;
        if (!r) return;
        var detail = r.querySelector('.arow__detail');
        var open = r.classList.toggle('is-open');
        if (detail) detail.hidden = !open;
      });
    }
    if (dom.btnTraceToggle) {
      dom.btnTraceToggle.addEventListener('click', function () {
        var off = document.body.getAttribute('data-trace') === 'off';
        document.body.setAttribute('data-trace', off ? 'on' : 'off');
        dom.btnTraceToggle.textContent = off ? '收起' : '展开';
        dom.btnTraceToggle.setAttribute('aria-expanded', off ? 'true' : 'false');
      });
    }
    if (dom.btnApprove) dom.btnApprove.addEventListener('click', function () { submitApproval('approve'); });
    if (dom.btnReject) dom.btnReject.addEventListener('click', function () { submitApproval('reject'); });
    if (dom.btnDemoTotp) dom.btnDemoTotp.addEventListener('click', fetchDemoTotp);
    if (dom.dlgFactors) {
      dom.dlgFactors.addEventListener('click', function (ev) {
        var b = ev.target && ev.target.closest ? ev.target.closest('[data-factor]') : null;
        if (!b || b.disabled) return;
        setApprovalFactor(b.getAttribute('data-factor'));
      });
    }
    if (dom.dlgSmsSend) dom.dlgSmsSend.addEventListener('click', function () { sendDemoSms(); });
    if (dom.dlgSmsCode) {
      dom.dlgSmsCode.addEventListener('click', function () {
        if (!state.approvalSms || !state.approvalSms.code) return;
        fillOtp(state.approvalSms.code);
        clearFieldError();
      });
    }
    if (dom.dlgFaceScan) dom.dlgFaceScan.addEventListener('click', function () { startFaceScan(); });
    if (dom.pendingPill) dom.pendingPill.addEventListener('click', reopenPendingApproval);
    if (dom.btnSkillsRefresh) dom.btnSkillsRefresh.addEventListener('click', function () { state.pages.skills = false; loadSkills(); });
    if (dom.btnServicesRefresh) dom.btnServicesRefresh.addEventListener('click', function () { state.pages.services = false; loadServices(); });
    if (dom.btnChannelRefresh) dom.btnChannelRefresh.addEventListener('click', loadSettings);
    if (dom.btnLlmTest) dom.btnLlmTest.addEventListener('click', function () { testLlm(); });
    if (dom.btnPushPreview) dom.btnPushPreview.addEventListener('click', function () { sendPush(); });
    if (dom.btnPluginsRefresh) dom.btnPluginsRefresh.addEventListener('click', function () { loadPlugins(true); });
    if (dom.pluginsErrorClose) dom.pluginsErrorClose.addEventListener('click', hidePluginsError);
    if (dom.pluginsGrid) {
      dom.pluginsGrid.addEventListener('click', function (ev) {
        var sw = ev.target && ev.target.closest ? ev.target.closest('[data-toggle]') : null;
        if (sw) {
          ev.preventDefault();
          togglePlugin(sw.getAttribute('data-toggle'));
          return;
        }
        var card = ev.target && ev.target.closest ? ev.target.closest('.plug') : null;
        if (!card || !card.getAttribute('data-plugin')) return;
        togglePluginOpen(card.getAttribute('data-plugin'));
      });
      dom.pluginsGrid.addEventListener('keydown', function (ev) {
        if (ev.key !== 'Enter' && ev.key !== ' ' && ev.key !== 'Spacebar') return;
        var head = ev.target && ev.target.closest ? ev.target.closest('.plug__head') : null;
        if (!head) return;
        ev.preventDefault();
        var card = head.closest('.plug');
        if (card) togglePluginOpen(card.getAttribute('data-plugin'));
      });
    }
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') { if (closeTopLayer()) ev.preventDefault(); }
    });
  }
  // ===================== 启动 =====================
  async function bootSession() {
    try {
      var session = await postJson('/api/session', {});
      if (!session || !session.session_id) throw new Error('会话创建失败');
      state.sessionId = session.session_id;
      state.user = session.user || null;
      renderAccounts(session);
      if (dom.healthSession) dom.healthSession.textContent = String(session.session_id).slice(0, 12);
    } catch (e) {
      showError(e.message || '无法连接后端服务，请确认服务已启动', function () { bootSession(); });
      if (dom.acctDelta) dom.acctDelta.innerHTML = '<span>账户读取失败</span>';
    }
  }
  // ===================== 专区：仪表盘 / 分析 =====================
  // 只读视图：数据来自 /api/dashboard 与 /api/analytics，不触碰账本，也不写审计链。
  var ZONE_META = {
    dashboard: {
      title: '钱在哪、花在哪、要不要动手',
      sub: '只读聚合：数据与对话共用同一套账本口径，看数不会改动任何账户，也不写审计链。'
    },
    analytics: {
      title: '花在哪里，和上一期比变了多少',
      sub: '所选区间与上一等长区间对比：类别、商户、异常、订阅都在同一口径下统计。'
    }
  };
  var ZONE_PRESETS = [
    { id: '7d', label: '近 7 天', days: 7 },
    { id: '30d', label: '近 30 天', days: 30 },
    { id: '90d', label: '近 90 天', days: 90 },
    { id: '180d', label: '近 180 天', days: 180 }
  ];
  var ZONE_GRANS = [
    { id: 'day', label: '按日' },
    { id: 'week', label: '按周' },
    { id: 'month', label: '按月' }
  ];

  function zone() { return state.zone; }

  // ---------- 基础格式 ----------
  function zoneIsoDay(d) {
    d = d instanceof Date ? d : new Date(d);
    if (isNaN(d.getTime())) d = new Date();
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
  }
  function zoneDayLabel(iso) {
    var s = String(iso || '');
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s);
    return m ? (Number(m[2]) + '/' + Number(m[3])) : s;
  }
  function zoneMonthLabel(iso) {
    var m = /^(\d{4})-(\d{2})/.exec(String(iso || ''));
    return m ? (Number(m[2]) + ' 月') : String(iso || '');
  }
  function zoneBucketLabel(bucket) {
    var s = String(bucket || '');
    var w = /^(\d{4})-W(\d{2})$/.exec(s);
    if (w) return '第 ' + Number(w[2]) + ' 周';
    if (/^\d{4}-\d{2}$/.test(s)) return zoneMonthLabel(s);
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return zoneDayLabel(s);
    return s;
  }
  function zoneTime(iso) {
    if (!iso) return '--:--';
    var s = String(iso);
    var m = /(\d{2}):(\d{2})/.exec(s.length > 10 ? s.slice(10) : s);
    return m ? m[1] + ':' + m[2] : s.slice(0, 5);
  }
  function zoneDateTime(iso) {
    if (!iso) return '--';
    var s = String(iso);
    var m = /^\d{4}-(\d{2})-(\d{2})/.exec(s);
    return m ? m[1] + '-' + m[2] + ' ' + zoneTime(s) : zoneTime(s);
  }
  function zoneWan(cents) {
    var yuan = Number(cents || 0) / 100;
    if (yuan >= 10000) {
      var w = yuan / 10000;
      return (w >= 100 ? w.toFixed(0) : w.toFixed(w >= 10 ? 1 : 2)) + '万';
    }
    return String(Math.round(yuan));
  }
  function zoneNiceCeil(v) {
    var n = Number(v) || 0;
    if (n <= 0) return 1;
    var mag = Math.pow(10, Math.floor(Math.log(n) / Math.LN10));
    var steps = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
    for (var i = 0; i < steps.length; i++) {
      if (n <= mag * steps[i]) return mag * steps[i];
    }
    return mag * 10;
  }
  function zoneDeltaPill(v, goodWhenUp) {
    if (v == null || v === '' || isNaN(Number(v))) {
      return '<span class="zkpi__delta zkpi__delta--flat">无对比</span>';
    }
    var n = Number(v);
    if (n === 0) return '<span class="zkpi__delta zkpi__delta--flat">持平</span>';
    var up = n > 0;
    var tone = (goodWhenUp ? up : !up) ? 'good' : 'bad';
    return '<span class="zkpi__delta zkpi__delta--' + tone + '">' + (up ? '↑' : '↓') + ' ' + Math.abs(n).toFixed(0) + '%</span>';
  }
  function zoneEmptyHtml(text) {
    return '<div class="zempty">' + esc(text || '暂无数据') + '</div>';
  }
  function zoneErrorHtml(msg) {
    return '<div class="zerror"><p class="zerror__text">' + esc(msg || '数据读取失败') + '</p>' +
      '<button class="btn-secondary" type="button" id="zoneRetry">重试</button></div>';
  }
  function zoneSkeleton() {
    var cells = [];
    for (var i = 0; i < 6; i++) cells.push('<div class="zskeleton zskeleton--kpi"></div>');
    return '<div class="zskeleton zskeleton--line"></div>' +
      '<div class="zkpis">' + cells.join('') + '</div>' +
      '<div class="zskeleton zskeleton--chart"></div>' +
      '<div class="zskeleton zskeleton--chart"></div>';
  }
  function zoneStamp(text) {
    var el = document.getElementById('zoneStamp');
    if (el) el.textContent = text || '';
  }

  // ---------- 结构片段 ----------
  function zoneSection(title, note, body, extra) {
    return '<section class="zcard' + (extra ? ' ' + extra : '') + '">' +
      '<div class="zcard__head"><h2 class="zcard__t">' + esc(title) + '</h2>' +
      (note ? '<span class="zcard__note">' + esc(note) + '</span>' : '') + '</div>' +
      '<div class="zcard__body">' + body + '</div></section>';
  }
  function zoneKpiCard(k) {
    var html = '<article class="zkpi">';
    html += '<div class="zkpi__head"><span class="zkpi__label">' + esc(k.label) + '</span>' + (k.delta != null ? zoneDeltaPill(k.delta, k.goodWhenUp) : '') + '</div>';
    html += '<div class="zkpi__value">' + esc(k.value) + '</div>';
    if (k.spark && k.spark.length > 1) html += zoneSparkSvg(k.spark, k.tone || 'accent');
    if (k.note) html += '<div class="zkpi__note">' + esc(k.note) + '</div>';
    html += '</article>';
    return html;
  }
  function zoneLegendRow(items) {
    return '<ul class="zlegend zlegend--inline">' + items.map(function (it) {
      return '<li class="zlegend__item"><i class="zlegend__dot zlegend__dot--' + it[0] + '"></i><span class="zlegend__name">' + esc(it[1]) + '</span></li>';
    }).join('') + '</ul>';
  }
  function zoneTableDetails(summary, head, rows) {
    var th = head.map(function (h) { return '<th scope="col">' + esc(h) + '</th>'; }).join('');
    return '<details class="zdetails"><summary>' + esc(summary) + '</summary>' +
      '<div class="ztable__wrap"><table class="ztable"><thead><tr>' + th + '</tr></thead><tbody>' +
      rows.join('') + '</tbody></table></div></details>';
  }

  // ---------- 图表（内联 SVG：无 canvas 依赖，自测桩也能渲染） ----------
  function zoneSparkSvg(values, tone) {
    var vals = (values || []).map(function (v) { return Number(v) || 0; });
    if (vals.length < 2) return '';
    var W = 104, H = 28, pad = 3;
    var max = Math.max.apply(null, vals), min = Math.min.apply(null, vals);
    if (max === min) { max = min + 1; }
    var pts = vals.map(function (v, i) {
      var x = (i / (vals.length - 1)) * W;
      var y = H - pad - ((v - min) / (max - min)) * (H - pad * 2);
      return x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');
    return '<svg class="zspark zspark--' + esc(tone || 'accent') + '" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" aria-hidden="true" focusable="false"><polyline points="' + pts + '"></polyline></svg>';
  }
  function zoneChartWidth() {
    var doc = typeof document !== 'undefined' ? document : null;
    var w = doc && doc.documentElement ? Number(doc.documentElement.clientWidth) || 0 : 0;
    if (!w) return 760;
    return Math.max(300, Math.min(760, w - 72));
  }
  function zoneFlowSvg(rows, opts) {
    opts = opts || {};
    if (!rows || !rows.length) return zoneEmptyHtml('暂无可用数据');
    var W = Math.max(300, Math.min(760, Number(opts.width) || zoneChartWidth()));
    var compact = W < 480;
    var H = compact ? 210 : 250, padL = compact ? 40 : 58, padR = compact ? 10 : 18, padT = 16, padB = compact ? 30 : 36;
    var innerW = W - padL - padR, innerH = H - padT - padB;
    var hi = 0, lo = 0;
    rows.forEach(function (r) {
      hi = Math.max(hi, Number(r.out_cents) || 0, Number(r.in_cents) || 0, Number(r.net_cents) || 0);
      lo = Math.min(lo, Number(r.net_cents) || 0);
    });
    if (hi <= 0) hi = 1;
    var top = zoneNiceCeil(hi);
    var bottom = lo < 0 ? -zoneNiceCeil(-lo) : 0;
    var span = top - bottom || 1;
    function yy(v) { return padT + innerH - ((Number(v) || 0) - bottom) / span * innerH; }
    var zeroY = yy(0);
    var n = rows.length, slot = innerW / n;
    var barW = Math.max(3, Math.min(16, slot * 0.28));
    var out = ['<svg class="zchart" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' + esc(opts.label || '收支趋势图') + '">'];
    for (var g = 0; g <= 4; g++) {
      var gv = bottom + span * g / 4;
      var gy = yy(gv);
      out.push('<line class="zchart__grid" x1="' + padL + '" x2="' + (W - padR) + '" y1="' + gy.toFixed(1) + '" y2="' + gy.toFixed(1) + '"></line>');
      out.push('<text class="zchart__axis" x="' + (padL - 10) + '" y="' + (gy + 4).toFixed(1) + '" text-anchor="end">' + esc(zoneWan(gv)) + '</text>');
    }
    var step = Math.max(1, Math.ceil(n / (compact ? 4 : 9)));
    var netPts = [];
    rows.forEach(function (r, i) {
      var cx = padL + slot * (i + 0.5);
      var outV = Math.abs(Number(r.out_cents) || 0), inV = Math.abs(Number(r.in_cents) || 0);
      var label = r.label || r.month || r.bucket || '';
      var aria = label + '：支出 ' + fmtMoney(outV) + '，收入 ' + fmtMoney(inV) + (r.net_cents != null ? '，净额 ' + fmtMoney(r.net_cents) : '');
      out.push('<g class="zchart__bar" tabindex="0" role="img" aria-label="' + esc(aria) + '">');
      out.push('<title>' + esc(aria) + '</title>');
      out.push('<rect class="zchart__rect zchart__rect--out" x="' + (cx - barW - 1).toFixed(1) + '" y="' + yy(outV).toFixed(1) + '" width="' + barW.toFixed(1) + '" height="' + Math.max(0, zeroY - yy(outV)).toFixed(1) + '" rx="2"></rect>');
      out.push('<rect class="zchart__rect zchart__rect--in" x="' + (cx + 1).toFixed(1) + '" y="' + yy(inV).toFixed(1) + '" width="' + barW.toFixed(1) + '" height="' + Math.max(0, zeroY - yy(inV)).toFixed(1) + '" rx="2"></rect>');
      out.push('</g>');
      if (r.net_cents != null) netPts.push(cx.toFixed(1) + ',' + yy(r.net_cents).toFixed(1));
      if (i % step === 0) out.push('<text class="zchart__x" x="' + cx.toFixed(1) + '" y="' + (H - 12) + '" text-anchor="middle">' + esc(label) + '</text>');
    });
    if (netPts.length > 1) out.push('<polyline class="zchart__net" points="' + netPts.join(' ') + '"></polyline>');
    out.push('</svg>');
    return out.join('');
  }
  function zoneStacked(rows, totalCents) {
    if (!rows || !rows.length) return zoneEmptyHtml('暂无支出构成');
    var total = Number(totalCents) || rows.reduce(function (s, r) { return s + (Number(r.cents) || 0); }, 0) || 1;
    var segs = rows.map(function (r, i) {
      var pct = (Number(r.cents) || 0) / total * 100;
      return '<span class="zstack__seg zstack__seg--' + ((i % 6) + 1) + '" style="width:' + pct.toFixed(2) + '%" title="' + esc(r.category + ' ' + fmtMoney(r.cents) + ' · ' + pct.toFixed(1) + '%') + '"></span>';
    }).join('');
    var legend = rows.map(function (r, i) {
      var pct = (Number(r.cents) || 0) / total * 100;
      return '<li class="zlegend__item"><i class="zlegend__dot zlegend__dot--' + ((i % 6) + 1) + '"></i>' +
        '<span class="zlegend__name">' + esc(r.category) + '</span>' +
        '<span class="zlegend__val">' + esc(fmtMoney(r.cents)) + '</span>' +
        '<span class="zlegend__pct">' + pct.toFixed(1) + '%</span></li>';
    }).join('');
    return '<div class="zstack">' + segs + '</div><ul class="zlegend">' + legend + '</ul>';
  }
  function zoneHbars(rows) {
    if (!rows || !rows.length) return zoneEmptyHtml('暂无商户数据');
    var max = 0;
    rows.forEach(function (r) { max = Math.max(max, Number(r.cents) || 0); });
    max = max || 1;
    return '<ul class="zhbars">' + rows.map(function (r) {
      var pct = (Number(r.cents) || 0) / max * 100;
      return '<li class="zhbars__row"><span class="zhbars__name">' + esc(r.merchant || r.name || '') + '</span>' +
        '<span class="zhbars__track"><i style="width:' + Math.max(1, pct).toFixed(1) + '%"></i></span>' +
        '<span class="zhbars__val">' + esc(fmtMoney(r.cents)) + '<span class="zhbars__count">' + esc(String(r.count || 0)) + ' 笔</span></span></li>';
    }).join('') + '</ul>';
  }

  // ---------- 仪表盘 ----------
  function zoneConclusionHtml(d) {
    var k = d.kpis || {};
    var budget = d.budget || [];
    var worst = budget.filter(function (b) { return b.state === 'over'; })[0];
    var parts = [];
    parts.push('本月支出 ' + fmtMoney(k.month_out_cents) + '、收入 ' + fmtMoney(k.month_in_cents) + '，净流入 ' + fmtMoney(k.net_flow_cents) + '。');
    if (worst) parts.push(worst.category + '已用掉预算 ' + worst.pct + '%，是当期最紧的一项。');
    if (Number(k.credit_due_cents) > 0) parts.push('信用卡待还 ' + fmtMoney(k.credit_due_cents) + '，到期前还清不计息。');
    var todoRows = (d.todo && d.todo.rows) || [];
    if (todoRows.length) parts.push('另有 ' + todoRows.length + ' 类待办等待处理。');
    return '<div class="zconclusion">' +
      '<div class="zconclusion__top"><span class="eyebrow">三句话看懂这个月</span>' +
      '<span class="zconclusion__asof">数据截至 ' + esc(zoneTime(d.as_of)) + '</span></div>' +
      '<p class="zconclusion__text">' + esc(parts.join('')) + '</p></div>';
  }
  function zoneBudgetHtml(rows) {
    if (!rows || !rows.length) return zoneEmptyHtml('还没有设置预算');
    return '<ul class="zbudget">' + rows.map(function (b) {
      var pct = Number(b.pct) || 0;
      var width = Math.max(0, Math.min(100, pct));
      return '<li class="zbudget__row zbudget__row--' + esc(b.state) + '">' +
        '<span class="zbudget__name">' + esc(b.category) + '</span>' +
        '<span class="zbudget__track"><i style="width:' + width.toFixed(1) + '%"></i></span>' +
        '<span class="zbudget__pct">' + pct + '%</span>' +
        '<span class="zbudget__val">' + esc(fmtMoney(b.used_cents)) + ' / ' + esc(fmtMoney(b.limit_cents)) + '</span></li>';
    }).join('') + '</ul>';
  }
  function zoneTodoHtml(todo) {
    todo = todo || {};
    var rows = todo.rows || [];
    var chain = todo.chain || {};
    var badge = chain.valid
      ? '<span class="zchain zchain--ok">审计链有效 · ' + (chain.checked || 0) + ' 条</span>'
      : '<span class="zchain zchain--bad">审计链异常</span>';
    var list = rows.length ? rows.map(function (r) {
      return '<li class="ztodo__row"><button class="ztodo__btn" type="button" data-ztodo="' + esc(r.kind) + '">' +
        '<span class="ztodo__label">' + esc(r.label) + '</span>' +
        '<span class="ztodo__count">' + esc(String(r.count)) + '</span>' +
        '<span class="ztodo__detail">' + esc(r.detail || '') + '</span></button></li>';
    }).join('') : '<li class="ztodo__row ztodo__row--clear">当前没有待处理事项</li>';
    return '<div class="ztodo__head">' + badge + '</div><ul class="ztodo">' + list + '</ul>';
  }
  function zoneRecentHtml(rows) {
    if (!rows || !rows.length) return zoneEmptyHtml('暂无流水');
    var body = rows.map(function (r) {
      var isIn = r.direction === 'in';
      return '<tr><td class="zcell--mono">' + esc(zoneDateTime(r.ts)) + '</td>' +
        '<td>' + esc(r.counterparty || r.category || '—') + (r.category ? '<span class="zcell__tag">' + esc(r.category) + '</span>' : '') + '</td>' +
        '<td class="zcell--muted zcell--acct">' + esc((r.account_name || '') + ' ' + (r.account_last4 || '')) + '</td>' +
        '<td class="zcell--num ' + (isIn ? 'zin' : 'zout') + '">' + (isIn ? '+' : '−') + esc(fmtMoney(Math.abs(Number(r.amount_cents) || 0)).replace('¥', '¥')) + '</td></tr>';
    }).join('');
    return '<div class="ztable__wrap"><table class="ztable"><thead><tr>' +
      '<th scope="col">时间</th><th scope="col">摘要</th><th scope="col" class="zcell--acct">账户</th>' +
      '<th scope="col" class="zcell--num">金额</th></tr></thead><tbody>' + body + '</tbody></table></div>';
  }
  function zoneRenderDashboard(d) {
    if (!d || !d.kpis) return zoneEmptyHtml('没有可展示的数据');
    var k = d.kpis, deltas = k.deltas || {}, sparks = k.sparklines || {};
    var kpis = [
      { label: '净资产', value: fmtMoney(k.net_worth_cents), note: '含理财 ' + fmtMoney(k.holdings_cents)
        + (Number(k.credit_due_cents) > 0 ? '，已扣信用卡待还' : '') },
      { label: '可用资金', value: fmtMoney(k.available_cents), note: '活期 + 储蓄，可直接动用' },
      { label: '本月支出', value: fmtMoney(k.month_out_cents), delta: deltas.month_out_pct, goodWhenUp: false, note: '较上月同期', spark: sparks.out_cents, tone: 'out' },
      { label: '本月收入', value: fmtMoney(k.month_in_cents), delta: deltas.month_in_pct, goodWhenUp: true, note: '较上月同期', spark: sparks.in_cents, tone: 'in' },
      { label: '本月净流入', value: fmtMoney(k.net_flow_cents), delta: deltas.net_flow_pct, goodWhenUp: true, note: '收入 − 支出' },
      { label: '信用卡待还', value: fmtMoney(k.credit_due_cents), note: Number(k.credit_due_cents) > 0 ? '到期前还清不计息' : '当前无待还' }
    ];
    var months = (d.trend && d.trend.months) || [];
    var trendRows = months.map(function (m) {
      return { label: zoneMonthLabel(m.month), out_cents: m.out_cents, in_cents: m.in_cents, net_cents: m.net_cents };
    });
    var trendTable = months.map(function (m) {
      return '<tr><td>' + esc(m.month) + '</td><td class="zcell--num">' + esc(fmtMoney(m.in_cents)) + '</td>' +
        '<td class="zcell--num">' + esc(fmtMoney(m.out_cents)) + '</td><td class="zcell--num">' + esc(fmtMoney(m.net_cents)) + '</td></tr>';
    });
    var comp = d.composition || {};
    var byAcc = (comp.by_account || []).map(function (a) {
      return '<tr><td>' + esc(a.name || '') + '</td><td class="zcell--muted">' + esc(a.number_masked || '') + '</td>' +
        '<td class="zcell--num">' + esc(fmtMoney(a.balance_cents)) + '</td></tr>';
    });
    var html = [];
    html.push(zoneConclusionHtml(d));
    html.push('<div class="zkpis">' + kpis.map(zoneKpiCard).join('') + '</div>');
    html.push(zoneSection('收支趋势 · 近 6 个月', '柱为支出与收入，折线为净流入；纵轴自 0 起，单位与账户一致',
      zoneFlowSvg(trendRows, { label: '近 6 个月收支与净流入' }) + zoneLegendRow([['out', '支出'], ['in', '收入'], ['net', '净流入']]) +
      zoneTableDetails('查看数据表', ['月份', '收入', '支出', '净流入'], trendTable), 'zcard--wide'));
    html.push(zoneSection('预算执行', '按本月已用金额／月度预算计算，超过 100% 标红',
      zoneBudgetHtml(d.budget || [])));
    html.push(zoneSection('支出构成 · 本月', '按类别占比（100% 堆叠），总支出 ' + fmtMoney(comp.total_out_cents),
      zoneStacked(comp.by_category || [], comp.total_out_cents) +
      (byAcc.length ? zoneTableDetails('按账户看余额', ['账户', '卡号', '余额'], byAcc) : '')));
    html.push(zoneSection('待办与风险', '待授权、预约转账、临近扣费与异常消费集中在这里',
      zoneTodoHtml(d.todo)));
    html.push(zoneSection('最近流水', '最新 ' + ((d.recent || []).length) + ' 笔，完整明细见「分析」页',
      zoneRecentHtml(d.recent || []), 'zcard--wide'));
    return html.join('');
  }

  // ---------- 分析页 ----------
  function zoneAnalyticsControls() {
    var z = zone();
    var presets = ZONE_PRESETS.map(function (p) {
      return '<button class="seg__btn' + (z.preset === p.id ? ' is-on' : '') + '" type="button" data-zpreset="' + p.id + '">' + esc(p.label) + '</button>';
    }).join('');
    var grans = ZONE_GRANS.map(function (g) {
      return '<button class="seg__btn' + (z.granularity === g.id ? ' is-on' : '') + '" type="button" data-zgran="' + g.id + '">' + esc(g.label) + '</button>';
    }).join('');
    return '<div class="zcontrols">' +
      '<div class="zcontrols__group"><span class="zcontrols__label">区间</span><div class="seg" role="group" aria-label="时间区间">' + presets + '</div></div>' +
      '<div class="zcontrols__group"><span class="zcontrols__label">起止</span>' +
      '<input class="zinput" type="date" id="zoneFrom" value="' + esc(z.from) + '" aria-label="开始日期">' +
      '<span class="zcontrols__sep" aria-hidden="true">→</span>' +
      '<input class="zinput" type="date" id="zoneTo" value="' + esc(z.to) + '" aria-label="结束日期"></div>' +
      '<div class="zcontrols__group"><span class="zcontrols__label">粒度</span><div class="seg" role="group" aria-label="聚合粒度">' + grans + '</div></div>' +
      '<div class="zcontrols__group zcontrols__group--end"><button class="btn-secondary" type="button" id="zoneCsv">导出 CSV</button></div>' +
      '</div>';
  }
  function zoneSortedCats(a) {
    var rows = (a.categories || []).slice();
    var key = zone().sortKey, dir = zone().sortDir;
    rows.sort(function (x, y) {
      var vx = x[key], vy = y[key];
      if (typeof vx === 'string' || typeof vy === 'string' || vx == null || vy == null) {
        var sx = String(vx == null ? '' : vx), sy = String(vy == null ? '' : vy);
        return sx.localeCompare(sy) * dir;
      }
      return ((Number(vx) || 0) - (Number(vy) || 0)) * dir;
    });
    return rows;
  }
  function zoneCatsHtml(a) {
    var rows = zoneSortedCats(a);
    var z = zone();
    function th(key, label, num) {
      var on = z.sortKey === key;
      var mark = on ? (z.sortDir > 0 ? ' ↑' : ' ↓') : '';
      return '<th scope="col" class="' + (num ? 'zcell--num ' : '') + 'zth' + (on ? ' is-sorted' : '') + '">' +
        '<button class="zth__btn" type="button" data-zsort="' + key + '">' + esc(label) + mark + '</button></th>';
    }
    if (!rows.length) return zoneEmptyHtml('该区间没有支出');
    var body = rows.map(function (c) {
      var budgetPct = c.budget_pct == null ? '—' : (c.budget_pct + '%');
      var delta = c.delta_pct == null ? '—' : ((c.delta_pct > 0 ? '↑ ' : (c.delta_pct < 0 ? '↓ ' : '')) + Math.abs(c.delta_pct).toFixed(0) + '%');
      return '<tr><td>' + esc(c.category) + '</td>' +
        '<td class="zcell--num">' + esc(fmtMoney(c.cents)) + '</td>' +
        '<td class="zcell--num">' + esc(String(c.pct)) + '%</td>' +
        '<td class="zcell--num ' + (c.delta_pct > 0 ? 'zout' : (c.delta_pct < 0 ? 'zin' : '')) + '">' + esc(delta) + '</td>' +
        '<td class="zcell--num ' + (c.over_budget ? 'zout' : '') + '">' + esc(budgetPct) + '</td></tr>';
    }).join('');
    return '<div class="ztable__wrap"><table class="ztable"><thead><tr>' +
      th('category', '类别') + th('cents', '金额', true) + th('pct', '占比', true) +
      th('delta_pct', '环比', true) + th('budget_pct', '预算使用', true) +
      '</tr></thead><tbody>' + body + '</tbody></table></div>';
  }
  function zoneAnomaliesHtml(a) {
    var rows = a.anomalies || [];
    if (!rows.length) return zoneEmptyHtml('该区间没有异常消费');
    return '<ul class="zlist">' + rows.map(function (r) {
      return '<li class="zlist__row"><span class="zlist__badge zlist__badge--' + esc(r.level || 'warn') + '">' + esc(r.level === 'high' ? '高' : '关注') + '</span>' +
        '<span class="zlist__main"><span class="zlist__title">' + esc(r.counterparty || r.category || '') + '</span>' +
        '<span class="zlist__meta">' + esc(zoneDayLabel(r.ts)) + ' · ' + esc(r.detail || r.rule || '') + '</span></span>' +
        '<span class="zlist__num">' + esc(fmtMoney(r.amount_cents)) + '</span></li>';
    }).join('') + '</ul>';
  }
  function zoneSubsHtml(a) {
    var rows = a.subscriptions || [];
    if (!rows.length) return zoneEmptyHtml('没有正在扣费的订阅');
    return '<ul class="zlist">' + rows.map(function (r) {
      var left = Number(r.days_left);
      var tone = left <= 3 ? 'bad' : (left <= 7 ? 'warn' : 'ok');
      return '<li class="zlist__row"><span class="zlist__badge zlist__badge--' + tone + '">' + (isFinite(left) ? (left + ' 天') : '—') + '</span>' +
        '<span class="zlist__main"><span class="zlist__title">' + esc(r.name || '') + '</span>' +
        '<span class="zlist__meta">' + esc(r.cycle || '') + ' · 下次 ' + esc(r.next_charge || '') + '</span></span>' +
        '<span class="zlist__num">' + esc(fmtMoney(r.amount_cents)) + '</span></li>';
    }).join('') + '</ul>';
  }
  function zoneSavingsHtml(a) {
    var s = a.savings || {};
    var suggestions = s.suggestions || [];
    if (!suggestions.length) return zoneEmptyHtml('该区间没有明显的节流空间');
    return '<div class="zsavings"><div class="zsavings__num"><span class="zsavings__label">可优化金额</span>' +
      '<span class="zsavings__value">' + esc(fmtMoney(s.potential_cents)) + '</span>' +
      '<span class="zsavings__note">按当前区间同类支出估算，仅供参考</span></div>' +
      '<ul class="zsavings__list">' + suggestions.map(function (t) {
        return '<li>' + esc(typeof t === 'string' ? t : (t.text || '')) + '</li>';
      }).join('') + '</ul></div>';
  }
  function zoneTxHtml(a) {
    var z = zone();
    var rows = a.transactions || [];
    if (!rows.length) return zoneEmptyHtml('该区间没有流水');
    var per = z.txPer, total = rows.length, pages = Math.max(1, Math.ceil(total / per));
    if (z.txPage > pages - 1) z.txPage = pages - 1;
    if (z.txPage < 0) z.txPage = 0;
    var slice = rows.slice(z.txPage * per, z.txPage * per + per);
    var body = slice.map(function (r) {
      var isIn = r.direction === 'in';
      return '<tr><td class="zcell--mono">' + esc(zoneDayLabel(r.ts) + ' ' + zoneTime(r.ts)) + '</td>' +
        '<td>' + esc(r.counterparty || '') + '</td>' +
        '<td class="zcell--muted">' + esc(r.category || '') + '</td>' +
        '<td class="zcell--muted zcell--acct">' + esc(r.account_last4 || '') + '</td>' +
        '<td class="zcell--num ' + (isIn ? 'zin' : 'zout') + '">' + (isIn ? '+' : '−') + esc(fmtMoney(Math.abs(Number(r.amount_cents) || 0))) + '</td></tr>';
    }).join('');
    return '<div class="ztable__wrap"><table class="ztable"><thead><tr>' +
      '<th scope="col">时间</th><th scope="col">摘要</th><th scope="col">类别</th><th scope="col" class="zcell--acct">账户</th>' +
      '<th scope="col" class="zcell--num">金额</th></tr></thead><tbody>' + body + '</tbody></table></div>' +
      '<div class="zpager"><span class="zpager__info">共 ' + total + ' 笔 · 第 ' + (z.txPage + 1) + ' / ' + pages + ' 页</span>' +
      '<span class="zpager__acts"><button class="btn-text" type="button" data-zpage="prev"' + (z.txPage <= 0 ? ' disabled' : '') + '>上一页</button>' +
      '<button class="btn-text" type="button" data-zpage="next"' + (z.txPage >= pages - 1 ? ' disabled' : '') + '>下一页</button></span></div>';
  }
  function zoneRenderAnalytics(a) {
    if (!a || !a.totals) return zoneEmptyHtml('没有可展示的数据');
    var t = a.totals, r = a.range || {};
    var series = (a.series || []).map(function (s) {
      return { label: zoneBucketLabel(s.bucket), out_cents: s.out_cents, in_cents: s.in_cents, net_cents: s.net_cents };
    });
    var seriesTable = (a.series || []).map(function (s) {
      return '<tr><td>' + esc(s.bucket) + '</td><td class="zcell--num">' + esc(fmtMoney(s.in_cents)) + '</td>' +
        '<td class="zcell--num">' + esc(fmtMoney(s.out_cents)) + '</td><td class="zcell--num">' + esc(fmtMoney(s.net_cents)) + '</td>' +
        '<td class="zcell--num">' + esc(String(s.count)) + '</td></tr>';
    });
    var kpis = [
      { label: '区间支出', value: fmtMoney(t.out_cents), delta: t.out_delta_pct, goodWhenUp: false, note: '上一等长区间 ' + fmtMoney(t.prev_out_cents) },
      { label: '区间收入', value: fmtMoney(t.in_cents), delta: t.in_delta_pct, goodWhenUp: true, note: '上一等长区间 ' + fmtMoney(t.prev_in_cents) },
      { label: '净额', value: fmtMoney(t.net_cents), note: '收入 − 支出' },
      { label: '日均支出', value: fmtMoney(t.avg_day_out_cents), note: t.txn_count + ' 笔 · 跨 ' + (r.span_days || 0) + ' 天' }
    ];
    var html = [];
    html.push(zoneAnalyticsControls());
    html.push('<div class="zkpis zkpis--four">' + kpis.map(zoneKpiCard).join('') + '</div>');
    html.push(zoneSection('收支序列 · ' + (r.start || '') + ' 至 ' + (r.end || ''), '柱为支出与收入，折线为净额；纵轴自 0 起',
      zoneFlowSvg(series, { label: '区间收支序列' }) + zoneLegendRow([['out', '支出'], ['in', '收入'], ['net', '净额']]) +
      zoneTableDetails('查看数据表', ['区间', '收入', '支出', '净额', '笔数'], seriesTable), 'zcard--wide'));
    html.push(zoneSection('类别排名', '点表头排序；预算使用率按区间覆盖月份折算', '<div id="zoneCats">' + zoneCatsHtml(a) + '</div>'));
    html.push(zoneSection('商户 TOP ' + ((a.merchants || []).length), '按区间支出金额排序，横条长度表示相对份额',
      zoneHbars(a.merchants || [])));
    html.push(zoneSection('异常消费', '按金额与频次偏离度排序，仅提示，不自动拦截', zoneAnomaliesHtml(a)));
    html.push(zoneSection('固定周期与扣费', '订阅与固定支出按下次扣费时间排序', zoneSubsHtml(a)));
    html.push(zoneSection('储蓄建议', '基于本期支出结构给出的可选动作', zoneSavingsHtml(a)));
    html.push(zoneSection('明细', '最新 100 笔，支持导出 CSV 自行核对', '<div id="zoneTx">' + zoneTxHtml(a) + '</div>', 'zcard--wide'));
    return html.join('');
  }

  // ---------- 数据加载 ----------
  function zoneEnsureSession() {
    if (state.sessionId) return Promise.resolve(state.sessionId);
    if (zone().sessionWait) return zone().sessionWait;
    bootSession();
    zone().sessionWait = new Promise(function (resolve, reject) {
      var tries = 0;
      var timer = setInterval(function () {
        tries++;
        if (state.sessionId) {
          clearInterval(timer);
          zone().sessionWait = null;
          resolve(state.sessionId);
        } else if (tries > 25) {
          clearInterval(timer);
          zone().sessionWait = null;
          reject(new Error('会话尚未就绪，请稍后重试'));
        }
      }, 200);
    });
    return zone().sessionWait;
  }
  function zoneRangeForPreset(presetId) {
    var p = ZONE_PRESETS.filter(function (x) { return x.id === presetId; })[0] || ZONE_PRESETS[3];
    var to = new Date();
    var from = new Date(to.getTime() - (p.days - 1) * 86400000);
    return { from: zoneIsoDay(from), to: zoneIsoDay(to), days: p.days };
  }
  function zoneDefaultGranularity(days) {
    if (days <= 31) return 'day';
    if (days <= 120) return 'week';
    return 'month';
  }
  function zoneFetchDashboard(force) {
    var z = zone();
    if (z.dash && !force && Date.now() - z.dashAt < 30000) {
      zonePaint(zoneRenderDashboard(z.dash));
      return Promise.resolve(z.dash);
    }
    z.dashBusy = true;
    zoneStamp('读取中…');
    return zoneEnsureSession().then(function (sid) {
      return getJson('/api/dashboard?session_id=' + encodeURIComponent(sid) + '&window=30d');
    }).then(function (d) {
      z.dash = d;
      z.dashAt = Date.now();
      z.dashBusy = false;
      zonePaint(zoneRenderDashboard(d));
      zoneStamp('更新于 ' + fmtTime(new Date()) + ' · 只读');
      return d;
    }).catch(function (e) {
      z.dashBusy = false;
      zonePaint(zoneErrorHtml(e && e.message ? e.message : '仪表盘读取失败'));
      zoneStamp('读取失败');
      throw e;
    });
  }
  function zoneFetchAnalytics(force) {
    var z = zone();
    if (z.ana && !force && Date.now() - z.anaAt < 30000) {
      zonePaint(zoneRenderAnalytics(z.ana));
      return Promise.resolve(z.ana);
    }
    z.anaBusy = true;
    zoneStamp('读取中…');
    var path = '/api/analytics?session_id=' + encodeURIComponent(state.sessionId || '') +
      '&from=' + encodeURIComponent(z.from) + '&to=' + encodeURIComponent(z.to) +
      '&granularity=' + encodeURIComponent(z.granularity);
    return zoneEnsureSession().then(function (sid) {
      return getJson('/api/analytics?session_id=' + encodeURIComponent(sid) +
        '&from=' + encodeURIComponent(z.from) + '&to=' + encodeURIComponent(z.to) +
        '&granularity=' + encodeURIComponent(z.granularity));
    }).then(function (a) {
      z.ana = a;
      z.anaAt = Date.now();
      z.anaBusy = false;
      zonePaint(zoneRenderAnalytics(a));
      zoneStamp('更新于 ' + fmtTime(new Date()) + ' · 只读');
      return a;
    }).catch(function (e) {
      z.anaBusy = false;
      zonePaint(zoneErrorHtml(e && e.message ? e.message : '分析数据读取失败'));
      zoneStamp('读取失败');
      throw e;
    });
  }
  function zonePaint(html) {
    var body = document.getElementById('zoneBody');
    if (body) body.innerHTML = html;
  }
  function loadZone(force) {
    var z = zone();
    if (z.view === 'analytics') {
      return zoneFetchAnalytics(!!force).catch(function () {});
    }
    return zoneFetchDashboard(!!force).catch(function () {});
  }
  function zoneStartPoll() {
    var z = zone();
    if (z.pollTimer) return;
    z.pollTimer = setInterval(function () {
      if (state.view !== 'zone') { zoneStopPoll(); return; }
      if (typeof document.hidden === 'boolean' && document.hidden) return;
      loadZone(true);
    }, 60000);
  }
  function zoneStopPoll() {
    var z = zone();
    if (z.pollTimer) { clearInterval(z.pollTimer); z.pollTimer = null; }
  }

  // ---------- 视图 / 路由 ----------
  function zoneSetHeader(view) {
    var meta = ZONE_META[view] || ZONE_META.dashboard;
    var h1 = document.getElementById('zoneH1');
    var sub = document.getElementById('zoneSub');
    if (h1) h1.textContent = meta.title;
    if (sub) sub.textContent = meta.sub;
    Array.prototype.forEach.call(document.querySelectorAll('[data-zone]'), function (btn) {
      var on = btn.getAttribute('data-zone') === view;
      btn.classList.toggle('is-on', on);
      btn.setAttribute('aria-selected', on ? 'true' : 'false');
    });
  }
  function zoneEnter(view, params) {
    var z = zone();
    z.view = view || z.view || 'dashboard';
    if (params) {
      if (params.preset) z.preset = params.preset;
      if (params.from) z.from = params.from;
      if (params.to) z.to = params.to;
      if (params.gran) z.granularity = params.gran;
    }
    if (!z.from || !z.to) {
      var r = zoneRangeForPreset(z.preset || '180d');
      z.from = r.from;
      z.to = r.to;
      z.granularity = z.granularity || zoneDefaultGranularity(r.days);
    }
    zoneSetHeader(z.view);
    zoneSyncHash();
    var body = document.getElementById('zoneBody');
    if (body && !body.firstChild) body.innerHTML = zoneSkeleton();
    loadZone(false);
    zoneStartPoll();
  }
  function zoneSwitchTab(view) {
    var z = zone();
    if (z.view === view) return;
    z.view = view;
    zoneSetHeader(view);
    zoneSyncHash();
    zonePaint(zoneSkeleton());
    loadZone(false);
  }
  function zoneBuildHash() {
    var z = zone();
    if (state.view !== 'zone') return '';
    if (z.view === 'analytics') {
      return '#/zone/analytics?preset=' + encodeURIComponent(z.preset || '') +
        '&from=' + encodeURIComponent(z.from) + '&to=' + encodeURIComponent(z.to) +
        '&gran=' + encodeURIComponent(z.granularity);
    }
    return '#/zone/dashboard';
  }
  function zoneSyncHash() {
    var hash = zoneBuildHash();
    if (!hash) return;
    zoneSetHash(hash);
  }
  function zoneSetHash(hash) {
    var z = zone();
    try {
      if (!window.location || typeof window.location.hash !== 'string') return;
      if (window.location.hash === hash) return;
      z.ignoreHash = true;
      window.location.hash = hash;
      setTimeout(function () { z.ignoreHash = false; }, 0);
    } catch (e) { /* 无 hash 路由的环境（自测桩）忽略 */ }
  }
  function zoneParseHash(hash) {
    var raw = String(hash || '').replace(/^#/, '');
    if (!raw) return null;
    if (raw.charAt(0) === '/') raw = raw.slice(1);
    var qi = raw.indexOf('?');
    var path = qi >= 0 ? raw.slice(0, qi) : raw;
    var query = qi >= 0 ? raw.slice(qi + 1) : '';
    var parts = path.split('/').filter(Boolean);
    var params = {};
    query.split('&').forEach(function (pair) {
      if (!pair) return;
      var kv = pair.split('=');
      if (!kv[0]) return;
      params[decodeURIComponent(kv[0])] = decodeURIComponent(kv[1] || '');
    });
    var view = parts[0] || 'home';
    if (view === 'zone') return { view: 'zone', zone: parts[1] || 'dashboard', params: params };
    return { view: view, params: params };
  }
  function zoneHandleHash() {
    var z = zone();
    if (z.ignoreHash) return;
    var hash = '';
    try { hash = (window.location && window.location.hash) || ''; } catch (e) { hash = ''; }
    var route = zoneParseHash(hash);
    if (!route) return;
    if (route.view === 'zone') {
      z.view = route.zone || 'dashboard';
      showView('zone');
      zoneEnter(z.view, route.params);
    } else if (route.view !== state.view || route.view === 'home') {
      showView(route.view);
    }
  }

  // ---------- 主题 ----------
  var THEME_LABEL = { system: '跟随系统', light: '浅色', dark: '深色' };
  function themeStored() {
    try { return (window.localStorage && window.localStorage.getItem('zs-theme')) || 'light'; } catch (e) { return 'light'; }
  }
  function themeResolve(mode) {
    if (mode === 'dark' || mode === 'light') return mode;
    var dark = false;
    try { dark = !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches); } catch (e) {}
    return dark ? 'dark' : 'light';
  }
  function themeApply(mode) {
    var m = THEME_LABEL[mode] ? mode : 'light';
    var resolved = themeResolve(m);
    document.body.setAttribute('data-theme', resolved);
    state.theme = m;
    try { window.localStorage && window.localStorage.setItem('zs-theme', m); } catch (e) {}
    var btn = document.getElementById('btnTheme');
    if (btn) {
      btn.setAttribute('title', '外观：' + THEME_LABEL[m] + '（点击切换）');
      btn.setAttribute('aria-label', '切换外观，当前' + THEME_LABEL[m]);
    }
    try {
      var meta = document.querySelector('meta[name="theme-color"]');
      if (meta) meta.setAttribute('content', resolved === 'dark' ? '#0B1220' : '#0B5FD0');
    } catch (e) {}
    return resolved;
  }
  function themeToggle() {
    var order = { system: 'light', light: 'dark', dark: 'system' };
    var next = order[state.theme || 'light'] || 'light';
    themeApply(next);
    toast('外观：' + THEME_LABEL[next], { sub: next === 'system' ? '跟随系统深浅色自动切换' : '已保存，下次打开仍生效' });
  }

  // ---------- 命令面板 ----------
  function paletteActions() {
    return [
      { id: 'zone-dashboard', label: '仪表盘', hint: '专区 · 净资产、本月收支、预算与待办', run: function () { showView('zone'); zoneSwitchTab('dashboard'); } },
      { id: 'zone-analytics', label: '分析', hint: '专区 · 类别、商户、异常与订阅', run: function () { showView('zone'); zoneSwitchTab('analytics'); } },
      { id: 'nav-home', label: '回到工作台', hint: '对话即操作', run: function () { showView('home'); } },
      { id: 'theme', label: '切换外观', hint: '跟随系统 / 浅色 / 深色', run: themeToggle },
      { id: 'audit', label: '打开审计链', hint: '哈希链校验与导出', run: function () { openAudit(); } },
      { id: 'tasks', label: '换一批快捷指令', run: function () { nextTaskPage(); } },
      { id: 'refresh', label: '刷新当前视图', run: function () { if (state.view === 'zone') loadZone(true); else bootSession(); } },
      { id: 'reset', label: '重置演示数据', hint: '清空并按种子重建', run: function () { confirmReset(); } }
    ];
  }
  function paletteFiltered() {
    var q = (state.paletteQuery || '').trim().toLowerCase();
    var all = state.paletteActions || [];
    if (!q) return all;
    return all.filter(function (a) {
      return (a.label + ' ' + (a.hint || '')).toLowerCase().indexOf(q) >= 0;
    });
  }
  function paletteRender() {
    var list = document.getElementById('paletteList');
    if (!list) return;
    var items = paletteFiltered();
    if (!items.length) {
      list.innerHTML = '<div class="palette__empty">没有匹配的命令</div>';
      return;
    }
    state.paletteIndex = Math.max(0, Math.min(state.paletteIndex || 0, items.length - 1));
    list.innerHTML = items.map(function (a, i) {
      return '<button class="palette__item' + (i === state.paletteIndex ? ' is-on' : '') + '" type="button" role="option" aria-selected="' + (i === state.paletteIndex ? 'true' : 'false') + '" data-palette="' + i + '">' +
        '<span class="palette__label">' + esc(a.label) + '</span>' +
        (a.hint ? '<span class="palette__hint">' + esc(a.hint) + '</span>' : '') + '</button>';
    }).join('');
  }
  function paletteOpen() {
    var el = document.getElementById('palette');
    var input = document.getElementById('paletteInput');
    if (!el) return;
    state.paletteActions = paletteActions();
    state.paletteQuery = '';
    state.paletteIndex = 0;
    el.hidden = false;
    if (input) { input.value = ''; try { input.focus(); } catch (e) {} }
    paletteRender();
  }
  function paletteClose() {
    var el = document.getElementById('palette');
    if (el) el.hidden = true;
  }
  function paletteRun(index) {
    var items = paletteFiltered();
    var action = items[index];
    paletteClose();
    if (action && action.run) action.run();
  }
  function paletteMove(delta) {
    var items = paletteFiltered();
    if (!items.length) return;
    state.paletteIndex = (state.paletteIndex + delta + items.length) % items.length;
    paletteRender();
  }

  // ---------- 事件绑定 ----------
  function bindZone() {
    if (dom.btnZoneRefresh) dom.btnZoneRefresh.addEventListener('click', function () { loadZone(true); });
    if (dom.btnTheme) dom.btnTheme.addEventListener('click', themeToggle);
    Array.prototype.forEach.call(document.querySelectorAll('[data-zone]'), function (btn) {
      btn.addEventListener('click', function () { zoneSwitchTab(btn.getAttribute('data-zone')); });
    });
    var body = document.getElementById('zoneBody');
    if (body) body.addEventListener('click', function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      var preset = t.closest('[data-zpreset]');
      if (preset) {
        zone().preset = preset.getAttribute('data-zpreset');
        var r = zoneRangeForPreset(zone().preset);
        zone().from = r.from;
        zone().to = r.to;
        zone().granularity = zoneDefaultGranularity(r.days);
        zone().txPage = 0;
        loadZone(true);
        return;
      }
      var gran = t.closest('[data-zgran]');
      if (gran) { zone().granularity = gran.getAttribute('data-zgran'); zone().txPage = 0; loadZone(true); return; }
      var sort = t.closest('[data-zsort]');
      if (sort) {
        var key = sort.getAttribute('data-zsort');
        var z = zone();
        if (z.sortKey === key) z.sortDir = -z.sortDir; else { z.sortKey = key; z.sortDir = -1; }
        var cats = document.getElementById('zoneCats');
        if (cats && z.ana) cats.innerHTML = zoneCatsHtml(z.ana);
        return;
      }
      var page = t.closest('[data-zpage]');
      if (page) {
        var z2 = zone();
        z2.txPage += page.getAttribute('data-zpage') === 'next' ? 1 : -1;
        var box = document.getElementById('zoneTx');
        if (box && z2.ana) box.innerHTML = zoneTxHtml(z2.ana);
        return;
      }
      var todo = t.closest('[data-ztodo]');
      if (todo) {
        var kind = todo.getAttribute('data-ztodo');
        var row = (((zone().dash || {}).todo || {}).rows || []).filter(function (r) { return r.kind === kind; })[0];
        if (row) showModal(row.label, '<p>' + esc(row.detail || '') + '</p>', null);
        return;
      }
      if (t.id === 'zoneCsv' || (t.closest && t.closest('#zoneCsv'))) { zoneExportCsv(); return; }
      if (t.id === 'zoneRetry' || (t.closest && t.closest('#zoneRetry'))) { loadZone(true); return; }
    });
    // 日期输入
    ['zoneFrom', 'zoneTo'].forEach(function (id) { /* 动态渲染后再绑定，见 zonePaint 后置钩子 */ });
    var fromEl = document.getElementById('zoneFrom');
    var toEl = document.getElementById('zoneTo');
    if (fromEl) fromEl.addEventListener('change', function () {
      zone().from = fromEl.value; zone().preset = '';
      zone().granularity = zoneDefaultGranularity(Math.max(1, (new Date(zone().to) - new Date(zone().from)) / 86400000 + 1));
      loadZone(true);
    });
    if (toEl) toEl.addEventListener('change', function () {
      zone().to = toEl.value; zone().preset = '';
      loadZone(true);
    });
    // 外观跟随系统
    try {
      if (window.matchMedia) {
        var mq = window.matchMedia('(prefers-color-scheme: dark)');
        if (mq && mq.addEventListener) mq.addEventListener('change', function () { if (state.theme === 'system') themeApply('system'); });
      }
    } catch (e) {}
    // 命令面板
    document.addEventListener('keydown', function (ev) {
      var meta = ev.metaKey || ev.ctrlKey;
      if (meta && (ev.key === 'k' || ev.key === 'K')) {
        ev.preventDefault();
        paletteOpen();
        return;
      }
      var pal = document.getElementById('palette');
      if (!pal || pal.hidden) return;
      if (ev.key === 'Escape') { ev.preventDefault(); paletteClose(); }
      else if (ev.key === 'ArrowDown') { ev.preventDefault(); paletteMove(1); }
      else if (ev.key === 'ArrowUp') { ev.preventDefault(); paletteMove(-1); }
      else if (ev.key === 'Enter') { ev.preventDefault(); paletteRun(state.paletteIndex || 0); }
    });
    var pal = document.getElementById('palette');
    if (pal) {
      pal.addEventListener('click', function (ev) {
        if (ev.target === pal) { paletteClose(); return; }
        var item = ev.target && ev.target.closest ? ev.target.closest('[data-palette]') : null;
        if (item) paletteRun(Number(item.getAttribute('data-palette')) || 0);
      });
    }
    var palInput = document.getElementById('paletteInput');
    if (palInput) palInput.addEventListener('input', function () {
      state.paletteQuery = palInput.value;
      state.paletteIndex = 0;
      paletteRender();
    });
    try {
      window.addEventListener('hashchange', zoneHandleHash);
    } catch (e) {}
    themeApply(themeStored());
  }

  // ---------- 导出 ----------
  function zoneCsvEscape(v) {
    var s = String(v == null ? '' : v);
    return '"' + s.replace(/"/g, '""') + '"';
  }
  function zoneExportCsv() {
    var a = zone().ana;
    if (!a || !a.transactions) { toast('还没有可导出的数据', { type: 'error' }); return; }
    var lines = ['时间,摘要,类别,账户,金额,方向'];
    a.transactions.forEach(function (r) {
      lines.push([
        zoneCsvEscape(r.ts), zoneCsvEscape(r.counterparty), zoneCsvEscape(r.category),
        zoneCsvEscape(r.account_last4), zoneCsvEscape((Number(r.amount_cents) || 0) / 100),
        zoneCsvEscape(r.direction === 'in' ? '收入' : '支出')
      ].join(','));
    });
    var text = '\ufeff' + lines.join('\r\n');
    var name = 'zerostep-明细-' + (a.range ? a.range.start + '_' + a.range.end : 'export') + '.csv';
    try {
      var blob = new Blob([text], { type: 'text/csv;charset=utf-8' });
      var url = URL.createObjectURL(blob);
      var link = document.createElement('a');
      link.href = url;
      link.download = name;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      toast('已导出 ' + a.transactions.length + ' 笔明细', { sub: name });
    } catch (e) {
      toast('当前环境不支持下载，已跳过', { type: 'error', sub: '请在浏览器中打开工作台' });
    }
  }

  function init() {
    cacheDom();
    bindEvents();
    setupOtp();
    setupVoice();
    autoGrow();
    traceIdle();
    renderSamples();
    bindZone();
    showView('home');
    state.hashReady = true;
    try {
      if (window.location && typeof window.location.hash === 'string') zoneHandleHash();
    } catch (e) { /* 无 hash 环境 */ }
    loadTasks();
    loadLlmStatus();
    watchViewport();
    registerServiceWorker();
    bootSession();
  }
  // 测试钩子：仅用于无浏览器自测，不参与业务逻辑
  window.__ZS = {
    state: state, dom: dom,
    handleEvent: handleEvent, showView: showView, sendText: sendText,
    openApproval: openApproval, submitApproval: submitApproval,
    renderAudit: renderAudit, loadAudit: loadAudit, openAudit: openAudit, closeAudit: closeAudit,
    renderAccounts: renderAccounts, refreshAccountSnapshot: refreshAccountSnapshot, loadSkills: loadSkills, loadServices: loadServices, loadSettings: loadSettings,
    applyLlmStatus: applyLlmStatus, loadLlmStatus: loadLlmStatus, renderLlmCard: renderLlmCard, testLlm: testLlm, toast: toast, showModal: showModal, closeAll: function () { closeMenu(); closeModal(); closeApprovalSheet(); closeAudit(); },
    loadTasks: loadTasks, loadTasksPage: loadTasksPage, nextTaskPage: nextTaskPage, taskPageCount: taskPageCount,
    loadPlugins: loadPlugins, renderPlugins: renderPlugins, togglePlugin: togglePlugin, togglePluginOpen: togglePluginOpen, pluginCounts: pluginCounts,
    sendPush: sendPush, renderPush: renderPush, setPushKind: setPushKind, resetPush: resetPush,
    registerServiceWorker: registerServiceWorker, applyViewportHeight: applyViewportHeight, isMobileViewport: isMobileViewport,
    toolLabel: toolLabel,
    setApprovalFactor: setApprovalFactor, sendDemoSms: sendDemoSms, startFaceScan: startFaceScan,
    applyLocked: applyLocked, clearLocked: clearLocked, lockedNow: lockedNow,
    finishTurn: finishTurn, traceIdle: traceIdle, applyFullResult: applyFullResult, setBusy: setBusy, init: init,
    renderAnswerStart: renderAnswerStart, renderAnswerDelta: renderAnswerDelta, renderAnswerEnd: renderAnswerEnd,
    renderInsightCard: renderInsightCard, cancelTurn: cancelTurn, setSendMode: setSendMode,
    loadZone: loadZone, zoneEnter: zoneEnter, zoneSwitchTab: zoneSwitchTab, zoneHandleHash: zoneHandleHash,
    zoneRenderDashboard: zoneRenderDashboard, zoneRenderAnalytics: zoneRenderAnalytics,
    zoneFlowSvg: zoneFlowSvg, zoneSparkSvg: zoneSparkSvg, zoneParseHash: zoneParseHash,
    zoneCatsHtml: zoneCatsHtml, zoneTxHtml: zoneTxHtml, zoneBudgetHtml: zoneBudgetHtml,
    zoneTodoHtml: zoneTodoHtml, zoneConclusionHtml: zoneConclusionHtml,
    themeApply: themeApply, themeToggle: themeToggle,
    paletteOpen: paletteOpen, paletteClose: paletteClose, paletteActions: paletteActions
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();