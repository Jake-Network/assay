// Live updates: one EventSource; ledger changes trigger a debounced htmx refresh of #content.
(function () {
  let timer = null;
  const live = document.getElementById('live');
  function refresh() {
    clearTimeout(timer);
    timer = setTimeout(() => { htmx.trigger(document.body, 'ledger-changed'); }, 400);
  }
  const es = new EventSource('/events');
  es.addEventListener('change', refresh);
  es.onopen = () => { live.classList.remove('off'); live.textContent = '● live'; };
  es.onerror = () => { live.classList.add('off'); live.textContent = '● reconnecting'; };

  let plot = null;
  async function drawChart() {
    const el = document.getElementById('chart');
    if (!el) { plot = null; return; }
    const d = await (await fetch(el.dataset.src)).json();
    const data = [d.t, d.value, d.best];
    if (plot && plot.root.isConnected) { plot.setData(data); return; }
    el.innerHTML = '';
    plot = new uPlot({
      width: Math.min(el.clientWidth || 900, 1200), height: 260,
      series: [{}, {label: 'experiment', stroke: 'transparent', points: {show: true, size: 4, fill: '#9db7f5', stroke: '#9db7f5'}},
               {label: 'best so far', stroke: '#2f6fed', width: 2, paths: uPlot.paths.stepped({align: 1})}],
      axes: [{}, {}],
    }, data, el);
  }
  document.body.addEventListener('htmx:afterSettle', drawChart);
  drawChart();
})();
