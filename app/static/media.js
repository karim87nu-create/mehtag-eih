(() => {
  const form=document.getElementById('chatForm');
  const input=document.getElementById('chatInput');
  const thread=document.getElementById('chatThread');
  if(!form||!input||!thread)return;
  const cue=/(?:(?:وريني|ورني|اعرضلي|فرجني|show me).*(?:صور|صورة|صوره|photos?|pictures?|images?)|(?:عايز|عاوز|محتاج|عايزه|عاوزه|محتاجه).*(?:صور|صورة|صوره)|^(?:صور|صورة|صوره)\b)/i;
  const followupCue=/^(?:ايوه\s*)?(?:فين|وريني|هات(?:ها|هم)?|اعرض(?:ها|هم)?)\s*[؟?!.]*$/i;
  let lastImageQuery='';

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
    lastImageQuery=text;
    const pending=document.createElement('div');pending.className='chat-message assistant entering media-pending';
    const pendingBubble=document.createElement('div');pendingBubble.className='bubble';pendingBubble.dir='rtl';pendingBubble.textContent='بدور على صور متاحة فعلًا…';
    pending.appendChild(pendingBubble);thread.appendChild(pending);scrollDown();
    try{
      const r=await fetch('/api/images?query='+encodeURIComponent(text),{credentials:'same-origin',cache:'no-store'});
      if(!r.ok)throw new Error(`HTTP ${r.status}`);
      const data=await r.json();
      pending.remove();
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
    }catch(_){pending.remove();addStatus('بحث الصور اتعطل مؤقتًا، ومش هقول إن الصور جاهزة وهي مش ظاهرة.')}
  }

  function sendChoice(value){
    input.value=value;
    input.dispatchEvent(new Event('input',{bubbles:true}));
    form.requestSubmit();
  }

  function choiceBar(labels){
    const bar=document.createElement('div');bar.className='maak-choice-bar';bar.dir='rtl';
    Object.assign(bar.style,{display:'flex',gap:'8px',flexWrap:'wrap',margin:'8px 56px 14px 8px'});
    labels.forEach(label=>{
      const b=document.createElement('button');b.type='button';b.textContent=label;
      Object.assign(b.style,{border:'1px solid rgba(20,20,20,.16)',background:'#fff',borderRadius:'999px',padding:'10px 16px',font:'inherit',cursor:'pointer'});
      if(label==='ابدأ'){b.style.background='#111';b.style.color='#fff'}
      b.onclick=()=>{bar.remove();sendChoice(label)};bar.appendChild(b);
    });
    thread.appendChild(bar);scrollDown();
  }

  function maybeAddChoices(node){
    if(!(node instanceof HTMLElement)||!node.classList.contains('chat-message')||!node.classList.contains('assistant'))return;
    const text=(node.querySelector('.bubble')?.textContent||'').trim();
    if(!text)return;
    const previous=thread.querySelector('.maak-choice-bar');if(previous)previous.remove();
    if(/جديد\s+ولا\s+مستعمل/.test(text))choiceBar(['جديد','مستعمل']);
    else if(/عندي الأساسيات|التفاصيل دي مناسبة|نبدأ/.test(text))choiceBar(['ابدأ','تعديل']);
  }

  const observer=new MutationObserver(records=>records.forEach(r=>r.addedNodes.forEach(maybeAddChoices)));
  observer.observe(thread,{childList:true});

  // Capture phase: the main handler clears the textarea immediately.
  form.addEventListener('submit',()=>{
    const text=input.value.trim();
    const choices=thread.querySelector('.maak-choice-bar');if(choices)choices.remove();
    if(cue.test(text))void renderImages(text);
    else if(lastImageQuery&&followupCue.test(text))void renderImages(lastImageQuery);
  },true);
})();
