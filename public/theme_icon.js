// Значок кнопки темы: полумесяц (тёмная тема) плавно перетекает в солнце (светлая) и обратно. Общий для чата
// (подключает public/theme.js), портала, страницы входа и демо виджета. Сам следит за темой страницы — атрибут
// data-theme на <html> (портал, вход, демо) или класс dark (Chainlit), — поэтому значок меняется и тогда, когда
// тему переключили в другой вкладке или в чате внутри окна виджета.
(() => {
  // Полумесяц и диск солнца — одинаковые по составу кривые (5 кубических Безье), поэтому одно переходит в другое.
  // Полумесяц совпадает с lucide «moon» (M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z), диск — с кругом r=4 lucide «sun».
  const MOON = [12, 3, 9.515, 5.485, 9.515, 9.515, 12, 12, 14.485, 14.485, 18.515, 14.485, 21, 12,
    21, 16.971, 16.971, 21, 12, 21, 7.029, 21, 3, 16.971, 3, 12, 3, 7.029, 7.029, 3, 12, 3];
  const SUN = [12, 8, 13.061, 8, 14.078, 8.421, 14.828, 9.172, 15.579, 9.922, 16, 10.939, 16, 12,
    16, 14.209, 14.209, 16, 12, 16, 9.791, 16, 8, 14.209, 8, 12, 8, 9.791, 9.791, 8, 12, 8];
  const RAYS = 'M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41';
  const DURATION = 500, ROOT = document.documentElement;
  const reduce = matchMedia('(prefers-reduced-motion: reduce)');
  const easeOut = x => 1 - (1 - x) ** 3, smooth = x => x * x * (3 - 2 * x);
  const state = new WeakMap();   // значок → {t: 0 солнце … 1 луна, raf}

  function isDark() {
    if (ROOT.dataset.theme) return ROOT.dataset.theme === 'dark';
    if (ROOT.classList.contains('dark') || ROOT.classList.contains('light')) return ROOT.classList.contains('dark');
    try { return localStorage.getItem('vite-ui-theme') !== 'light'; } catch (e) { return true; }   // Chainlit ещё не поставил класс
  }

  function draw(svg, t) {
    const p = SUN.map((s, i) => +(s + (MOON[i] - s) * t).toFixed(3));
    let d = `M${p[0]} ${p[1]}`;
    for (let i = 2; i < p.length; i += 6) d += `C${p.slice(i, i + 6).join(' ')}`;
    // Пока луна заполняется в диск, она поворачивается; лучи выходят из центра в последней трети перехода.
    const body = svg.querySelector('.sun-body'), rays = svg.querySelector('.sun-rays');
    body.setAttribute('d', d + 'Z');
    body.setAttribute('transform', `rotate(${(90 * (1 - t)).toFixed(2)} 12 12)`);
    const s = smooth(Math.max(0, 1 - t / .65));
    rays.setAttribute('transform', `rotate(${(90 * (1 - s)).toFixed(2)} 12 12) translate(12 12) scale(${s.toFixed(3)}) translate(-12 -12)`);
    rays.setAttribute('opacity', s.toFixed(3));
  }

  function animate(svg, to) {
    const st = state.get(svg);
    cancelAnimationFrame(st.raf);
    const from = st.t, time = DURATION * Math.abs(to - from);
    if (reduce.matches || !time) { st.t = to; draw(svg, to); return; }
    let start = null;
    const step = now => {
      start ??= now;
      const x = Math.min(1, (now - start) / time);
      st.t = from + (to - from) * easeOut(x);
      draw(svg, st.t);
      if (x < 1) st.raf = requestAnimationFrame(step);
    };
    st.raf = requestAnimationFrame(step);
  }

  // Новые значки: в разметке портала, входа и демо они уже есть, в чат кнопку рисует React — туда значок
  // добавляем сами (два значка lucide скрывает public/custom.css) и возвращаем, если React перерисовал кнопку.
  function setup() {
    const btn = document.getElementById('theme-toggle');
    if (btn && !btn.querySelector('.theme-icon')) {
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('class', 'theme-icon');
      svg.setAttribute('viewBox', '0 0 24 24');
      svg.setAttribute('aria-hidden', 'true');
      btn.prepend(svg);
    }
    document.querySelectorAll('svg.theme-icon').forEach(svg => {
      if (state.has(svg)) return;
      if (!svg.querySelector('.sun-body')) svg.innerHTML = `<path class="sun-rays" d="${RAYS}"/><path class="sun-body"/>`;
      state.set(svg, {t: dark ? 1 : 0, raf: 0});
      draw(svg, dark ? 1 : 0);
    });
  }

  let dark = isDark();
  new MutationObserver(() => {
    if (isDark() === dark) return;
    dark = !dark;
    document.querySelectorAll('svg.theme-icon').forEach(svg => state.has(svg) && animate(svg, dark ? 1 : 0));
  }).observe(ROOT, {attributes: true, attributeFilter: ['class', 'data-theme']});
  new MutationObserver(setup).observe(document, {childList: true, subtree: true});
  setup();
})();
