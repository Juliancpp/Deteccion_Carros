'use strict';
const $ = (id) => document.getElementById(id);
const all = (selector) => [...document.querySelectorAll(selector)];
const api = '/api/hotel';
const cases = api + '/visit-cases';
const categories = {
  visits_completed: 'Visitas completadas',
  entries_unpaired: 'Entradas sin salida',
  exits_unpaired: 'Salidas sin entrada',
  review_pending: 'Revisión pendiente',
};
const states = {
  ACTIVA: 'Activa',
  HABITACION_CONFIRMADA: 'Habitación confirmada',
  COMPLETADA: 'Completada',
  SALIDA_HUERFANA: 'Salida sin entrada',
  SIN_RESOLVER: 'Sin resolver',
  CIERRE_EXCEPCIONAL: 'Cierre excepcional',
  HABITACION_SIN_ASIGNAR: 'Habitación sin asignar',
};
const titles = {
  control: 'Control vehicular',
  historial: 'Historial',
  reportes: 'Reportes',
  camaras: 'Cámaras',
  configuracion: 'Configuración',
};
let session,
  route,
  category = '',
  page = 1,
  pageSize = 20,
  filters = {},
  sequence = 0,
  statsSequence = 0;
const pendingCommands = new Map();
let detailSequence = 0;
let currentEdit, selectedEntry, settings, downloadUrl, initialized;
var pollingTimers = [];
const message = (value) => {
  $('operationMessage').textContent = value;
};
function node(tag, text, props = {}) {
  const item = document.createElement(tag);
  if (text != null) item.textContent = text;
  return Object.assign(item, props);
}
function button(text, handler, props) {
  const b = node('button', text, { type: 'button', ...props });
  b.addEventListener('click', () => perform(b, handler));
  return b;
}
async function perform(control, task) {
  control.disabled = true;
  try {
    await task();
  } catch (error) {
    message(error.message);
  } finally {
    control.disabled = false;
  }
}
function localTime(value) {
  if (value == null) return '—';
  if (
    typeof value === 'string' &&
    /^\d{4}-\d\d-\d\d[T ]\d\d:\d\d/.test(value) &&
    !/(Z|[+-]\d\d:?\d\d)$/.test(value)
  )
    value += '-05:00';
  const date = new Date(value);
  return isNaN(date)
    ? '—'
    : date.toLocaleString('es-EC', { timeZone: 'America/Guayaquil', dateStyle: 'short', timeStyle: 'short' });
}
function currentMonth() {
  const parts = new Intl.DateTimeFormat('en', {
    timeZone: 'America/Guayaquil',
    year: 'numeric',
    month: '2-digit',
  }).formatToParts(new Date());
  return parts.find((x) => x.type === 'year').value + '-' + parts.find((x) => x.type === 'month').value;
}
function login() {
  pollingTimers.splice(0).forEach(clearInterval);
  location.replace('/login');
}
async function mutate(path, method, body) {
  const signature = JSON.stringify([path, method, body]);
  if (!pendingCommands.has(signature))
    pendingCommands.set(
      signature,
      Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, '0')).join(''),
    );
  const result = await request(path, method, body, false, pendingCommands.get(signature));
  pendingCommands.delete(signature);
  return result;
}
async function request(path, method = 'GET', body, raw = false, key) {
  const headers = new Headers();
  if (method !== 'GET') headers.set('X-CSRF-Token', session?.csrf_token || '');
  if (body !== undefined) headers.set('Content-Type', 'application/json');
  if (key) headers.set('Idempotency-Key', key);
  const res = await fetch(path, {
    method,
    headers,
    body: JSON.stringify(body),
    credentials: 'same-origin',
    cache: 'no-store',
  });
  if (res.status === 401) {
    login();
    throw Error('La sesión ha caducado');
  }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw Error(
      typeof data.detail === 'string'
        ? data.detail
        : 'La operación falló (HTTP ' + res.status + '). Comprueba los datos e intenta nuevamente.',
    );
  }
  return raw ? res : res.json();
}
function formValues(form) {
  return Object.fromEntries([...new FormData(form)].filter(([, v]) => v !== ''));
}
function period() {
  return $('monthPicker').value;
}
function selectMonth(v) {
  $('monthPicker').value = v;
  $('historyMonth').value = v;
}
function show(name, visible) {
  $(name).hidden = !visible;
}
function navigate() {
  route = location.hash.slice(1);
  if (!Object.hasOwn(titles, route)) route = 'control';
  $('title').textContent = titles[route];
  all('nav a').forEach((a) => {
    if (a.hash === '#' + route) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  show('overview', route === 'control');
  show('history', ['historial', 'control'].includes(route));
  show('filterForm', route === 'historial');
  show('historyLink', route === 'control');
  if (route === 'control' && (category || Object.keys(filters).length)) {
    resetFilters();
    loadHistory();
  }
  show('reports', route === 'reportes');
  show('cameras', route === 'camaras');
  show('settings', route === 'configuracion');
  if (route === 'configuracion' && settings) loadSettings().catch((e) => message(e.message));
}
function resetFilters() {
  filters = {};
  category = '';
  page = 1;
  $('filterForm').reset();
  $('historyMonth').value = period();
}
function setCategory(key) {
  resetFilters();
  category = key;
  location.hash = 'historial';
  loadHistory();
}
function cell(row, text) {
  const td = node('td', text);
  row.append(td);
  return td;
}
function renderRow(v) {
  const row = node('tr'),
    first = cell(row, localTime(v.instant * 1000));
  first.append(
    node(
      'small',
      v.source === 'legacy_eventos'
        ? 'Histórico importado'
        : v.kind === 'room_review'
          ? 'Observación de habitación'
          : 'Registro del sistema',
    ),
  );
  cell(row, [...new Set([v.camera_id, v.exit_camera].filter(Boolean))].join(' / ') || '—');
  cell(row, v.plate || 'Sin placa');
  cell(row, v.room_number ?? '—');
  const state = cell(row, states[v.state] || v.state);
  if (v.pending) state.append(node('small', 'Revisión pendiente', { className: 'warn' }));
  const actions = cell(row);
  const detail = button('Ver detalle', () => openDetail(v.id));
  detail.dataset.detail = v.id;
  actions.append(detail);
  if (v.kind === 'room_review') return row;
  actions.append(button('Editar placa', () => openEditor(v, 'plate')));
  if (v.kind === 'entry' && !v.exceptional_reason) {
    if (v.association_id) actions.append(button('Desasociar', () => unlink(v)));
    else if ($('associationModal')) actions.append(button('Asociar salida', () => openAssociation(v)));
  }
  return row;
}
async function loadHistory() {
  const seq = ++sequence;
  const params = new URLSearchParams({ ...filters, page, page_size: pageSize });
  if (category) params.set('category', category);
  if (period()) params.set('month', period());
  $('listTitle').textContent = categories[category] || 'Historial de visitas';
  $('eventsMeta').textContent = 'Consultando visitas…';
  try {
    const data = await request(cases + '?' + params);
    if (sequence !== seq) return;
    const pages = Math.max(1, Math.ceil(data.total / pageSize));
    if (page > pages) {
      page = pages;
      return loadHistory();
    }
    $('eventsTable').replaceChildren(...data.visits.map(renderRow));
    if (!data.total) {
      const row = node('tr');
      cell(row, 'Sin resultados con estos filtros.').colSpan = 6;
      $('eventsTable').append(row);
    }
    $('eventsMeta').textContent = data.total + (data.total===1?' registro':' registros');
    $('pageIndicator').textContent = page + ' / ' + pages;
    $('btnPrevPage').disabled = page === 1;
    $('btnNextPage').disabled = page >= pages;
  } catch (e) {
    if (seq === sequence) {
      $('eventsTable').replaceChildren();
      $('eventsMeta').textContent = e.message;
    }
  }
}
async function loadStats() {
  const seq = ++statsSequence;
  try {
    const counts = await request(cases + '/indicators' + (period() ? '?month=' + period() : ''));
    if (seq === statsSequence)
      all('[data-category]').forEach(
        (b) => (b.querySelector('strong').textContent = counts[b.dataset.category]),
      );
  } catch (e) {
    if (seq === statsSequence) {
      all('.cards strong').forEach((n) => (n.textContent = '—'));
      message(e.message);
    }
  }
}
const refresh = () => Promise.all([loadHistory(), loadStats()]);
function closeDialog(dialog) {
  dialog.querySelectorAll('video').forEach((v) => v.pause());
  dialog.close();
}
async function openEditor(v, field) {
  currentEdit = { v, field };
  const plate = field === 'plate';
  $('editorTitle').textContent = plate ? 'Editar placa' : 'Corregir habitación';
  $('modalPlateOriginal').textContent = v.plate_original || 'Sin placa';
  $('editLabel').textContent = plate ? 'Placa corregida' : 'Habitación corregida';
  const input = $('modalPlate');
  input.type = plate ? 'text' : 'number';
  input.min = '1';
  input.maxLength = 64;
  input.value = (plate ? v.plate : v.room_number) ?? '';
  $('correctionMessage').textContent = '';
  if ($('correctionModal').open) closeDialog($('correctionModal'));
  $('editor').showModal();
  input.focus();
}
async function saveCorrection(e) {
  e.preventDefault();
  const { v, field } = currentEdit;
  await perform($('btnSaveCorrection'), async () => {
    try {
      const val = $('modalPlate').value;
      await mutate(
        cases + '/' + encodeURIComponent(v.id) + '/' + field,
        'PATCH',
        field === 'plate' ? { plate: val } : { room_number: Number(val) },
      );
      $('correctionMessage').textContent = '✓ Corrección guardada';
      message('Corrección guardada.');
      closeDialog($('editor'));
      await refresh();
    } catch (err) {
      $('correctionMessage').textContent = err.message;
    }
  });
}
async function openAssociation(v) {
  selectedEntry = v;
  $('selectedEntry').textContent = 'Entrada ' + v.id + ' · ' + (v.plate || 'Sin placa');
  $('associationMessage').textContent = 'Buscando salidas posteriores…';
  $('availableExits').replaceChildren();
  $('confirmAssociation').disabled = true;
  $('associationModal').showModal();
  try {
    await appendExits(v, 1);
  } catch (e) {
    $('associationMessage').textContent = e.message;
  }
}
async function appendExits(v, pageNumber) {
  try {
    const data = await request(
      cases + '/' + encodeURIComponent(v.id) + '/available-exits?page=' + pageNumber,
    );
    if (selectedEntry !== v) return;
    data.exits.forEach((ev) => {
      const label = node('label'),
        radio = node('input', null, { type: 'radio', name: 'exit_event_id', value: ev.id, required: true });
      radio.onchange = () => ($('confirmAssociation').disabled = false);
      label.append(
        radio,
        document.createTextNode(
          [ev.id, ev.plate || 'Sin placa', ev.camera_id, localTime(ev.instant * 1000)].join(' · '),
        ),
      );
      $('availableExits').append(label);
    });
    if (data.more) {
      const next = button('Más salidas', async () => {
        next.remove();
        await appendExits(v, pageNumber + 1);
      });
      $('availableExits').append(next);
    }
    $('associationMessage').textContent = data.exits.length
      ? 'Confirma la salida correspondiente.'
      : 'Sin salidas posteriores disponibles.';
  } catch (e) {
    $('associationMessage').textContent = e.message;
  }
}
async function associate(e) {
  e.preventDefault();
  await perform($('confirmAssociation'), async () => {
    try {
      await mutate(api + '/visits/associate', 'POST', {
        entry_event_id: selectedEntry.id,
        exit_event_id: new FormData(e.target).get('exit_event_id'),
      });
      closeDialog($('associationModal'));
      message('Visita completada.');
      await refresh();
    } catch (err) {
      $('associationMessage').textContent = err.message;
    }
  });
}
async function unlink(v) {
  if (!confirm('¿Desasociar la salida y devolver los dos casos a revisión?')) return;
  await mutate(api + '/visits/' + v.association_id + '/association', 'DELETE', undefined);
  message('Salida desasociada.');
  await refresh();
}
function evidencePreview(frame, readings) {
  const panel = document.createElement('div');
  const select = document.createElement('select');
  select.setAttribute('aria-label', 'Hipótesis OCR para inspeccionar');
  for (const reading of readings) {
    select.add(new Option(`${reading.raw_text} · ${reading.variant_type}`));
  }
  const canvas = document.createElement('canvas');
  canvas.style.maxWidth = '100%';
  const photo = new Image();
  const draw = () => {
    if (!photo.naturalWidth) return;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(photo, 0, 0);
    const points = readings[select.selectedIndex]?.text_polygon;
    if (!points?.length) return;
    ctx.beginPath();
    ctx.moveTo(...points[0]);
    for (const point of points.slice(1)) ctx.lineTo(...point);
    ctx.closePath();
    ctx.strokeStyle = '#ffbf00';
    ctx.lineWidth = 2;
    ctx.stroke();
  };
  photo.onload = () => {
    canvas.width = photo.naturalWidth;
    canvas.height = photo.naturalHeight;
    draw();
  };
  photo.onerror = () => {
    panel.textContent = 'Fotograma no disponible.';
  };
  select.addEventListener('change', draw);
  panel.append(select, canvas);
  photo.src = frame.evidence_url;
  return panel;
}
function addMedia(panel, url, type) {
  if (!url) {
    panel.append(node('p', type === 'video' ? 'Sin clip registrado.' : 'Sin fotografía registrada.'));
    return;
  }
  const media = node(type === 'video' ? 'video' : 'img');
  if (type === 'video') {
    media.controls = true;
    media.preload = 'metadata';
  } else media.alt = 'Fotografía del evento';
  media.onerror = () => media.replaceWith(node('p', 'Archivo no disponible o formato incompatible.'));
  media.src = url;
  panel.append(media);
  if (type !== 'video')
    panel.append(node('a', 'Abrir fotografía', { href: url, target: '_blank', rel: 'noopener' }));
}
function eventPanel(ev) {
  const panel = node('article');
  panel.append(
    node('h3', ev.event_type),
    node('p', [ev.event_id, ev.camera_id, localTime(ev.occurred_at)].join(' · ')),
    node(
      'p',
      'OCR · ' + (ev.verification_status || 'Sin lectura') + ' · ' + (ev.plate_original || 'Sin placa'),
    ),
  );
  addMedia(panel, ev.evidence_url, 'image');
  addMedia(panel, ev.clip_url, 'video');
  let readings = [];
  try {
    readings = JSON.parse(ev.plate_candidates || '[]');
  } catch {}
  for (const frame of ev.ocr_diagnostics?.frames || []) {
    const options = Array.isArray(readings) ? readings.filter((r) => r.frame_id === frame.frame_id) : [];
    if (frame.evidence_url) {
      panel.append(
        node('a', 'Abrir fotograma OCR', { href: frame.evidence_url, target: '_blank', rel: 'noopener' }),
        evidencePreview(frame, options),
      );
    }
    for (const r of options)
      panel.append(node('p', [r.raw_text, r.variant_type].filter(Boolean).join(' · ')));
  }
  return panel;
}
async function openDetail(key) {
  const version = ++detailSequence;
  const dialog = $('correctionModal'),
    panel = $('modalAuditLogs');
  if (!dialog.open) dialog.showModal();
  panel.textContent = 'Cargando visita…';
  try {
    const v = await request(cases + '/' + encodeURIComponent(key));
    if (version !== detailSequence || !dialog.open) return;
    panel.replaceChildren(node('h3', v.id), node('p', states[v.state] || v.state));
    const notice = node('p', '', { role: 'status' });
    panel.append(notice);
    async function action(path, body, question) {
      if (question && !confirm(question)) return;
      try {
        await mutate(cases + '/' + encodeURIComponent(key) + '/' + path, 'POST', body);
        await refresh();
        await openDetail(key);
      } catch (e) {
        notice.textContent = e.message;
      }
    }
    if (v.events) {
      panel.append(
        node('p', 'Entrada · ' + localTime(v.entry_at)),
        node('p', 'Salida · ' + localTime(v.exit_at)),
        node('p', 'Habitación · ' + (v.room_number ?? 'Sin asignación')),
        node('p', 'Placa · ' + (v.plate || 'Sin placa') + ' · ' + (v.identification_status || 'Sin lectura')),
        node(
          'p',
          'Asociación · ' +
            (v.association_origin || 'Sin vínculo') +
            (v.association_confidence != null ? ' · confianza registrada ' + v.association_confidence : ''),
        ),
        node('p', v.pending ? 'Revisión pendiente' : 'Sin revisión pendiente'),
      );
      panel.append(
        button(v.pending ? 'Resolver revisión' : 'Marcar revisión', () =>
          action(v.pending ? 'review' : 'flag', undefined, '¿Confirmar cambio de revisión?'),
        ),
      );
      if (v.kind === 'entry' && !v.exceptional_reason) {
        panel.append(button('Corregir habitación', () => openEditor(v, 'room')));
        if (!v.exit_event_id && session.user.role === 'admin')
          panel.append(
            button('Cierre excepcional', () => {
              const reason = prompt('Indica el motivo del cierre excepcional');
              if (reason?.trim())
                return action(
                  'exceptional-close',
                  { reason: reason.trim() },
                  '¿Cerrar sin salida vinculada?',
                );
            }),
          );
      }
      panel.append(node('h3', 'Eventos técnicos'), ...v.events.map(eventPanel));
    }
    if (v.rooms?.length) panel.append(node('h3', 'Observaciones de habitación'));
    for (const r of v.rooms || []) {
      panel.append(
        node(
          'p',
          'Habitación ' +
            (r.number ?? 'indeterminada') +
            ' · ' +
            localTime(r.instant * 1000) +
            ' · ' +
            r.origin,
        ),
      );
      if (r.evidence_url) addMedia(panel, r.evidence_url, 'image');
      if (!v.events)
        panel.append(
          button('Asignar a entrada', async () => {
            const entry = prompt('ID de la visita de entrada');
            if (!entry?.trim() || !confirm('¿Vincular la observación a esta visita?')) return;
            try {
              await mutate(
                cases + '/' + encodeURIComponent(entry.trim()) + '/room-observations/' + r.id,
                'POST',
                undefined,
              );
              await refresh();
              await openDetail(entry.trim());
            } catch (e) {
              notice.textContent = e.message;
            }
          }),
        );
    }
    if (v.events) {
      panel.append(node('h3', 'Historial de modificaciones'));
      for (const entry of v.history || []) {
        let changes = {};
        try {
          changes = JSON.parse(entry.detail || '{}');
        } catch {}
        const explanation = Object.entries(changes)
          .map(([key, value]) => key + ' = ' + value)
          .join(', ');
        panel.append(
          node('p', [entry.action, entry.actor, entry.recorded_at, explanation].filter(Boolean).join(' · ')),
        );
      }
      for (const c of v.corrections || [])
        panel.append(node('p', [c.action, c.old_value, c.new_value, c.created_at].join(' · ')));
    }
  } catch (e) {
    if (version === detailSequence) panel.textContent = e.message;
  }
}
function buildCameras() {
  for (const cam of ['A1', 'A5']) {
    const article = node('article'),
      snapshot = node('div', null, { className: 'snapshot' });
    snapshot.append(
      node('img', null, { id: 'cam' + cam + '_img', alt: 'Última imagen ' + cam }),
      node('span', 'SIN IMAGEN', { id: 'cam' + cam + '_overlay' }),
    );
    article.append(
      node('h3', 'Cámara ' + cam),
      node('p', 'Consultando…', { id: 'cam' + cam + '_state' }),
      snapshot,
      node('div', null, { id: 'info' + cam }),
    );
    $('cameraGrid').append(article);
  }
}
async function loadStatus() {
  try {
    const status = await request(api + '/monitor');
    $('detectorStatus').textContent = status.running ? '● Detector Online' : '○ Detector sin señal reciente';
    for (const [cam, c] of Object.entries(status.cameras)) {
      $('cam' + cam + '_state').textContent = c.connected ? 'Conectado' : 'Sin conexión confirmada';
      const metrics = [
        ['Actualización', localTime(status.updated_at ? status.updated_at * 1000 : null)],
        ['Última imagen', localTime(c.snapshot.updated_at)],
        ['Captura', c.capture_fps == null ? 'Sin dato' : c.capture_fps + ' FPS'],
        ['Inferencia', c.inference_fps == null ? 'Sin dato' : c.inference_fps + ' FPS'],
        [
          'Geometría',
          c.geometry_configured == null
            ? 'Sin dato'
            : c.geometry_configured
              ? (c.geometry_features?.join(', ') || 'Configurada')
              : 'Sin configurar',
        ],
        [
          'Errores',
          c.error ? 'Error de captura reportado' : status.running ? 'Sin error reportado' : 'Sin telemetría',
        ],
        [
          'Último resultado OCR',
          c.last_reading
            ? (c.last_reading.engine_status || 'Motor sin dato') +
              ' · ' +
              c.last_reading.plate_status +
              ' · ' +
              localTime(c.last_reading.occurred_at)
            : 'Sin lectura registrada',
        ],
        [
          'OCR de la sesión (ambas cámaras)',
          status.ocr_counts
            ? Object.entries(status.ocr_counts)
                .map(([k, v]) => k + ' ' + v)
                .join(' · ') || 'Sin invocaciones'
            : 'Sin telemetría',
        ],
      ];
      $('info' + cam).replaceChildren(...metrics.map(([k, v]) => node('p', k + ' · ' + v)));
    }
  } catch (e) {
    $('detectorStatus').textContent = 'Telemetría no disponible';
    message(e.message);
    for (const cam of ['A1', 'A5']) $('cam' + cam + '_state').textContent = 'Sin conexión confirmada';
  }
}
async function refreshSnapshot(cam) {
  const overlay = $('cam' + cam + '_overlay'),
    img = $('cam' + cam + '_img');
  try {
    const response = await request(
      api + '/cameras/' + cam + '/snapshot?allow_stale=true&ts=' + Date.now(),
      'GET',
      undefined,
      true,
    );
    const old = img.src;
    img.src = URL.createObjectURL(await response.blob());
    if (old.startsWith('blob:')) URL.revokeObjectURL(old);
    overlay.textContent =
      response.headers.get('X-Snapshot-Stale') === 'true' ? 'Imagen desactualizada' : 'Última captura';
  } catch (e) {
    overlay.textContent = img.hasAttribute('src') ? 'Imagen desactualizada' : 'SIN IMAGEN';
  }
}
function discardReport() {
  if (downloadUrl) URL.revokeObjectURL(downloadUrl);
  downloadUrl = null;
  $('download').removeAttribute('href');
  show('download', false);
  $('reportStatus').textContent = '';
}
let reportVersion = 0;
async function report(event) {
  event.preventDefault();
  const version = ++reportVersion;
  discardReport();
  $('reportStatus').textContent = 'Generando archivo…';
  await perform(event.submitter, async () => {
    try {
      const res = await request(api + '/reports/visits', 'POST', formValues($('reportForm')), true);
      const blob = await res.blob();
      if (version !== reportVersion) return;
      downloadUrl = URL.createObjectURL(blob);
      $('download').href = downloadUrl;
      show('download', true);
      $('reportStatus').textContent = res.headers.get('X-Visit-Count') + ' registros exportados.';
    } catch (e) {
      if (version === reportVersion) $('reportStatus').textContent = e.message;
    }
  });
}
let settingsVersion = 0;
async function loadSettings() {
  const version = ++settingsVersion;
  $('pageSize').disabled = true;
  $('settingsForm').querySelector('button').hidden = true;
  const response = await request(api + '/display-settings');
  if (version !== settingsVersion) return;
  settings = response;
  pageSize = settings.page_size;
  $('readOnlySettings').textContent =
    'Asociación automática · estancia máxima ' +
    settings.association_seconds +
    ' s · continuidad de track ' +
    settings.track_seconds +
    ' s (solo lectura).';
  $('pageSize').value = pageSize;
  $('pageSize').disabled = !settings.editable;
  $('settingsForm').querySelector('button').hidden = !settings.editable;
  $('settingStatus').textContent = settings.editable
    ? 'Cambios registrados en auditoría.'
    : 'Solo lectura para tu rol.';
  $('revisions').replaceChildren();
  for (const r of settings.history) {
    const line = node(
      'p',
      'Revisión ' + r.id + ' · ' + r.page_size + ' visitas por página · ' + r.changed_at + ' UTC ',
    );
    if (settings.editable && r.page_size !== pageSize)
      line.append(button('Restaurar valor', () => saveSize(r.page_size)));
    $('revisions').append(line);
  }
  if (settings.editable && pageSize !== 20)
    $('revisions').append(button('Restaurar valor inicial (20)', () => saveSize(20)));
}
async function saveSize(size) {
  if (!confirm('¿Aplicar esta preferencia para los operadores?')) return;
  try {
    await request(api + '/display-settings', 'PATCH', { page_size: size, revision: settings.revision });
    await loadSettings();
    page = 1;
    await loadHistory();
    $('settingStatus').textContent = 'Preferencia guardada.';
  } catch (err) {
    $('settingStatus').textContent = err.message;
  }
}
function bindUI() {
  all('.states').forEach((s) => {
    s.add(new Option('Todos', ''));
    Object.entries(states).forEach(([k, v]) => s.add(new Option(v, k)));
  });
  selectMonth(currentMonth());
  $('reportForm').elements.month.value = period();
  all('[data-category]').forEach((b) => (b.onclick = () => setCategory(b.dataset.category)));
  $('monthPicker').onchange = () => {
    selectMonth(period());
    page = 1;
    refresh();
  };
  $('filterForm').onsubmit = (e) => {
    e.preventDefault();
    selectMonth($('historyMonth').value);
    filters = formValues(e.target);
    page = 1;
    refresh();
  };
  $('clearFilters').onclick = () => {
    resetFilters();
    selectMonth('');
    refresh();
  };
  $('btnPrevPage').onclick = () => {
    if (page > 1) page--;
    loadHistory();
  };
  $('btnNextPage').onclick = () => {
    page++;
    loadHistory();
  };
  $('associationForm').onsubmit = associate;
  $('correctionForm').onsubmit = saveCorrection;
  all('[data-close]').forEach((b) => (b.onclick = () => closeDialog(b.closest('dialog'))));
  $('reportForm').onsubmit = report;
  $('reportForm').oninput = () => {
    reportVersion++;
    discardReport();
  };
  $('settingsForm').onsubmit = (e) => {
    e.preventDefault();
    perform(e.submitter, () => saveSize(Number($('pageSize').value)));
  };
  $('logoutBtn').onclick = () =>
    perform($('logoutBtn'), async () => {
      await request('/api/auth/logout', 'POST');
      login();
    });
  window.addEventListener('hashchange', navigate);
  window.addEventListener('pagehide', () => {
    discardReport();
    for (const cam of ['A1', 'A5']) URL.revokeObjectURL($('cam' + cam + '_img').src);
  });
}
function startPolling() {
  if (pollingTimers.length) return;
  pollingTimers.push(
    setInterval(loadStatus, 10000),
    setInterval(loadStats, 15000),
    setInterval(() => {
      if (!document.querySelector('dialog[open]')) loadHistory();
    }, 20000),
    setInterval(() => {
      if (route === 'camaras') for (const cam of ['A1', 'A5']) refreshSnapshot(cam);
    }, 5000),
  );
}
function initializeDashboard() {
  return (initialized ??= (async () => {
    session = await request('/api/auth/me');
    $('userGreeting').textContent = session.user.username;
    if (!$('associationModal')) console.error('Asociación: faltan elementos DOM: associationModal');
    buildCameras();
    bindUI();
    navigate();
    await loadSettings().catch((e) => message(e.message));
    await Promise.all([refresh(), loadStatus(), refreshSnapshot('A1'), refreshSnapshot('A5')]);
    startPolling();
  })().catch((e) => message(e.message)));
}
initializeDashboard();
