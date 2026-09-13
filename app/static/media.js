(() => {
  const form=document.getElementById('chatForm');
  const input=document.getElementById('chatInput');
  const thread=document.getElementById('chatThread');
  if(!form||!input||!thread)return;
  const cue=/(?:(?:وريني|ورني|اعرضلي|فرجني|show me).*(?:صور|صورة|صوره|photos?|pictures?|images?)|(?:عايز|عاوز|محتاج|عايزه|عاوزه|محتاجه).*(?:صور|صورة|صوره)|^(?:صور|صورة|صوره)\b)/i;

  function scrollDown(){
    const conversation=document.getElementById('conversation');
    if(conversation)requestAnimationFrame(()=>conversation.scrollTo({top:conversation.scrollHeight,behavior:'smooth'}));
  }

  function addStatus(text){
    const row=document.createElement('div');row.className='chat-message assistant entering';
    const avatar=document.createElement('div');avatar.className='assistant-avatar';avatar.textContent='م';
    const bubble=document.createElement('div');bubble.className='bubble';bubble.dir='rtl';bubble.textContent=text;
    row.append(avatar,bubble);thread.append(row);scrollDown();
  }

  async function renderImages(text){
    try{
      const r=await fetch('/api/images?query='+encodeURIComponent(text),{credentials:'same-origin',cache:'no-store'});
      if(!r.ok)throw new Error(`HTTP ${r.status}`);
      const data=await r.json();
      if(!data.items||!data.items.length){
        addStatus('ملقتش صور مناسبة من المصادر المتاحة دلوقتي. جرّب اكتب اسم الموديل بشكل أدق.');
        return;
      }
      const box=document.createElement('div');box.className='chat-image-grid';box.dir='rtl';
      for(const item of data.items.slice(0,8)){
        const a=document.createElement('a');a.href=item.url;a.target='_blank';a.rel='noopener noreferrer';a.title=item.title||data.query;
        const img=document.createElement('img');img.src=item.thumbnail;img.alt=item.title||data.query;img.loading='lazy';img.decoding='async';
        img.addEventListener('error',()=>a.remove(),{once:true});
        a.appendChild(img);box.appendChild(a);
      }
      thread.appendChild(box);scrollDown();
    }catch(_){
      addStatus('بحث الصور اتعطل مؤقتًا. المحادثة نفسها شغالة عادي.');
    }
  }

  // Capture phase is intentional: the main chat submit handler clears the
  // textarea immediately. We must copy the text before that happens.
  form.addEventListener('submit',()=>{
    const text=input.value.trim();
    if(!cue.test(text))return;
    void renderImages(text);
  },true);
})();
