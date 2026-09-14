(() => {
  if (window.__CHATGPT_LOCAL_DB_SYNC_V4__) return;
  window.__CHATGPT_LOCAL_DB_SYNC_V4__ = true;

  function wake() {
    try { chrome.runtime.sendMessage({type:'CHATDB_WAKE'}, () => void chrome.runtime.lastError); } catch {}
  }

  function addUi() {
    if (document.getElementById('chatgpt-local-db-sync-btn')) return;
    const btn=document.createElement('button');
    btn.id='chatgpt-local-db-sync-btn';
    btn.textContent='同步聊天库';
    Object.assign(btn.style,{
      position:'fixed',right:'18px',bottom:'18px',zIndex:'2147483647',padding:'10px 14px',
      borderRadius:'10px',border:'1px solid #666',background:'#111827',color:'#fff',cursor:'pointer',fontSize:'14px'
    });
    document.body.appendChild(btn);
    btn.onclick=()=>{
      if (btn.disabled) return;
      btn.disabled=true; btn.textContent='同步中…';
      chrome.runtime.sendMessage({type:'CHATDB_MANUAL_SYNC'},r=>{
        const err=chrome.runtime.lastError;
        if (err || !r?.ok) btn.textContent='同步失败';
        else btn.textContent=`完成 ${r.synced_conversations||0}`;
        setTimeout(()=>{btn.disabled=false;btn.textContent='同步聊天库';},2500);
      });
    };
  }

  addUi();
  wake();
  setInterval(wake,2000);
})();
