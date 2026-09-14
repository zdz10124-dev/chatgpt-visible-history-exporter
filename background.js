// ChatGPT 本地聊天数据库同步器 - MV3 service worker
// 所有认证都由 Edge 自身处理；不保存 Cookie / accessToken。

const LOCAL = 'http://127.0.0.1:17891';
let syncing = false;
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function getToken() {
  const r = await fetch('https://chatgpt.com/api/auth/session', {credentials:'include'});
  if (!r.ok) throw new Error('无法读取 ChatGPT 登录会话：HTTP ' + r.status);
  const j = await r.json();
  const t = j.accessToken || j.access_token;
  if (!t) throw new Error('未取得 accessToken，请确认 Edge 中已登录 ChatGPT。');
  return t;
}

function api(token) {
  return async function get(path, retry=0) {
    const r = await fetch('https://chatgpt.com' + path, {
      credentials:'include',
      headers:{accept:'application/json', authorization:'Bearer ' + token}
    });
    if ((r.status===429 || r.status>=500) && retry<5) {
      await sleep((retry+1)*1000);
      return get(path,retry+1);
    }
    if (!r.ok) throw new Error(path + ' HTTP ' + r.status + ' ' + (await r.text()).slice(0,300));
    return r.json();
  };
}

async function listRegular(get, archived) {
  const out=[]; let offset=0; const limit=100;
  while (true) {
    const q=new URLSearchParams({offset:String(offset),limit:String(limit),order:'updated',is_archived:String(archived)});
    const j=await get('/backend-api/conversations?' + q);
    const items=Array.isArray(j.items)?j.items:[];
    out.push(...items); offset+=items.length;
    if (!items.length || items.length<limit || (Number.isFinite(j.total)&&offset>=j.total)) break;
  }
  return out;
}

function collectProjects(obj,out=new Map(),depth=0) {
  if (!obj || depth>12) return out;
  if (Array.isArray(obj)) { obj.forEach(x=>collectProjects(x,out,depth+1)); return out; }
  if (typeof obj!=='object') return out;
  const id=obj.id||obj.gizmo_id||obj.gizmoId;
  if (typeof id==='string' && id.startsWith('g-p-')) out.set(id,{id,title:obj.title||obj.name||obj.display_name||'未命名项目'});
  Object.values(obj).forEach(x=>collectProjects(x,out,depth+1));
  return out;
}

async function projectSummaries(get) {
  let sidebar;
  try { sidebar=await get('/backend-api/gizmos/snorlax/sidebar'); } catch { return []; }
  const projects=[...collectProjects(sidebar).values()], out=[];
  for (const p of projects) {
    let cursor='0', guard=0;
    while (cursor!=null && guard++<10000) {
      const j=await get(`/backend-api/gizmos/${encodeURIComponent(p.id)}/conversations?cursor=${encodeURIComponent(cursor)}`);
      const items=Array.isArray(j.items)?j.items:[];
      items.forEach(x=>out.push({...x,__project:p}));
      cursor=j.cursor??null;
      if (!items.length && cursor==null) break;
    }
  }
  return out;
}

function dedupe(items) {
  const m=new Map();
  for (const x of items) {
    const id=x && (x.id||x.conversation_id); if (!id) continue;
    const old=m.get(id); m.set(id, old ? {...old,...x,__project:x.__project||old.__project} : x);
  }
  return [...m.values()];
}

function branch(conv) {
  const map=conv&&conv.mapping; if (!map || typeof map!=='object') return [];
  let id=conv.current_node;
  if (!id || !map[id]) {
    const parents=new Set(Object.values(map).map(n=>n&&n.parent).filter(Boolean));
    const leaves=Object.entries(map).filter(([k])=>!parents.has(k));
    leaves.sort((a,b)=>(b[1]?.message?.create_time||0)-(a[1]?.message?.create_time||0));
    id=leaves[0]?.[0];
  }
  const chain=[], seen=new Set();
  while (id && map[id] && !seen.has(id)) { seen.add(id); chain.push(map[id]); id=map[id].parent; }
  return chain.reverse();
}

function textOf(m) {
  const c=m&&m.content; if (!c) return '';
  const hidden=new Set(['thoughts','reasoning','reasoning_recap','model_editable_context','system_error','computer_output']);
  if (hidden.has(String(c.content_type||''))) return '';
  if (Array.isArray(c.parts)) return c.parts.map(p=>typeof p==='string'?p:(p&&typeof p.text==='string'?p.text:'')).filter(Boolean).join('\n').trim();
  if (typeof c.text==='string') return c.text.trim();
  if (typeof c.result==='string') return c.result.trim();
  return '';
}

function visible(m) {
  if (!m || !m.author) return false;
  const role=m.author.role; if (role!=='user' && role!=='assistant') return false;
  const md=m.metadata||{};
  if (md.is_visually_hidden_from_conversation===true || md.is_hidden===true || md.hidden===true) return false;
  if (role==='assistant') {
    if (m.recipient && m.recipient!=='all') return false;
    if (md.is_tool_message===true || md.is_reasoning===true) return false;
  }
  return !!textOf(m);
}

function parseConversation(conv,s) {
  const messages=[];
  branch(conv).forEach(node=>{
    const m=node&&node.message;
    if (!visible(m)) return;
    messages.push({id:m.id,role:m.author.role,time:m.create_time??null,text:textOf(m)});
  });
  return {
    id:conv.conversation_id||conv.id||s.id,
    title:conv.title||s.title||'未命名会话',
    create_time:conv.create_time??s.create_time??null,
    update_time:conv.update_time??s.update_time??null,
    archived:!!s.is_archived,
    project:s.__project?{id:s.__project.id,title:s.__project.title}:null,
    messages
  };
}

async function postLocal(path, body) {
  const r = await fetch(LOCAL + path, {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body||{})
  });
  const text=await r.text();
  let j; try { j=JSON.parse(text); } catch { j={ok:false,error:text}; }
  if (!r.ok || !j.ok) throw new Error(j.error || ('local HTTP ' + r.status));
  return j;
}

async function syncAll() {
  const token=await getToken(), get=api(token);
  const [normal,archived,projects]=await Promise.all([
    listRegular(get,false), listRegular(get,true), projectSummaries(get)
  ]);
  const summaries=dedupe([...normal,...archived,...projects]);
  let ok=0, failed=0, totalMessages=0, batch=[];
  for (const s of summaries) {
    const id=s.id||s.conversation_id;
    try {
      const conv=await get('/backend-api/conversation/'+encodeURIComponent(id));
      const parsed=parseConversation(conv,s);
      if (parsed.messages.length) {
        totalMessages += parsed.messages.length;
        batch.push(parsed);
      }
      if (batch.length>=20) {
        const r=await postLocal('/api/sync/batch',{conversations:batch});
        ok+=r.conversations||0; batch=[];
      }
    } catch (e) {
      failed++; console.warn('[ChatDB] conversation failed',id,e);
    }
  }
  if (batch.length) {
    const r=await postLocal('/api/sync/batch',{conversations:batch});
    ok+=r.conversations||0;
  }
  return {ok:failed===0, discovered:summaries.length, synced_conversations:ok, synced_messages:totalMessages, failed};
}

async function checkAiRequest() {
  if (syncing) return;
  try {
    const r=await fetch(LOCAL + '/api/sync/poll',{cache:'no-store'});
    if (!r.ok) return;
    const j=await r.json();
    if (!j.requested) return;
    syncing=true;
    try {
      const result=await syncAll();
      await postLocal('/api/sync/complete', result);
    } catch (e) {
      console.error('[ChatDB AI Sync]',e);
      try { await postLocal('/api/sync/complete',{ok:false,error:e?.stack||e?.message||String(e)}); } catch {}
    } finally { syncing=false; }
  } catch {}
}

chrome.runtime.onMessage.addListener((msg,sender,sendResponse)=>{
  if (!msg) return;
  if (msg.type==='CHATDB_WAKE') {
    checkAiRequest().then(()=>sendResponse({ok:true})).catch(e=>sendResponse({ok:false,error:String(e)}));
    return true;
  }
  if (msg.type==='CHATDB_MANUAL_SYNC') {
    if (syncing) { sendResponse({ok:false,error:'sync already running'}); return; }
    syncing=true;
    syncAll().then(r=>sendResponse(r)).catch(e=>sendResponse({ok:false,error:e?.stack||e?.message||String(e)})).finally(()=>{syncing=false;});
    return true;
  }
});

chrome.runtime.onInstalled.addListener(()=>checkAiRequest());
chrome.runtime.onStartup.addListener(()=>checkAiRequest());
