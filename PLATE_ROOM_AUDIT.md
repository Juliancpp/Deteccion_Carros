# Auditoría Técnica — Lectura de Placas y Asignación de Habitaciones

Esta auditoría describe el flujo actual, código reutilizable, duplicaciones, defectos, riesgos y modificaciones necesarias para el endurecimiento del pipeline de OCR y habitaciones del MVP Hotel.

---

## 1. Flujo Actual Real

El sistema opera en tres hilos principales coordinados de forma asíncrona:

1. **MultiCameraCoordinator (Hilo del Programador de Inferencia)**:
   - Captura frames de las cámaras `A1` y `A5` a la tasa de FPS objetivo.
   - Aplica detección YOLO y tracking mediante `ByteTrack` de forma aislada por cámara.
   - Evalúa cruces de línea/corredor. Si se genera una confirmación, publica un `EventCandidate` en la cola del consumidor.
   - Almacena snapshots de referencia de las cámaras en caliente y alimenta un buffer de frames (`TrackFrameBuffer`).

2. **Runner Loop (scripts/run_hotel_mvp.py - Hilo Principal)**:
   - Captura los resultados del coordinador.
   - Alimenta el `TrackFrameBuffer` con cada frame limpio y sus bboxes.
   - Actualiza el `HotelPhase1Analyzer` pasándole los bboxes y el frame anotado.
   - El analizador delega en `DirectionalCorridor` (dirección INWARD/OUTWARD) y `RoomDetector` (votos por zona de habitación).

3. **MultiCameraEventConsumer (Hilo del Consumidor)**:
   - Obtiene el `EventCandidate` de la cola y lo retiene temporalmente en una lista de pendientes.
   - Espera hasta que el track sea finalizado por el analizador, la habitación sea firme (`CONFIRMADA` o `PROBABLE`), o pase el timeout (`room_timeout` de 8.0s).
   - Ejecuta OCR Multiframe:
     - Extrae hasta 5 mejores frames del `TrackFrameBuffer`.
     - De cada frame, genera 3 crops (completo, mitad inferior, mitad inferior central).
     - Ejecuta `PlateReader.read` (EasyOCR con preprocesamiento CLAHE + OTSU).
     - Llama a `vote_plates_multiframe` para normalizar (con corrección de caracteres posicionales AAA0000/AAA000) y calcular el ganador.
   - Clasifica el evento (`classify_event`), guarda evidencia física anotada, guarda metadatos JSON y escribe los resultados en la base de datos `hotel_mvp.db` (`HotelStore`).
   - Envía la solicitud de codificación de clip a `ClipManager`.

---

## 2. Código Reutilizable

- **EasyOCR (app/ocr.py)**: `PlateReader` realiza la lectura en CPU usando preprocesamiento básico (grises, CLAHE, redimensionamiento, umbral de Otsu).
- **Normalización y Corrección (app/hotel/plate_voting.py)**: `correct_plate_characters` maneja mapeo de caracteres comunes por posición (ej. `0` -> `O` en zona de letras, `O` -> `0` en zona numérica).
- **Votación de Placas (app/hotel/plate_voting.py)**: `vote_plates_multiframe` agrupa candidatos por placa corregida y pondera según la confianza y calidad del frame.
- **Corredor Direccional (app/hotel/directional_corridor.py)**: `DirectionalCorridor` proyecta el bottom-center del vehículo sobre un eje normalizado para determinar dirección y consistencia.
- **Base de Datos y Reconstrucción (app/hotel/store.py)**: `HotelStore` maneja el esquema de base de datos, el guardado de eventos y la reconstrucción transaccional de visitas.

---

## 3. Duplicaciones y Redundancias

- **Generación de Eventos Paralelos**: Actualmente, si `VirtualLineDetector` y `PassageCorridorDetector` están activos simultáneamente, ambos emiten eventos para el mismo track, generando registros de base de datos duplicados y contradictorios. Para el MVP Hotel, la ruta unificada del `DirectionalCorridor` y `Phase1Analyzer` debe ser la única fuente de verdad para la creación de eventos.
- **Mapeo de BBox en Evidencias**: Tanto `HotelEventWorker._save_evidence` como `MultiCameraEventConsumer._process_resolved_candidate` implementan lógica similar de dibujo de paneles informativos y rectángulos de evidencia, lo que debería centralizarse.

---

## 4. Defectos y Vulnerabilidades Críticas

### A. Placas
1. **Falso Consenso Multiframe (Vulnerabilidad de Voto)**:
   - `_crops()` extrae 3 recortes de **un mismo frame**. Si EasyOCR lee la misma placa en 2 o 3 de estos recortes, se añaden al acumulador de candidatos.
   - `vote_plates_multiframe` considera que el consenso tiene un recuento de 2 o 3 (`best_count >= 2`), y etiqueta el resultado como `LEIDA` con alta confianza. Esto permite que una lectura consistente en un **único frame** gane la categoría `LEIDA` de manera fraudulenta, violando el principio multiframe (que exige coincidencia entre frames temporales diferentes).
2. **Ausencia de Localizador de Placa**:
   - Actualmente se asume que la placa está en la mitad inferior o central inferior del vehículo. No existe localización real por gradientes, bordes o contornos, lo que degrada la precisión cuando la placa está desplazada o el ángulo de la cámara varía.
3. **OCR en Hilo de Control Secuencial**:
   - Aunque se ejecuta en el hilo de consumidor, un volumen alto de recortes por procesar (hasta 15 por vehículo) puede saturar la cola del consumidor si las llamadas a EasyOCR toman más del tiempo entre vehículos.

### B. Habitaciones
1. **Falsos Positivos de Paso Frontal (Crucial)**:
   - `RoomDetector` solo cuenta observaciones dentro de zonas. Si un vehículo transita lentamente por la vía principal (corredor) y cruza tangencialmente el polígono de una habitación por 4 frames, es clasificado automáticamente como `CONFIRMADA`.
   - No hay análisis de trayectoria final, velocidad, permanencia o desaparición del track dentro de la zona.
2. **Origen de Salidas Incierto**:
   - Si un track se inicia en medio del corredor común, el sistema asume la habitación más cercana o la primera detectada, inventando datos de origen sin una aparición clara dentro del umbral de la habitación.
3. **Superposición de Polígonos**:
   - Si los polígonos configurados en `.env` se superponen o cubren parte de la vía de circulación, el detector no puede discriminar a cuál pertenece, asignando arbitrariamente la de mayor conteo.

---

## 5. Riesgos del Pipeline

- **Fragmentación de Track ID**: Si ByteTrack pierde el ID del vehículo por oclusión o cambio de iluminación y genera un nuevo ID, se producirán dos eventos inconexos (ej. una entrada sin habitación y otra con habitación), rompiendo la coherencia de la visita en SQLite.
- **Lentitud de EasyOCR en CPU**: Procesar variantes de múltiples imágenes con EasyOCR sin consensus temprano puede causar cuellos de botella de hasta 1-2 segundos por vehículo, ralentizando el hilo consumidor.
- **Falta de Evidencia Visual Suficiente**: Intentar leer placas en condiciones subóptimas (baja resolución, desenfoque de movimiento, sobreexposición nocturna) genera lecturas erróneas que se introducen al votador.

---

## 6. Datos que Faltan en el Pipeline Actual

- **Identificación de Calidad Óptica**: Faltan métricas de nitidez (Laplaciano), brillo, contraste, y tamaño relativo del bbox de la placa en píxeles.
- **Fusión de Tracks Fragmentados**: Falta un componente (`track_fragment_linker.py`) que enlace de manera conservadora tracks secuenciales con características idénticas.
- **Desglose de Confianza de Habitación**: El cálculo actual es una proporción simple de conteos. Se requiere una puntuación multifactorial que considere:
  - Número de observaciones.
  - Dirección del movimiento.
  - Posición final/profundidad dentro del polígono.
  - Desaparición del track dentro de la zona.
- **Auditoría de Configuración y Polígonos**: No existe una herramienta para superponer gráficamente los polígonos de habitaciones y el corredor sobre el stream real para calibración visual.

---

## 7. Modificaciones Necesarias (Plan de Endurecimiento)

### Fase 1: Calidad Óptica e Inspección de Placas
- Crear `inspect_plate_visibility.py` para medir y evaluar la visibilidad y características ópticas de la placa/vehículo.
- Extender `TrackFrameBuffer` para asociar cada frame con su timestamp, bounding box y un score de calidad robusto basado en nitidez, área, contraste y penalizaciones de borde.

### Fase 2: Localización y Preprocesamiento Avanzado de Placas
- Implementar en `PlateReader` una búsqueda por niveles:
  - Región inferior del vehículo.
  - Región inferior-central.
  - Localización CPU ligera (bordes/contornos rectangulares con relación de aspecto de placas de Ecuador).
  - Crop completo del vehículo como fallback.
- Aplicar variantes de preprocesamiento (Original ampliado, Escala de Grises, CLAHE, Threshold adaptativo, Otsu) limitadas a un máximo de candidatos y variantes (`5 frames x 3 crops x 3 variantes`).

### Fase 3: Normalización y Votación Multiframe Estricta
- Modificar `correct_plate_characters` para aplicar mapeos de caracteres estrictos en base a la posición alfabética/numérica.
- Asegurar que la votación agrupe candidatos y cuente **frames físicos únicos** que respaldan cada lectura, no el total de cultivos.
- Exigir al menos 2 frames distintos que coincidan para clasificar como `LEIDA`.

### Fase 4: Enlazador de Tracks Fragmentados
- Desarrollar `track_fragment_linker.py` para fusionar de forma segura tracks de la misma cámara y dirección con un espacio temporal corto (<2.0s) y distancia espacial compatible.

### Fase 5: Detector de Habitaciones con Trayectorias
- Reescribir `RoomDetector` para calificar habitaciones usando un score multifactorial (observaciones en zona, dirección compatible, profundidad, desaparición, penalización por ambigüedad espacial).
- Asegurar que los vehículos que pasen frente a una puerta sin detenerse o desaparecer queden como `NO_DETERMINADA` o `PROBABLE`, nunca `CONFIRMADA`.
- Implementar validación de origen en salidas (`OUTWARD`).

### Fase 6: Unificación de Eventos y Persistencia Auditada
- Configurar el coordinador y el consumidor para usar exclusivamente `DirectionalCorridor + Phase1Analyzer` como la única fuente de verdad para la creación de eventos.
- Guardar de forma inmutable los candidatos de placas crudos y el breakdown de puntuaciones de la habitación elegida en la base de datos para auditorías técnicas.

---

## 8. Pruebas Reales Pendientes (Ground Truth)

Para validar el endurecimiento del sistema, se requiere:
1. **Dataset de Validación**: Crear un conjunto de imágenes/clips reales de entrada y salida etiquetados con la placa verdadera y la habitación de destino/origen en `data/validation/`.
2. **Evaluación de Precisión**: Diseñar scripts de evaluación automática (`evaluate_plate_accuracy.py` y `evaluate_room_accuracy.py`) para calcular métricas como `false_plate_rate` y `false_confirmed_room_rate`.
3. **Prueba de Campo con Protocolo**: Ejecutar un protocolo de campo estructurado (`PLATE_ROOM_FIELD_TEST.md`) con al menos 5 vehículos en diferentes condiciones y trayectorias controladas por habitación.
