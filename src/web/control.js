'use strict';
(() => {
  const $=s=>document.querySelector(s);
  let state=null,csrf='',busy=false,initialized=false,serverClosed=false,toastTimer,refreshTicket=0,attempt='',lastActiveJob='';
  const names={export:'更新资源',generate:'生成页面',verify:'资源校验'};
  const states={starting:'准备中',authenticating:'验证会话',exporting:'读取内容',downloading:'保存媒体',rendering:'生成页面',verifying:'校验媒体',complete:'完成',partial:'含未完成项',failed:'失败',cancelled:'已停止'};
  const node=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  function toast(text,ok=false){clearTimeout(toastTimer);$('#toast').textContent=text;$('#toast').classList.toggle('success',ok);$('#toast').hidden=false;toastTimer=setTimeout(()=>$('#toast').hidden=true,9000);}
  async function post(path,data={}){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(data)});const result=await r.json();if(!r.ok)throw Error(result.error||'操作失败');return result;}
  function updateControls(){
    const locked=busy||serverClosed||state?.busy||state?.session.busy;
    document.querySelectorAll('button[type=submit],[data-provider],[data-auth],[data-job],[data-remove-library]').forEach(b=>b.disabled=!!locked);
    $('#cancel').disabled=busy||serverClosed;$('#shutdown').disabled=busy||serverClosed;$('#clear-history').disabled=busy||serverClosed||!state?.history.length;
  }
  async function action(fn){if(busy||serverClosed)return;busy=true;updateControls();try{await fn();if(!serverClosed)await refresh();}catch(e){toast(e.message);}finally{busy=false;updateControls();}}
  function selectTab(tab){
    document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));document.querySelectorAll('.tab').forEach(s=>s.classList.toggle('active',s.id==='tab-'+tab));
    $('#page-title').textContent={export:'导出任务',library:'备份库',account:'账号与设置'}[tab];
    $('#page-description').textContent={export:'选择保存位置，导出资源并生成浏览页面。',library:'管理已导出的资源和页面。',account:'登录 Replive 账号，设置默认保存位置。'}[tab];
  }
  document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>selectTab(b.dataset.tab));
  function preview(id){return '/preview/'+id+'/index.html';}
  function dateTime(value){
    const date=new Date(value);if(!value||Number.isNaN(date.getTime()))return '未记录';
    const pad=n=>String(n).padStart(2,'0');
    return date.getFullYear()+'-'+pad(date.getMonth()+1)+'-'+pad(date.getDate())+' '+pad(date.getHours())+':'+pad(date.getMinutes())+':'+pad(date.getSeconds());
  }
  function duration(job,running=false){
    const start=Date.parse(job.started),end=job.finished?Date.parse(job.finished):running?Date.now():NaN;
    if(!Number.isFinite(start)||!Number.isFinite(end)||end<start)return '未记录';
    let seconds=Math.floor((end-start)/1000);if(!seconds)return '不足1秒';
    const hours=Math.floor(seconds/3600);seconds%=3600;const minutes=Math.floor(seconds/60);seconds%=60;
    return (hours?hours+'小时 ':'')+(minutes?minutes+'分 ':'')+seconds+'秒';
  }
  function jobTimes(job){
    const times=node('div','history-times');
    for(const [label,value] of [['开始',job.started],['结束',job.finished]]){
      const item=node('span'),time=node('time','',dateTime(value));if(value)time.dateTime=value;item.append(label+' ',time);times.append(item);
    }
    times.append(node('span','','耗时 '+duration(job)));return times;
  }
  function renderJob(){
    $('#current-task').hidden=!state?.busy||!state.job;if(!state?.busy||!state.job)return;
    const j=state.job;$('#job-state').textContent=states[j.state]||j.state;$('#job-detail').textContent=j.detail;$('#job-kind').textContent=names[j.kind];$('#job-path').textContent=j.output;$('#job-time').textContent='开始 '+dateTime(j.started)+' · 已运行 '+duration(j,true);if(j.started)$('#job-time').dateTime=j.started;
    const done=j.media_done||0,total=j.media_total||0;$('#job-count').textContent=total?done+' / '+total+' 个文件':j.pages?'已读取 '+j.pages+' 页':'';
    if(total){$('#progress').max=total;$('#progress').value=done;}else $('#progress').removeAttribute('value');
  }
  async function startJob(data){
    const result=await post('/api/jobs',data);++refreshTicket;state.job=result.job;state.busy=true;lastActiveJob=result.job.id;renderJob();updateControls();
  }
  $('#export-form').onsubmit=e=>{e.preventDefault();action(async()=>{if(!state.session.saved){selectTab('account');toast('请先登录。');return;}await post('/api/settings',{output:$('#output').value});await startJob({kind:'export',output:$('#output').value});});};
  $('#settings-form').onsubmit=e=>{e.preventDefault();action(async()=>{await post('/api/settings',{output:$('#default-output').value});$('#output').value=$('#default-output').value;toast('保存位置已更新。',true);});};
  $('#sms-form').onsubmit=e=>{e.preventDefault();action(async()=>{await post('/api/auth/send',{country:$('#country').value,phone:$('#phone').value});clearSNS();toast('验证码已发送。',true);});};
  $('#login-form').onsubmit=e=>{e.preventDefault();action(async()=>{try{await post('/api/auth/login',{code:$('#code').value});$('#phone').value='';clearSNS();toast('登录成功。',true);}finally{$('#code').value='';}});};
  $('#token-form').onsubmit=e=>{e.preventDefault();action(async()=>{try{await post('/api/auth/token',{token:$('#token').value});clearSNS();toast('会话已导入。',true);}finally{$('#token').value='';}});};
  function clearSNS(){attempt='';$('#sns-login').hidden=true;$('#sns-callback').value='';$('#sns-open').removeAttribute('href');}
  document.querySelectorAll('[data-provider]').forEach(b=>b.onclick=()=>action(async()=>{
    clearSNS();clearTimeout(toastTimer);$('#toast').hidden=true;
    const result=await post('/api/auth/sns-start',{provider:b.dataset.provider});attempt=result.attempt;
    $('#sns-title').textContent=(result.provider==='google'?'Google':'X / Twitter')+' 登录';$('#sns-open').href=result.auth_url;$('#sns-callback').value='';$('#sns-login').hidden=false;
    $('#sns-instructions').textContent=result.automatic_callback?'在系统浏览器完成登录，授权结果会自动返回。':'此环境需要手动粘贴登录回调地址。';
    $('#sns-login details').open=!result.automatic_callback;
  }));
  $('#sns-system-open').onclick=()=>action(()=>post('/api/auth/sns-open',{attempt}));
  $('#sns-form').onsubmit=e=>{e.preventDefault();action(async()=>{try{await post('/api/auth/sns-finish',{attempt,callback:$('#sns-callback').value});clearSNS();toast('登录成功。',true);}finally{$('#sns-callback').value='';}});};
  $('#sns-cancel').onclick=()=>action(async()=>{await post('/api/auth/sns-cancel');clearSNS();});
  document.querySelectorAll('[data-auth]').forEach(b=>b.onclick=()=>action(async()=>{await post('/api/auth/'+b.dataset.auth);clearSNS();toast(b.dataset.auth==='forget'?'已退出登录。':'检查完成。',true);}));
  $('#clear-history').onclick=()=>action(async()=>{await post('/api/jobs/history/clear');toast('最近任务已清除。',true);});
  $('#cancel').onclick=()=>action(()=>post('/api/jobs/cancel'));
  $('#shutdown').onclick=()=>action(async()=>{await post('/api/shutdown');serverClosed=true;$('#session-badge').textContent='服务已关闭';$('#current-task').hidden=true;toast('服务已关闭。',true);});
  function libraryButton(label,kind,id){const b=node('button','secondary',label);b.dataset.job=kind;b.onclick=()=>action(()=>startJob({kind,library:id}));return b;}
  function renderLibraries(){
    const grid=$('#libraries');grid.replaceChildren();for(const lib of state.libraries){
      const p=node('article','panel library-card');p.append(node('h3','',lib.name),node('div','library-path',lib.path));const stats=node('div','statistics');
      for(const [value,label] of [[lib.summary.rooms??'—','聊天'],[lib.summary.messages??'—','消息'],[lib.summary.answers??'—','回答视频']]){const block=node('div');block.append(node('strong','',value),node('small','',label));stats.append(block);}p.append(stats);
      const actions=node('div','actions');if(lib.generated){const a=node('a','primary','打开页面 ↗');a.href=preview(lib.id);a.target='_blank';a.rel='noopener';actions.append(a);}
      actions.append(libraryButton('生成页面','generate',lib.id),libraryButton('校验资源','verify',lib.id),libraryButton('更新资源','export',lib.id));
      const remove=node('button','secondary library-remove','移除');remove.dataset.removeLibrary=lib.id;remove.title='从备份库移除，保留文件';remove.onclick=()=>action(async()=>{await post('/api/libraries/remove',{library:lib.id});toast('已从备份库移除，文件保留。',true);});actions.append(remove);p.append(actions);
      if(lib.generated){const reports=node('div','actions');for(const [file,label] of [['导出报告.json','导出报告'],['缺失资源.json','缺失资源'],['备份说明.md','文件说明']]){const a=node('a','text-button',label);a.href='/preview/'+lib.id+'/'+encodeURIComponent(file);a.target='_blank';a.rel='noopener';reports.append(a);}p.append(reports);}
      if(lib.verified)p.append(node('p','muted',lib.verified.ok?'✓ 校验通过 · '+lib.verified.media_files+' 个媒体':'校验存在异常，请查看校验报告'));grid.append(p);
    }if(!state.libraries.length)grid.append(node('div','panel muted','暂无备份。'));
  }
  let lastLibrary='';
  async function refresh(){
    const ticket=++refreshTicket,response=await fetch('/api/status');if(!response.ok)throw Error('无法读取控制台状态');const snapshot=await response.json();if(ticket!==refreshTicket)return;
    state=snapshot;csrf=state.csrf;if(!initialized){$('#output').value=state.settings.output;$('#default-output').value=state.settings.output;initialized=true;}
    $('#session-badge').textContent=state.session.account?.display_name?'已登录 · '+state.session.account.display_name:state.session.saved?'已保存会话':'尚未登录';$('#auth-detail').textContent=state.session.detail;renderJob();
    if(state.busy&&state.job)lastActiveJob=state.job.id;
    else if(lastActiveJob&&state.job?.id===lastActiveJob&&state.job.finished){toast(state.job.detail,state.job.state==='complete');lastActiveJob='';}
    if(attempt&&['expired','failed','cancelled','complete'].includes(state.session.sns?.state)){const result=state.session.sns;clearSNS();toast(result.detail,result.state==='complete');}
    const history=$('#history');history.replaceChildren();for(const j of [...state.history].reverse()){const row=node('div','history-row'),copy=node('div','history-copy');copy.append(node('div','',j.detail),jobTimes(j));row.append(node('strong','',names[j.kind]||j.kind),copy,node('span','history-state',states[j.state]||j.state));history.append(row);}if(!state.history.length)history.append(node('p','muted','暂无已完成任务。'));
    const signature=JSON.stringify(state.libraries);if(signature!==lastLibrary){renderLibraries();lastLibrary=signature;}updateControls();
  }
  const dialog=$('#folder-dialog');let parent='',current='',chooseMode='output';
  async function folders(path,fallback=false){const r=await fetch('/api/folders?path='+encodeURIComponent(path)+(fallback?'&fallback=1':'')),v=await r.json();if(!r.ok)throw Error(v.error);current=v.path;parent=v.parent;$('#folder-path').value=v.path;$('#folder-list').replaceChildren();for(const name of v.directories){const b=node('button','','▱  '+name);b.onclick=()=>action(()=>folders(current+'/'+name));$('#folder-list').append(b);}}
  async function openFolder(mode){chooseMode=mode;$('#folder-dialog h2').textContent=mode==='import'?'选择已有备份':'选择父文件夹';$('#folder-select').textContent=mode==='import'?'添加备份':'使用此位置';$('#folder-name').value=mode==='import'?'':'reparchive-backup';await folders(state.settings.output.replace(/[\\/][^\\/]+$/,''),true);dialog.showModal();}
  $('#browse').onclick=()=>action(()=>openFolder('output'));$('#import').onclick=()=>action(()=>openFolder('import'));$('#folder-close').onclick=()=>dialog.close();$('#folder-up').onclick=()=>action(()=>folders(parent));
  $('#folder-path-form').onsubmit=e=>{e.preventDefault();action(()=>folders($('#folder-path').value));};
  $('#folder-select').onclick=()=>action(async()=>{const name=$('#folder-name').value.trim();if(/[\\/]/.test(name)||name==='..'||name==='.')throw Error('请填写单个文件夹名称。');const value=current+(name?'/'+name:'');if(chooseMode==='import'){await post('/api/libraries/import',{path:value});toast('备份已添加。',true);}else $('#output').value=value;dialog.close();});
  refresh().catch(e=>toast(e.message));setInterval(()=>{if(!serverClosed&&!busy)refresh().catch(()=>{});},1500);
})();
