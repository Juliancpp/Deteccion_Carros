# OCR del runner del hotel · Fase C1

## Flujo ejecutado

| Paso | Punto de ejecución | Resultado |
|---|---|---|
| Captura y detección | [RTSPStream._read_loop](app/video/rtsp_stream.py#L288), [MultiCameraCoordinator._process_camera](app/multicamera/coordinator.py#L278) | La última secuencia decodificada alimenta YOLO y el tracker. El cruce crea el evento |
| Selección | [runner](scripts/run_hotel_mvp.py#L75), [TrackFrameBuffer.best](app/hotel/frame_buffer.py#L100) | Se guardan hasta seis frames por track durante 20 s. El consumidor solicita los cinco mejores entre tracks vinculados dentro de una sesión, o usa el frame de cruce |
| Motor | [PlateReader._load_reader](app/ocr.py#L40), [runner](scripts/run_hotel_mvp.py#L100) | EasyOCR en CPU con idioma inglés. Una instancia compartida con el consumidor asíncrono |
| Regiones y reconocimiento | [PlateReader.inspect_frame](app/ocr.py#L61), [locate_plate_candidates](app/ocr.py#L163) | Hasta seis regiones entre vehículo, porciones inferiores y contornos. CLAHE, Otsu y umbral adaptativo. Se conservan las respuestas por variante y los vértices del texto en píxeles del JPEG de revisión |
| Decisión | [MultiCameraEventConsumer._inspect_frames](app/multicamera/event_consumer.py#L650), [vote_plates_multiframe](app/hotel/plate_voting.py#L42) | El camino activo usa observed_only. Cámara, sesión y secuencia identifican la imagen física, con timestamp como respaldo. Se toma un voto por texto y frame y se pondera por confianza y calidad. LEIDA exige coincidencia exacta, al menos dos frames con peso suficiente, score y margen frente a rivales. Las puntuaciones son heurísticas, pendientes de calibración |
| Escritura | [consumidor](app/multicamera/event_consumer.py#L397), [HotelStore.save_event_and_update_visit](app/hotel/store.py#L160) | Decisión, observaciones, diagnóstico y ruta de evidencia en la fila hotelera. La base legacy continúa recibiendo su proyección resumida |
| Revisión | [detalle autenticado](app/api.py#L368), [openDetail](web/app.js#L65) | Permite inspeccionar original, confianza del motor, variante y región sobre la imagen; el editor conserva la corrección por separado |

Los consumidores históricos de `PlateReader.read` conservan el contrato de lectura completa. El runner del servicio usa el consumidor multicámara. La función de votación conserva su modo anterior para las interfaces históricas; al reutilizarla en C2 usar el modo de observación del runner.

## Contrato persistido

* `plate_candidates` guarda el texto original, texto normalizado, tipo de formato, confianza OCR, calidad, variante, recorte, polígono y referencia al frame. El recorte se almacena una vez por fotograma con lecturas, hasta cinco imágenes por evento. Los JPEG conservan los píxeles del vehículo sin dibujar texto encima. Las fotos y clips nuevos usan UUID en el nombre. Se aplica el árbol de medios del proceso de retención existente. Una evidencia puede haber caducado cuando se revisa su evento.
* `ocr_diagnostics_json` es una columna aditiva nullable. Contiene estado técnico, métricas, frames y evidencia seleccionada. Los estados técnicos son OK, MOTOR_NO_DISPONIBLE, ERROR_OCR y SIN_CANDIDATO. Este último indica que faltó una región procesable. Un error parcial puede coexistir con lecturas útiles de otra variante. El estado global da prioridad a los errores.
* `plate_status` conserva NO_LEGIBLE, PROBABLE y LEIDA e incorpora PARCIAL. El texto parcial queda en el diagnóstico y la columna de placa completa queda vacía. Una lectura de formato completo a baja confianza puede mostrarse como PROBABLE, con revisión. El consenso no rellena caracteres desconocidos ni traduce letras a números.
* `plate_confidence` es el score de decisión. Los campos de soporte, segunda opción, margen y motivos explican la decisión. El original puede revisar varias veces la misma placa en distintos recortes de un solo frame; esas lecturas comparten un voto.
* La corrección administrativa persistida en `event_plate_corrections` alimenta `verification_status=CORREGIDA_MANUALMENTE`. La decisión OCR original sigue disponible. Las filas previas sin diagnóstico siguen siendo consultables. Las futuras importaciones legacy con texto se marcan PROBABLE y requieren revisión; este cambio no reinterpreta filas ya importadas.
* El estado del runner ofrece detecciones del último frame, tracks activos, cruces, publicaciones y descartes por cámara. `ocr_session_counts` acumula eventos, frames, regiones, invocaciones reales, fallos y decisiones del consumidor. Las métricas temporales de selección, localización, preprocesamiento, motor, evidencia OCR y voto figuran en el diagnóstico. La persistencia emite su duración en el log. El contador histórico `ocr_completed` suma decisiones completas probables y leídas. La exactitud solo puede calcularse frente a etiquetas independientes.

## Pesos utilizados en Fedora

EasyOCR 1.7.2 y PyTorch 2.13.0+cpu. La descarga local se hizo con el lector oficial en `.venv/easyocr`. Coincidieron los MD5 del catálogo incluido en EasyOCR y se calcularon los siguientes SHA-256. Para repetir con esa caché, exportar `EASYOCR_MODULE_PATH="$PWD/.venv/easyocr"`.

| Archivo | Origen | SHA-256 local |
|---|---|---|
| craft_mlt_25k.pth | [Release de EasyOCR](https://github.com/JaidedAI/EasyOCR/releases/tag/pre-v1.1.6) | 4a5efbfb48b4081100544e75e1e2b57f8de3d84f213004b14b85fd4b3748db17 |
| english_g2.pth | [Release de EasyOCR](https://github.com/JaidedAI/EasyOCR/releases/tag/v1.3) | e2272681d9d67a04e2dff396b6e95077bc19001f8f6d3593c307b9852e1c29e8 |
| yolo11n.pt | Detector vehicular ya presente en models. Hash del archivo disponible en Fedora | 0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1 |

Los proyectos de EasyOCR y CRAFT publican licencias Apache-2.0 y MIT, respectivamente. Las páginas de proyecto no establecen por separado las condiciones de cada archivo de pesos. Los pesos quedaron privados en la PC. YOLO11 se documenta bajo AGPL-3.0. No se encontró un detector entrenado específicamente para matrículas entre los modelos del proyecto. [EasyOCR](https://github.com/JaidedAI/EasyOCR) · [CRAFT](https://github.com/clovaai/CRAFT-pytorch) · [YOLO11](https://docs.ultralytics.com/models/yolo11/)

## Comparación reproducible

Usar un directorio privado bajo data/validation y un proceso separado por variante. Una fila de manifest.csv representa un vehículo. Columnas: `sample_id,true_plate,plate_visible,camera_id`. Usar un identificador opaco, 1/0 para visibilidad y referencia vacía cuando no hay transcripción fiable. Incluir vehículos omitidos por el detector y negativos; etiquetar antes de examinar las predicciones.

1. Para el ensayo de OCR, usar un frames.csv con `sample_id,image_path,plate_bbox`. Cada ruta apunta a un recorte local autorizado relativo al CSV. Hasta cinco frames distintos del mismo vehículo, idénticos para ambas versiones. La caja opcional contiene cuatro coordenadas x1 y1 x2 y2 en píxeles del recorte. Para medir reconocimiento aislado usar recortes ajustados a la placa; para evaluar las propuestas usar recortes de vehículo. Registrar el tipo de entrada en el nombre del ensayo.
2. Para el pipeline completo, reproducir videos locales en una copia desechable del checkout con base nueva. Configurar todas las cámaras habilitadas con CAMERA_A1_VIDEO_PATH o CAMERA_A5_VIDEO_PATH a **archivos locales** y VIDEO_LOOP=false para cada una. Usar la misma geometría, fragmentos y duración en ambas versiones. Limitar la duración a la primera reproducción del clip, pues el stream intenta reconectar tras EOF. Conservar el resultado SQLite, el estado y los tiempos del proceso por separado. Para cada ejecución, anotar event_id junto al vehículo en su copia del manifiesto mediante cámara, instante y evidencia. Dejarlo vacío si se perdió el vehículo. El evaluador cuenta omisiones y eventos sobrantes y evita usar una placa coincidente como identidad.
3. El evaluador admite también predicciones externas `sample_id,plate_text,plate_status,plate_iou,wall_ms,cpu_ms,rss_peak_mib` para un detector especializado con procedencia documentada. Exige una fila por vehículo. Los campos de medición son opcionales. Usar IoU cero si hay caja anotada pero ninguna propuesta, vacío cuando no hay anotación. El adaptador SQLite puntúa decisiones y cobertura del replay. Para medir IoU y recursos del pipeline completo hay que proporcionar esas mediciones en el CSV.

Desde la raíz, con la caché de pesos ya preparada y los nombres de salida aún libres:

```sh
source .venv/cpu/bin/activate
export EASYOCR_MODULE_PATH="$PWD/.venv/easyocr" OMP_NUM_THREADS=2
baseline=$(mktemp -d .venv/ocr-original.XXXXXXXX)
git archive 05d1207 app | tar -x -C "$baseline"
datos=data/validation/plates
python scripts/benchmark_ocr_crops.py --checkout "$baseline" --frames "$datos/frames.csv" --output "$datos/original.csv"
python scripts/benchmark_ocr_crops.py --checkout . --frames "$datos/frames.csv" --output "$datos/c1.csv"
for version in original c1; do
  python scripts/evaluate_plate_accuracy.py --scope recorte --pipeline "$version" --manifest "$datos/manifest.csv" --predictions "$datos/$version.csv"
done
python scripts/evaluate_plate_accuracy.py --scope completo --pipeline c1 --manifest "$datos/replay-c1.csv" --replay-db "$datos/replay-c1.db"
```

La tasa de visibilidad usa todos los vehículos. La localización usa IoU ≥ 0,5 respecto de las cajas anotadas. Exactitud y parciales usan vehículos con transcripción. Un parcial acertado es una subcadena propia, contigua y no vacía. CER suma inserciones, eliminaciones y sustituciones y divide por caracteres de referencia. Los falsos positivos de lectura sobre placas invisibles y las confirmaciones LEIDA incorrectas se informan por separado. Un denominador vacío produce null. El tiempo de recortes incluye localización, lectura y votación; excluye carga del modelo, decodificación y puntuación contra anotaciones. El pico RSS corresponde a todo el proceso.

## Validación y límites

Se ejecutaron 237 pruebas, incluidas las 219 de la línea base, en una copia aislada. El JUnit local quedó en `.venv/c1/suite.xml`. Se probó el detalle con Chromium, la persistencia de estados con SQLite y las variantes de lectura con un motor controlado.

En una prueba de funcionamiento con EasyOCR real se generaron dos imágenes de 320 × 80 con texto impreso y un desplazamiento horizontal. Un proceso por versión, un único vehículo sintético y dos hilos OpenMP. Ambas versiones recuperaron el texto de prueba. El tiempo de pared fue 2,800 s para la base y 2,830 s para C1; CPU 5,908 s y 5,964 s; pico RSS 918,2 y 918,0 MiB. En otra ejecución individual de C1 se midieron 3 regiones, 9 llamadas, 7 ms de localización, 39 ms de preprocesamiento y 1.480 ms de motor. Esta muestra no caracteriza la exactitud o el rendimiento de las cámaras del hotel.

Quedan pendientes imágenes autorizadas representativas de A1/A5 con variación de iluminación, velocidad, ángulo y distancia. La resolución efectiva de la placa, desenfoque, reflejos, saturación y cobertura deben medirse en esas imágenes. Para C2 se dispone del evento, cámara, tiempo, identidad lógica de track, decisión, revisión y soporte visual. La asociación entre visitas sigue requiriendo reglas temporales y evidencia adicional.
