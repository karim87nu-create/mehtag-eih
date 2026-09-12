/* Execution evidence is rendered inside the existing conversation, without changing its core. */
(() => {
  const shown = new Set(); let running = false;
  async function poll() {
    if (running || document.hidden) return;
    let id;
    try { id=localStorage.getItem('maak_conversation_thread_v1'); } catch (_) { return; }
    if (!id) return;
    const thread=document.getElementById('chatThread'); if (!thread) return;
    running=true;
    try {
      const response=await fetch(`/api/execution/conversations/${encodeURIComponent(id)}`,{cache:'no-store',credentials:'same-origin'});
      if (!response.ok) return;
      const data=await response.json();
      if (localStorage.getItem('maak_conversation_thread_v1')!==id) return;
      for (const notice of data.notices) {
        const key=`${id}:${notice.id}`;
        if (shown.has(key)) continue;
        const existing=[...thread.querySelectorAll('.bubble')].some(n=>n.textContent===notice.content);
        if (!existing) {
          const row=document.createElement('div'); row.className='chat-message assistant';
          const avatar=document.createElement('div');avatar.className='assistant-avatar';avatar.textContent='م';
          const bubble=document.createElement('div');bubble.className='bubble';bubble.dir='auto';bubble.textContent=notice.content;
          row.append(avatar,bubble);thread.append(row);
        }
        shown.add(key);
      }
      let panel=document.getElementById('executionEvidence');
      if (!panel) {panel=document.createElement('section');panel.id='executionEvidence';panel.dir='rtl';thread.append(panel);}
      panel.replaceChildren();
      for (const request of data.requests) {
        const box=document.createElement('div');box.className='chat-context-card';
        const heading=document.createElement('b');heading.textContent=request.card.label;box.append(heading);
        for (const lead of request.leads) {
          const line=document.createElement('p');
          const labels={SENT:'القناة أكدت استلام الطلب',RESPONDED:'وصل رد',PENDING:'في انتظار الإرسال',SKIPPED:'تم العثور عليها؛ لم يتم التواصل',UNCERTAIN:'الإرسال غير مؤكد',FAILED:'الإرسال غير مؤكد',BLOCKED:'الإرسال متوقف'};
          line.textContent=`${lead.name} — ${labels[lead.status]||'لم يتأكد التواصل'}`;box.append(line);
          if (lead.source && lead.source.startsWith('https://www.openstreetmap.org/')) {
            const source=document.createElement('a');source.href=lead.source;source.target='_blank';source.rel='noopener noreferrer';source.textContent='مصدر بيانات الجهة';box.append(source);
          }
        }
        panel.append(box);
      }
      if (data.requests.length) {const credit=document.createElement('small');credit.textContent=data.attribution;panel.append(credit);}
    } catch (_) { /* Keep previously confirmed evidence during connection failures. */ }
    finally {running=false;}
  }
  setInterval(poll,4000);poll();document.addEventListener('visibilitychange',poll);
})();
