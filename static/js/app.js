/* ============================================================================
 * IDEA MECE — 프론트엔드
 *
 * 성능 설계
 *  · 트리 전체를 다시 그리지 않는다. 생성/수정/삭제는 해당 노드(또는 그 자식
 *    목록)의 DOM만 국소적으로 갱신한다.
 *  · 자식 DOM은 펼칠 때 처음 만든다(지연 렌더링). 수천 개 항목이 있어도
 *    화면에 보이는 만큼만 DOM에 존재한다.
 *  · 노드 카드는 <template> 복제로 생성하고, 여러 개를 붙일 때는
 *    DocumentFragment로 한 번에 반영해 레이아웃 계산 횟수를 최소화한다.
 *  · 이벤트는 컨테이너 한 곳에서 위임 처리한다. 노드마다 리스너를 달지 않으므로
 *    노드를 지워도 리스너가 남지 않는다(메모리 누수 방지).
 *
 * 보안 설계
 *  · 서버에서 받은 모든 문자열은 textContent로만 넣는다(innerHTML 미사용) → XSS 차단.
 *  · 상태 변경 요청에는 CSRF 토큰 헤더를 붙인다.
 *  · API 키는 화면에서 서버로 한 번 전달된 뒤 입력란에서 즉시 지운다. 브라우저에
 *    저장(localStorage 등)하지 않는다.
 * ========================================================================== */
(function () {
  'use strict';

  // ── 부트스트랩 설정 (CSP 때문에 인라인 스크립트 대신 JSON 블록에서 읽는다) ──
  var BOOT = (function () {
    try {
      return JSON.parse(document.getElementById('app-bootstrap').textContent);
    } catch (e) {
      return { csrfToken: '', limits: {}, models: [] };
    }
  })();
  var LIMITS = BOOT.limits || {};
  var MAX_CHILDREN = LIMITS.maxChildren || 10;

  // ── 상태 ──────────────────────────────────────────────────────────────
  var state = {
    topic: '',
    nodes: new Map(),      // id -> node
    children: new Map(),   // parentKey('root'|id) -> [id, ...]
    built: new Set(),      // 자식 DOM을 이미 만든 parentKey
    expanded: new Set(),   // 펼쳐진 노드 id
    settings: { configured: false, model: BOOT.defaultModel, email: '' },
    mail: null,
    busy: false
  };

  // ── DOM 참조 ──────────────────────────────────────────────────────────
  var $ = function (id) { return document.getElementById(id); };
  var el = {
    startPanel: $('start-panel'),
    startForm: $('start-form'),
    topicInput: $('topic-input'),
    countInput: $('count-input'),
    btnGenerateRoot: $('btn-generate-root'),
    startHint: $('start-hint'),
    workspace: $('workspace'),
    topicTitle: $('workspace-title'),
    statNodes: $('stat-nodes'),
    statAreas: $('stat-areas'),
    treeRoot: $('tree-root'),
    btnReport: $('btn-report'),
    btnReset: $('btn-reset'),
    btnCollapseAll: $('btn-collapse-all'),
    btnExpandAll: $('btn-expand-all'),
    reportPanel: $('report-panel'),
    reportMeta: $('report-meta'),
    reportSummary: $('report-summary'),
    btnDownloadReport: $('btn-download-report'),
    btnMail: $('btn-mail'),
    modal: $('settings-modal'),
    settingsForm: $('settings-form'),
    apiKeyInput: $('api-key-input'),
    modelSelect: $('model-select'),
    emailInput: $('email-input'),
    btnOpenSettings: $('btn-open-settings'),
    btnCloseSettings: $('btn-close-settings'),
    btnCancelSettings: $('btn-cancel-settings'),
    btnClearKey: $('btn-clear-key'),
    settingsIndicator: $('settings-indicator'),
    apiKeyHint: $('api-key-hint'),
    overlay: $('overlay'),
    overlayText: $('overlay-text'),
    toastStack: $('toast-stack'),
    nodeTemplate: $('node-template')
  };

  // ── 유틸 ──────────────────────────────────────────────────────────────
  function keyOf(parentId) { return parentId === null || parentId === undefined ? 'root' : String(parentId); }

  function clampCount(value) {
    var n = parseInt(value, 10);
    if (isNaN(n) || n < 1) { n = 1; }
    if (n > MAX_CHILDREN) { n = MAX_CHILDREN; }
    return n;
  }

  function toast(message, kind) {
    var node = document.createElement('div');
    node.className = 'toast' + (kind ? ' is-' + kind : '');
    node.textContent = message;                   // textContent → XSS 불가
    el.toastStack.appendChild(node);
    window.setTimeout(function () {
      if (node.parentNode) { node.parentNode.removeChild(node); }
    }, kind === 'error' ? 6200 : 3600);
  }

  function setOverlay(visible, text) {
    if (text) { el.overlayText.textContent = text; }
    el.overlay.hidden = !visible;
  }

  function setBusy(button, busy, busyText) {
    if (!button) { return; }
    var label = button.querySelector('.btn-text');
    if (busy) {
      button.disabled = true;
      button.classList.add('is-busy');
      if (label && busyText) {
        if (!button.dataset.idleText) { button.dataset.idleText = label.textContent; }
        label.textContent = busyText;
      }
    } else {
      button.disabled = false;
      button.classList.remove('is-busy');
      if (label && button.dataset.idleText) {
        label.textContent = button.dataset.idleText;
        delete button.dataset.idleText;
      }
    }
  }

  // ── 통신 ──────────────────────────────────────────────────────────────
  function request(method, url, body, timeoutMs) {
    var controller = new AbortController();
    var timer = window.setTimeout(function () { controller.abort(); }, timeoutMs || 240000);
    var options = {
      method: method,
      headers: { 'Accept': 'application/json' },
      credentials: 'same-origin',
      signal: controller.signal
    };
    if (body !== undefined && body !== null) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    if (method !== 'GET') {
      options.headers['X-CSRF-Token'] = BOOT.csrfToken;
    }
    return fetch(url, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        window.clearTimeout(timer);
        if (!response.ok || data.ok === false) {
          var error = new Error(data.error || '요청을 처리하지 못했습니다.');
          error.status = response.status;
          error.needSettings = !!data.need_settings;
          throw error;
        }
        return data;
      });
    }).catch(function (error) {
      window.clearTimeout(timer);
      if (error.name === 'AbortError') {
        throw new Error('응답이 지연되어 요청을 중단했습니다. 생성 개수를 줄여 다시 시도해 주세요.');
      }
      if (!error.status && error.message === 'Failed to fetch') {
        throw new Error('서버에 연결할 수 없습니다. 네트워크 상태를 확인해 주세요.');
      }
      throw error;
    });
  }

  function handleError(error) {
    toast(error.message || '오류가 발생했습니다.', 'error');
    if (error.needSettings || error.status === 401) { openSettings(); }
  }

  // ── 상태 반영 ─────────────────────────────────────────────────────────
  function indexNodes(list) {
    for (var i = 0; i < list.length; i++) {
      var node = list[i];
      state.nodes.set(node.id, node);
      var key = keyOf(node.parent_id);
      var bucket = state.children.get(key);
      if (!bucket) { bucket = []; state.children.set(key, bucket); }
      if (bucket.indexOf(node.id) === -1) { bucket.push(node.id); }
    }
  }

  function updateStats() {
    var total = state.nodes.size;
    var roots = (state.children.get('root') || []).length;
    el.statNodes.textContent = String(total);
    el.statAreas.textContent = String(roots);
  }

  function showWorkspace(show) {
    el.workspace.hidden = !show;
  }

  // ── 렌더링 ────────────────────────────────────────────────────────────
  function listFor(parentId) {
    // 부모 li 안의 자식 ul (없으면 생성)
    if (parentId === null) { return el.treeRoot; }
    var li = document.querySelector('.node[data-id="' + parentId + '"]');
    if (!li) { return null; }
    var list = li.querySelector(':scope > .tree');
    if (!list) {
      list = document.createElement('ul');
      list.className = 'tree';
      list.setAttribute('role', 'group');
      li.appendChild(list);
    }
    return list;
  }

  function createNodeElement(node, indexLabel) {
    var li = el.nodeTemplate.content.firstElementChild.cloneNode(true);
    li.dataset.id = String(node.id);
    li.dataset.depth = String(Math.min(4, node.depth));
    li.querySelector('.node-index').textContent = indexLabel;
    li.querySelector('.node-title').textContent = node.title || '';
    li.querySelector('.node-content').textContent = node.content || '';
    var countInput = li.querySelector('.count-mini');
    countInput.max = String(MAX_CHILDREN);
    countInput.value = String(node.depth >= 2 ? 2 : 3);
    refreshChildIndicator(li, node);
    return li;
  }

  function refreshChildIndicator(li, node) {
    var toggle = li.querySelector('.node-toggle');
    var info = li.querySelector('.node-childinfo');
    var count = node.child_count || 0;
    if (count > 0) {
      toggle.hidden = false;
      var open = state.expanded.has(node.id);
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.setAttribute('aria-label', (open ? '접기' : '펼치기') + ' · 하위 ' + count + '개');
      info.hidden = open;
      info.textContent = '하위 아이디어 ' + count + '개 (눌러서 펼치기)';
    } else {
      toggle.hidden = true;
      toggle.setAttribute('aria-expanded', 'false');
      info.hidden = true;
      info.textContent = '';
    }
  }

  function childIndexLabel(prefix, position) {
    return prefix ? prefix + '.' + (position + 1) : String(position + 1);
  }

  function indexPrefixOf(parentId) {
    if (parentId === null || parentId === undefined) { return ''; }
    var li = document.querySelector('.node[data-id="' + parentId + '"]');
    if (!li) { return ''; }
    return li.querySelector('.node-index').textContent || '';
  }

  /**
   * 특정 부모의 자식 DOM을 만든다(지연 렌더링).
   * 이미 만들어져 있으면 아무 일도 하지 않는다.
   */
  function buildChildren(parentId, autoExpandDepth) {
    var key = keyOf(parentId);
    if (state.built.has(key)) { return; }
    var ids = state.children.get(key);
    if (!ids || !ids.length) { return; }
    var container = listFor(parentId);
    if (!container) { return; }

    var prefix = indexPrefixOf(parentId);
    var fragment = document.createDocumentFragment();
    for (var i = 0; i < ids.length; i++) {
      var node = state.nodes.get(ids[i]);
      if (!node) { continue; }
      fragment.appendChild(createNodeElement(node, childIndexLabel(prefix, i)));
    }
    container.appendChild(fragment);
    state.built.add(key);

    if (autoExpandDepth && autoExpandDepth > 0) {
      for (var j = 0; j < ids.length; j++) {
        var child = state.nodes.get(ids[j]);
        if (child && child.child_count > 0) {
          expandNode(child.id, autoExpandDepth - 1);
        }
      }
    }
  }

  function expandNode(nodeId, cascadeDepth) {
    var node = state.nodes.get(nodeId);
    if (!node) { return; }
    state.expanded.add(nodeId);
    buildChildren(nodeId, cascadeDepth || 0);
    var li = document.querySelector('.node[data-id="' + nodeId + '"]');
    if (li) {
      var list = li.querySelector(':scope > .tree');
      if (list) { list.hidden = false; }
      refreshChildIndicator(li, node);
    }
  }

  function collapseNode(nodeId) {
    var node = state.nodes.get(nodeId);
    state.expanded.delete(nodeId);
    var li = document.querySelector('.node[data-id="' + nodeId + '"]');
    if (li) {
      var list = li.querySelector(':scope > .tree');
      if (list) { list.hidden = true; }
      if (node) { refreshChildIndicator(li, node); }
    }
  }

  /** 해당 부모의 하위 번호만 다시 매긴다(전체 재렌더링 금지). */
  function renumber(parentId) {
    var key = keyOf(parentId);
    var ids = state.children.get(key);
    if (!ids) { return; }
    var prefix = indexPrefixOf(parentId);
    for (var i = 0; i < ids.length; i++) {
      var li = document.querySelector('.node[data-id="' + ids[i] + '"]');
      if (!li) { continue; }
      li.querySelector('.node-index').textContent = childIndexLabel(prefix, i);
      if (state.built.has(String(ids[i]))) { renumber(ids[i]); }
    }
  }

  function renderFresh(topic, nodes) {
    state.topic = topic || '';
    state.nodes.clear();
    state.children.clear();
    state.built.clear();
    state.expanded.clear();
    el.treeRoot.textContent = '';            // 기존 DOM 해제
    indexNodes(nodes || []);
    el.topicTitle.textContent = state.topic;
    buildChildren(null, 1);                  // 최상위 + 한 단계까지 펼쳐 보여준다
    updateStats();
    showWorkspace(state.nodes.size > 0);
  }

  /** 생성된 자식들을 해당 부모 밑에만 덧붙인다. */
  function appendGenerated(parentId, created) {
    indexNodes(created);
    var key = keyOf(parentId);
    var parent = parentId === null ? null : state.nodes.get(parentId);
    if (parent) { parent.child_count = (state.children.get(key) || []).length; }

    if (!state.built.has(key)) {
      // 아직 자식 DOM이 없으면 전체를 한 번에 만든다
      buildChildren(parentId, 0);
    } else {
      var container = listFor(parentId);
      if (container) {
        var ids = state.children.get(key) || [];
        var prefix = indexPrefixOf(parentId);
        var fragment = document.createDocumentFragment();
        for (var i = 0; i < created.length; i++) {
          var node = state.nodes.get(created[i].id);
          if (!node) { continue; }
          var position = ids.indexOf(node.id);
          var li = createNodeElement(node, childIndexLabel(prefix, position < 0 ? i : position));
          li.classList.add('is-new');
          fragment.appendChild(li);
        }
        container.appendChild(fragment);
      }
    }

    if (parentId !== null) {
      state.expanded.add(parentId);
      var parentLi = document.querySelector('.node[data-id="' + parentId + '"]');
      if (parentLi) {
        var list = parentLi.querySelector(':scope > .tree');
        if (list) { list.hidden = false; }
        if (parent) { refreshChildIndicator(parentLi, parent); }
      }
    }
    renumber(parentId);
    updateStats();
    showWorkspace(true);
  }

  /** 하위 전체를 상태에서 제거한다(메모리 해제). */
  function forgetSubtree(nodeId) {
    var stack = [nodeId];
    while (stack.length) {
      var current = stack.pop();
      var kids = state.children.get(String(current));
      if (kids) {
        for (var i = 0; i < kids.length; i++) { stack.push(kids[i]); }
        state.children.delete(String(current));
      }
      state.built.delete(String(current));
      state.expanded.delete(current);
      state.nodes.delete(current);
    }
  }

  // ── 동작: 아이디어 생성 ───────────────────────────────────────────────
  function generateRoot(event) {
    event.preventDefault();
    if (state.busy) { return; }
    var topic = el.topicInput.value.trim();
    if (topic.length < 2) {
      toast('주제를 2자 이상 입력해 주세요.', 'error');
      el.topicInput.focus();
      return;
    }
    var count = clampCount(el.countInput.value);
    el.countInput.value = String(count);

    state.busy = true;
    setBusy(el.btnGenerateRoot, true, '생성 중…');
    setOverlay(true, '아이디어를 만들고 있습니다…');

    request('POST', '/api/tree', { topic: topic, count: count })
      .then(function (data) {
        renderFresh(data.topic, data.nodes);
        el.reportPanel.hidden = true;
        toast(data.nodes.length + '개의 핵심 영역을 만들었습니다.', 'success');
        window.setTimeout(function () {
          el.workspace.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }, 80);
      })
      .catch(handleError)
      .then(function () {
        state.busy = false;
        setBusy(el.btnGenerateRoot, false);
        setOverlay(false);
      });
  }

  function expandIdea(li, button) {
    var nodeId = parseInt(li.dataset.id, 10);
    var node = state.nodes.get(nodeId);
    if (!node || li.classList.contains('is-busy')) { return; }
    var countInput = li.querySelector('.count-mini');
    var count = clampCount(countInput.value);
    countInput.value = String(count);

    li.classList.add('is-busy');
    setBusy(button, true, '생성 중…');

    request('POST', '/api/nodes/' + nodeId + '/children', { count: count })
      .then(function (data) {
        appendGenerated(nodeId, data.nodes || []);
        toast('"' + node.title + '" 아래에 ' + (data.nodes || []).length + '개를 추가했습니다.', 'success');
      })
      .catch(handleError)
      .then(function () {
        li.classList.remove('is-busy');
        setBusy(button, false);
      });
  }

  // ── 동작: 수정 ───────────────────────────────────────────────────────
  function openEdit(li) {
    var node = state.nodes.get(parseInt(li.dataset.id, 10));
    if (!node) { return; }
    var form = li.querySelector('[data-role="edit-form"]');
    form.querySelector('.edit-title').value = node.title || '';
    form.querySelector('.edit-content').value = node.content || '';
    form.hidden = false;
    li.querySelector('.node-tools').hidden = true;
    form.querySelector('.edit-title').focus();
  }

  function closeEdit(li) {
    li.querySelector('[data-role="edit-form"]').hidden = true;
    li.querySelector('.node-tools').hidden = false;
  }

  function saveEdit(li, form) {
    var nodeId = parseInt(li.dataset.id, 10);
    var node = state.nodes.get(nodeId);
    if (!node) { return; }
    var title = form.querySelector('.edit-title').value.trim();
    var content = form.querySelector('.edit-content').value.trim();
    if (!title) {
      toast('제목은 비워 둘 수 없습니다.', 'error');
      return;
    }
    var saveButton = form.querySelector('[data-action="save-edit"]');
    saveButton.disabled = true;

    request('PATCH', '/api/nodes/' + nodeId, { title: title, content: content })
      .then(function (data) {
        node.title = data.node.title;
        node.content = data.node.content;
        li.querySelector('.node-title').textContent = node.title;   // 해당 노드만 갱신
        li.querySelector('.node-content').textContent = node.content;
        closeEdit(li);
        toast('수정했습니다.', 'success');
      })
      .catch(handleError)
      .then(function () { saveButton.disabled = false; });
  }

  // ── 동작: 삭제 ───────────────────────────────────────────────────────
  function deleteNode(li) {
    var nodeId = parseInt(li.dataset.id, 10);
    var node = state.nodes.get(nodeId);
    if (!node) { return; }
    var childCount = node.child_count || 0;
    var message = childCount > 0
      ? '"' + node.title + '" 항목과 하위 아이디어 전체를 삭제할까요?'
      : '"' + node.title + '" 항목을 삭제할까요?';
    if (!window.confirm(message)) { return; }

    li.classList.add('is-busy');
    request('DELETE', '/api/nodes/' + nodeId, null, 60000)
      .then(function (data) {
        var parentId = node.parent_id;
        var key = keyOf(parentId);
        var bucket = state.children.get(key);
        if (bucket) {
          var at = bucket.indexOf(nodeId);
          if (at !== -1) { bucket.splice(at, 1); }
        }
        forgetSubtree(nodeId);
        if (li.parentNode) { li.parentNode.removeChild(li); }   // 해당 가지 DOM만 제거

        if (parentId !== null && parentId !== undefined) {
          var parent = state.nodes.get(parentId);
          if (parent) {
            parent.child_count = (state.children.get(key) || []).length;
            var parentLi = document.querySelector('.node[data-id="' + parentId + '"]');
            if (parentLi) { refreshChildIndicator(parentLi, parent); }
          }
        }
        renumber(parentId === undefined ? null : parentId);
        updateStats();
        if (state.nodes.size === 0) { showWorkspace(false); el.reportPanel.hidden = true; }
        toast('삭제했습니다. (' + data.removed + '개 항목)', 'success');
      })
      .catch(function (error) {
        li.classList.remove('is-busy');
        handleError(error);
      });
  }

  // ── 동작: 보고서 ─────────────────────────────────────────────────────
  function generateReport() {
    if (state.busy || state.nodes.size === 0) { return; }
    state.busy = true;
    setBusy(el.btnReport, true, '작성 중…');
    setOverlay(true, '보고서를 작성하고 있습니다…');

    request('POST', '/api/report', {}, 600000)
      .then(function (data) {
        el.reportMeta.textContent =
          '핵심 영역 ' + data.section_count + '개 · 세부 검토 과제 ' + data.node_count +
          '건을 바탕으로 작성되었습니다.';
        el.reportSummary.textContent = data.summary || '';
        el.btnDownloadReport.setAttribute('href', data.download_url);
        el.btnDownloadReport.setAttribute('download', data.filename);
        state.mail = data.mail || null;
        updateMailLink(data.filename);
        el.reportPanel.hidden = false;
        (data.warnings || []).forEach(function (warning) { toast(warning, 'error'); });
        toast('보고서가 완성되었습니다.', 'success');
        el.reportPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
      })
      .catch(handleError)
      .then(function () {
        state.busy = false;
        setBusy(el.btnReport, false);
        setOverlay(false);
      });
  }

  function updateMailLink() {
    if (!state.mail) { return; }
    var to = encodeURIComponent(state.settings.email || '');
    var subject = encodeURIComponent(state.mail.subject || '');
    var body = encodeURIComponent(state.mail.body || '');
    el.btnMail.setAttribute('href', 'mailto:' + to + '?subject=' + subject + '&body=' + body);
  }

  function resetAll() {
    if (!window.confirm('지금까지 만든 아이디어를 모두 삭제할까요? 되돌릴 수 없습니다.')) { return; }
    request('POST', '/api/reset', {})
      .then(function () {
        renderFresh('', []);
        el.reportPanel.hidden = true;
        el.topicInput.value = '';
        state.mail = null;
        toast('초기화했습니다.', 'success');
        el.startPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
      })
      .catch(handleError);
  }

  function expandAll() {
    // 현재 상태에 있는 모든 항목을 펼친다(추가 통신 없음).
    // 얕은 단계부터 펼쳐야 자식 DOM이 부모 안에 올바르게 들어간다.
    var ids = [];
    state.nodes.forEach(function (node, id) {
      if (node.child_count > 0) { ids.push(id); }
    });
    ids.sort(function (a, b) {
      return (state.nodes.get(a).depth || 0) - (state.nodes.get(b).depth || 0);
    });
    for (var i = 0; i < ids.length; i++) { expandNode(ids[i], 0); }
  }

  function collapseAll() {
    state.nodes.forEach(function (node, id) {
      if (node.child_count > 0 && node.depth > 0) { collapseNode(id); }
    });
  }

  // ── 설정 ─────────────────────────────────────────────────────────────
  function applySettings(settings) {
    state.settings.configured = !!settings.configured;
    state.settings.model = settings.model || BOOT.defaultModel;
    state.settings.email = settings.email || '';
    el.modelSelect.value = state.settings.model;
    el.emailInput.value = state.settings.email;
    el.settingsIndicator.hidden = state.settings.configured;
    el.startHint.hidden = state.settings.configured;
    el.apiKeyInput.placeholder = settings.configured
      ? (settings.masked_key || '등록된 키 사용 중')
      : 'AIza...';
    el.apiKeyHint.textContent = settings.configured
      ? '키가 등록되어 있습니다. 약 ' + (settings.ttl_minutes || 120) +
        '분간 유지되며 서버에 저장되지 않습니다. 변경할 때만 새로 입력하세요.'
      : '키는 서버에 저장되지 않으며 이 브라우저 세션에만 보관됩니다.';
    updateMailLink();
  }

  function loadSettings() {
    return request('GET', '/api/settings').then(function (data) {
      applySettings(data.settings || {});
    }).catch(function () { /* 초기 로딩 실패는 조용히 무시 */ });
  }

  function openSettings() {
    el.modal.hidden = false;
    window.setTimeout(function () { el.apiKeyInput.focus(); }, 40);
  }

  function closeSettings() {
    el.modal.hidden = true;
    el.apiKeyInput.value = '';          // 입력된 키를 DOM에 남기지 않는다
  }

  function saveSettings(event) {
    event.preventDefault();
    var payload = {
      api_key: el.apiKeyInput.value.trim(),
      model: el.modelSelect.value,
      email: el.emailInput.value.trim()
    };
    if (!payload.api_key && !state.settings.configured) {
      toast('Gemini API 키를 입력해 주세요.', 'error');
      el.apiKeyInput.focus();
      return;
    }
    var button = $('btn-save-settings');
    button.disabled = true;
    request('POST', '/api/settings', payload)
      .then(function (data) {
        applySettings(data.settings || {});
        el.apiKeyInput.value = '';
        closeSettings();
        toast(data.message || '설정을 저장했습니다.', 'success');
      })
      .catch(handleError)
      .then(function () { button.disabled = false; });
  }

  function clearKey() {
    if (!window.confirm('등록된 API 키를 삭제할까요?')) { return; }
    request('DELETE', '/api/settings', {})
      .then(function (data) {
        applySettings(data.settings || {});
        el.apiKeyInput.value = '';
        toast(data.message || 'API 키를 삭제했습니다.', 'success');
      })
      .catch(handleError);
  }

  // ── 이벤트 위임 ───────────────────────────────────────────────────────
  function onTreeClick(event) {
    var button = event.target.closest('button[data-action]');
    if (!button) { return; }
    var li = button.closest('.node');
    if (!li) { return; }
    var action = button.dataset.action;

    if (action === 'toggle') {
      var nodeId = parseInt(li.dataset.id, 10);
      if (state.expanded.has(nodeId)) { collapseNode(nodeId); } else { expandNode(nodeId, 0); }
      return;
    }
    if (action === 'expand') { expandIdea(li, button); return; }
    if (action === 'edit') { openEdit(li); return; }
    if (action === 'cancel-edit') { closeEdit(li); return; }
    if (action === 'delete') { deleteNode(li); return; }
  }

  function onTreeSubmit(event) {
    var form = event.target.closest('[data-role="edit-form"]');
    if (!form) { return; }
    event.preventDefault();
    var li = form.closest('.node');
    if (li) { saveEdit(li, form); }
  }

  function onTreeKeydown(event) {
    // 노드 카드의 숫자 입력란에서 엔터 → 바로 생성
    if (event.key !== 'Enter') { return; }
    var input = event.target;
    if (!input.classList || !input.classList.contains('count-mini')) { return; }
    event.preventDefault();
    var li = input.closest('.node');
    if (li) { expandIdea(li, li.querySelector('[data-action="expand"]')); }
  }

  // ── 초기화 ───────────────────────────────────────────────────────────
  function init() {
    el.countInput.max = String(MAX_CHILDREN);
    el.topicInput.maxLength = LIMITS.maxTopic || 300;

    el.startForm.addEventListener('submit', generateRoot);
    el.treeRoot.addEventListener('click', onTreeClick);
    el.treeRoot.addEventListener('submit', onTreeSubmit);
    el.treeRoot.addEventListener('keydown', onTreeKeydown);
    el.btnReport.addEventListener('click', generateReport);
    el.btnReset.addEventListener('click', resetAll);
    el.btnExpandAll.addEventListener('click', expandAll);
    el.btnCollapseAll.addEventListener('click', collapseAll);

    el.btnOpenSettings.addEventListener('click', openSettings);
    el.btnCloseSettings.addEventListener('click', closeSettings);
    el.btnCancelSettings.addEventListener('click', closeSettings);
    el.btnClearKey.addEventListener('click', clearKey);
    el.settingsForm.addEventListener('submit', saveSettings);
    el.modal.addEventListener('click', function (event) {
      if (event.target === el.modal) { closeSettings(); }
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !el.modal.hidden) { closeSettings(); }
    });
    el.btnMail.addEventListener('click', function () {
      toast('메일 창이 열립니다. 내려받은 워드 파일을 직접 첨부한 뒤 발송해 주세요.');
    });

    loadSettings();
    // 기존 작업 내용 복원 (새로고침/재접속 대응)
    request('GET', '/api/tree')
      .then(function (data) {
        if (data.nodes && data.nodes.length) {
          renderFresh(data.topic, data.nodes);
          el.topicInput.value = '';     // 초기 화면 입력란은 항상 빈 값 유지
        }
      })
      .catch(function () { /* 무시 */ });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
