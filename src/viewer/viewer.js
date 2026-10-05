'use strict';
(() => {
  const archive=window.__REPARCHIVE__||window.__REPLIVE_ARCHIVE__;
  const main=document.querySelector('#main'),app=document.querySelector('#app'),modal=document.querySelector('#modal');
  if(!archive){main.textContent='アーカイブデータが見つかりません。';return;}
  const loaded={},pending={},filters={answers:'',people:'',saved:''};
  let activeRoom=null,view='chats',query='',date='',mode='messages';
  let playingMedia=null;
  function stopPlayback(){if(playingMedia){playingMedia.pause();playingMedia=null;}}
  // Media events do not bubble. Capture them for inline and modal players alike.
  document.addEventListener('play',e=>{if(!(e.target instanceof HTMLMediaElement))return;if(playingMedia!==e.target)stopPlayback();playingMedia=e.target;},true);
  // Preserve favorites made with earlier generated pages.
  const legacyKey='replive-local-'+archive.rooms.map(r=>r.id).join('-');
  const storageKey='reparchive-'+archive.rooms.map(r=>r.id).join('-');
  let saved;try{saved=new Set(JSON.parse(localStorage.getItem(storageKey)||localStorage.getItem(legacyKey)||'[]'));}catch{saved=new Set();}
  const persist=()=>{try{localStorage.setItem(storageKey,JSON.stringify([...saved]));}catch{}};
  const localURL=path=>path.split('/').map(encodeURIComponent).join('/');
  function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;}
  function button(label,fn,cls=''){const b=el('button',cls,label);b.onclick=fn;return b;}
  function image(src,cls,alt=''){const i=el('img',cls);i.src=localURL(src);i.alt=alt;i.loading='lazy';return i;}
  function person(uid){return archive.people.find(p=>p.id===uid)||{name:'',avatar:''};}
  function avatar(src,cls='avatar'){return src?image(src,cls):el('div',cls);}
  function showModal(node){modal.querySelectorAll('audio,video').forEach(v=>v.pause());document.querySelector('#modal-body').replaceChildren(node);modal.showModal();}
  function closeModal(){modal.querySelectorAll('audio,video').forEach(v=>v.pause());modal.close();document.querySelector('#modal-body').replaceChildren();}
  document.querySelector('.modal-close').onclick=closeModal;
  modal.addEventListener('click',e=>{if(e.target===modal)closeModal();});
  modal.addEventListener('cancel',e=>{e.preventDefault();closeModal();});
  function favorite(key){
    const b=button(saved.has(key)?'★':'☆',()=>{
      saved.has(key)?saved.delete(key):saved.add(key);persist();b.textContent=saved.has(key)?'★':'☆';b.classList.toggle('saved',saved.has(key));
      b.setAttribute('aria-pressed',String(saved.has(key)));if(view==='saved'){renderSidebar();renderSaved();}
    },'star');
    b.classList.toggle('saved',saved.has(key));b.setAttribute('aria-label','お気に入り');b.setAttribute('aria-pressed',String(saved.has(key)));return b;
  }
  function videoPlayer(item){
    const box=el('section');box.append(el('h3','',person(item.user).name));const v=el('video');v.controls=true;v.preload='metadata';v.src=localURL(item.video);if(item.poster)v.poster=localURL(item.poster);
    if(item.cues?.length){const captions=v.addTextTrack('subtitles','日本語','ja');for(const cue of item.cues)captions.addCue(new VTTCue(...cue));captions.mode='hidden';box.append(button('字幕',()=>{captions.mode=captions.mode==='showing'?'hidden':'showing';},'pill'));}
    else if(item.subtitles){const track=el('track');track.kind='subtitles';track.srclang='ja';track.label='日本語';track.src=localURL(item.subtitles);v.append(track);}
    box.append(v,el('p','',item.question||''));if(item.video){const a=el('a','','動画ファイルを開く');a.href=localURL(item.video);a.target='_blank';a.rel='noopener';box.append(a);}showModal(box);
  }
  function loadRoom(room){
    if(loaded[room.id])return Promise.resolve(loaded[room.id]);if(pending[room.id])return pending[room.id];
    pending[room.id]=new Promise((resolve,reject)=>{const s=document.createElement('script');s.src=localURL(room.data);
      s.onload=()=>{delete pending[room.id];loaded[room.id]?resolve(loaded[room.id]):reject(new Error('データを読み込めません'));};
      s.onerror=()=>{delete pending[room.id];s.remove();reject(new Error('データが見つかりません'));};document.head.append(s);
    });return pending[room.id];
  }
  window.RepArchive=window.RepliveArchive={registerRoom(id,messages){loaded[id]=messages;}};
  function countFor(uid){
    if(view==='answers')return archive.answers.filter(a=>!uid||a.user===uid).length+' リプライ';
    if(view==='people')return archive.rooms.filter(r=>!uid||r.user===uid).reduce((n,r)=>n+r.count,0)+' メッセージ';
    let count=archive.answers.filter(a=>(!uid||a.user===uid)&&saved.has('answer:'+a.id)).length;
    for(const r of archive.rooms.filter(r=>!uid||r.user===uid))count+=(loaded[r.id]||[]).filter(m=>saved.has('message:'+r.id+':'+m.id)).length;return count+' 件';
  }
  function renderSidebar(){
    document.querySelector('#side-title').textContent={chats:'CHATS',answers:'リプライ',people:'推し',saved:'保存'}[view];
    document.querySelector('.fandom-tab').textContent={chats:'Fandom Chat',answers:'リプライ・カード',people:'プロフィール',saved:'お気に入り'}[view];
    const target=document.querySelector('#room-list');target.replaceChildren();const filter=document.querySelector('#room-search').value.toLowerCase();
    if(view==='chats'){
      for(const r of archive.rooms.filter(r=>r.name.toLowerCase().includes(filter))){const b=button('',()=>selectRoom(r),'room');b.classList.toggle('active',r.id===activeRoom?.id);b.setAttribute('aria-pressed',String(r.id===activeRoom?.id));const copy=el('div','room-copy');copy.append(el('strong','',r.name),el('small','',r.latest?.text||(r.latest?.image?'写真':r.latest?.video?'動画':'メッセージ')));b.append(avatar(r.avatar),copy,el('time','',r.latest?.date.slice(5,10).replace('-','/')||''));target.append(b);}
    }else{
      const all=button('',()=>selectPerson(''),'room all-people');all.classList.toggle('active',!filters[view]);all.setAttribute('aria-pressed',String(!filters[view]));all.append(el('div','avatar','♧'),el('strong','','すべての推し'),el('span','count',countFor('')));target.append(all);
      for(const p of archive.people.filter(p=>p.name.toLowerCase().includes(filter))){const b=button('',()=>selectPerson(p.id),'room');b.classList.toggle('active',p.id===filters[view]);b.setAttribute('aria-pressed',String(p.id===filters[view]));const copy=el('div','room-copy');copy.append(el('strong','',p.name),el('small','',countFor(p.id)));b.append(avatar(p.avatar),copy);target.append(b);}
    }if(!target.children.length)target.append(el('p','person-empty','該当する推しはありません'));
  }
  document.querySelector('#room-search').oninput=renderSidebar;
  function renderView(){if(view==='answers')renderAnswers();else if(view==='people')renderPeople();else if(view==='saved')renderSaved();else if(activeRoom)renderChat();else main.replaceChildren(el('div','empty','チャットを選択してください'));}
  function selectPerson(uid){filters[view]=uid;renderSidebar();renderView();}
  function navigation(next,uid){
    stopPlayback();
    view=next;if(uid!==undefined&&next!=='chats')filters[next]=uid;document.querySelector('#room-search').value='';app.classList.remove('mobile-detail');app.classList.toggle('mobile-other',next!=='chats');
    document.querySelectorAll('#navigation button').forEach(b=>{b.classList.toggle('active',b.dataset.view===next);b.setAttribute('aria-current',b.dataset.view===next?'page':'false');});renderSidebar();renderView();
  }
  document.querySelectorAll('#navigation button').forEach(b=>b.onclick=()=>navigation(b.dataset.view));
  async function selectRoom(room){
    activeRoom=room;query='';date='';mode='messages';navigation('chats');app.classList.add('mobile-detail');main.replaceChildren(el('div','empty','読み込み中…'));
    try{await loadRoom(room);if(activeRoom!==room||view!=='chats')return;renderChat();}catch(e){if(activeRoom===room&&view==='chats')main.replaceChildren(el('div','empty',e.message));}
  }
  function renderChat(){
    if(!activeRoom||!loaded[activeRoom.id])return;stopPlayback();const room=activeRoom;main.replaceChildren();const header=el('header','chat-header');header.append(button('‹',()=>app.classList.remove('mobile-detail'),'back'),avatar(room.avatar));
    const title=el('div');title.append(el('strong','',room.name),el('small','','Fandom Chat'));header.append(title);const tools=el('div','tools');tools.append(button(mode==='media'?'チャット':'写真・動画',()=>{mode=mode==='media'?'messages':'media';renderChat();},'pill'));header.append(tools);main.append(header);
    const toolbar=el('div','toolbar'),search=el('input');search.type='search';search.placeholder='メッセージを検索';search.setAttribute('aria-label','メッセージを検索');search.value=query;search.oninput=()=>{query=search.value;renderMessages();};
    const calendar=el('input');calendar.type='date';calendar.value=date;calendar.setAttribute('aria-label','日付へ移動');calendar.onchange=()=>{date=calendar.value;renderMessages();};toolbar.append(search,calendar);main.append(toolbar,el('div','conversation'));renderMessages(true);
  }
  function filtered(){let list=loaded[activeRoom.id]||[];if(query)list=list.filter(m=>(m.text+' '+m.question).toLowerCase().includes(query.toLowerCase()));if(date)list=list.filter(m=>m.date.slice(0,10)===date);if(mode==='media')list=list.filter(m=>m.image||m.video);return list;}
  function messageNode(m,r){
    const outgoing=m.outgoing===true,sender=m.sender||(outgoing?'あなた':r.name);
    const row=el('article',outgoing?'message-row outgoing':'message-row');row.dataset.message=m.id;row.setAttribute('aria-label',sender+'のメッセージ');if(!outgoing)row.append(avatar(m.senderAvatar===undefined?r.avatar:m.senderAvatar));const bubble=el('div','bubble');if(!outgoing&&sender!==r.name)bubble.append(el('div','message-sender',sender));if(m.question)bubble.append(el('p','question',m.question));if(m.text)bubble.append(el('p','',m.text));
    if(m.image){bubble.classList.add('media');const i=image(m.image,'','写真');i.onclick=()=>showModal(image(m.image,'','写真'));bubble.append(i);}
    if(m.video){bubble.classList.add('media');const v=el('video');v.controls=true;v.preload='none';v.src=localURL(m.video);if(m.poster)v.poster=localURL(m.poster);bubble.append(v);}
    if(!m.text&&!m.image&&!m.video)bubble.append(el('p','',m.deleted?'削除されたメッセージ':'添付ファイル未保存'));
    const meta=el('div','message-meta');meta.append(el('time','',m.date.slice(11,16)),favorite('message:'+r.id+':'+m.id));if(outgoing)row.append(meta,bubble);else row.append(bubble,meta);return row;
  }
  function renderMessages(latest=false){
    const target=main.querySelector('.conversation');if(!target)return;stopPlayback();target.replaceChildren();const list=filtered();target.classList.toggle('media-grid',mode==='media');target.onscroll=null;
    if(!list.length){target.append(el('div','empty','該当するメッセージはありません'));return;}
    if(mode==='media'){const grid=el('div','grid');for(const m of list){const b=button('',()=>m.video?videoPlayer({...m,user:activeRoom.user}):showModal(image(m.image,'','写真')),'video-card');if(m.image||m.poster)b.append(image(m.image||m.poster,'video-cover','写真・動画'));b.append(el('div','card-copy',m.date.slice(0,10)+(m.video?' · 動画':' · 写真')));grid.append(b);}target.append(grid);return;}
    // The entire room is already local. Render its history once; images remain lazy.
    const fragment=document.createDocumentFragment();let last='';for(const m of list){const d=m.date.slice(0,10);if(d!==last){fragment.append(el('div','date-divider',d.replaceAll('-',' / ')));last=d;}fragment.append(messageNode(m,activeRoom));}target.append(fragment);
    let followLatest=latest&&!query&&!date;const pin=()=>{if(followLatest&&target.isConnected)target.scrollTop=target.scrollHeight;};
    target.onscroll=()=>{followLatest=target.scrollHeight-target.scrollTop-target.clientHeight<50;};target.querySelectorAll('img').forEach(i=>i.addEventListener('load',pin,{once:true}));if(followLatest){pin();requestAnimationFrame(pin);}else target.scrollTop=0;
  }
  function viewFrame(title,description=''){stopPlayback();main.replaceChildren();const h=el('header','view-header');h.append(el('h2','',title),el('p','',description));const scroll=el('div','view-scroll');main.append(h,scroll);return scroll;}
  function personSelect(cls='mobile-filter'){const select=el('select',cls);select.setAttribute('aria-label','推しで絞り込む');const all=el('option','','すべての推し');all.value='';select.append(all);for(const p of archive.people){const o=el('option','',p.name);o.value=p.id;select.append(o);}select.value=filters[view];select.onchange=()=>selectPerson(select.value);return select;}
  function answerCard(a){const card=el('article','video-card'),cover=button('',()=>a.video?videoPlayer(a):showModal(el('p','','動画ファイル未保存')));cover.style.cssText='display:block;width:100%;padding:0';cover.setAttribute('aria-label',person(a.user).name+'のリプライを再生');if(a.poster)cover.append(image(a.poster,'video-cover','リプライ'));const copy=el('div','card-copy');copy.append(el('strong','',person(a.user).name),el('p','',a.question),el('small','',a.date.slice(0,10)+' · '+Math.floor(a.duration/60)+':'+String(Math.floor(a.duration%60)).padStart(2,'0')),favorite('answer:'+a.id));card.append(cover,copy);return card;}
  let answerQuery='',cardsMode=false;
  function renderAnswers(){
    const scroll=viewFrame('リプライ',countFor(filters.answers)),toolbar=el('div','toolbar'),search=el('input'),grid=el('div','grid');search.type='search';search.placeholder='質問を検索';search.setAttribute('aria-label','質問を検索');search.value=answerQuery;
    const render=()=>{grid.replaceChildren();const match=item=>(!filters.answers||item.user===filters.answers);
      if(cardsMode){for(const c of archive.cards.filter(c=>match(c)&&c.text.toLowerCase().includes(answerQuery.toLowerCase()))){const box=el('article','video-card'),copy=el('div','card-copy');copy.append(el('strong','',person(c.user).name),el('p','',c.text),el('small','',c.date.slice(0,10)));const a=archive.answers.find(a=>a.card===c.id);if(a?.video)copy.append(button('回答を見る',()=>videoPlayer(a),'pill'));box.append(copy);grid.append(box);}}
      else grid.append(...archive.answers.filter(a=>match(a)&&a.question.toLowerCase().includes(answerQuery.toLowerCase())).map(answerCard));if(!grid.children.length)grid.append(el('p','empty',cardsMode?'カードはありません':'リプライはありません'));
    };
    search.oninput=()=>{answerQuery=search.value;render();};const toggle=button(cardsMode?'リプライ':'カード',()=>{cardsMode=!cardsMode;toggle.textContent=cardsMode?'リプライ':'カード';render();},'pill');toolbar.append(personSelect(),search,toggle);scroll.append(toolbar,grid);render();
  }
  function renderPeople(){
    const scroll=viewFrame('推し'),toolbar=el('div','toolbar people-toolbar');toolbar.append(personSelect());scroll.append(toolbar);const grid=el('div','grid profile-grid');
    for(const p of archive.people.filter(p=>!filters.people||p.id===filters.people)){const c=el('section','profile-card');c.append(p.background?image(p.background,'profile-bg',p.name+'の背景'):el('div','profile-bg profile-bg-placeholder'),avatar(p.avatar),el('h3','',p.name),el('p','','@'+(p.unique||'')));const actions=el('div','profile-actions'),r=archive.rooms.find(r=>r.user===p.id);if(r)actions.append(button('CHATS',()=>selectRoom(r),'pill'));actions.append(button('リプライ',()=>navigation('answers',p.id),'pill'));c.append(actions);grid.append(c);}scroll.append(grid);
  }
  async function renderSaved(){
    const current=filters.saved,scroll=viewFrame('保存'),toolbar=el('div','toolbar people-toolbar');toolbar.append(personSelect());scroll.append(toolbar);const content=el('div');content.append(el('div','empty','読み込み中…'));scroll.append(content);
    try{await Promise.all(archive.rooms.map(loadRoom));if(view!=='saved'||filters.saved!==current||!scroll.isConnected)return;renderSidebar();content.replaceChildren();const grid=el('div','grid');grid.append(...archive.answers.filter(a=>(!current||a.user===current)&&saved.has('answer:'+a.id)).map(answerCard));if(grid.children.length)content.append(grid);
      let count=grid.children.length;for(const r of archive.rooms.filter(r=>!current||r.user===current)){const messages=loaded[r.id].filter(m=>saved.has('message:'+r.id+':'+m.id));if(!messages.length)continue;const box=el('div','conversation');box.style.overflow='visible';box.append(el('h3','',r.name));box.append(...messages.map(m=>messageNode(m,r)));content.append(box);count+=messages.length;}if(!count)content.append(el('div','empty','お気に入りはありません'));
    }catch(e){if(scroll.isConnected)content.replaceChildren(el('div','empty',e.message));}
  }
  document.querySelector('#about').onclick=()=>{const box=el('section');box.append(el('h2','','RepArchive'),el('p','',archive.rooms.length+' CHATS · '+archive.rooms.reduce((s,r)=>s+r.count,0)+' メッセージ · '+archive.answers.length+' リプライ'),el('p','',archive.created.slice(0,10)+' に生成'));const a=el('a','','エクスポートレポート');a.href=localURL('导出报告.json');box.append(a);showModal(box);};
  navigation('chats');if(archive.rooms.length&&matchMedia('(min-width:701px)').matches)selectRoom(archive.rooms[0]);
})();
