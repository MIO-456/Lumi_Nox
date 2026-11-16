(function () {
  'use strict';

  const palettes = [
    {main: '#ff9fc8', soft: '#ffd0e4', dark: '#321c2b'},
    {main: '#ff9b57', soft: '#ffd0a7', dark: '#382417'},
  ];
  const phaseLabels = {idle: '待机', starting: '连接中', ready: '待命', generating: '思考中', synthesizing: '待播', playing: '发言中', stopping: '停止中', cancelling: '已打断'};
  const activePhases = new Set(['playing']);
  const cancelledPhases = new Set(['cancelling', 'cancelled', 'failed']);
  const text = (value, fallback) => typeof value === 'string' && value ? value : (fallback || '');
  const clamp = (value, low, high) => Math.max(low, Math.min(high, Number(value) || 0));

  function create(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  }

  function makeRobot(role, index) {
    const palette = palettes[index % palettes.length];
    const figure = create('div', `avatar avatar-${index}`);
    figure.style.setProperty('--avatar-main', palette.main);
    figure.style.setProperty('--avatar-soft', palette.soft);
    figure.innerHTML = '<div class="antenna"><i></i></div><div class="robot-head"><span class="eye left"></span><span class="eye right"></span><span class="visor"></span></div><div class="robot-neck"></div><div class="robot-body"><span class="badge"></span><i class="arm left"></i><i class="arm right"></i><div class="chest-light"></div></div><div class="robot-shadow"></div>';
    const card = create('article', `character-card character-${index}`);
    card.style.setProperty('--avatar-main', palette.main);
    card.style.setProperty('--avatar-soft', palette.soft);
    const halo = create('div', 'character-halo');
    const meta = create('div', 'character-meta');
    const name = create('strong', 'character-name', text(role && role.name, index ? '角色二' : '角色一'));
    const state = create('span', 'character-state', '待机');
    const level = create('span', 'character-level', 'OFFLINE DEMO');
    meta.append(name, state, level);
    card.append(halo, figure, meta);
    return {card, state, name, level, figure};
  }

  function createBoard() {
    const wrap = create('div', 'board-stage');
    const header = create('div', 'board-header');
    header.append(create('span', 'board-kicker', 'LIVE ACTIVITY'), create('strong', 'board-title', '五子棋对决'), create('span', 'board-state', '等待棋盘'));
    const body = create('div', 'board-body');
    const board = create('canvas', 'gomoku-board');
    board.width = 750; board.height = 750;
    const score = create('div', 'board-score');
    body.append(board, score);
    wrap.append(header, body);
    return {wrap, header, board, score, stateLabel: header.querySelector('.board-state')};
  }

  function drawBoard(canvas, state) {
    const context = canvas.getContext('2d');
    if (!context) return;
    const size = Number(state && state.size) || 15;
    const width = canvas.width;
    const pad = 42;
    const gap = (width - pad * 2) / Math.max(1, size - 1);
    context.clearRect(0, 0, width, width);
    const gradient = context.createLinearGradient(0, 0, width, width);
    gradient.addColorStop(0, '#39273b'); gradient.addColorStop(1, '#211827');
    context.fillStyle = gradient; context.fillRect(0, 0, width, width);
    context.strokeStyle = 'rgba(237, 199, 126, .45)'; context.lineWidth = 2;
    for (let i = 0; i < size; i += 1) {
      const point = pad + gap * i;
      context.beginPath(); context.moveTo(pad, point); context.lineTo(width - pad, point); context.stroke();
      context.beginPath(); context.moveTo(point, pad); context.lineTo(point, width - pad); context.stroke();
    }
    context.fillStyle = 'rgba(237,199,126,.8)';
    [[3, 3], [3, 11], [7, 7], [11, 3], [11, 11]].forEach(([row, col]) => { context.beginPath(); context.arc(pad + col * gap, pad + row * gap, 5, 0, Math.PI * 2); context.fill(); });
    const winning = new Set((state && state.winning_line || []).map(([row, col]) => `${row},${col}`));
    const owners = Object.keys(state && state.scores || {});
    (state && state.board || []).forEach((row, r) => row.forEach((value, c) => {
      if (!value) return;
      const owner = Object.keys(state.colors || {}).find(name => state.colors[name] === (value === 1 ? 'black' : 'white')) || owners[value - 1] || '';
      const slot = Math.max(0, owners.indexOf(owner));
      const palette = palettes[slot % palettes.length];
      const x = pad + c * gap, y = pad + r * gap, radius = gap * .37;
      context.save(); context.shadowColor = palette.main; context.shadowBlur = 12; context.fillStyle = slot ? '#17111c' : palette.main;
      context.beginPath(); context.arc(x, y, radius, 0, Math.PI * 2); context.fill(); context.shadowBlur = 0; context.strokeStyle = slot ? palette.main : palette.soft; context.lineWidth = slot ? 6 : 2; context.stroke();
      if (winning.has(`${r},${c}`)) { context.strokeStyle = '#edc77e'; context.lineWidth = 5; context.beginPath(); context.arc(x, y, radius + 5, 0, Math.PI * 2); context.stroke(); }
      context.restore();
    }));
    if (state && state.last_move) { const [row, col] = state.last_move; const x = pad + col * gap, y = pad + row * gap; context.fillStyle = '#f5f0f4'; context.beginPath(); context.arc(x, y, gap * .08, 0, Math.PI * 2); context.fill(); }
  }

  function renderBoard(view, activity, program) {
    const state = activity && activity.state || {};
    const names = Object.keys(state.scores || {});
    const phase = program && program.phase;
    const boardPhase = new Set(['activity', 'finalizing', 'transitioning']).has(phase);
    const visible = !!(activity && boardPhase && (activity.running || activity.phase === 'active' || activity.state || activity.result));
    view.wrap.hidden = !visible;
    view.stateLabel.textContent = activity && activity.running ? (state.actor ? `${state.actor} 落子` : '对局进行中') : (state.status === 'match_over' || state.match_result ? '对局结算' : '等待棋盘');
    view.score.replaceChildren();
    names.slice(0, 2).forEach((name, index) => { const item = create('div', `board-score-item score-${index}`); item.append(create('span', 'score-name', name), create('strong', 'score-number', String(state.scores[name] || 0))); view.score.append(item); });
    if (names.length === 0 && activity && activity.result && activity.result.scores) Object.entries(activity.result.scores).forEach(([name, value], index) => { const item = create('div', `board-score-item score-${index}`); item.append(create('span', 'score-name', name), create('strong', 'score-number', String(value || 0))); view.score.append(item); });
    drawBoard(view.board, state);
  }

  function init(container) {
    const shell = create('div', 'room-shell');
    const backdrop = create('div', 'studio-backdrop');
    const sign = create('div', 'studio-sign', 'LUMI / NOX');
    const top = create('div', 'room-topline'); top.append(create('span', 'room-label', 'VIRTUAL STUDIO'), create('span', 'room-light', '● ON AIR'));
    const floor = create('div', 'studio-floor');
    const stageCenter = create('div', 'stage-center');
    stageCenter.append(create('div', 'center-line', 'LIVE / 01'), create('div', 'caption', '欢迎来到双角色直播间'));
    const roleLayer = create('div', 'role-layer');
    const left = makeRobot({name: '示例A'}, 0), right = makeRobot({name: '示例B'}, 1);
    roleLayer.append(left.card, right.card);
    const subtitle = create('div', 'subtitle-box'); subtitle.append(create('span', 'subtitle-speaker'), create('p', 'subtitle-text'));
    const waiting = create('div', 'stage-waiting', '示例舞台 · 点击“体验聊天”开始');
    const board = createBoard(); board.wrap.hidden = true;
    shell.append(backdrop, sign, top, stageCenter, floor, roleLayer, board.wrap, subtitle, waiting);
    container.replaceChildren(shell);
    let previous = null;
    let lastStatus = {roles: [{name: '示例A'}, {name: '示例B'}], current_request: null, program: null, mode: 'demo'};

    function applyStatus(status) {
      status = status || {};
      lastStatus = Object.assign({}, lastStatus, status);
      const roles = Array.isArray(status.roles) && status.roles.length ? status.roles : [{name: '示例A'}, {name: '示例B'}];
      const connected = status.__connected !== false;
      const stopping = !!status.stopping || !!(status.program && ['stopping', 'cleanup_failed'].includes(status.program.phase));
      const current = connected && !stopping ? status.current_request : null;
      const phase = current && current.phase;
      shell.classList.toggle('disconnected', !connected);
      const running = !!(status.running || status.program && status.program.running);
      shell.classList.toggle('running', running);
      top.querySelector('.room-light').textContent = !connected ? '● DISCONNECTED' : running ? '● LOCAL RUN' : '● STANDBY';
      stageCenter.hidden = !!(status.program && ['activity', 'finalizing'].includes(status.program.phase));
      shell.classList.toggle('single', roles.length === 1);
      shell.style.setProperty('--level', String(clamp(status.audio_level, 0, 1)));
      [left, right].forEach((view, index) => { const role = roles[index]; view.card.hidden = !role; view.name.textContent = text(role && role.name, index ? '角色二' : '角色一'); view.level.textContent = !status.run_id || status.mode === 'demo' ? 'SILENT DEMO' : 'LIVE SERVICE'; });
      [left, right].forEach((view, index) => { const name = roles[index] && roles[index].name; const speaking = !!(connected && !stopping && current && current.speaker === name && activePhases.has(phase)); const queued = !!(connected && !stopping && current && current.speaker === name && !speaking && !cancelledPhases.has(phase)); view.card.classList.toggle('speaking', speaking); view.card.classList.toggle('queued', queued); view.state.textContent = speaking ? '发言中' : queued ? (phaseLabels[phase] || '待播') : '待机'; view.figure.classList.toggle('talking', speaking); });
      const showSubtitle = !!(connected && !stopping && current && activePhases.has(phase) && !cancelledPhases.has(phase) && current.text);
      subtitle.hidden = !showSubtitle;
      if (showSubtitle) { subtitle.querySelector('.subtitle-speaker').textContent = current.speaker || '角色'; subtitle.querySelector('.subtitle-text').textContent = current.text; }
      const waitingText = !connected ? '控制台断开 · 舞台已静音' : stopping ? '正在停止 · 舞台已静音' : current && phase === 'synthesizing' ? `${current.speaker || '角色'} · 正在准备发言` : current && phase === 'generating' ? `${current.speaker || '角色'} · 正在思考` : status && (status.running || status.program && status.program.running) ? '直播进行中' : '示例舞台 · 点击“体验聊天”开始';
      waiting.textContent = waitingText; waiting.hidden = showSubtitle;
      renderBoard(board, status.program && status.program.activity, status.program);
      shell.classList.toggle('has-game', !board.wrap.hidden);
      if (previous && current && previous.request_id !== current.request_id) subtitle.classList.remove('fresh');
      if (showSubtitle) { subtitle.classList.add('fresh'); window.setTimeout(() => subtitle.classList.remove('fresh'), 120); }
      previous = current;
    }
    function setConnection(connected) { applyStatus(Object.assign({}, lastStatus, {__connected: connected, current_request: connected ? lastStatus.current_request : null, audio_level: connected ? lastStatus.audio_level : 0})); }
    return {applyStatus, setConnection, board: board.wrap};
  }

  window.RoomView = {init};
}());
