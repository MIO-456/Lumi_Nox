'use strict';

let configuration = null;
let latest = {run_id: '', mode: 'demo', roles: [], requests: [], events: []};
let dirty = false;
let polling = false;
let pollAgain = false;
let pollTimer = null;
let connected = true;
let lastFeedSignature = '';
let lastModelSignature = '';
let wasDisconnected = false;
let capabilities = {gomoku: false};
const $ = id => document.getElementById(id);
const phaseNames = {idle: '尚未开始', starting: '正在连接', ready: '等待输入', generating: '正在思考', synthesizing: '待播', playing: '正在播放', stopping: '正在停止', cancelling: '已打断'};
const outcomeNames = {complete: '已播完', simulated: '演示完成', cancelled: '已取消', failed: '失败'};
const programPhaseNames = {idle: '尚未开始', preparing: '准备中', starting: '正在启动', opening: '开场中', chat: '聊天中', activity: '活动进行中', finalizing: '整场结算中', running: '运行中', ready: '运行中', transitioning: '正在切换', stopping: '正在清理', ending: '正在收尾', finished: '节目已结束', stopped: '已停止', failed: '运行失败', blocked: '等待清理', cleanup_failed: '清理未完成'};
const stageKinds = [['opening', '开场'], ['chat', '聊天'], ['activity', '五子棋'], ['ending', '收尾']];
const terminalPhases = new Set(['complete', 'simulated', 'cancelled', 'failed']);

function api(path, data) {
  const options = data === undefined ? {cache: 'no-store', signal: AbortSignal.timeout(5000)} : {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-Public-Host': '1'}, body: JSON.stringify(data)
  };
  return fetch(path, options).then(response => response.json().then(body => {
    if (!response.ok) throw new Error(body.error || '操作未完成。');
    return body;
  }));
}
function notice(message, error) { $('notice').textContent = message || ''; $('notice').hidden = !message; $('notice').classList.toggle('error', !!error); }
function markDirty() { dirty = true; $('saved').textContent = '有未保存的修改'; updateControls(); }
function text(value, fallback) { return typeof value === 'string' && value ? value : (fallback || ''); }
function escapeErrorCode(value) { const source = text(value).toLowerCase(); if (!source) return ''; if (source.includes('timeout') || source.includes('超时')) return 'timeout'; if (source.includes('audio') || source.includes('声音') || source.includes('播放')) return 'audio_error'; if (source.includes('依赖') || source.includes('aiohttp')) return 'dependency_missing'; if (source.includes('cancel') || source.includes('取消') || source.includes('打断')) return 'cancelled'; return 'provider_error'; }
function run(action) { notice(''); return Promise.resolve().then(action).catch(error => { notice(error.message || '操作未完成。', true); }); }
function field(parent, title, value, change, type, hint) {
  const label = document.createElement('label'); label.className = 'field'; label.append(document.createTextNode(title));
  const input = document.createElement(type === 'textarea' ? 'textarea' : 'input');
  if (type && type !== 'textarea') input.type = type;
  input.value = value == null ? '' : value; input.autocomplete = type === 'password' ? 'new-password' : 'off';
  input.addEventListener('input', () => { change(type === 'number' ? Number(input.value) : input.value); markDirty(); });
  label.append(input);
  if (hint) { const small = document.createElement('small'); small.className = 'hint'; small.textContent = hint; label.append(small); }
  parent.append(label); return input;
}
function checkbox(parent, title, value, change) {
  const label = document.createElement('label'); label.className = 'check-label'; const input = document.createElement('input'); input.type = 'checkbox'; input.checked = value;
  input.addEventListener('change', () => { change(input.checked); markDirty(); }); label.append(input, document.createTextNode(title)); parent.append(label); return input;
}
function selectField(parent, title, value, options, change) {
  const label = document.createElement('label'); label.className = 'field'; label.append(document.createTextNode(title));
  const element = document.createElement('select'); options.forEach(pair => element.add(new Option(pair[1], pair[0]))); element.value = value;
  element.addEventListener('change', () => { change(element.value); markDirty(); }); label.append(element); parent.append(label); return element;
}
function button(parent, label, action) { const item = document.createElement('button'); item.type = 'button'; item.textContent = label; item.addEventListener('click', () => run(action)); parent.append(item); return item; }

function renderConfig() {
  if (!configuration) return;
  $('mode').value = configuration.mode;
  $('services').replaceChildren();
  [['llm', '聊天服务'], ['tts', '语音服务']].forEach(([name, title]) => {
    const data = configuration[name], block = document.createElement('section'); block.className = 'service-block';
    const heading = document.createElement('h3'); heading.textContent = title; block.append(heading);
    const protocol = document.createElement('p'); protocol.className = 'hint'; protocol.textContent = name === 'llm' ? 'OpenAI-compatible · 文本回复' : 'OpenAI-compatible · WAV 语音输出'; block.append(protocol);
    field(block, '基础地址', data.base_url, value => data.base_url = value, 'url', '填写服务商给出的 API 基础地址，通常以 /v1 结尾。');
    field(block, '模型名', data.model, value => data.model = value);
    const key = field(block, 'API Key', '', value => data.api_key = value, 'password'); key.placeholder = data.has_api_key ? '已保存 · 留空保持原 Key' : '无鉴权的本地服务可以留空';
    checkbox(block, '清除已保存的 Key', false, value => data.clear_api_key = value);
    field(block, '请求超时（秒）', data.timeout, value => data.timeout = value, 'number');
    if (name === 'llm') { const row = document.createElement('div'); row.className = 'row'; field(row, '回复长度上限', data.max_tokens, value => data.max_tokens = value, 'number'); selectField(row, '长度字段', data.token_limit_field, [['max_tokens', 'max_tokens'], ['max_completion_tokens', 'max_completion_tokens'], ['', '不发送']], value => data.token_limit_field = value); block.append(row); }
    $('services').append(block);
  });
  renderRoles(); renderRoleSelects(); renderProgramEditor();
  const device = configuration.audio.device; $('device').replaceChildren(new Option('系统默认设备', ''));
  if (device !== null) $('device').add(new Option(String(device), String(device))); $('device').value = device === null ? '' : String(device);
  dirty = false; $('saved').textContent = '本机配置'; updateControls();
}
function renderRoles() {
  $('roles').replaceChildren();
  (configuration.roles || []).forEach((role, index) => {
    const card = document.createElement('section'); card.className = 'role-card'; const heading = document.createElement('div'); heading.className = 'role-heading';
    const title = document.createElement('h3'); title.textContent = `角色 ${index + 1}`; heading.append(title); const options = document.createElement('div'); options.className = 'role-options';
    checkbox(heading, '启用', role.enabled, value => { role.enabled = value; options.hidden = !value; }); card.append(heading); options.hidden = !role.enabled;
    field(options, '角色名', role.name, value => role.name = value); field(options, '角色设定', role.system_prompt, value => role.system_prompt = value, 'textarea'); field(options, '音色名 / ID', role.voice, value => role.voice = value, 'text', '由你选择的语音服务提供。');
    const avatar = role.avatar; checkbox(options, '连接 VTube Studio', avatar.enabled, value => avatar.enabled = value);
    const details = document.createElement('details'); details.className = 'avatar-options'; const summary = document.createElement('summary'); summary.textContent = 'VTS 连接与嘴型'; details.append(summary);
    field(details, 'VTS 地址', avatar.url, value => avatar.url = value); field(details, '模型 ID', avatar.model_id, value => avatar.model_id = value, 'text', '加载模型后读取列表，选择当前模型。'); field(details, '嘴型输入参数', avatar.mouth_parameter, value => avatar.mouth_parameter = value); field(details, '嘴型灵敏度', avatar.mouth_gain, value => avatar.mouth_gain = value, 'number');
    const actions = document.createElement('div'); actions.className = 'avatar-buttons'; [['读取模型', false], ['首次授权并读取', true]].forEach(([label, authorize]) => { const item = button(actions, label, async () => { if (dirty) throw new Error('请先保存配置。'); await api('/api/avatar', {speaker: role.name, authorize}); await pollNow(); }); item.className = 'idle-action'; }); details.append(actions); options.append(details); card.append(options); $('roles').append(card);
  });
}
function activeRoleNames() {
  const running = latest && Array.isArray(latest.roles) && latest.roles.length && (latest.running || latest.run_id || latest.mode === 'demo');
  const source = running ? latest.roles : (configuration && configuration.roles || []).filter(role => role && role.enabled);
  return source.filter(role => role && role.name).map(role => role.name);
}
function renderRoleSelects() {
  const speakerValue = $('speaker').value, previewValue = $('preview-role').value;
  const speakerNames = activeRoleNames();
  const previewNames = (configuration && configuration.roles || []).filter(role => role && role.enabled && role.name).map(role => role.name);
  const speakerSignature = JSON.stringify(speakerNames), previewSignature = JSON.stringify(previewNames);
  if ($('speaker').dataset.names !== speakerSignature) {
    $('speaker').replaceChildren(new Option('自动轮换', '')); speakerNames.forEach(name => $('speaker').add(new Option(name, name)));
    $('speaker').dataset.names = speakerSignature;
  }
  if ($('preview-role').dataset.names !== previewSignature) {
    $('preview-role').replaceChildren(); previewNames.forEach(name => $('preview-role').add(new Option(name, name)));
    $('preview-role').dataset.names = previewSignature;
  }
  $('speaker').value = speakerNames.includes(speakerValue) ? speakerValue : '';
  $('preview-role').value = previewNames.includes(previewValue) ? previewValue : (previewNames[0] || '');
}
function renderProgramEditor() {
  const program = configuration && configuration.program; $('stages').replaceChildren(); if (!program) return;
  $('auto-chat-interval').value = program.auto_chat_interval; $('gomoku-port').value = program.gomoku_port;
  (program.stages || []).forEach((stage, index) => {
    const card = document.createElement('article'); card.className = 'stage'; card.dataset.index = String(index); const number = document.createElement('span'); number.className = 'stage-number'; number.textContent = String(index + 1).padStart(2, '0'); card.append(number);
    const content = document.createElement('div'); content.className = 'stage-content'; const row = document.createElement('div'); row.className = 'stage-fields'; field(row, '环节名称', stage.title, value => stage.title = value); selectField(row, '类型', stage.kind, stageKinds, value => { stage.kind = value; if (value === 'activity') stage.activity = 'gomoku'; else delete stage.activity; renderProgramEditor(); }); const duration = field(row, '时长（秒）', stage.duration, value => stage.duration = value, 'number'); duration.min = '1'; duration.max = '86400'; duration.step = '1'; content.append(row);
    if (stage.kind !== 'activity') { const details = document.createElement('details'); details.className = 'stage-prompt'; const summary = document.createElement('summary'); summary.textContent = '本环节提示'; details.append(summary); field(details, '告诉角色这段要聊什么', stage.prompt || '', value => stage.prompt = value, 'textarea'); content.append(details); }
    const actions = document.createElement('div'); actions.className = 'stage-actions'; [['上移', -1], ['下移', 1]].forEach(([label, offset]) => { const item = button(actions, label, () => { const other = index + offset; if (other < 0 || other >= program.stages.length) return; [program.stages[index], program.stages[other]] = [program.stages[other], program.stages[index]]; renderProgramEditor(); markDirty(); }); item.disabled = index + offset < 0 || index + offset >= program.stages.length; }); const remove = button(actions, '删除', () => { program.stages.splice(index, 1); renderProgramEditor(); markDirty(); }); remove.setAttribute('aria-label', `删除第 ${index + 1} 个环节`); card.append(content, actions); $('stages').append(card);
  });
}
function currentRunPayload(extra) { return Object.assign({}, extra || {}, latest.run_id ? {run_id: latest.run_id} : {}); }
function isBusy() { return !!(latest.running || latest.program && (latest.program.running || latest.program.blocked) || latest.operation && latest.operation.running); }
function canSubmit() { if (!connected) return false; const programPhase = latest.program && latest.program.phase; const allowedProgram = !latest.program || ['opening', 'chat', 'activity', 'ending'].includes(programPhase); return !!(latest.running && !latest.stopping && !latest.preview && allowedProgram); }
function isTalking() { const current = latest.current_request; return !!(canSubmit() && current && !terminalPhases.has(current.phase)); }
function updateControls() {
  const busy = isBusy(), programRunning = !!(latest.program && latest.program.running), submit = canSubmit(), talking = isTalking();
  $('mode').disabled = busy; $('devices').disabled = busy;
  $('configuration').disabled = busy; $('save').disabled = busy; $('start').disabled = busy || dirty; $('run-mode').disabled = busy; $('next').disabled = !programRunning || latest.stopping; $('end').disabled = !programRunning || latest.stopping || latest.program.stage && latest.program.stage.kind === 'ending'; $('stop').disabled = !busy; $('interrupt').disabled = !talking;
  ['input', 'send', 'say', 'speaker'].forEach(id => $(id).disabled = !submit); ['probe', 'preview'].forEach(id => $(id).disabled = busy || dirty || !configuration || configuration.mode !== 'live'); document.querySelectorAll('.idle-action').forEach(item => item.disabled = busy || dirty);
  $('demo-chat').disabled = busy || !connected; $('demo-program').disabled = busy || !connected;
  $('dependency-note').hidden = capabilities.gomoku;
}
function renderRunMode() { const program = $('run-mode').value === 'program'; $('program-hint').hidden = !program; $('start').textContent = program ? '开始节目' : '开始对话'; }
function renderProgramStatus(program) {
  const value = program || {}, stage = value.stage || {}, active = !!value.running; const label = programPhaseNames[value.phase] || value.phase || '准备开始'; $('program-phase').textContent = label; $('program-stage').textContent = stage.title || ($('run-mode').value === 'manual' ? '手动对话：由你输入消息' : '保存配置，检查连接后开始'); $('stage-name').textContent = stage.title || '示例舞台'; $('stage-label').textContent = active ? label : '当前环节';
  const seconds = value.remaining_seconds; const rounded = typeof seconds === 'number' && Number.isFinite(seconds) ? Math.ceil(Math.max(0, seconds)) : null; $('program-countdown').textContent = rounded === null ? '—' : `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, '0')}`; $('program-error').textContent = value.last_error || ''; $('program-error').hidden = !value.last_error;
  const stages = value.stages || configuration && configuration.program && configuration.program.stages || []; $('program-steps').replaceChildren(); stages.forEach((item, index) => { const step = document.createElement('i'); step.className = 'program-step'; if (active && index === value.stage_index) step.classList.add('current'); else if (active && index < value.stage_index) step.classList.add('done'); if (value.phase === 'failed' && index === value.stage_index) step.classList.add('failed'); $('program-steps').append(step); });
  const url = value.activity && value.activity.url; $('activity-link').hidden = true; if (url && value.activity.platform_running) { try { const target = new URL(url, location.origin); if (['http:', 'https:'].includes(target.protocol) && ['127.0.0.1', 'localhost', '[::1]'].includes(target.hostname)) { $('activity-link').href = target.href; $('activity-link').hidden = false; } } catch (error) {} }
}
function requestState(record) { if (!record) return ''; if (record.outcome) return outcomeNames[record.outcome] || record.outcome; return phaseNames[record.phase] || record.phase || '排队'; }
function renderFeed(requests) {
  const items = (requests || []).slice(-40);
  const signature = JSON.stringify([latest.run_id, items.map(r => [r.request_id, r.phase, r.outcome, r.text, r.speaker])]);
  if (signature === lastFeedSignature) return;
  lastFeedSignature = signature;
  const feed = $('request-feed');
  const follow = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 45;
  feed.replaceChildren();
  if (!items.length) {
    feed.append(Object.assign(document.createElement('div'), {className: 'feed-empty', textContent: '开始后发条消息，或体验自动运行的完整节目。'}));
    return;
  }
  for (const record of items) {
    const manual = record.kind === 'chat' && record.source === 'manual';
    const item = document.createElement('article');
    item.className = `feed-item ${manual ? 'manual' : 'auto'} ${record.phase || ''}`;
    const head = document.createElement('div'); head.className = 'feed-head';
    const name = document.createElement('strong'); name.textContent = manual ? '本地观众' : record.source === 'activity' ? '活动解说' : '节目发言';
    const state = document.createElement('span'); state.className = `feed-state ${record.phase || ''}`; state.textContent = requestState(record);
    head.append(name, state); item.append(head);
    if (manual) {
      const input = document.createElement('p'); input.className = 'feed-text'; input.textContent = record.input; item.append(input);
    }
    if (record.text && !['cancelled', 'failed', 'cancelling'].includes(record.phase)) {
      const reply = document.createElement('p'); reply.className = 'feed-text reply-text';
      reply.textContent = `${record.speaker || '角色'}：${record.text}`; item.append(reply);
    }
    if (record.error) { const error = document.createElement('small'); error.className = 'feed-state failed'; error.textContent = record.error; item.append(error); }
    feed.append(item);
  }
  if (follow) feed.scrollTop = feed.scrollHeight;
}
function stageTimings(record) {
  const timings = record && record.timings || {};
  return ['queued', 'generating', 'synthesizing', 'playing'].filter(phase => timings[phase] != null).map(phase => `${({queued: '排队', generating: '生成', synthesizing: '合成', playing: '播放'})[phase]} ${Number(timings[phase]).toFixed(0)}ms`).join(' · ');
}
function renderTimeline(requests) {
  const target = $('request-timeline'); const list = requests || []; $('timeline-count').textContent = `${list.length} 条`; target.replaceChildren(); if (!list.length) { target.append(Object.assign(document.createElement('span'), {className: 'muted', textContent: '暂无请求'})); return; }
  list.slice(-40).forEach(record => { const item = document.createElement('article'); item.className = `timeline-item ${record.outcome || ''}`; const head = document.createElement('header'); head.append(document.createElement('span'), document.createElement('span')); head.firstChild.textContent = text(record.kind, 'request'); head.lastChild.textContent = text(record.source, 'unknown'); const title = document.createElement('strong'); title.textContent = `${record.request_id ? record.request_id.slice(0, 8) : '—'} · ${text(record.speaker, '自动')}`; const detail = document.createElement('small'); detail.textContent = `${requestState(record)} · ${Number(record.elapsed_ms || 0).toFixed(0)} ms`; const timing = document.createElement('small'); timing.className = 'timeline-timings'; timing.textContent = stageTimings(record) || '尚无阶段耗时'; item.append(head, title, detail, timing); target.append(item); });
}
function eventRows(data) {
  const rows = []; const run = text(data.run_id, latest.run_id); (data.events || []).forEach(item => rows.push({source: 'session', run_id: run, seq: item.seq, request_id: item.data && item.data.request_id || '', timestamp: item.timestamp || item.data && item.data.timestamp || 0, type: item.event_type || item.type || 'event'})); const program = data.program || {}; (program.events || []).forEach(item => rows.push({source: 'program', run_id: run, seq: item.seq, request_id: item.data && (item.data.request_id || item.data.task_id) || '', timestamp: item.timestamp || 0, type: item.event_type || item.type || 'program'})); return rows.sort((a, b) => Number(a.timestamp || 0) - Number(b.timestamp || 0));
}
function renderEvents(data) {
  const filter = $('event-filter').value; const requestFilter = $('request-filter').value; const failedOnly = $('failed-filter').checked; let rows = eventRows(data).filter(row => (filter === 'all' || row.source === filter) && (requestFilter === 'all' || row.request_id === requestFilter)); const gaps = [];
  ['session', 'program'].forEach(source => { let previous = null; eventRows(data).filter(row => row.source === source && Number.isFinite(Number(row.seq))).forEach(row => { if (previous !== null && row.seq > previous + 1) gaps.push(source); previous = row.seq; }); });
  const failedIds = new Set((data.requests || []).filter(record => record.outcome === 'failed').map(record => record.request_id));
  if (failedOnly) rows = rows.filter(row => failedIds.has(row.request_id) || /failed|error/.test(row.type));
  const truncated = gaps.length || (data.events || []).some(e => e.seq > 200) || (data.program && data.program.event_seq > 200) || rows.length > 100; $('event-truncation').textContent = truncated ? '日志可能已截断 · 仅保留最近记录' : ''; rows = rows.slice(-100); $('events').textContent = rows.length ? rows.map(row => { const stamp = row.timestamp ? new Date(Number(row.timestamp) * 1000).toLocaleTimeString() : '--:--:--'; return `${stamp}  ${row.source}#${row.seq == null ? '—' : row.seq}  ${row.type}`; }).join('\n') : '暂无事件';
}
function renderAudio(value) { const level = Math.max(0, Math.min(1, Number(value) || 0)); $('audio-meter-fill').style.width = `${Math.round(level * 100)}%`; $('audio-level').textContent = level > .02 ? `${Math.round(level * 100)}%` : '静音'; }
function renderStatus(data) {
  latest = Object.assign({run_id: '', mode: 'demo', roles: [], requests: [], events: []}, data || {}); latest.requests = Array.isArray(latest.requests) ? latest.requests : [];
  if (latest.running || latest.program && (latest.program.running || latest.program.blocked)) { $('run-mode').value = latest.run_mode || (latest.program ? 'program' : 'manual'); renderRunMode(); }
  const phase = phaseNames[latest.phase] || latest.phase || '尚未开始'; const programActive = latest.program && (latest.program.running || latest.program.blocked); $('status-pill').textContent = latest.operation && latest.operation.running ? '检查中' : programActive ? (programPhaseNames[latest.program.phase] || latest.program.phase) : latest.running ? phase : latest.mode === 'demo' ? '示例舞台' : phase; $('status-pill').classList.toggle('live', !!(latest.running || programActive)); $('status-pill').classList.toggle('error', !!latest.last_error || latest.program && latest.program.last_error);
  $('connection-label').classList.toggle('offline', !connected); $('connection-label').innerHTML = `<b></b>${connected ? '控制台在线' : '控制台断开'}`; $('run-id-label').textContent = latest.run_id ? `RUN ${latest.run_id.slice(0, 10)}` : 'RUN —'; $('debug-run').textContent = latest.run_id ? latest.run_id.slice(0, 12) : '未运行'; $('queue').textContent = `排队 ${latest.queue_size || 0} 条${latest.next_speaker ? ` · 下一位 ${latest.next_speaker}` : ''}`; $('demo-note').hidden = latest.mode !== 'demo'; renderProgramStatus(latest.program); renderRoleSelects(); renderFeed(latest.requests); renderTimeline(latest.requests); renderEvents(latest); renderAudio(latest.audio_level); updateControls();
  const requestFilter = $('request-filter'); const currentFilter = requestFilter.value; requestFilter.replaceChildren(new Option('全部请求', 'all')); latest.requests.slice(-40).forEach(record => requestFilter.add(new Option(record.request_id.slice(0, 8), record.request_id))); requestFilter.value = Array.from(requestFilter.options).some(option => option.value === currentFilter) ? currentFilter : 'all'; renderEvents(latest);
  if (window.RoomView) window.RoomView.applyStatus(Object.assign({}, latest, {__connected: connected}));
  const op = latest.operation || {};
  $('check-result').textContent = latest.last_error || op.result && op.result.message || op.message || '保存真实服务配置后可进行检查。';
  const modelSignature = JSON.stringify(op.result && op.result.models ? op.result : null);
  if (modelSignature !== lastModelSignature) {
    lastModelSignature = modelSignature; $('model-results').replaceChildren();
    if (op.result && op.result.models) op.result.models.forEach(model => {
      const row = document.createElement('div'); row.className = 'model-item';
      const label = document.createElement('span'); label.textContent = `${model.modelName}${model.modelLoaded ? ' · 当前加载' : ''}`; row.append(label);
      const pick = button(row, '使用此 ID', () => {
        if (isBusy()) throw new Error('请先停止当前运行。');
        const role = configuration.roles.find(item => item.name === op.result.speaker);
        if (role) { role.avatar.model_id = model.modelID; renderRoles(); markDirty(); notice('已填入模型 ID，请保存配置。'); }
      });
      pick.className = 'model-choice'; row.append(pick); $('model-results').append(row);
    });
  }
  document.querySelectorAll('.model-choice').forEach(item => item.disabled = isBusy());
}
function renderInitialRoom() { if (!window.RoomView) return; const view = window.RoomView.init($('room-stage')); window.RoomView.applyStatus = view.applyStatus; window.RoomView.setConnection = view.setConnection; view.applyStatus({run_id: '', mode: 'demo', roles: [{name: '示例A'}, {name: '示例B'}], requests: [], current_request: null, program: null, __connected: true}); }
async function pollNow() {
  if (polling) { pollAgain = true; return; }
  polling = true;
  try {
    const result = await api('/api/status'); connected = true;
    if (wasDisconnected) notice('连接已恢复。');
    wasDisconnected = false; renderStatus(result);
  } catch (error) {
    connected = false; wasDisconnected = true;
    if (window.RoomView) window.RoomView.applyStatus(Object.assign({}, latest, {__connected: false, current_request: null, audio_level: 0}));
    renderAudio(0); updateControls();
    $('connection-label').classList.add('offline'); $('connection-label').innerHTML = '<b></b>控制台断开';
    notice('控制页连接中断，舞台已停止说话；正在等待恢复。', true);
  } finally {
    polling = false; window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(pollNow, pollAgain ? 0 : connected ? 650 : 1500);
    pollAgain = false;
  }
}
function postControl(path, extra) { return api(path, currentRunPayload(extra)); }
function send(speech) { const value = $('input').value.trim(), speaker = $('speaker').value || null; if (!value) throw new Error('请输入非空文字。'); if (speech && !speaker) throw new Error('直接朗读需要先选择一个角色。'); return postControl(speech ? '/api/say' : '/api/chat', {text: value, speaker}).then(() => { $('input').value = ''; return pollNow(); }); }
function exportDebug() { const payload = {run_id: latest.run_id || '', mode: latest.mode || '', event_seq: latest.event_seq || 0, program_event_seq: latest.program && latest.program.event_seq || 0, requests: (latest.requests || []).map(record => ({request_id: record.request_id, kind: record.kind, source: record.source, phase: record.phase, outcome: record.outcome, error_code: escapeErrorCode(record.error), queued_at: record.queued_at, updated_at: record.updated_at, timings: record.timings || {}, elapsed_ms: record.elapsed_ms}))}; const blob = new Blob([JSON.stringify(payload, null, 2)], {type: 'application/json'}); const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = `lumi-nox-debug-${(latest.run_id || 'idle').slice(0, 12)}.json`; link.click(); URL.revokeObjectURL(link.href); }

$('mode').addEventListener('change', () => { configuration.mode = $('mode').value; markDirty(); });
$('run-mode').addEventListener('change', renderRunMode);
$('auto-chat-interval').addEventListener('input', () => { configuration.program.auto_chat_interval = Number($('auto-chat-interval').value); markDirty(); });
$('gomoku-port').addEventListener('input', () => { configuration.program.gomoku_port = Number($('gomoku-port').value); markDirty(); });
$('add-stage').addEventListener('click', () => { const stages = configuration.program.stages; const index = stages.findIndex(stage => stage.kind === 'ending'); stages.splice(index < 0 ? stages.length : index, 0, {id: `stage-${Date.now()}`, kind: 'chat', title: '聊天', duration: 60, prompt: '自然地和观众聊天。'}); renderProgramEditor(); markDirty(); });
$('device').addEventListener('change', () => { const value = $('device').value; configuration.audio.device = value === '' ? null : (/^\d+$/.test(value) ? Number(value) : value); markDirty(); });
['service', 'roles', 'program'].forEach(name => $(name + '-tab').addEventListener('click', () => ['service', 'roles', 'program'].forEach(other => { $(other + '-tab').setAttribute('aria-selected', String(name === other)); $(other === 'program' ? 'program-panel-settings' : other + '-panel').hidden = name !== other; })));
$('save').addEventListener('click', () => run(async () => { configuration = await api('/api/config', configuration); renderConfig(); notice('配置已保存到本机。'); }));
$('devices').addEventListener('click', () => run(async () => { const result = await api('/api/devices'), selected = configuration.audio.device; $('device').replaceChildren(new Option('系统默认设备', '')); (result.devices || []).forEach(device => $('device').add(new Option(`${device.id} · ${device.name}`, String(device.id)))); if (selected !== null && !Array.from($('device').options).some(option => option.value === String(selected))) $('device').add(new Option(`${selected} · 当前未找到`, String(selected))); $('device').value = selected === null ? '' : String(selected); }));
$('start').addEventListener('click', () => run(() => api('/api/start', {run_mode: $('run-mode').value}).then(pollNow)));
$('demo-chat').addEventListener('click', () => run(() => api('/api/demo', {run_mode: 'manual'}).then(() => { latest.requests = []; $('request-feed').replaceChildren(); return pollNow(); })));
$('demo-program').addEventListener('click', () => run(() => { if (!capabilities.gomoku) throw new Error('完整节目需要 aiohttp；请先安装宿主依赖，或体验聊天。'); return api('/api/demo', {run_mode: 'program'}).then(pollNow); }));
[['next', '/api/next'], ['end', '/api/end'], ['stop', '/api/stop'], ['interrupt', '/api/interrupt']].forEach(([id, path]) => $(id).addEventListener('click', () => run(() => postControl(path).then(pollNow))));
$('probe').addEventListener('click', () => run(() => api('/api/probe-chat', {}).then(pollNow))); $('preview').addEventListener('click', () => run(() => api('/api/preview', {speaker: $('preview-role').value}).then(pollNow))); $('composer').addEventListener('submit', event => { event.preventDefault(); run(() => send(false)); }); $('say').addEventListener('click', () => run(() => send(true))); $('event-filter').addEventListener('change', () => renderEvents(latest)); $('request-filter').addEventListener('change', () => renderEvents(latest)); $('failed-filter').addEventListener('change', () => renderEvents(latest)); $('export-debug').addEventListener('click', exportDebug);

(async function boot() { renderInitialRoom(); try { [configuration, capabilities] = await Promise.all([api('/api/config'), api('/api/capabilities')]); renderConfig(); $('demo-program').disabled = !capabilities.gomoku; await pollNow(); } catch (error) { connected = false; notice(error.message || '无法读取本机控制台。', true); if (window.RoomView) window.RoomView.applyStatus({roles: [{name: '示例A'}, {name: '示例B'}], current_request: null, __connected: false}); } }());
