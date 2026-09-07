/* issuebot dashboard: the two charts and the "Poll now" status line. No inline scripts (CSP). */
(function () {
  "use strict";

  // --- the Poll now button: report what POST /api/v1/refresh answered --------------------
  document.body.addEventListener("htmx:afterRequest", function (event) {
    var status = document.getElementById("refresh-status");
    var info = event.detail && event.detail.pathInfo;
    if (!status || !info || info.requestPath !== "/api/v1/refresh") {
      return;
    }
    var xhr = event.detail.xhr;
    if (xhr && xhr.status === 202) {
      var body = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch (error) {
        body = null;
      }
      status.textContent = body && body.coalesced ? "poll already requested" : "poll requested";
    } else {
      status.textContent = "refresh failed (" + (xhr ? xhr.status : "no response") + ")";
    }
  });

  // --- the charts: /api/v1/stats?window=<N>d on load and every chart_poll_s seconds ------
  var script = document.currentScript;
  var closedCanvas = document.getElementById("closed-chart");
  var runsCanvas = document.getElementById("runs-chart");
  if (!script || !closedCanvas || !runsCanvas || typeof Chart === "undefined") {
    return;
  }
  var windowText = script.dataset.chartWindow || "30d";
  var pollSeconds = Number(script.dataset.chartPollS) || 60;
  var charts = {};
  var series = null;

  // Chart.js paints on a canvas, so the theme tokens cannot reach it through CSS: read
  // them off the document instead, and read them again whenever the theme changes. The
  // bar colours are the same ones the Kanban columns use for "complete" and "review".
  function palette() {
    var style = window.getComputedStyle(document.documentElement);
    function token(name, fallback) {
      return style.getPropertyValue(name).trim() || fallback;
    }
    return {
      closed: token("--chart-closed", "#5319e7"),
      runs: token("--chart-runs", "#1d76db"),
      ink: token("--chart-ink", "#6b7280"),
      grid: token("--chart-grid", "#d9dee5")
    };
  }

  function draw(canvas, key, label, points, colors) {
    var color = colors[key];
    var labels = points.map(function (point) { return point.day.slice(5); });
    var values = points.map(function (point) { return point[key]; });
    if (charts[key]) {
      var chart = charts[key];
      chart.data.labels = labels;
      chart.data.datasets[0].data = values;
      chart.data.datasets[0].backgroundColor = color;
      chart.options.scales.x.ticks.color = colors.ink;
      chart.options.scales.x.grid.color = colors.grid;
      chart.options.scales.y.ticks.color = colors.ink;
      chart.options.scales.y.grid.color = colors.grid;
      chart.update();
      return;
    }
    charts[key] = new Chart(canvas, {
      type: "bar",
      data: { labels: labels, datasets: [{ label: label, data: values, backgroundColor: color }] },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: colors.ink }, grid: { color: colors.grid } },
          y: {
            beginAtZero: true,
            ticks: { precision: 0, color: colors.ink },
            grid: { color: colors.grid }
          }
        }
      }
    });
  }

  function render() {
    if (!series) {
      return;
    }
    var colors = palette();
    draw(closedCanvas, "closed", "issues closed", series, colors);
    draw(runsCanvas, "runs", "agent runs", series, colors);
  }

  function refresh() {
    fetch("/api/v1/stats?window=" + encodeURIComponent(windowText), { headers: { Accept: "application/json" } })
      .then(function (response) { return response.ok ? response.json() : Promise.reject(response.status); })
      .then(function (body) {
        series = body.series;
        render();
      })
      .catch(function () { /* the next poll tries again */ });
  }

  // theme.js fires this after the toggle, and after an OS change while it is in charge
  document.addEventListener("issuebot:themechange", render);

  refresh();
  window.setInterval(refresh, pollSeconds * 1000);
})();
