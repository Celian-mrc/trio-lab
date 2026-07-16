// Dashboard /admin : graphiques Chart.js (games/jour/plateforme, tailles de
// tables). Données injectées côté serveur en JSON dans des <script
// type="application/json"> plutôt qu'en attributs HTML, pour rester lisible
// avec de gros volumes (jusqu'à 48 jours x 5 plateformes).

document.addEventListener("DOMContentLoaded", function () {
  var foreground = "#fafafa";
  var mutedFg = "#a1a1aa";
  var border = "#27272a";
  var card = "#0c0c0f";

  Chart.defaults.color = mutedFg;
  Chart.defaults.borderColor = border;
  Chart.defaults.font.family = "system-ui, 'Segoe UI', sans-serif";
  Chart.defaults.font.size = 12;
  Chart.defaults.plugins.legend.labels.color = foreground;
  Chart.defaults.plugins.tooltip.backgroundColor = card;
  Chart.defaults.plugins.tooltip.titleColor = foreground;
  Chart.defaults.plugins.tooltip.bodyColor = mutedFg;
  Chart.defaults.plugins.tooltip.borderColor = border;
  Chart.defaults.plugins.tooltip.borderWidth = 1;
  Chart.defaults.plugins.tooltip.padding = 8;

  // Palette qualitative fixe par plateforme (cohérente d'un chargement à
  // l'autre, contrairement à une palette générée à la volée).
  var PLATFORM_COLORS = {
    br1: "#fb923c",
    eun1: "#c084fc",
    euw1: "#4ade80",
    kr: "#38bdf8",
    na1: "#fbbf24",
  };
  var FALLBACK_COLORS = ["#f87171", "#2dd4bf", "#a3e635", "#f472b6"];

  function colorFor(key, index) {
    return PLATFORM_COLORS[key] || FALLBACK_COLORS[index % FALLBACK_COLORS.length];
  }

  var perDayEl = document.getElementById("chart-per-day");
  var perDayData = JSON.parse(document.getElementById("admin-per-day-data").textContent);
  if (perDayEl && perDayData.days.length) {
    new Chart(perDayEl, {
      type: "line",
      data: {
        labels: perDayData.days,
        datasets: perDayData.platforms.map(function (p, i) {
          var color = colorFor(p, i);
          return {
            label: p,
            data: perDayData.series[p],
            borderColor: color,
            backgroundColor: color,
            pointRadius: 2,
            tension: 0.25,
          };
        }),
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          x: { grid: { color: border } },
          y: { grid: { color: border }, beginAtZero: true },
        },
      },
    });
  }

  var sizesEl = document.getElementById("chart-sizes");
  var sizesData = JSON.parse(document.getElementById("admin-sizes-data").textContent);
  if (sizesEl && sizesData.labels.length) {
    new Chart(sizesEl, {
      type: "bar",
      data: {
        labels: sizesData.labels,
        datasets: [
          {
            label: "Taille",
            data: sizesData.bytes.map(function (b) {
              return b / (1024 * 1024);
            }),
            backgroundColor: "#38bdf8",
          },
        ],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function (ctx) {
                return ctx.parsed.x.toFixed(1) + " Mo";
              },
            },
          },
        },
        scales: {
          x: { grid: { color: border }, title: { display: true, text: "Mo", color: mutedFg } },
          y: { grid: { display: false } },
        },
      },
    });
  }
});
