# Guía de Despliegue en Ubuntu Server — Hotel MVP

Esta guía explica paso a paso cómo desplegar el pipeline detector y el dashboard en un servidor con Ubuntu Server 22.04 o superior.

## Requisitos Previos

Asegúrate de instalar los paquetes del sistema necesarios:
```bash
sudo apt update
sudo apt install -y python3-pip python3-venv ffmpeg nginx sqlite3
```

---

## 1. Verificación del Entorno (Server Doctor)

Antes de activar los servicios, ejecuta el script de diagnóstico para verificar que el hardware y las dependencias del sistema estén listos:
```bash
./venv/bin/python scripts/server_doctor.py
```
El script validará:
- Versión de Python (mínimo 3.10)
- Disponibilidad de la utilidad `ffmpeg`
- Funcionamiento de base de datos SQLite y WAL
- Soporte CUDA (si dispone de GPU)
- Permisos de escritura en las carpetas `data/captures`, `data/clips`, `data/runtime` y `data/reportes`

---

## 2. Configuración de Variables de Entorno

Edita el archivo `.env` del repositorio para configurar la autenticación del dashboard y otros parámetros:
```ini
DASHBOARD_AUTH_ENABLED=true
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=tu_contraseña_segura
RETENTION_DAYS=15
```

---

## 3. Instalación de Servicios Systemd

El repositorio incluye plantillas de servicios en la carpeta `deploy/systemd/`. 

Sigue estos pasos para instalarlos:

1. **Reemplazar marcadores**:
   En cada archivo `.service`, reemplaza:
   - `REPLACE_USER` con tu nombre de usuario del sistema (ej. `julian`).
   - `REPLACE_WORKING_DIR` con la ruta absoluta del repositorio (ej. `/home/julian/repos/relax_julian/Deteccion_Carros`).

2. **Copiar archivos a systemd**:
   ```bash
   sudo cp deploy/systemd/deteccion-*.service /etc/systemd/system/
   sudo cp deploy/systemd/deteccion-*.timer /etc/systemd/system/
   ```

3. **Recargar systemd y habilitar servicios**:
   ```bash
   sudo systemctl daemon-reload
   
   # Habilitar e iniciar el pipeline detector
   sudo systemctl enable --now deteccion-carros.service
   
   # Habilitar e iniciar el dashboard web
   sudo systemctl enable --now deteccion-dashboard.service
   
   # Habilitar e iniciar la purga diaria automatizada (3:00 AM)
   sudo systemctl enable --now deteccion-retention.timer
   ```

4. **Monitorear los servicios**:
   ```bash
   sudo systemctl status deteccion-carros.service
   sudo systemctl status deteccion-dashboard.service
   sudo systemctl list-timers --all
   ```
   Para ver registros en tiempo real:
   ```bash
   journalctl -u deteccion-carros.service -f -n 100
   ```

---

## 4. Servidor Web Nginx (Proxy Inverso)

Para permitir acceso externo seguro a través del puerto 80 estándar:

1. Edita `deploy/nginx/deteccion-carros.conf.example` reemplazando `REPLACE_WORKING_DIR` con la ruta del repositorio.
2. Copia la configuración a Nginx:
   ```bash
   sudo cp deploy/nginx/deteccion-carros.conf.example /etc/nginx/sites-available/deteccion-carros
   sudo ln -s /etc/nginx/sites-available/deteccion-carros /etc/nginx/sites-enabled/
   ```
3. Remueve el sitio default de Nginx (si aplica) y reinicia el servicio:
   ```bash
   sudo rm -f /etc/nginx/sites-enabled/default
   sudo nginx -t && sudo systemctl restart nginx
   ```

Ahora el dashboard estará disponible en la dirección IP de tu servidor o dominio.
