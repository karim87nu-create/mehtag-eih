(() => {
  const form = document.getElementById('chatForm');
  const input = document.getElementById('chatInput');
  const thread = document.getElementById('chatThread');
  if (!form || !input || !thread) return;

  const LAST_IMAGE_KEY = 'maak_last_image_query_v2';
  const imageCue = /(?:(?:وريني|ورني|اعرض(?:لي)?|فرجني|هات(?:لي)?|show\s+me).{0,48}(?:صور|صوره|صورة|photos?|pictures?|images?)|(?:عايز|عاوز|محتاج|عايزه|عاوزه|محتاجه).{0,32}(?:صور|صوره|صورة)|^(?:صور|صوره|صورة)(?=\s|ال|ل|$))/i;
  const startWithImage = /^(?:صور|صوره|صورة)(?=\s|ال|ل|$)/i;
  const followupCue = /^\s*(?:(?:طب|طيب)\s+)?(?:ايوه\s*)?(?:فين(?:\s+(?:الصور|الصورة|الصوره))?|وريني(?:\s+(?:الصور|الصورة|الصوره))?|هات(?:ها|هم|\s+الصور|\s+الصورة|\s+الصوره)?|اعرض(?:ها|هم|\s+الصور|\s+الصورة|\s+الصوره)?)\s*[؟?!.]*\s*$/i;

  function storedGet(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
  function storedSet(key, value) { try { localStorage.setItem(key, value); } catch (_) {} }

  function scrollDown() {
    const conversation = document.getElementById('conversation');
    requestAnimationFrame(() => {
      if (conversation) conversation.scrollTo({ top: conversation.scrollHeight, behavior: 'smooth' });
      else window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
    });
  }

  function addStatus(text, cls = '') {
    const row = document.createElement('div');
    row.className = 'chat-message assistant entering' + (cls ? ' ' + cls : '');
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.dir = 'rtl';
    bubble.textContent = text;
    row.appendChild(bubble);
    thread.appendChild(row);
    scrollDown();
    return row;
  }

  function addUser(text) {
    const empty = document.getElementById('emptyThread');
    if (empty) empty.classList.add('hidden');
    document.body.classList.add('has-conversation');
    const row = document.createElement('div');
    row.className = 'chat-message user entering';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.dir = 'rtl';
    bubble.textContent = text;
    row.appendChild(bubble);
    thread.appendChild(row);
    scrollDown();
  }

  function cleanQuery(text) {
    let value = String(text || '').trim();
    const directed = value.match(/(?:هات(?:لي)?|وريني|ورني|اعرض(?:لي)?|فرجني|عايز(?:ه)?|عاوز(?:ه)?|محتاج(?:ه)?)\s+(?:صور|صورة|صوره)\s*(.+)$/i);
    if (directed && directed[1]) value = directed[1].trim();
    else value = value.replace(/^(?:صور|صورة|صوره|الصور)\s*/i, '').trim();
    value = value.replace(/^(?:ال|لـ|ل)\s*/i, '').trim();

    // Common typo seen in the active RKV250 conversation. Keep the correction
    // narrow so unrelated product codes are never rewritten.
    if (/\brve\s*-?\s*250\b/i.test(value)) value = value.replace(/rve\s*-?\s*250/ig, 'RKV250');
    return value || String(text || '').trim();
  }

  function lastQueryFromDom() {
    const nodes = [...thread.querySelectorAll('.chat-message.user .bubble')].reverse();
    for (const node of nodes) {
      const text = (node.textContent || '').trim();
      if (imageCue.test(text)) return cleanQuery(text);
    }
    return '';
  }

  function getLastQuery() {
    return storedGet(LAST_IMAGE_KEY) || lastQueryFromDom();
  }

  async function renderImages(query) {
    query = cleanQuery(query);
    if (!query) return;
    storedSet(LAST_IMAGE_KEY, query);
    const pending = addStatus('بدور على الصور…', 'media-pending');
    try {
      const response = await fetch('/api/images?query=' + encodeURIComponent(query), { credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const data = await response.json();
      pending.remove();
      if (!data.items || !data.items.length) {
        addStatus('ملقتش صور مناسبة للموديل ده من المصادر المتاحة.');
        return;
      }
      const old = thread.querySelector('.chat-image-grid[data-hotfix="1"]');
      if (old) old.remove();
      const box = document.createElement('div');
      box.className = 'chat-image-grid';
      box.dataset.hotfix = '1';
      box.dir = 'rtl';
      for (const item of data.items.slice(0, 8)) {
        const a = document.createElement('a');
        a.href = item.url;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        a.title = item.title || data.query || query;
        const img = document.createElement('img');
        img.src = item.thumbnail;
        img.alt = item.title || data.query || query;
        img.loading = 'lazy';
        img.decoding = 'async';
        img.addEventListener('error', () => a.remove(), { once: true });
        a.appendChild(img);
        box.appendChild(a);
      }
      thread.appendChild(box);
      scrollDown();
    } catch (_) {
      pending.remove();
      addStatus('بحث الصور اتعطل دلوقتي، فمش هقول إن الصور ظهرت وهي ما ظهرتش.');
    }
  }

  function maybeHandle(text, sourceEvent) {
    text = String(text || '').trim();
    if (!text) return false;

    if (followupCue.test(text)) {
      const last = getLastQuery();
      if (!last) return false;
      sourceEvent.preventDefault();
      sourceEvent.stopImmediatePropagation();
      addUser(text);
      input.value = '';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      void renderImages(last);
      return true;
    }

    if (imageCue.test(text)) {
      const query = cleanQuery(text);
      storedSet(LAST_IMAGE_KEY, query);
      // The legacy client misses Arabic messages that START with صورة/صور
      // because JavaScript \b is ASCII-oriented. Render those here while still
      // allowing the normal chat turn through so conversation context is kept.
      if (startWithImage.test(text)) void renderImages(query);
    }
    return false;
  }

  document.addEventListener('submit', event => {
    if (event.target !== form) return;
    maybeHandle(input.value, event);
  }, true);

  document.addEventListener('keydown', event => {
    if (event.target !== input || event.key !== 'Enter' || event.shiftKey) return;
    maybeHandle(input.value, event);
  }, true);
})();
