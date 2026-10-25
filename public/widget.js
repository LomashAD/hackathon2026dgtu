/* Помощник по документообороту — поп-ап виджет для внутреннего портала.

   Подключение — одна строка на любой странице портала:
     <script src="https://<адрес помощника>/widget.js" defer></script>
   Необязательные атрибуты: data-title (заголовок окна), data-label (подсказка у кнопки), data-color (фон кнопки;
   по умолчанию — оформление DESIGN.md «Graphite»: кнопка-знак помощника, окно с рамкой Graphite Edge).

   Чат открывается в iframe с адреса помощника и требует входа. Адрес портала должен быть в
   config.yaml → portal.embed_origins (иначе браузер не покажет чат в iframe: frame-ancestors).
   Разметка — в Shadow DOM: стили портала не ломают виджет, стили виджета не задевают портал. */
(() => {
  const me = document.currentScript;
  if (!me || window.__douAssistantWidget) return;
  window.__douAssistantWidget = true;
  const base = new URL(me.src, location.href).origin;
  const color = /^#[0-9a-f]{3,8}$/i.test(me.dataset.color || '') ? me.dataset.color : '';
  const font = "'Inter', ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";

  const host = document.createElement('div');
  host.id = 'dou-assistant-widget';
  const root = host.attachShadow({mode: 'open'});
  // Шрифт Inter на страницу портала не подключаем (@font-face из Shadow DOM не работает, а общий задел бы портал):
  // подписи кнопки и окна — Inter, если он есть на портале, иначе системный шрифт; сам чат в iframe — Inter
  root.innerHTML = `<style>
    :host { all: initial; }
    /* DESIGN.md «Graphite»: кнопка — знак помощника (public/favicon.svg), рамка Graphite Edge, окно — Obsidian Canvas
       с углами 12 px, как у кнопки: окно вырастает из неё (.morph ниже) */
    .fab { position: fixed; right: 24px; bottom: 24px; width: 56px; height: 56px; padding: 0; border-radius: 12px;
           border: 1px solid #262626; background: ${color || '#0a0a0a'}; cursor: pointer; z-index: 2147483000;
           display: grid; place-items: center; box-shadow: rgba(0,0,0,.25) 0 4px 4px 0; }
    .fab:hover { border-color: #464646; }
    .fab:focus-visible, .close:focus-visible { outline: 2px solid #737373; outline-offset: 2px; }
    .fab svg { width: 100%; height: 100%; fill: none; stroke-width: 1.75; }
    .hint { position: absolute; right: 68px; top: 12px; white-space: nowrap; background: #171717; color: #fafafa;
            border: 1px solid #262626; font: 500 14px/20px ${font}; padding: 4px 12px; border-radius: 9999px;
            box-shadow: rgba(0,0,0,.25) 0 4px 4px 0;
            opacity: 0; transform: translateX(12px); pointer-events: none; transition: opacity .2s ease-out, transform .2s ease-out; }
    /* Подсказка выезжает из-под кнопки при наведении и при фокусе с клавиатуры */
    .fab:hover .hint, .fab:focus-visible .hint { opacity: 1; transform: none; pointer-events: auto; }
    /* Окно растянуто вниз до кнопки — кнопка уходит; хватает места под окном — возвращается (скрипт ниже) */
    .fab { transition: opacity .2s ease-out, visibility 0s; }
    .fab.tucked { opacity: 0; visibility: hidden; transition: opacity .2s ease-out, visibility 0s .2s; }
    @media (prefers-reduced-motion: reduce) { .hint { transform: none; transition: none; } .fab, .fab.tucked { transition: none; } }
    /* Размер и нижний отступ окна меняются мышью (--panel-w/-h/-b задаёт скрипт ниже), но окно всегда помещается на экране */
    .panel { position: fixed; right: 24px; bottom: var(--panel-b, 96px); box-sizing: border-box; width: var(--panel-w, 520px);
             height: var(--panel-h, 640px); max-width: calc(100vw - 48px); max-height: calc(100vh - var(--panel-b, 96px) - 24px);
             display: none; flex-direction: column; background: #0a0a0a; border: 1px solid #262626; border-radius: 12px;
             overflow: hidden; box-shadow: rgba(0,0,0,.3) 0 8px 10px 0; z-index: 2147483000; }
    .panel.open, .panel.closing { display: flex; }
    .panel.closing { pointer-events: none; }   /* уже закрыто, видно только до конца анимации */
    /* Копия кнопки: при открытии раздвигает края от кнопки до размеров окна, при закрытии сжимается обратно (скрипт ниже).
       Лежит под окном и кнопкой: окно проявляется поверх неё, а в начале и в конце её закрывает сама кнопка */
    .morph { position: fixed; z-index: 2147482999; box-sizing: border-box; border: 1px solid #262626; border-radius: 12px;
             background: #0a0a0a; display: grid; place-items: center; pointer-events: none; }
    .morph[hidden] { display: none; }
    .morph svg { width: 54px; height: 54px; fill: none; stroke-width: 1.75; }   /* знак того же размера, что на кнопке */
    /* Окно прижато к правому краю, поэтому тянуть — за уголки слева, левый, верхний или нижний край */
    .rs { position: absolute; z-index: 1; touch-action: none; }
    .rs-l { left: 0; top: 0; bottom: 0; width: 6px; cursor: ew-resize; }
    .rs-t { left: 0; top: 0; right: 0; height: 6px; cursor: ns-resize; }
    .rs-b { left: 0; bottom: 0; right: 0; height: 6px; cursor: ns-resize; }
    .grip { left: 0; width: 18px; height: 18px; color: #737373; }
    .rs-lt { top: 0; cursor: nwse-resize; border-radius: 12px 0 0 0; }
    .rs-lb { bottom: 0; cursor: nesw-resize; border-radius: 0 0 0 12px; }
    .grip:hover, .grip.dragging { color: #fafafa; }
    .grip:focus-visible { outline: 2px solid #737373; outline-offset: -2px; }
    .grip svg { position: absolute; left: 4px; top: 4px; width: 10px; height: 10px; fill: none; stroke: currentColor; stroke-width: 1.5; stroke-linecap: round; }
    .resizing iframe { pointer-events: none; }   /* иначе iframe перехватит мышь, пока тянут */
    .bar { background: #171717; color: #fafafa; padding: 8px 8px 8px 16px; border-bottom: 1px solid #262626; display: flex;
           justify-content: space-between; align-items: center; gap: 8px; font: 600 14px/20px ${font}; }
    .close { width: 32px; height: 32px; display: grid; place-items: center; background: none; border: 1px solid rgba(255,255,255,.1);
             border-radius: 8px; color: #a1a1a1; font: 18px/1 ${font}; cursor: pointer; }
    .close:hover { color: #fafafa; background: #262626; }
    iframe { border: 0; flex: 1; width: 100%; background: #0a0a0a; }
    @media (max-width: 700px) { .panel { left: 8px; right: 8px; width: auto; height: 640px; max-width: none; bottom: 88px; max-height: calc(100vh - 120px); }
                                .rs { display: none; }
                                .fab { right: 16px; bottom: 16px; } .hint { display: none; } }
  </style>
  <div class="panel" role="dialog"><div class="bar"><span class="title"></span>
    <button class="close" type="button" aria-label="Закрыть">×</button></div>
    <iframe title="Чат-помощник по документообороту" allow="clipboard-write"></iframe>
    <div class="rs rs-l" data-resize="l" aria-hidden="true"></div>
    <div class="rs rs-t" data-resize="t" aria-hidden="true"></div>
    <div class="rs rs-b" data-resize="b" aria-hidden="true"></div>
    <div class="rs grip rs-lt" data-resize="lt" role="button" tabindex="0" aria-label="Изменить размер окна, левый верхний угол: стрелки, Home — прежний размер"
         title="Потяните, чтобы изменить размер окна. Двойной щелчок — прежний размер"><svg viewBox="0 0 10 10" aria-hidden="true">
      <path d="M1 5 5 1M1 9 9 1"/></svg></div>
    <div class="rs grip rs-lb" data-resize="lb" role="button" tabindex="0" aria-label="Изменить размер окна, левый нижний угол: стрелки, Home — прежний размер"
         title="Потяните, чтобы изменить размер окна. Двойной щелчок — прежний размер"><svg viewBox="0 0 10 10" aria-hidden="true">
      <path d="M1 5 5 9M1 1 9 9"/></svg></div></div>
  <button class="fab" type="button" aria-label="Открыть помощника"><svg viewBox="0 0 32 32" aria-hidden="true">
    <defs><linearGradient id="fab-spectrum" x1="0" y1="0" x2="1" y2="0"><stop offset=".15" stop-color="#ffb931"/>
      <stop offset=".3" stop-color="#ffffff"/><stop offset=".4" stop-color="#ffffff"/><stop offset=".5" stop-color="#2e73fc"/></linearGradient></defs>
    <rect x="8" y="8" width="16" height="16" rx="4" stroke="#fafafa" transform="rotate(-12 16 16)"/>
    <rect x="9.25" y="9.25" width="13.5" height="13.5" rx="3.5" stroke="url(#fab-spectrum)" transform="rotate(20 16 16)"/></svg>
    <span class="hint"></span></button>`;

  const title = me.dataset.title || 'Помощник по документообороту';
  root.querySelector('.title').textContent = title;
  root.querySelector('.panel').setAttribute('aria-label', title);
  root.querySelector('.hint').textContent = me.dataset.label || 'Спросить про документооборот';

  const panel = root.querySelector('.panel'), frame = root.querySelector('iframe'), fab = root.querySelector('.fab');
  // Кнопка уходит, когда открытое окно растянуто вниз до неё, и возвращается, когда под окном снова хватает места
  const updateFab = () => {
    const p = panel.getBoundingClientRect(), f = fab.getBoundingClientRect();
    fab.classList.toggle('tucked', panel.classList.contains('open') && p.bottom > f.top - 8);
  };
  // Окно вырастает из кнопки: копия знака (.morph) раздвигает края от кнопки до размеров окна, а цвета фона, рамки
  // и тени переходят от кнопки к окну; затем поверх копии проявляется само окно. При закрытии — то же в обратную сторону,
  // нажатие посреди анимации разворачивает её с того же места. prefers-reduced-motion — сразу, без анимации
  const morph = document.createElement('div'), calm = matchMedia('(prefers-reduced-motion: reduce)');
  morph.className = 'morph';
  morph.hidden = true;
  morph.setAttribute('aria-hidden', 'true');
  morph.append(fab.querySelector('svg').cloneNode(true));
  morph.querySelector('defs').remove();   // градиент знака — у кнопки, второй такой id не нужен
  root.append(morph);
  const EASE = 'cubic-bezier(.2, .8, .2, 1)';
  let morphing = [];
  const look = (el, r = el.getBoundingClientRect(), s = getComputedStyle(el)) => ({left: r.left + 'px', top: r.top + 'px',
    width: r.width + 'px', height: r.height + 'px', backgroundColor: s.backgroundColor, borderColor: s.borderTopColor, boxShadow: s.boxShadow});
  const alpha = el => +getComputedStyle(el).opacity;
  function morphPanel(opening) {
    const now = !morph.hidden && {look: look(morph), logo: alpha(morph.firstChild)};
    const shown = now ? alpha(panel) : opening ? 0 : 1;   // у закрытого окна (display: none) opacity всё равно 1
    morphing.forEach(a => a.cancel());
    morph.hidden = calm.matches;
    panel.classList.toggle('closing', !opening && !calm.matches);
    if (calm.matches) return;
    const icon = look(fab), win = look(panel), from = now ? now.look : opening ? icon : win;
    const run = (el, frames, duration) => el.animate(frames, {duration, fill: 'both'});
    const batch = morphing = opening ? [   // 360 мс: края — 260 мс, потом поверх копии проявляется окно
      run(morph, [{...from, easing: EASE}, {...win, offset: .72}, win], 360),
      run(morph.firstChild, [{opacity: now ? now.logo : 1}, {opacity: 0, offset: .4}, {opacity: 0}], 360),
      run(panel, [{opacity: shown}, {opacity: shown, offset: .7}, {opacity: 1}], 360),
    ] : [   // 300 мс: окно гаснет, под ним копия, потом её края сжимаются до кнопки
      run(panel, [{opacity: shown}, {opacity: 0, offset: .3}, {opacity: 0}], 300),
      run(morph, [from, {...from, offset: .3, easing: EASE}, icon], 300),
      run(morph.firstChild, [{opacity: now ? now.logo : 0}, {opacity: now ? now.logo : 0, offset: .55}, {opacity: 1}], 300),
    ];
    Promise.all(batch.map(a => a.finished)).then(() => {
      if (morphing !== batch) return;
      batch.forEach(a => a.cancel());
      morph.hidden = true;
      panel.classList.remove('closing');
    }, () => { /* развернули посреди анимации */ });
  }
  const open = () => {
    if (!frame.src) frame.src = base + '/?chat';
    if (panel.classList.contains('open')) return;
    panel.classList.add('open');
    updateFab();
    morphPanel(true);
  };
  const close = () => {
    if (!panel.classList.contains('open')) return;
    panel.classList.remove('open');
    updateFab();
    morphPanel(false);
  };
  fab.onclick = () => (panel.classList.contains('open') ? close() : open());
  root.querySelector('.close').onclick = close;
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  // Esc внутри чата: iframe сообщает о нём сам (public/theme.js)
  window.addEventListener('message', e => { if (e.origin === base && e.data && e.data.type === 'dou-assistant:close') close(); });
  window.addEventListener('resize', updateFab);
  window.douAssistant = {open, close};   // портал может открыть помощника своей кнопкой

  // Размер окна: тянуть за уголки слева или за левый, верхний, нижний край, стрелки на уголках; двойной щелчок или Home — прежний.
  // Запоминается в браузере сотрудника для адреса портала
  const SIZE_KEY = 'dou-assistant:panel-size', MIN_W = 360, MIN_H = 400, EDGE = 24;
  function setSize(w, h, b, save) {
    const v = {w: Math.round(w), h: Math.round(h), b: Math.round(b)};
    panel.style.setProperty('--panel-w', v.w + 'px');
    panel.style.setProperty('--panel-h', v.h + 'px');
    panel.style.setProperty('--panel-b', v.b + 'px');
    if (save) try { localStorage.setItem(SIZE_KEY, JSON.stringify(v)); } catch (e) { /* размер живёт до перезагрузки */ }
    updateFab();
  }
  // Новые края окна: r — прежние, dx/dy — сдвиг указателя, mode — какие края двигать (l, t, b); правый край всегда на месте
  function moveEdges(r, mode, dx, dy, save) {
    let {left, top, bottom} = r;
    if (mode.includes('l')) left = Math.max(EDGE, Math.min(left + dx, r.right - MIN_W));
    if (mode.includes('t')) top = Math.max(EDGE, Math.min(top + dy, bottom - MIN_H));
    if (mode.includes('b')) bottom = Math.min(innerHeight - EDGE, Math.max(bottom + dy, top + MIN_H));
    setSize(r.right - left, bottom - top, innerHeight - bottom, save);
  }
  function resetSize() {
    ['--panel-w', '--panel-h', '--panel-b'].forEach(p => panel.style.removeProperty(p));
    try { localStorage.removeItem(SIZE_KEY); } catch (e) { /* нечего удалять */ }
    updateFab();
  }
  try {   // сохранённый размер больше экрана ужмёт max-width/max-height в CSS
    const s = JSON.parse(localStorage.getItem(SIZE_KEY));
    if (s && s.w >= MIN_W && s.h >= MIN_H) {
      panel.style.setProperty('--panel-w', s.w + 'px');
      panel.style.setProperty('--panel-h', s.h + 'px');
      if (s.b >= EDGE) panel.style.setProperty('--panel-b', s.b + 'px');
    }
  } catch (e) { /* размер по умолчанию */ }
  root.querySelectorAll('[data-resize]').forEach(h => {
    h.addEventListener('pointerdown', e => {
      if (e.button !== 0) return;
      e.preventDefault();
      const r = panel.getBoundingClientRect(), x0 = e.clientX, y0 = e.clientY;
      h.setPointerCapture(e.pointerId);
      panel.classList.add('resizing');
      h.classList.add('dragging');
      h.onpointermove = m => moveEdges(r, h.dataset.resize, m.clientX - x0, m.clientY - y0, false);
      h.onpointerup = h.onpointercancel = () => {
        h.onpointermove = h.onpointerup = h.onpointercancel = null;
        panel.classList.remove('resizing');
        h.classList.remove('dragging');
        moveEdges(panel.getBoundingClientRect(), '', 0, 0, true);   // запомнить, что получилось на экране
      };
    });
    if (!h.classList.contains('grip')) return;
    h.ondblclick = resetSize;
    h.onkeydown = e => {   // стрелка двигает уголок в свою сторону
      const step = e.shiftKey ? 64 : 16;
      const d = {ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step]}[e.key];
      if (d) { e.preventDefault(); moveEdges(panel.getBoundingClientRect(), h.dataset.resize, d[0], d[1], true); }
      if (e.key === 'Home') { e.preventDefault(); resetSize(); }
    };
  });

  const mount = () => document.body.appendChild(host);
  if (document.body) mount(); else document.addEventListener('DOMContentLoaded', mount);
})();
