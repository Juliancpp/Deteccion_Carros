"""Editor local de sesiones de calibración. Tk se importa solo al abrir una ventana."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import cv2
from PIL import Image, ImageTk
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from app.hotel.geometry import Feature, Layout, LINE_KINDS
from app.hotel.geometry_overlay import draw_features
from app.settings import load_settings
from scripts.calibrate_hotel_zones import _capture_frame
from scripts.calibrate_geometry import export_review

ROOT = Path(__file__).resolve().parents[1]
SESSIONS = ROOT / "data/runtime/calibraciones"
ROOMS = {"A1": (1, 2, 3), "A5": (4, 5, 6)}
NAMES = {"hotel_entry": "Entrada", "hotel_exit": "Salida", "room": "Habitación", "approach": "Aproximación"}


class Countdown:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.left = 300
        self.deadline = None

    @property
    def remaining(self):
        return self.left if self.deadline is None else max(0, self.deadline - self.clock())

    def pause(self):
        self.left = self.remaining
        self.deadline = None

    def start(self):
        if self.deadline is None and self.left:
            self.deadline = self.clock() + self.left

    def extend(self):
        expired = self.remaining == 0
        running = self.deadline is not None
        self.pause()
        self.left += 300
        if expired or running:
            self.start()


def new_feature(name, kind):
    return dict(id=name, kind=kind, points=[], enabled=kind not in LINE_KINDS, invert_direction=False)


class Drawing:
    def __init__(self, layout):
        self.data = json.loads(json.dumps(asdict(layout)))
        self.figures = self.data["features"]
        for name, kind in [("acceso-entry", "hotel_entry"), ("acceso-exit", "hotel_exit")]:
            if not any(f["kind"] == kind for f in self.figures):
                self.figures.append(new_feature(name, kind))
        for room in ROOMS[layout.camera_id]:
            for name, kind in [(f"hab-{room}", "room"), (f"aprox-{room}", "approach")]:
                if not any(f["id"] == name for f in self.figures):
                    self.figures.append(new_feature(name, kind))
        self.original = deepcopy(self.figures)
        self.history = []
        self.selected = 0

    @property
    def active(self):
        return self.figures[self.selected]

    def remember(self):
        self.history.append((deepcopy(self.figures), self.selected))
        self.history = self.history[-100:]

    def undo(self):
        if self.history:
            self.figures[:], self.selected = self.history.pop()
            return True
        return False

    def restore(self):
        self.remember()
        initial = next((f for f in self.original if f["id"] == self.active["id"]), None)
        self.figures[self.selected] = deepcopy(initial or new_feature(self.active["id"], self.active["kind"]))

    def layout(self):
        # Empty slots in the list are available tools. Partially drawn figures must validate.
        return Layout.parse(
            {**self.data, "features": [f for f in self.figures if f["points"]]}, self.data["camera_id"]
        )


def write_draft(directory, drawing):
    payload = {"data": drawing.data, "original": drawing.original, "selected": drawing.selected}
    with tempfile.NamedTemporaryFile(dir=directory, mode="w", encoding="utf8", delete=False) as temp:
        json.dump(payload, temp, ensure_ascii=False)
        temp.flush()
        os.fsync(temp.fileno())
    os.replace(temp.name, directory / "draft.json")


def read_draft(path, camera):
    payload = json.loads(path.read_text())
    data = payload["data"]
    if data["camera_id"] != camera:
        raise ValueError("El borrador pertenece a otra cámara")
    model = Drawing(Layout(camera, ()))
    for figures in (data["features"], payload["original"]):
        ids = set()
        for f in figures:
            if (
                not isinstance(f["id"], str)
                or not f["id"].strip()
                or f["id"] in ids
                or f["kind"] not in NAMES
            ):
                raise ValueError("Figura inválida en borrador")
            ids.add(f["id"])
            for point in f["points"]:
                if len(point) != 2 or not all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in point):
                    raise ValueError("Coordenadas inválidas en borrador")
    model.data, model.figures, model.original = data, data["features"], payload["original"]
    model.selected = int(payload["selected"])
    if not 0 <= model.selected < len(model.figures):
        raise ValueError("Selección inválida en borrador")
    return model


def save_revision(directory, drawing, frame, destination=None):
    target = destination or directory / datetime.now().strftime("revision-%Y%m%d-%H%M%S-%f")
    return export_review(target, drawing.layout(), frame)


def distance_to_edge(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    t = max(0, min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length)) if length else 0
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


class Editor:
    def __init__(self, window, frame, drawing, directory, output=None):
        self.output = output
        self.win, self.frame, self.model, self.directory = window, frame, drawing, directory
        self.timer = Countdown()
        self.drag = None
        self.expired = False
        self.tick_id = None
        self.solo = tk.BooleanVar(window)
        self.message = tk.StringVar(window, "Selecciona una figura para editarla.")
        camera = drawing.data["camera_id"]
        window.title(f"Hotel Relax · Calibración {camera}")
        window.geometry(
            f"{min(1350, window.winfo_screenwidth() - 50)}x{min(800, window.winfo_screenheight() - 90)}"
        )
        bar = ttk.Frame(window, padding=8)
        bar.pack(fill="x")
        ttk.Label(bar, text=f"Cámara {camera}", font=("Sans", 16, "bold")).pack(side="left")
        self.clock_label = ttk.Label(bar, text="05:00", font=("Monospace", 18))
        self.clock_label.pack(side="left", padx=20)
        self.pause_btn = ttk.Button(bar, text="Pausar", command=self.toggle_time)
        self.pause_btn.pack(side="left")
        ttk.Button(bar, text="+5 minutos", command=self.extend).pack(side="left", padx=3)
        ttk.Button(bar, text="Guardar revisión", command=self.save).pack(side="right")
        panel = ttk.Frame(window, padding=6)
        panel.pack(side="right", fill="y")
        ttk.Label(panel, text="Geometrías").pack(anchor="w")
        self.listbox = tk.Listbox(panel, width=34, height=12, exportselection=False)
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self.select)
        ttk.Checkbutton(panel, text="Ver solo la seleccionada", variable=self.solo, command=self.render).pack(
            anchor="w"
        )
        for caption, action in [
            ("Activar / desactivar", self.toggle),
            ("Invertir flecha", self.invert),
            ("Borrar puntos", self.clear),
            ("Restaurar figura inicial", self.restore),
            ("Deshacer", self.undo),
            ("Añadir grupo de habitaciones", lambda: self.add("room")),
            ("Añadir aproximación", lambda: self.add("approach")),
        ]:
            ttk.Button(panel, text=caption, command=action).pack(fill="x", pady=1)
        ttk.Label(
            panel,
            wraplength=250,
            justify="left",
            text="Clic para dibujar. Arrastra un vértice o el centro de una línea. En un polígono, clic sobre un lado inserta un vértice. Clic derecho en un vértice lo elimina.",
        ).pack(pady=8)
        ttk.Label(window, textvariable=self.message, wraplength=750, padding=6).pack(side="bottom", fill="x")
        self.canvas = tk.Canvas(window, background="#222", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _: self.render())
        self.canvas.bind("<Button-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.motion)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.canvas.bind("<Button-3>", self.remove_vertex)
        window.bind("<Control-z>", lambda _: self.undo())
        window.bind("<Control-s>", lambda _: self.save())
        window.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()
        window.update_idletasks()
        if not window.winfo_viewable():
            window.wait_visibility()
        self.render()
        self.timer.start()
        self.tick()

    def refresh(self):
        self.listbox.delete(0, "end")
        for f in self.model.figures:
            state = "vacía" if not f["points"] else "activa" if f["enabled"] else "apagada"
            self.listbox.insert("end", f"{NAMES[f['kind']]} · {f['id']} · {state}")
        self.listbox.selection_set(self.model.selected)
        self.listbox.see(self.model.selected)
        self.render()

    def select(self, _):
        choices = self.listbox.curselection()
        if choices:
            self.model.selected = choices[0]
            self.render()

    def tick(self):
        seconds = math.ceil(self.timer.remaining)
        self.clock_label.configure(
            text=f"{seconds // 60:02}:{seconds % 60:02}", foreground="#ad3c10" if seconds <= 30 else "#26383d"
        )
        if seconds == 0 and not self.expired:
            self.expired = True
            self.timer.pause()
            self.pause_btn.configure(text="Pausar", state="disabled")
            self.checkpoint("Tiempo agotado. Borrador guardado. Puedes guardar o extender la sesión.")
        elif 0 < seconds <= 30 and not getattr(self, "warned", False):
            self.warned = True
            self.message.set("Quedan 30 segundos. Puedes guardar ahora o extender la sesión.")
        self.tick_id = self.win.after(200, self.tick)

    def toggle_time(self):
        if self.timer.deadline is None:
            self.timer.start()
            self.pause_btn.configure(text="Pausar")
        else:
            self.timer.pause()
            self.pause_btn.configure(text="Reanudar")

    def extend(self):
        self.timer.extend()
        self.expired = self.warned = False
        self.pause_btn.configure(text="Reanudar" if self.timer.deadline is None else "Pausar", state="normal")
        self.message.set("Se añadieron cinco minutos.")

    def checkpoint(self, msg="Borrador guardado."):
        try:
            write_draft(self.directory, self.model)
        except OSError as exc:
            self.message.set(f"No se pudo guardar el borrador: {exc}")
            return False
        self.message.set(msg)
        return True

    def changed(self):
        self.refresh()
        self.checkpoint()

    def render(self):
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if min(cw, ch) < 3:
            return
        h, w = self.frame.shape[:2]
        scale = min(cw / w, ch / h)
        iw, ih = max(2, round(w * scale)), max(2, round(h * scale))
        self.origin = ((cw - iw) // 2, (ch - ih) // 2)
        self.span = (iw - 1, ih - 1)
        view = cv2.resize(self.frame, (iw, ih), interpolation=cv2.INTER_AREA)
        active = self.model.active
        shapes = [] if self.solo.get() else [f for f in self.model.figures if f is not active]
        shapes.append(active)
        complete = []
        for f in shapes:
            pts = f["points"]
            if len(pts) >= (2 if f["kind"] in LINE_KINDS else 3):
                complete.append(Feature(**f))
            else:
                pix = [(round(x * (iw - 1)), round(y * (ih - 1))) for x, y in pts]
                for a, b in zip(pix, pix[1:]):
                    cv2.line(view, a, b, (0, 210, 255), 2)
        draw_features(view, complete, include_disabled=True)
        for x, y in active["points"]:
            xy = (round(x * (iw - 1)), round(y * (ih - 1)))
            cv2.circle(view, xy, 6, (15, 15, 15), -1)
            cv2.circle(view, xy, 4, (255, 255, 255), -1)
        self.photo = ImageTk.PhotoImage(
            Image.fromarray(cv2.cvtColor(view, cv2.COLOR_BGR2RGB)), master=self.win
        )
        self.canvas.delete("all")
        self.canvas.create_image(*self.origin, image=self.photo, anchor="nw")

    def local(self, event, clamp=False):
        x, y = (event.x - self.origin[0]) / self.span[0], (event.y - self.origin[1]) / self.span[1]
        if clamp or (0 <= x <= 1 and 0 <= y <= 1):
            return [max(0, min(1, x)), max(0, min(1, y))]

    def pixel(self, p):
        return [p[0] * self.span[0], p[1] * self.span[1]]

    def hit(self, p):
        points = self.model.active["points"]
        if points:
            distances = [math.dist(self.pixel(p), self.pixel(v)) for v in points]
            if min(distances) <= 10:
                return distances.index(min(distances))

    def press(self, event):
        p = self.local(event)
        if p is None:
            return
        f = self.model.active
        pts = f["points"]
        index = self.hit(p)
        line = f["kind"] in LINE_KINDS
        if index is not None:
            self.drag = (index, p, deepcopy(pts))
            return
        if line and len(pts) == 2:
            if distance_to_edge(self.pixel(p), *map(self.pixel, pts)) <= 10:
                self.drag = ("line", p, deepcopy(pts))
            else:
                self.message.set("Arrastra un extremo o pulsa Borrar puntos para redibujar.")
            return
        self.model.remember()
        insert = len(pts)
        if not line and len(pts) >= 3:
            distances = [
                distance_to_edge(self.pixel(p), self.pixel(a), self.pixel(b))
                for a, b in zip(pts, pts[1:] + pts[:1])
            ]
            if min(distances) <= 10:
                insert = distances.index(min(distances)) + 1
        pts.insert(insert, p)
        self.changed()

    def motion(self, event):
        if self.drag is None:
            return
        i, start, original = self.drag
        p = self.local(event, clamp=True)
        pts = self.model.active["points"]
        if i == "line":
            delta = [
                max(-min(v[k] for v in original), min(1 - max(v[k] for v in original), p[k] - start[k]))
                for k in (0, 1)
            ]
            pts[:] = [[x + delta[0], y + delta[1]] for x, y in original]
        else:
            pts[i] = p
        self.render()

    def release(self, event):
        if self.drag is None:
            return
        _, _, original = self.drag
        self.drag = None
        pts = self.model.active["points"]
        if pts != original:
            updated = deepcopy(pts)
            pts[:] = original
            self.model.remember()
            pts[:] = updated
            self.changed()

    def remove_vertex(self, event):
        p = self.local(event)
        i = self.hit(p) if p is not None else None
        if i is not None and self.model.active["kind"] not in LINE_KINDS:
            self.model.remember()
            self.model.active["points"].pop(i)
            self.changed()

    def toggle(self):
        self.model.remember()
        f = self.model.active
        f["enabled"] = not f["enabled"]
        self.changed()

    def invert(self):
        f = self.model.active
        if f["kind"] in LINE_KINDS:
            self.model.remember()
            f["invert_direction"] = not f["invert_direction"]
            self.changed()

    def restore(self):
        self.model.restore()
        self.changed()

    def clear(self):
        self.model.remember()
        self.model.active["points"] = []
        self.changed()

    def undo(self):
        if self.model.undo():
            self.changed()

    def add(self, kind):
        name = simpledialog.askstring("Nueva figura", "Identificador de la figura", parent=self.win)
        if not name:
            return
        name = name.strip()
        if not name or any(f["id"] == name for f in self.model.figures):
            self.message.set("Introduce un identificador único.")
            return
        self.model.remember()
        self.model.figures.append(new_feature(name, kind))
        self.model.selected = len(self.model.figures) - 1
        self.changed()

    def save(self):
        try:
            dest = save_revision(self.directory, self.model, self.frame, self.output)
            self.output = None
        except (ValueError, OSError, cv2.error) as exc:
            self.message.set(f"Revisa la figura antes de guardar: {exc}")
            return
        self.checkpoint(f"Revisión guardada en {dest}")

    def close(self):
        if not self.checkpoint():
            messagebox.showerror("Borrador pendiente", self.message.get(), parent=self.win)
            return
        self.win.after_cancel(self.tick_id)
        self.win.destroy()


def open_session(parent, camera, frame, directory, config=None, draft=None, edit=None, output=None):
    if draft:
        drawing = read_draft(draft, camera)
    else:
        drawing = Drawing(Layout.load(config or ROOT / f"config/geometry/{camera}.json", camera))
    if edit:
        matches = [i for i, f in enumerate(drawing.figures) if f["id"] == edit["id"]]
        if matches:
            drawing.selected = matches[0]
        elif edit["kind"]:
            drawing.figures.append(new_feature(edit["id"], edit["kind"]))
            drawing.selected = len(drawing.figures) - 1
        else:
            raise ValueError("Una figura nueva requiere --kind")
        if edit["kind"] and edit["kind"] != drawing.active["kind"]:
            drawing.remember()
            drawing.figures[drawing.selected] = new_feature(edit["id"], edit["kind"])
        for key in ("enabled", "invert_direction"):
            if edit[key] is not None:
                drawing.active[key] = edit[key]
    directory.mkdir(parents=True, exist_ok=False)
    if not cv2.imwrite(str(directory / "frame.png"), frame):
        raise OSError("No se pudo conservar el frame")
    write_draft(directory, drawing)
    win = tk.Toplevel(parent)
    return Editor(win, frame, drawing, directory, output)


def desktop(camera=None, image=None, camera_frame=False, config=None, output=None, resume=None, edit=None):
    root = tk.Tk()
    root.title("Hotel Relax · Calibración")
    wrapper = ttk.Frame(root, padding=22)
    wrapper.pack(fill="both", expand=True)
    selected = tk.StringVar(root, value=camera or "A1")
    ttk.Label(wrapper, text="Calibración de cámaras", font=("Sans", 18, "bold")).pack(anchor="w")
    ttk.Label(wrapper, text="Cada sesión dispone de cinco minutos para editar.").pack(anchor="w", pady=6)
    combo = ttk.Combobox(wrapper, values=list(ROOMS), textvariable=selected, state="readonly", width=12)
    combo.pack(anchor="w", pady=4)
    if camera:
        combo.configure(state="disabled")

    def launch(source):
        cam = selected.get()
        draft = None
        try:
            if source == "draft":
                filename = resume or filedialog.askopenfilename(
                    parent=root,
                    initialdir=SESSIONS,
                    title="Recuperar draft.json",
                    filetypes=[("Borrador", "draft.json")],
                )
                if not filename:
                    return
                draft = Path(filename)
                path = draft.parent / "frame.png"
            elif source == "choose":
                filename = filedialog.askopenfilename(
                    parent=root,
                    title=f"Imagen de {cam}",
                    filetypes=[("Imágenes", "*.jpg *.jpeg *.png *.bmp")],
                )
                if not filename:
                    return
                path = Path(filename)
            elif source == "snapshot":
                files = sorted((ROOT / "data/rtsp_test").glob(f"{cam}_*.jpg"))
                if not files:
                    raise ValueError("No hay capturas locales; selecciona una imagen.")
                path = files[-1]
            else:
                path = image
            if source == "live":
                settings = load_settings(override_env=True)
                frame = _capture_frame(settings.dvr, next(c for c in settings.cameras if c.camera_id == cam))
            else:
                frame = cv2.imread(str(path))
                if frame is None:
                    raise ValueError(f"Imagen ilegible: {path}")
            directory = SESSIONS / cam / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            editor = open_session(root, cam, frame, directory, config, draft, edit, output)
            root.withdraw()
            root.wait_window(editor.win)
            if camera and (output or edit or image or camera_frame or resume):
                root.destroy()
            else:
                root.deiconify()
        except (OSError, ValueError, KeyError, TypeError, IndexError, RuntimeError, cv2.error) as exc:
            messagebox.showerror("Calibración", str(exc), parent=root)

    for title, source in [
        ("Abrir captura local de la cámara", "snapshot"),
        ("Elegir imagen…", "choose"),
        ("Capturar frame del DVR", "live"),
        ("Recuperar borrador…", "draft"),
    ]:
        ttk.Button(wrapper, text=title, command=lambda s=source: launch(s)).pack(fill="x", pady=3)
    if camera:
        root.after_idle(
            lambda: launch(
                "draft" if resume else "live" if camera_frame else "image" if image else "snapshot"
            )
        )
    root.mainloop()
    return 0
