// Тема чата: по умолчанию как в системе; кнопка в шапке (солнце — светлая, полумесяц — тёмная) переключает
// тему одним нажатием вместо меню Chainlit. Выбор хранится под ключом Chainlit «vite-ui-theme» — он общий
// с порталом (тот же адрес), поэтому чат во всплывающем окне меняет тему вместе с порталом и наоборот.
(() => {
  const KEY = 'vite-ui-theme', dark = matchMedia('(prefers-color-scheme: dark)');
  const sys = () => (dark.matches ? 'dark' : 'light');
  const current = () => (document.documentElement.classList.contains('dark') ? 'dark' : 'light');
  const saved = () => { try { return localStorage.getItem(KEY); } catch (e) { return null; } };

  // У Chainlit по умолчанию тёмная тема (default_theme = "dark", «system» в конфиге не задать). Скрипт
  // выполняется раньше бандла Chainlit, поэтому, пока тему не выбирали, записываем «как в системе».
  if (saved() === null) try { localStorage.setItem(KEY, 'system'); } catch (e) { /* нет хранилища */ }

  // Провайдер темы Chainlit (React-контекст с setTheme): через него меняется и состояние приложения
  // (логотип, уведомления), а не только класс на <html>.
  function provider() {
    const el = document.querySelector('#theme-toggle') || document.querySelector('#root *');
    const key = el && Object.keys(el).find(k => k.startsWith('__reactFiber$'));
    for (let f = key ? el[key] : null; f; f = f.return) {
      const v = f.memoizedProps && f.memoizedProps.value;
      if (v && typeof v.setTheme === 'function' && 'theme' in v) return v;
    }
    return null;
  }

  function apply(value) {   // light | dark | system
    const p = provider();
    if (p) p.setTheme(value);
    else try { localStorage.setItem(KEY, value); } catch (e) { /* без хранилища тема живёт до перезагрузки */ }
    // Класс и переменные theme.json ставим и сами: провайдер не перерисовывается, если значение не изменилось,
    // и не следит за сменой темы системы.
    const t = value === 'system' ? sys() : value, root = document.documentElement;
    root.classList.remove('light', 'dark');
    root.classList.add(t);
    Object.entries((window.theme || {})[t] || {}).forEach(([k, v]) => root.style.setProperty(k, v));
  }

  const toggleBtn = e => e.target instanceof Element && e.target.closest('#theme-toggle');
  const label = b => { b.title = b.ariaLabel = current() === 'dark' ? 'Включить светлую тему' : 'Включить тёмную тему'; };

  // Меню Chainlit открывается по pointerdown и клавишам — перехватываем их до React; нажатие обрабатываем по click
  // (Enter и пробел на кнопке тоже дают click).
  window.addEventListener('pointerdown', e => { if (toggleBtn(e)) e.stopPropagation(); }, true);
  window.addEventListener('keydown', e => {
    if (toggleBtn(e) && ['Enter', ' ', 'ArrowDown', 'ArrowUp'].includes(e.key)) e.stopPropagation();
  }, true);
  window.addEventListener('click', e => {
    const b = toggleBtn(e);
    if (!b) return;
    e.stopPropagation();
    const next = current() === 'dark' ? 'light' : 'dark';
    apply(next === sys() ? 'system' : next);   // вернулись к теме системы — снова следуем за ней
    label(b);
  }, true);
  document.addEventListener('mouseover', e => { const b = toggleBtn(e); if (b) label(b); }, true);

  // Тему переключили на портале (или в другой вкладке) — применяем здесь.
  window.addEventListener('storage', e => {
    if (e.key === KEY) apply(e.newValue === 'light' || e.newValue === 'dark' ? e.newValue : 'system');
  });
  dark.addEventListener('change', () => { const v = saved(); if (v !== 'light' && v !== 'dark') apply('system'); });
})();
