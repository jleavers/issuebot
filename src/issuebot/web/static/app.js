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

  function draw(canvas, key, label, color, series) {
    var labels = series.map(function (point) { return point.day.slice(5); });
    var values = series.map(function (point) { return point[key]; });
    if (charts[key]) {
      charts[key].data.labels = labels;
      charts[key].data.datasets[0].data = values;
      charts[key].update();
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
        scales: { y: { beginAtZero: true, ticks: { precision: 0 } } }
      }
    });
  }

  function refresh() {
    fetch("/api/v1/stats?window=" + encodeURIComponent(windowText), { headers: { Accept: "application/json" } })
      .then(function (response) { return response.ok ? response.json() : Promise.reject(response.status); })
      .then(function (body) {
        draw(closedCanvas, "closed", "issues closed", "#5319e7", body.series);
        draw(runsCanvas, "runs", "agent runs", "#1d76db", body.series);
      })
      .catch(function () { /* the next poll tries again */ });
  }

  refresh();
  window.setInterval(refresh, pollSeconds * 1000);
})();
