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
    /* DESIGN.md «Graphite»: кнопка — знак помощника (public/favicon.svg), рамка Graphite Edge, окно — Obsidian Canvas с радиусом 10 px */
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
    @media (prefers-reduced-motion: reduce) { .hint { transform: none; transition: none; } }
    /* Размер окна меняется мышью (--panel-w/--panel-h задаёт скрипт ниже), но окно всегда помещается на экране */
    .panel { position: fixed; right: 24px; bottom: 96px; box-sizing: border-box; width: var(--panel-w, 520px); height: var(--panel-h, 640px);
             max-width: calc(100vw - 48px); max-height: calc(100vh - 120px);
             display: none; flex-direction: column; background: #0a0a0a; border: 1px solid #262626; border-radius: 10px;
             overflow: hidden; box-shadow: rgba(0,0,0,.3) 0 8px 10px 0; z-index: 2147483000; }
    .panel.open { display: flex; }
    /* Окно прижато к правому нижнему углу, поэтому тянуть — за левый верхний угол, левый или верхний край */
    .rs { position: absolute; z-index: 1; touch-action: none; }
    .rs-x { left: 0; top: 0; bottom: 0; width: 6px; cursor: ew-resize; }
    .rs-y { left: 0; top: 0; right: 0; height: 6px; cursor: ns-resize; }
    .rs-xy { left: 0; top: 0; width: 18px; height: 18px; cursor: nwse-resize; color: #737373; border-radius: 10px 0 0 0; }
    .rs-xy:hover, .resizing .rs-xy { color: #fafafa; }
    .rs-xy:focus-visible { outline: 2px solid #737373; outline-offset: -2px; }
    .rs-xy svg { position: absolute; left: 4px; top: 4px; width: 10px; height: 10px; fill: none; stroke: currentColor; stroke-width: 1.5; stroke-linecap: round; }
    .resizing iframe { pointer-events: none; }   /* иначе iframe перехватит мышь, пока тянут */
    .bar { background: #171717; color: #fafafa; padding: 8px 8px 8px 16px; border-bottom: 1px solid #262626; display: flex;
           justify-content: space-between; align-items: center; gap: 8px; font: 600 14px/20px ${font}; }
    .close { width: 32px; height: 32px; display: grid; place-items: center; background: none; border: 1px solid rgba(255,255,255,.1);
             border-radius: 8px; color: #a1a1a1; font: 18px/1 ${font}; cursor: pointer; }
    .close:hover { color: #fafafa; background: #262626; }
    iframe { border: 0; flex: 1; width: 100%; background: #0a0a0a; }
    @media (max-width: 700px) { .panel { left: 8px; right: 8px; width: auto; height: 640px; max-width: none; bottom: 88px; } .rs { display: none; }
                                .fab { right: 16px; bottom: 16px; } .hint { display: none; } }
  </style>
  <div class="panel" role="dialog"><div class="bar"><span class="title"></span>
    <button class="close" type="button" aria-label="Закрыть">×</button></div>
    <iframe title="Чат-помощник по документообороту" allow="clipboard-write"></iframe>
    <div class="rs rs-x" data-resize="x" aria-hidden="true"></div>
    <div class="rs rs-y" data-resize="y" aria-hidden="true"></div>
    <div class="rs rs-xy" data-resize="xy" role="button" tabindex="0" aria-label="Изменить размер окна: стрелки, Home — прежний размер"
         title="Потяните, чтобы изменить размер окна. Двойной щелчок — прежний размер"><svg viewBox="0 0 10 10" aria-hidden="true">
      <path d="M1 5 5 1M1 9 9 1"/></svg></div></div>
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

  const panel = root.querySelector('.panel'), frame = root.querySelector('iframe');
  const open = () => { if (!frame.src) frame.src = base + '/?chat'; panel.classList.add('open'); };
  const close = () => panel.classList.remove('open');
  root.querySelector('.fab').onclick = () => (panel.classList.contains('open') ? close() : open());
  root.querySelector('.close').onclick = close;
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  // Esc внутри чата: iframe сообщает о нём сам (public/theme.js)
  window.addEventListener('message', e => { if (e.origin === base && e.data && e.data.type === 'dou-assistant:close') close(); });
  window.douAssistant = {open, close};   // портал может открыть помощника своей кнопкой

  // Размер окна: тянуть за левый верхний угол или край, стрелки на уголке; двойной щелчок или Home — прежний.
  // Запоминается в браузере сотрудника для адреса портала
  const SIZE_KEY = 'dou-assistant:panel-size', MIN_W = 360, MIN_H = 400;
  function setSize(w, h, save) {
    w = Math.round(Math.max(MIN_W, Math.min(w, innerWidth - 48)));
    h = Math.round(Math.max(MIN_H, Math.min(h, innerHeight - 120)));
    panel.style.setProperty('--panel-w', w + 'px');
    panel.style.setProperty('--panel-h', h + 'px');
    if (save) try { localStorage.setItem(SIZE_KEY, JSON.stringify({w, h})); } catch (e) { /* размер живёт до перезагрузки */ }
  }
  function resetSize() {
    panel.style.removeProperty('--panel-w');
    panel.style.removeProperty('--panel-h');
    try { localStorage.removeItem(SIZE_KEY); } catch (e) { /* нечего удалять */ }
  }
  try {   // сохранённый размер больше экрана ужмёт max-width/max-height в CSS
    const s = JSON.parse(localStorage.getItem(SIZE_KEY));
    if (s && s.w >= MIN_W && s.h >= MIN_H) { panel.style.setProperty('--panel-w', s.w + 'px'); panel.style.setProperty('--panel-h', s.h + 'px'); }
  } catch (e) { /* размер по умолчанию */ }
  root.querySelectorAll('[data-resize]').forEach(h => {
    h.addEventListener('pointerdown', e => {
      if (e.button !== 0) return;
      e.preventDefault();
      const r = panel.getBoundingClientRect(), x0 = e.clientX, y0 = e.clientY, mode = h.dataset.resize;
      h.setPointerCapture(e.pointerId);
      panel.classList.add('resizing');
      h.onpointermove = m => setSize(mode === 'y' ? r.width : r.width + x0 - m.clientX,
                                     mode === 'x' ? r.height : r.height + y0 - m.clientY, false);
      h.onpointerup = h.onpointercancel = () => {
        h.onpointermove = h.onpointerup = h.onpointercancel = null;
        panel.classList.remove('resizing');
        const done = panel.getBoundingClientRect();
        setSize(done.width, done.height, true);
      };
    });
  });
  const grip = root.querySelector('.rs-xy');
  grip.ondblclick = resetSize;
  grip.onkeydown = e => {
    const step = e.shiftKey ? 64 : 16, r = panel.getBoundingClientRect();
    const d = {ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step]}[e.key];
    if (d) { e.preventDefault(); setSize(r.width + d[0], r.height + d[1], true); }
    if (e.key === 'Home') { e.preventDefault(); resetSize(); }
  };

  const mount = () => document.body.appendChild(host);
  if (document.body) mount(); else document.addEventListener('DOMContentLoaded', mount);
})();
