(() => {
  const form=document.getElementById('chatForm');
  const input=document.getElementById('chatInput');
  const thread=document.getElementById('chatThread');
  if(!form||!input||!thread)return;
  const cue=/(?:وريني|ورني|اعرضلي|فرجني|show me).*(?:صور|صورة|photos?|pictures?|images?)/i;
  form.addEventListener('submit',async()=>{
    const text=input.value.trim();
    if(!cue.test(text))return;
    try{
      const r=await fetch('/api/images?query='+encodeURIComponent(text),{credentials:'same-origin',cache:'no-store'});
      if(!r.ok)return;
      const data=await r.json();
      if(!data.items||!data.items.length)return;
      const box=document.createElement('div');box.className='chat-image-grid';
      for(const item of data.items.slice(0,8)){
        const a=document.createElement('a');a.href=item.url;a.target='_blank';a.rel='noopener noreferrer';
        const img=document.createElement('img');img.src=item.thumbnail;img.alt=item.title||data.query;img.loading='lazy';
        a.appendChild(img);box.appendChild(a);
      }
      thread.appendChild(box);
      const conversation=document.getElementById('conversation');if(conversation)conversation.scrollTop=conversation.scrollHeight;
    }catch(_){ }
  });
})();
