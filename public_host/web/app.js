'use strict';
let configuration, dirty = false, latest = {}, lastMessages = '', lastModelResult = '';
const $ = id => document.getElementById(id);
const phaseNames = {idle:'尚未开始',starting:'正在连接',ready:'等待输入',generating:'正在生成回复',synthesizing:'正在合成声音',playing:'正在播放',stopping:'正在停止'};
const outcomeNames = {complete:'已播完',simulated:'演示完成',cancelled:'已取消',failed:'失败'};

async function api(path, data) {
  const response = await fetch(path, data === undefined ? {cache:'no-store'} : {
    method:'POST', headers:{'Content-Type':'application/json','X-Public-Host':'1'}, body:JSON.stringify(data)
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || '操作未完成。');
  return body;
}
function notice(message, error=false) { $('notice').textContent=message; $('notice').hidden=!message; $('notice').classList.toggle('error',error); }
function edit() { dirty=true; $('saved').textContent='有未保存的修改'; updateControls(); }
function field(parent, title, value, change, type='text', hint='') {
  const label=document.createElement('label'); label.className='field'; label.append(document.createTextNode(title));
  const input=document.createElement(type==='textarea'?'textarea':'input');
  if(type!=='textarea') input.type=type;
  input.value=value ?? ''; input.autocomplete=type==='password'?'new-password':'off';
  input.addEventListener('input',()=>{ change(type==='number'?Number(input.value):input.value); edit(); });
  label.append(input); if(hint){const small=document.createElement('small');small.className='hint';small.textContent=hint;label.append(small);} parent.append(label); return input;
}
function checkbox(parent,title,value,change) {
  const label=document.createElement('label');label.className='check-label';const input=document.createElement('input');input.type='checkbox';input.checked=value;
  input.addEventListener('change',()=>{change(input.checked);edit();});label.append(input,document.createTextNode(title));parent.append(label);return input;
}
function select(parent,title,value,options,change) {
  const label=document.createElement('label');label.className='field';label.append(document.createTextNode(title));
  const element=document.createElement('select'); for(const [id,name] of options) element.add(new Option(name,id));element.value=value;
  element.addEventListener('change',()=>{change(element.value);edit();});label.append(element);parent.append(label);return element;
}
function button(parent,text,action) {const b=document.createElement('button');b.type='button';b.textContent=text;b.addEventListener('click',()=>run(action));parent.append(b);return b;}

function renderConfig() {
  $('mode').value=configuration.mode;
  $('services').replaceChildren();
  for(const [name,title] of [['llm','聊天服务'],['tts','语音服务']]) {
    const data=configuration[name], block=document.createElement('section');block.className='service-block';
    const heading=document.createElement('h3');heading.textContent=title;block.append(heading);
    const protocol=document.createElement('p');protocol.className='hint';protocol.textContent=name==='llm'?'OpenAI-compatible · 文本回复':'OpenAI-compatible · WAV 语音输出';block.append(protocol);
    field(block,'基础地址',data.base_url,v=>data.base_url=v,'url','填写服务商给出的 API 基础地址，通常以 /v1 结尾。更换地址后需重新填写 Key。');
    field(block,'模型名',data.model,v=>data.model=v);
    const key=field(block,'API Key', '',v=>data.api_key=v,'password');key.placeholder=data.has_api_key?'已保存 · 留空保持原 Key':'无鉴权的本地服务可以留空';
    checkbox(block,'清除已保存的 Key',false,v=>data.clear_api_key=v);
    field(block,'请求超时（秒）',data.timeout,v=>data.timeout=v,'number');
    if(name==='llm') {
      const row=document.createElement('div');row.className='row';
      field(row,'回复长度上限',data.max_tokens,v=>data.max_tokens=v,'number');
      select(row,'长度字段',data.token_limit_field,[['max_tokens','max_tokens'],['max_completion_tokens','max_completion_tokens'],['','不发送']],v=>data.token_limit_field=v);block.append(row);
    }
    $('services').append(block);
  }
  renderRoles();renderRoleSelects();
  const device=configuration.audio.device;
  $('device').replaceChildren(new Option('系统默认设备',''));
  if(device!==null) $('device').add(new Option(String(device),String(device)));
  $('device').value=device===null?'':String(device);
  dirty=false;$('saved').textContent='本机配置';updateControls();
}
function renderRoles() {
  $('roles').replaceChildren();
  configuration.roles.forEach((role,index)=>{
    const card=document.createElement('section');card.className='role-card';const title=document.createElement('div');title.className='role-heading';
    const h=document.createElement('h3');h.textContent=`角色 ${index+1}`;title.append(h);
    const options=document.createElement('div');checkbox(title,'启用',role.enabled,v=>{role.enabled=v;options.hidden=!v;});card.append(title);options.hidden=!role.enabled;
    field(options,'角色名',role.name,v=>role.name=v);
    field(options,'角色设定',role.system_prompt,v=>role.system_prompt=v,'textarea');
    field(options,'音色名 / ID',role.voice,v=>role.voice=v,'text','由你选择的语音服务提供。');
    const avatar=role.avatar;checkbox(options,'连接 VTube Studio',avatar.enabled,v=>avatar.enabled=v);
    const avatarOptions=document.createElement('details');avatarOptions.className='avatar-options';const summary=document.createElement('summary');summary.textContent='VTS 连接与嘴型';avatarOptions.append(summary);
    field(avatarOptions,'VTS 地址',avatar.url,v=>avatar.url=v);
    field(avatarOptions,'模型 ID',avatar.model_id,v=>avatar.model_id=v,'text','加载模型后读取列表，选择当前模型。宿主不会自动切换你的皮套。');
    field(avatarOptions,'嘴型输入参数',avatar.mouth_parameter,v=>avatar.mouth_parameter=v);
    field(avatarOptions,'嘴型灵敏度',avatar.mouth_gain,v=>avatar.mouth_gain=v,'number');
    const actions=document.createElement('div');actions.className='avatar-buttons';
    for(const [text,authorize] of [['读取模型',false],['首次授权并读取',true]]) {
      const b=button(actions,text,async()=>{if(dirty)throw new Error('请先保存配置。');await api('/api/avatar',{speaker:role.name,authorize});await poll();});b.className='idle-action';
    }
    avatarOptions.append(actions);options.append(avatarOptions);card.append(options);$('roles').append(card);
  });
}
function renderRoleSelects() {
  $('speaker').replaceChildren(new Option('自动轮换',''));$('preview-role').replaceChildren();
  for(const role of configuration.roles.filter(r=>r.enabled)) {
    $('speaker').add(new Option(role.name,role.name));$('preview-role').add(new Option(role.name,role.name));
  }
}
function updateControls() {
  const busy=!!(latest.running || latest.operation?.running), talking=latest.running&&!latest.stopping&&!latest.preview;
  $('configuration').disabled=busy;$('save').disabled=busy;$('start').disabled=busy||dirty;
  $('stop').disabled=!busy;$('interrupt').disabled=!talking;
  for(const id of ['input','send','say','speaker']) $(id).disabled=!talking;
  for(const id of ['probe','preview']) $(id).disabled=busy||dirty||configuration?.mode!=='live';
  document.querySelectorAll('.idle-action').forEach(b=>b.disabled=busy||dirty);
  document.querySelectorAll('.model-choice').forEach(b=>b.disabled=busy);
}
function renderStatus(data) {
  latest=data;updateControls();
  const phase=phaseNames[data.phase]||data.phase;
  $('status-pill').textContent=data.operation?.running?'连接检查中':phase;$('status-pill').classList.toggle('active',data.running);
  $('phase').textContent=data.speaker?`${phase} · ${data.speaker}`:phase;
  $('queue').textContent=`排队 ${data.queue_size||0} 条${data.next_speaker?' · 下一位 '+data.next_speaker:''}`;
  $('demo-note').hidden=configuration.mode!=='demo';
  const signature=JSON.stringify(data.messages);
  if(signature!==lastMessages&&data.messages.length) {
    lastMessages=signature;$('messages').replaceChildren();
    for(const m of data.messages) {
      const article=document.createElement('article');article.className='message '+(m.role==='user'?'user':'assistant');
      const header=document.createElement('header'), name=document.createElement('span'),outcome=document.createElement('span');
      name.textContent=m.role==='user'?'你':m.speaker;outcome.className='outcome';outcome.textContent=outcomeNames[m.outcome]||m.outcome||'';
      header.append(name,outcome);const p=document.createElement('p');p.textContent=m.text;article.append(header,p);$('messages').append(article);
    }
    $('messages').scrollTop=$('messages').scrollHeight;
  }
  $('events').textContent=data.events.slice(-25).map(e=>`${e.event_type}  ${JSON.stringify(e.data)}`).join('\n');
  const op=data.operation || {};
  if(!op.result?.models) { $('model-results').replaceChildren(); lastModelResult=''; }
  $('check-result').textContent=data.last_error || (op.result?.message) || op.message || '先保存配置。检查与试听在会话停止时使用。';
  if(op.result?.models && JSON.stringify(op.result)!==lastModelResult) {
    lastModelResult=JSON.stringify(op.result);$('model-results').replaceChildren();
    for(const model of op.result.models) {
      const row=document.createElement('div');row.className='model-item';const text=document.createElement('span');text.textContent=`${model.modelName}${model.modelLoaded?' · 当前加载':''}`;row.append(text);
      const pick=button(row,'使用此 ID',()=>{const role=configuration.roles.find(r=>r.name===op.result.speaker);if(role){role.avatar.model_id=model.modelID;renderRoles();edit();notice('已填入模型 ID，请保存配置。');}});pick.className='model-choice';pick.disabled=!!(data.running||op.running);$('model-results').append(row);
    }
  }
}
async function poll() {renderStatus(await api('/api/status'));}
async function run(action) {try{notice('');await action();}catch(error){notice(error.message,true);}}
$('mode').addEventListener('change',()=>{configuration.mode=$('mode').value;edit();$('demo-note').hidden=configuration.mode!=='demo';});
$('device').addEventListener('change',()=>{const value=$('device').value;configuration.audio.device=value===''?null:(/^\d+$/.test(value)?Number(value):value);edit();});
for(const name of ['service','roles']) $(name+'-tab').addEventListener('click',()=>{
  for(const other of ['service','roles']) {$(other+'-tab').setAttribute('aria-selected',String(name===other));$(other+'-panel').hidden=name!==other;}
});
$('save').addEventListener('click',()=>run(async()=>{configuration=await api('/api/config',configuration);renderConfig();notice('配置已保存到本机。');}));
$('devices').addEventListener('click',()=>run(async()=>{
  const result=await api('/api/devices'), selected=configuration.audio.device;$('device').replaceChildren(new Option('系统默认设备',''));
  for(const device of result.devices) $('device').add(new Option(`${device.id} · ${device.name}`,String(device.id)));
  if(selected!==null&&!Array.from($('device').options).some(o=>o.value===String(selected))) $('device').add(new Option(`${selected} · 当前未找到`,String(selected)));
  $('device').value=selected===null?'':String(selected);
}));
for(const [id,path] of [['start','start'],['stop','stop'],['interrupt','interrupt'],['probe','probe-chat']]) $(id).addEventListener('click',()=>run(async()=>{await api('/api/'+path,{});if(id==='start'){lastMessages='';$('messages').replaceChildren();}await poll();}));
$('preview').addEventListener('click',()=>run(async()=>{await api('/api/preview',{speaker:$('preview-role').value});await poll();}));
async function send(speech=false) {
  const text=$('input').value, speaker=$('speaker').value||null;
  if(speech&&!speaker) throw new Error('直接朗读需要先选择一个角色。');
  await api(speech?'/api/say':'/api/chat',{text,speaker});$('input').value='';await poll();
}
$('composer').addEventListener('submit',event=>{event.preventDefault();run(()=>send());});
$('say').addEventListener('click',()=>run(()=>send(true)));
(async()=>{
  try{configuration=await api('/api/config');renderConfig();await poll();}
  catch(error){notice(error.message,true);return;}
  setInterval(()=>poll().catch(()=>notice('控制页连接中断，请确认宿主仍在运行。',true)),650);
})();
