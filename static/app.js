// Renders a Chart.js line chart for every <canvas data-chart="/api/chart/SYM"> in a root.
(function () {
  function initCharts(root) {
    (root || document).querySelectorAll('canvas[data-chart]').forEach(function (el) {
      if (el.dataset.ready) return;
      el.dataset.ready = '1';
      fetch(el.dataset.chart)
        .then(function (r) { return r.json(); })
        .then(function (data) {
          var pts = data.points || [];
          if (!pts.length) {
            el.replaceWith(Object.assign(document.createElement('p'),
              { className: 'text-sm text-gray-500', textContent: 'No price data yet.' }));
            return;
          }
          new Chart(el, {
            type: 'line',
            data: {
              labels: pts.map(function (p) { return p.date; }),
              datasets: [{ data: pts.map(function (p) { return p.close; }),
                borderColor: '#6366f1', borderWidth: 2, pointRadius: 0, tension: 0.2 }],
            },
            options: {
              maintainAspectRatio: false,
              plugins: { legend: { display: false } },
              scales: { x: { ticks: { maxTicksLimit: 5 } } },
            },
          });
        })
        .catch(function () {});
    });
  }
  window.initCharts = initCharts;
  document.addEventListener('DOMContentLoaded', function () { initCharts(document); });
  document.body && document.body.addEventListener('htmx:afterSwap', function (e) { initCharts(e.target); });
  document.addEventListener('htmx:afterSettle', function (e) { initCharts(e.target); });
})();
