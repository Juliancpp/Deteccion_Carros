// DOM Elements
const detectorStatus = document.getElementById("detectorStatus");
const statEntradas = document.getElementById("statEntradas");
const statSalidas = document.getElementById("statSalidas");
const statVisitasActivas = document.getElementById("statVisitasActivas");
const statRevision = document.getElementById("statRevision");
const statOcr = document.getElementById("statOcr");

const camA1_state = document.getElementById("camA1_state");
const camA1_img = document.getElementById("camA1_img");
const camA1_cap = document.getElementById("camA1_cap");
const camA1_inf = document.getElementById("camA1_inf");
const camA1_tracks = document.getElementById("camA1_tracks");
const camA1_plate = document.getElementById("camA1_plate");

const cam5_state = document.getElementById("camA5_state");
const camA5_img = document.getElementById("camA5_img");
const camA5_cap = document.getElementById("camA5_cap");
const camA5_inf = document.getElementById("camA5_inf");
const camA5_tracks = document.getElementById("camA5_tracks");
const camA5_plate = document.getElementById("camA5_plate");

const filterForm = document.getElementById("filterForm");
const eventsTable = document.getElementById("eventsTable");
const eventsMeta = document.getElementById("eventsMeta");
const pageIndicator = document.getElementById("pageIndicator");
const btnPrevPage = document.getElementById("btnPrevPage");
const btnNextPage = document.getElementById("btnNextPage");

const btnRebuild = document.getElementById("btnRebuild");
const btnExport = document.getElementById("btnExport");
const refreshBtn = document.getElementById("refreshBtn");
const logoutBtn = document.getElementById("logoutBtn");
const userGreeting = document.getElementById("userGreeting");
const camA1_overlay = document.getElementById("camA1_overlay");
const camA5_overlay = document.getElementById("camA5_overlay");

// Modals
const correctionModal = document.getElementById("correctionModal");
const btnCloseModal = document.getElementById("btnCloseModal");
const btnCancelCorrection = document.getElementById("btnCancelCorrection");
const correctionForm = document.getElementById("correctionForm");

const videoModal = document.getElementById("videoModal");
const btnCloseVideoModal = document.getElementById("btnCloseVideoModal");
const evidenceVideo = document.getElementById("evidenceVideo");
const videoModalTitle = document.getElementById("videoModalTitle");

// Pagination State
let currentPage = 1;
const pageSize = 15;
let csrfToken = sessionStorage.getItem("csrfToken") || "";
const snapshotState = {
  A1: { loading: false, objectUrl: "", lastOkAt: null, backoffMs: 2000 },
  A5: { loading: false, objectUrl: "", lastOkAt: null, backoffMs: 2000 }
};

// Helper to fetch JSON
async function apiCall(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (["POST", "PATCH", "DELETE"].includes(String(options.method || "GET").toUpperCase()) && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const res = await fetch(url, {...options, headers, credentials: "same-origin"});
  if (res.status === 401 && !url.startsWith("/api/auth/")) {
    window.location.href = "/login";
    throw new Error("Sesión requerida");
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `HTTP Error ${res.status}`);
  }
  return res.json();
}

async function loadMe() {
  try {
    const me = await apiCall("/api/auth/me");
    csrfToken = me.csrf_token || csrfToken;
    if (csrfToken) sessionStorage.setItem("csrfToken", csrfToken);
    userGreeting.textContent = me.username ? `Hola, ${me.username}` : "Sesión activa";
  } catch (err) {
    window.location.href = "/login";
  }
}

// Format Datetime
function formatDateTime(isoString) {
  if (!isoString) return "-";
  const date = new Date(isoString);
  if (isNaN(date.getTime())) return isoString;
  
  return date.toLocaleString("es-EC", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit"
  });
}

// Render Table Events
function renderEventsTable(events) {
  eventsTable.innerHTML = "";
  if (!events || events.length === 0) {
    eventsTable.innerHTML = `<tr><td colspan="8" class="empty">No se encontraron eventos coincidentes.</td></tr>`;
    return;
  }

  events.forEach(ev => {
    const row = document.createElement("tr");
    
    // Status text & colors
    const plateClass = (ev.plate_status || "").toLowerCase();
    const roomClass = (ev.room_status || "").toLowerCase();
    const typeClass = (ev.event_type || "").toLowerCase();

    // Plate formatting
    const plateText = ev.plate || "NO_DETECTADA";
    const plateBadge = `<span class="plate-txt">${plateText}</span><span class="plate-status-indicator ${plateClass}">${ev.plate_status}</span>`;

    // Room formatting
    const roomNum = ev.room_number ? `#${ev.room_number}` : "NO_DET";
    const roomBadge = `<span class="room-txt">${roomNum}</span><span class="room-status-indicator">${ev.room_status}</span>`;

    // Badges & alerts
    const movementBadge = `<span class="badge-tipo ${typeClass}">${ev.event_type}</span>`;
    const reviewWarning = ev.review_required ? `<span class="review-warning">⚠ Requiere Revisión</span>` : ``;

    // Media cells
    let evidenceCells = "";
    if (ev.evidence_url) {
      evidenceCells += `<a href="${ev.evidence_url}" target="_blank" class="evidence-btn">📷 Foto</a>`;
    } else {
      evidenceCells += `<span class="empty-text">Sin foto</span> `;
    }

    if (ev.clip_status === "READY" && ev.clip_url) {
      evidenceCells += `<a class="evidence-btn" onclick="playVideo('${ev.clip_url}', '${ev.event_id}')">🎥 Video</a>`;
    } else if (ev.clip_status === "PENDING") {
      evidenceCells += `<span class="empty-text">⏳ Video...</span>`;
    } else if (ev.clip_status === "FAILED") {
      evidenceCells += `<span class="empty-text" title="${ev.clip_error || ''}">❌ Falló</span>`;
    } else {
      evidenceCells += `<span class="empty-text">No clip</span>`;
    }

    // Actions
    const actionButtons = `
      <button type="button" class="btn-correct" onclick="openCorrection('${ev.event_id}')">Corregir</button>
      ${ev.review_required ? `<button type="button" class="btn-resolve" onclick="resolveEvent('${ev.event_id}')">Resolver</button>` : ""}
    `;

    row.innerHTML = `
      <td>${formatDateTime(ev.occurred_at)}</td>
      <td><strong>${ev.camera_id}</strong></td>
      <td>${movementBadge}</td>
      <td>${plateBadge}</td>
      <td>${roomBadge}</td>
      <td>
        <span style="font-size:11px; color:#d1d5db;">${ev.notes || "-"}</span>
        ${reviewWarning}
      </td>
      <td>${evidenceCells}</td>
      <td>${actionButtons}</td>
    `;
    eventsTable.appendChild(row);
  });
}

// Fetch and load events list
async function loadEvents() {
  const plate = document.getElementById("filterPlate").value;
  const camera = document.getElementById("filterCamera").value;
  const type = document.getElementById("filterType").value;
  const room = document.getElementById("filterRoom").value;
  const plateStatus = document.getElementById("filterPlateStatus").value;
  const review = document.getElementById("filterReview").value;
  const dateFrom = document.getElementById("filterDateFrom").value;
  const dateTo = document.getElementById("filterDateTo").value;

  let query = `/api/hotel/events?page=${currentPage}&page_size=${pageSize}`;
  if (plate) query += `&plate=${encodeURIComponent(plate)}`;
  if (camera) query += `&camera=${encodeURIComponent(camera)}`;
  if (type) query += `&event_type=${encodeURIComponent(type)}`;
  if (room) query += `&room=${encodeURIComponent(room)}`;
  if (plateStatus) query += `&plate_status=${encodeURIComponent(plateStatus)}`;
  if (review) query += `&review_required=${encodeURIComponent(review)}`;
  if (dateFrom) query += `&date_from=${encodeURIComponent(dateFrom)}`;
  if (dateTo) query += `&date_to=${encodeURIComponent(dateTo)}`;

  try {
    const res = await apiCall(query);
    renderEventsTable(res.events);
    
    // Pagination controls
    eventsMeta.textContent = `Total: ${res.total} registros encontrados.`;
    pageIndicator.textContent = `Página ${res.page} de ${res.pages || 1}`;
    btnPrevPage.disabled = res.page <= 1;
    btnNextPage.disabled = res.page >= res.pages;
  } catch (err) {
    eventsMeta.textContent = "Error al cargar el historial.";
    console.error(err);
  }
}

// Fetch stats summary
async function loadStats() {
  try {
    const stats = await apiCall("/api/hotel/summary");
    statEntradas.textContent = stats.entradas_hoy;
    statSalidas.textContent = stats.salidas_hoy;
    statVisitasActivas.textContent = stats.visits_open;
    statRevision.textContent = stats.review_pending;
    statOcr.textContent = stats.ocr_completed;
  } catch (err) {
    console.error("Error fetching summary stats:", err);
  }
}

// Fetch detector runtime status and snapshots
async function loadStatus() {
  try {
    const statusInfo = await apiCall("/api/hotel/status");
    
    if (statusInfo.running) {
      detectorStatus.textContent = "● Detector Online";
      detectorStatus.className = "badge-detector online";
    } else {
      detectorStatus.textContent = "○ Detector Offline";
      detectorStatus.className = "badge-detector offline";
    }

    const cams = statusInfo.cameras || {};
    
    // Update A1
    if (cams.A1) {
      camA1_state.textContent = cams.A1.connected ? "Conectado" : "Desconectado";
      camA1_state.className = `camera-badge ${cams.A1.connected ? 'connected' : ''}`;
      camA1_cap.textContent = `${cams.A1.capture_fps} FPS`;
      camA1_inf.textContent = `${cams.A1.inference_fps} FPS`;
      camA1_tracks.textContent = cams.A1.active_tracks;
      camA1_plate.textContent = cams.A1.last_plate ? `${cams.A1.last_plate} (Hab: ${cams.A1.last_room || '?'})` : "-";
      
    } else {
      camA1_state.textContent = "Desconectado";
      camA1_state.className = "camera-badge";
    }

    // Update A5
    if (cams.A5) {
      cam5_state.textContent = cams.A5.connected ? "Conectado" : "Desconectado";
      cam5_state.className = `camera-badge ${cams.A5.connected ? 'connected' : ''}`;
      camA5_cap.textContent = `${cams.A5.capture_fps} FPS`;
      camA5_inf.textContent = `${cams.A5.inference_fps} FPS`;
      camA5_tracks.textContent = cams.A5.active_tracks;
      camA5_plate.textContent = cams.A5.last_plate ? `${cams.A5.last_plate} (Hab: ${cams.A5.last_room || '?'})` : "-";
      
    } else {
      cam5_state.textContent = "Desconectado";
      cam5_state.className = "camera-badge";
    }
  } catch (err) {
    detectorStatus.textContent = "○ Servidor Inaccesible";
    detectorStatus.className = "badge-detector offline";
  }
}

async function refreshSnapshot(cameraId, imgEl, overlayEl) {
  const state = snapshotState[cameraId];
  if (state.loading) return;
  state.loading = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 4000);
  try {
    const res = await fetch(`/api/hotel/cameras/${cameraId}/snapshot?ts=${Date.now()}`, {
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const type = res.headers.get("Content-Type") || "";
    if (!type.includes("image/jpeg")) throw new Error("Respuesta no JPEG");
    const blob = await res.blob();
    if (!blob.size) throw new Error("JPEG vacío");
    const url = URL.createObjectURL(blob);
    const oldUrl = state.objectUrl;
    imgEl.src = url;
    state.objectUrl = url;
    if (oldUrl) URL.revokeObjectURL(oldUrl);
    state.lastOkAt = new Date();
    state.backoffMs = 2000;
    overlayEl.textContent = "EN LÍNEA";
    overlayEl.className = "snapshot-overlay ok";
  } catch (err) {
    const hasImage = Boolean(state.lastOkAt);
    overlayEl.textContent = hasImage ? `IMAGEN DESACTUALIZADA · ${state.lastOkAt.toLocaleTimeString("es-EC")}` : "SIN IMAGEN";
    overlayEl.className = hasImage ? "snapshot-overlay warn" : "snapshot-overlay";
    state.backoffMs = Math.min(state.backoffMs * 1.5, 10000);
  } finally {
    clearTimeout(timeout);
    state.loading = false;
  }
}

function refreshSnapshots() {
  refreshSnapshot("A1", camA1_img, camA1_overlay);
  refreshSnapshot("A5", camA5_img, camA5_overlay);
}

// play video in modal
function playVideo(url, eventId) {
  evidenceVideo.src = url;
  videoModalTitle.textContent = `Reproducción: ${eventId}`;
  videoModal.classList.remove("hidden");
  evidenceVideo.play().catch(() => {});
}

// close video modal
function closeVideoModal() {
  evidenceVideo.pause();
  evidenceVideo.src = "";
  videoModal.classList.add("hidden");
}

// Open correction modal
async function openCorrection(eventId) {
  try {
    const ev = await apiCall(`/api/hotel/events/${eventId}`);
    
    document.getElementById("modalEventId").value = ev.event_id;
    document.getElementById("modalPlate").value = ev.plate || "";
    document.getElementById("modalPlateStatus").value = ev.plate_status || "LEIDA";
    document.getElementById("modalRoom").value = ev.room_number || "";
    document.getElementById("modalRoomStatus").value = ev.room_status || "CONFIRMADA";
    document.getElementById("modalEventType").value = ev.event_type;
    document.getElementById("modalReviewRequired").checked = !!ev.review_required;
    document.getElementById("modalNotes").value = ev.notes || "";

    // Load audit logs inside modal
    const logsContainer = document.getElementById("modalAuditLogs");
    logsContainer.innerHTML = "";
    if (ev.audit_logs && ev.audit_logs.length > 0) {
      ev.audit_logs.forEach(log => {
        const item = document.createElement("div");
        item.className = "audit-log-item";
        item.innerHTML = `
          <span>[${formatDateTime(log.changed_at)}] ${log.changed_by}:</span>
          Campo <strong>${log.field_name}</strong> modificado de "<em>${log.old_value || ''}</em>" a "<em>${log.new_value || ''}</em>"
          ${log.notes ? `<br><small style="color:var(--muted)">Nota: ${log.notes}</small>` : ''}
        `;
        logsContainer.appendChild(item);
      });
    } else {
      logsContainer.innerHTML = `<span class="empty-text">Sin auditorías registradas.</span>`;
    }

    correctionModal.classList.remove("hidden");
  } catch (err) {
    alert("Error al cargar detalles de la corrección: " + err.message);
  }
}

// Close correction modal
function closeCorrectionModal() {
  correctionModal.classList.add("hidden");
}

// Submit manual correction form
correctionForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const eventId = document.getElementById("modalEventId").value;
  const plate = document.getElementById("modalPlate").value.trim().toUpperCase();
  const plateStatus = document.getElementById("modalPlateStatus").value;
  const room = document.getElementById("modalRoom").value;
  const roomStatus = document.getElementById("modalRoomStatus").value;
  const eventType = document.getElementById("modalEventType").value;
  const reviewRequired = document.getElementById("modalReviewRequired").checked ? 1 : 0;
  const correctionNotes = document.getElementById("modalNotes").value.trim();

  const payload = {
    plate,
    plate_status: plateStatus,
    room_number: room ? parseInt(room) : null,
    room_status: roomStatus,
    event_type: eventType,
    review_required: reviewRequired,
    correction_notes: correctionNotes,
    notes: correctionNotes
  };

  try {
    await apiCall(`/api/hotel/events/${eventId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    
    closeCorrectionModal();
    // Refresh page data
    loadEvents();
    loadStats();
  } catch (err) {
    alert("Error al guardar cambios: " + err.message);
  }
});

// Resolve event quickly
async function resolveEvent(eventId) {
  if (!confirm("¿Está seguro de marcar este evento como Resuelto / OK?")) return;
  try {
    await apiCall(`/api/hotel/events/${eventId}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ notes: "Marcado como verificado por el dashboard." })
    });
    loadEvents();
    loadStats();
  } catch (err) {
    alert("Error al resolver: " + err.message);
  }
}

// Rebuild vehicle visits history
btnRebuild.addEventListener("click", async () => {
  if (!confirm("Esto eliminará la historia actual de Visitas y la reconstruirá cronológicamente. ¿Desea continuar?")) return;
  btnRebuild.disabled = true;
  btnRebuild.textContent = "Reconstruyendo...";
  try {
    const res = await apiCall("/api/hotel/rebuild-visits", { method: "POST" });
    alert(res.message);
    loadEvents();
    loadStats();
  } catch (err) {
    alert("Error al reconstruir: " + err.message);
  } finally {
    btnRebuild.disabled = false;
    btnRebuild.textContent = "Reconstruir Visitas";
  }
});

// Trigger Excel Report Export
btnExport.addEventListener("click", async () => {
  btnExport.disabled = true;
  btnExport.textContent = "Generando...";
  try {
    const res = await apiCall("/api/hotel/export", { method: "POST" });
    // Force browser download
    window.location.href = res.path;
  } catch (err) {
    alert("Fallo al exportar reporte Excel: " + err.message);
  } finally {
    btnExport.disabled = false;
    btnExport.textContent = "Exportar Excel";
  }
});

logoutBtn.addEventListener("click", async () => {
  try {
    await apiCall("/api/auth/logout", { method: "POST" });
  } finally {
    sessionStorage.removeItem("csrfToken");
    window.location.href = "/login";
  }
});

// Filter submission
filterForm.addEventListener("submit", (e) => {
  e.preventDefault();
  currentPage = 1;
  loadEvents();
});

// Refresh button
refreshBtn.addEventListener("click", () => {
  loadEvents();
  loadStats();
  loadStatus();
});

// Paging Controls
btnPrevPage.addEventListener("click", () => {
  if (currentPage > 1) {
    currentPage--;
    loadEvents();
  }
});

btnNextPage.addEventListener("click", () => {
  currentPage++;
  loadEvents();
});

// Close buttons listeners
btnCloseModal.addEventListener("click", closeCorrectionModal);
btnCancelCorrection.addEventListener("click", closeCorrectionModal);
btnCloseVideoModal.addEventListener("click", closeVideoModal);

// Initial Load
loadMe().then(() => {
  loadEvents();
  loadStats();
  loadStatus();
  refreshSnapshots();
});

// Polling routines
setInterval(loadStatus, 1500); // 1.5s real-time camera stats & status
setInterval(loadStats, 5000);  // 5s statistics summary
setInterval(refreshSnapshots, 2000);
