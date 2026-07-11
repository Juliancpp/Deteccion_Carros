"""
Deteccion_Carros — Tracker (Seguimiento de objetos)
====================================================
Gestiona los IDs y el estado de los vehículos en pantalla,
exponiendo las detecciones en una estructura limpia y estandarizada.
"""

from typing import Dict, List, Any


class VehicleTracker:
    """
    Gestiona los IDs asignados por ByteTrack y mantiene
    el estado de los vehículos que están activos en pantalla.
    """

    def __init__(self):
        # Almacena el historial de vehículos activos en el frame actual
        self.active_vehicles: Dict[int, Dict[str, Any]] = {}

    def reset(self) -> None:
        """Limpia el estado de IDs activos mantenido por el tracker local."""
        self.active_vehicles.clear()

    def update(self, results) -> List[Dict[str, Any]]:
        """
        Procesa el output de model.track() de YOLO y actualiza
        los vehículos activos.

        Args:
            results: Lista de objetos Results devueltos por YOLO.

        Returns:
            Lista de diccionarios de vehículos detectados en el frame actual:
            [
                {
                    "id": 5,
                    "class": "car",
                    "confidence": 0.91,
                    "bbox": [x1, y1, x2, y2]
                },
                ...
            ]
        """
        current_frame_vehicles = []
        self.active_vehicles.clear()  # Limpiar para este frame

        for result in results:
            if result.boxes is None or len(result.boxes) == 0:
                continue

            boxes = result.boxes
            for i in range(len(boxes)):
                # Obtener bounding box
                x1, y1, x2, y2 = boxes.xyxy[i].cpu().numpy().astype(int)

                # Clase y confianza
                class_id = int(boxes.cls[i].cpu().numpy())
                confidence = float(boxes.conf[i].cpu().numpy())
                
                # Importar nombres de clase de config para homogeneizar
                from config import CLASS_NAMES
                class_name = CLASS_NAMES.get(class_id, "Desconocido")

                # Obtener ID asignado por el Tracker.
                # Si no hay ID de tracking (por ejemplo, primer frame o no tracked),
                # asignamos None.
                track_id = None
                if boxes.id is not None:
                    track_id = int(boxes.id[i].cpu().numpy())

                vehicle_data = {
                    "id": track_id,
                    "class": class_name,
                    "class_id": class_id,
                    "confidence": confidence,
                    "bbox": [int(x1), int(y1), int(x2), int(y2)]
                }

                current_frame_vehicles.append(vehicle_data)

                # Si tiene ID, lo registramos en los vehículos activos de este frame
                if track_id is not None:
                    self.active_vehicles[track_id] = vehicle_data

        return current_frame_vehicles

    def get_active_ids(self) -> List[int]:
        """Devuelve una lista ordenada con los IDs activos en este frame."""
        return sorted(list(self.active_vehicles.keys()))

    def get_vehicle_count(self) -> int:
        """Devuelve el total de vehículos activos en este frame."""
        return len(self.active_vehicles)
