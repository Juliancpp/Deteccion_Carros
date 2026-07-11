const entradasHoy = document.getElementById("entradasHoy");
const salidasHoy = document.getElementById("salidasHoy");
const totalVehiculos = document.getElementById("totalVehiculos");
const eventsTable = document.getElementById("eventsTable");
const statusText = document.getElementById("statusText");
const refreshBtn = document.getElementById("refreshBtn");
const chartCanvas = document.getElementById("hourlyChart");

let hourlyChart = null;

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  return response.json();
}

function splitDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return { date: "-", time: "-" };
  }

  return {
    date: date.toLocaleDateString("es-EC"),
    time: date.toLocaleTimeString("es-EC", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }),
  };
}

function renderStats(stats) {
  entradasHoy.textContent = stats.entradas_hoy ?? 0;
  salidasHoy.textContent = stats.salidas_hoy ?? 0;
  totalVehiculos.textContent = stats.total_vehiculos ?? 0;
  renderChart(stats.vehiculos_por_hora || []);
}

function renderChart(rows) {
  const labels = rows.map((row) => row.hour);
  const values = rows.map((row) => row.count);

  if (hourlyChart) {
    hourlyChart.data.labels = labels;
    hourlyChart.data.datasets[0].data = values;
    hourlyChart.update();
    return;
  }

  hourlyChart = new Chart(chartCanvas, {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          label: "Vehículos",
          data: values,
          backgroundColor: "#0f8b8d",
          borderRadius: 4,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        y: {
          beginAtZero: true,
          ticks: { precision: 0 },
        },
      },
      plugins: {
        legend: { display: false },
      },
    },
  });
}

function renderEvents(events) {
  eventsTable.innerHTML = "";

  if (!events.length) {
    eventsTable.innerHTML = `<tr><td class="empty" colspan="5">Sin eventos registrados</td></tr>`;
    return;
  }

  const fragment = document.createDocumentFragment();
  events.forEach((event) => {
    const row = document.createElement("tr");
    const parts = splitDateTime(event.fecha_hora);
    const type = String(event.tipo_evento || "").toLowerCase();
    const imageCell = event.imagen_url
      ? `<a class="evidence-link" href="${event.imagen_url}" target="_blank" rel="noopener">Ver imagen</a>`
      : "-";

    row.innerHTML = `
      <td>${parts.date}</td>
      <td>${parts.time}</td>
      <td>${event.placa || "No detectada"}</td>
      <td><span class="badge ${type}">${type.toUpperCase() || "-"}</span></td>
      <td>${imageCell}</td>
    `;
    fragment.appendChild(row);
  });

  eventsTable.appendChild(fragment);
}

async function loadDashboard() {
  statusText.textContent = "Actualizando...";
  try {
    const [stats, eventsPayload] = await Promise.all([
      fetchJson("/api/stats"),
      fetchJson("/api/events/25"),
    ]);
    renderStats(stats);
    renderEvents(eventsPayload.events || []);
    statusText.textContent = "Datos actualizados";
  } catch (error) {
    statusText.textContent = "Error cargando datos";
    console.error(error);
  }
}

refreshBtn.addEventListener("click", loadDashboard);
loadDashboard();
setInterval(loadDashboard, 15000);
