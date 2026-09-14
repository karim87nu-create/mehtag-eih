(() => {
  const form=document.getElementById('chatForm');
  const input=document.getElementById('chatInput');
  const thread=document.getElementById('chatThread');
  if(!form||!input||!thread)return;

  const uiFix=document.createElement('style');
  uiFix.textContent=`
    .chat-shell{max-width:100vw!important;overflow-x:clip!important}
    .conversation-hero,.chat-thread{min-width:0!important;width:100%!important;max-width:100%!important}
    .chat-thread{gap:4px!important}
    .chat-message{width:100%!important;min-width:0!important;max-width:100%!important;direction:ltr!important;margin:10px 0!important;padding:0!important}
    .chat-message.user{justify-content:flex-end!important;padding:0!important}
    .chat-message.assistant{justify-content:flex-start!important;padding:0!important}
    .chat-message .bubble{min-width:0!important;max-width:min(82%,560px)!important;overflow-wrap:anywhere!important;word-break:break-word!important;white-space:pre-wrap!important}
    .chat-context-card,.chat-image-grid,.maak-choice-bar{max-width:100%!important;margin-inline:0!important}
    body.has-conversation .home-cases,body.has-conversation .quiet-card{display:none!important}
    .reminder-alert .bubble{border:1px solid rgba(239,199,132,.22)!important;border-radius:18px!important;padding:11px 14px!important;background:rgba(230,185,109,.08)!important}
    @media(max-width:600px){
      .chat-shell{width:100%!important;max-width:100vw!important;padding-left:12px!important;padding-right:12px!important}
      .chat-message .bubble{max-width:90%!important}
      .conversation-composer{max-width:calc(100vw - 18px)!important}
    }`;
  document.head.appendChild(uiFix);

  const cue=/(?:(?:وريني|ورني|اعرضلي|فرجني|show me).*(?:صور|صورة|صوره|photos?|pictures?|images?)|(?:عايز|عاوز|محتاج|عايزه|عاوزه|محتاجه).*(?:صور|صورة|صوره)|^(?:صور|صورة|صوره)\b)/i;
  const followupCue=/^(?:ايوه\s*)?(?:فين|وريني|هات(?:ها|هم)?|اعرض(?:ها|هم)?)\s*[؟?!.]*$/i;
  const reminderCue=/(?:فكرني|فكّرني|ذكرني|ذكّرني|تذكير|remind\s+me)/i;
  const reminderTimeCue=/(?:\d|[٠-٩]|الساعة|الساعه|بكرة|بكره|غدا|غداً|بعد\s|الصبح|صباح|مساء|بالليل|ليل|\bam\b|\bpm\b)/i;
  const reminderCancel=/^(?:خلاص\s+)?(?:بلاش|الغي|إلغي|الغيه|متفكرنيش|ما تفكرنيش)\s*[.!؟?]*$/i;
  const THREAD_KEY='maak_conversation_thread_v1';
  const PENDING_REMINDER_KEY='maak_pending_reminder_v1';
  let lastImageQuery='';
  let reminderBusy=false;

  function storedGet(key){try{return localStorage.getItem(key)}catch(_){return null}}
  function storedSet(key,value){try{localStorage.setItem(key,value)}catch(_){}}
  function storedRemove(key){try{localStorage.removeItem(key)}catch(_){}}

  function scrollDown(){
    const conversation=document.getElementById('conversation');
    if(conversation)requestAnimationFrame(()=>conversation.scrollTo({top:conversation.scrollHeight,behavior:'smooth'}));
    else requestAnimationFrame(()=>window.scrollTo({top:document.body.scrollHeight,behavior:'smooth'}));
  }

  function addStatus(text,extraClass=''){
    const row=document.createElement('div');
    row.className='chat-message assistant entering'+(extraClass?' '+extraClass:'');
    const bubble=document.createElement('div');
    bubble.className='bubble';bubble.dir='rtl';bubble.textContent=text;
    row.appendChild(bubble);thread.appendChild(row);scrollDown();
    return row;
  }

  function addUser(text){
    const empty=document.getElementById('emptyThread');
    if(empty)empty.classList.add('hidden');
    document.body.classList.add('has-conversation');
    const row=document.createElement('div');row.className='chat-message user entering';
    const bubble=document.createElement('div');bubble.className='bubble';bubble.dir='rtl';bubble.textContent=text;
    row.appendChild(bubble);thread.appendChild(row);scrollDown();
    return row;
  }

  async function renderImages(text){
    lastImageQuery=text;
    const pending=addStatus('بدور على صور متاحة فعلًا…','media-pending');
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
    }catch(_){
      pending.remove();
      addStatus('بحث الصور اتعطل مؤقتًا، ومش هقول إن الصور جاهزة وهي مش ظاهرة.');
    }
  }

  function sendChoice(value){
    input.value=value;
    input.dispatchEvent(new Event('input',{bubbles:true}));
    form.requestSubmit();
  }

  function choiceBar(labels){
    const bar=document.createElement('div');bar.className='maak-choice-bar';bar.dir='rtl';
    Object.assign(bar.style,{display:'flex',gap:'8px',flexWrap:'wrap',margin:'8px 0 14px'});
    labels.forEach(label=>{
      const b=document.createElement('button');b.type='button';b.textContent=label;
      Object.assign(b.style,{border:'1px solid rgba(239,199,132,.18)',background:'rgba(255,255,255,.04)',color:'#f8eddb',borderRadius:'999px',padding:'10px 16px',font:'inherit',cursor:'pointer'});
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
  }

  const observer=new MutationObserver(records=>records.forEach(r=>r.addedNodes.forEach(maybeAddChoices)));
  observer.observe(thread,{childList:true});

  function pendingReminder(){
    try{
      const raw=storedGet(PENDING_REMINDER_KEY);
      return raw?JSON.parse(raw):null;
    }catch(_){return null}
  }

  function combinedReminderText(base,current){
    const value=(current||'').trim();
    if(!value)return base;
    if(/(?:الساعة|الساعه|بعد\s)/i.test(value))return `${base} ${value}`;
    if(/[0-9٠-٩]/.test(value))return `${base} ${value.replace(/([0-9٠-٩]{1,2}(?::[0-9٠-٩]{2})?)/,'الساعة $1')}`;
    return `${base} ${value}`;
  }

  async function askNotificationPermission(){
    if(!('Notification' in window)||Notification.permission!=='default')return;
    try{await Notification.requestPermission()}catch(_){}
  }

  async function sendReminder(text,pending=null){
    if(reminderBusy)return;
    reminderBusy=true;
    const current=text.trim();
    const fullText=pending?combinedReminderText(pending.text,current):current;
    const displayText=current;
    addUser(displayText);
    input.value='';
    input.dispatchEvent(new Event('input',{bubbles:true}));
    const wait=addStatus('بسجل التذكير…','media-pending');
    try{
      const r=await fetch('/api/reminders/chat',{
        method:'POST',cache:'no-store',credentials:'same-origin',
        headers:{'Content-Type':'application/json',Accept:'application/json'},
        body:JSON.stringify({
          message:fullText,
          display_message:displayText,
          thread_id:storedGet(THREAD_KEY)||pending?.thread_id||null,
          locale:navigator.language||'ar-EG'
        })
      });
      const data=await r.json();
      wait.remove();
      if(!r.ok||!data.message?.content)throw new Error(`HTTP ${r.status}`);
      if(data.thread_id)storedSet(THREAD_KEY,data.thread_id);
      addStatus(data.message.content);
      if(data.reminder?.needs_time){
        storedSet(PENDING_REMINDER_KEY,JSON.stringify({text:fullText,thread_id:data.thread_id||null}));
      }else{
        storedRemove(PENDING_REMINDER_KEY);
        if(data.reminder?.created)void askNotificationPermission();
      }
    }catch(_){
      wait.remove();
      addStatus('معرفتش أسجل التذكير دلوقتي، ومش هقول إنه اتسجل وهو ما اتسجلش.');
    }finally{
      reminderBusy=false;
    }
  }

  async function checkDueReminders(){
    try{
      const r=await fetch('/api/reminders/due',{credentials:'same-origin',cache:'no-store'});
      if(!r.ok)return;
      const data=await r.json();
      for(const item of (data.items||[])){
        const text=`⏰ تذكير: ${item.text}`;
        addStatus(text,'reminder-alert');
        if('Notification' in window&&Notification.permission==='granted'){
          try{new Notification('معاك — تذكير',{body:item.text})}catch(_){}
        }
        await fetch(`/api/reminders/${encodeURIComponent(item.id)}/ack`,{
          method:'POST',credentials:'same-origin',cache:'no-store'
        });
      }
    }catch(_){}
  }

  form.addEventListener('submit',event=>{
    const text=input.value.trim();
    const choices=thread.querySelector('.maak-choice-bar');if(choices)choices.remove();

    const pending=pendingReminder();
    if(pending&&reminderCancel.test(text)){
      event.preventDefault();event.stopImmediatePropagation();
      storedRemove(PENDING_REMINDER_KEY);
      addUser(text);input.value='';input.dispatchEvent(new Event('input',{bubbles:true}));
      addStatus('تمام، مش هسجل التذكير ده.');
      return;
    }
    if(reminderCue.test(text)||(pending&&reminderTimeCue.test(text))){
      event.preventDefault();event.stopImmediatePropagation();
      void sendReminder(text,pending);
      return;
    }

    if(cue.test(text))void renderImages(text);
    else if(lastImageQuery&&followupCue.test(text))void renderImages(lastImageQuery);
  },true);

  void checkDueReminders();
  setInterval(checkDueReminders,30000);
})();