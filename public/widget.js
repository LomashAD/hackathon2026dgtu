/* Помощник по документообороту — поп-ап виджет для внутреннего портала.

   Подключение — одна строка на любой странице портала:
     <script src="https://<адрес помощника>/widget.js" defer></script>
   Необязательные атрибуты: data-title (заголовок окна), data-label (подсказка у кнопки), data-color (цвет).

   Чат открывается в iframe с адреса помощника и требует входа. Адрес портала должен быть в
   config.yaml → portal.embed_origins (иначе браузер не покажет чат в iframe: frame-ancestors).
   Разметка — в Shadow DOM: стили портала не ломают виджет, стили виджета не задевают портал. */
(() => {
  const me = document.currentScript;
  if (!me || window.__douAssistantWidget) return;
  window.__douAssistantWidget = true;
  const base = new URL(me.src, location.href).origin;
  const color = /^#[0-9a-f]{3,8}$/i.test(me.dataset.color || '') ? me.dataset.color : '#1f5fae';

  const host = document.createElement('div');
  host.id = 'dou-assistant-widget';
  const root = host.attachShadow({mode: 'open'});
  root.innerHTML = `<style>
    :host { all: initial; }
    .fab { position: fixed; right: 24px; bottom: 24px; width: 60px; height: 60px; border-radius: 50%; border: none;
           background: ${color}; color: #fff; font-size: 26px; cursor: pointer; z-index: 2147483000;
           box-shadow: 0 6px 20px rgba(0,0,0,.25); }
    .hint { position: absolute; right: 70px; top: 15px; white-space: nowrap; background: #fff; color: #1d2433;
            font: 13px/1.3 system-ui, -apple-system, 'Segoe UI', sans-serif; padding: 6px 10px; border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,.15); }
    .panel { position: fixed; right: 24px; bottom: 96px; width: 520px; height: 640px; max-height: calc(100vh - 120px);
             display: none; flex-direction: column; background: #fff; border-radius: 14px; overflow: hidden;
             box-shadow: 0 12px 40px rgba(0,0,0,.3); z-index: 2147483000; }
    .panel.open { display: flex; }
    .bar { background: ${color}; color: #fff; padding: 10px 14px; display: flex; justify-content: space-between;
           align-items: center; font: 600 15px/1.3 system-ui, -apple-system, 'Segoe UI', sans-serif; }
    .bar button { background: none; border: none; color: #fff; font-size: 22px; line-height: 1; cursor: pointer; }
    iframe { border: 0; flex: 1; width: 100%; }
    @media (max-width: 700px) { .panel { left: 8px; right: 8px; width: auto; } .hint { display: none; } }
  </style>
  <div class="panel" role="dialog"><div class="bar"><span class="title"></span>
    <button class="close" type="button" aria-label="Закрыть">×</button></div>
    <iframe title="Чат-помощник по документообороту" allow="clipboard-write"></iframe></div>
  <button class="fab" type="button" aria-label="Открыть помощника">💬<span class="hint"></span></button>`;

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

  const mount = () => document.body.appendChild(host);
  if (document.body) mount(); else document.addEventListener('DOMContentLoaded', mount);
})();
