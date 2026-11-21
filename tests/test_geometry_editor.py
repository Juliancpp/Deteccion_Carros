import json
import os
import tkinter as tk

import cv2
import numpy as np
import pytest

from scripts.geometry_editor import (
    Countdown,
    Drawing,
    Layout,
    ROOT,
    open_session,
    read_draft,
    save_revision,
    write_draft,
)


@pytest.mark.parametrize("camera,rooms", [("A1", [1, 2, 3]), ("A5", [4, 5, 6])])
def test_session_preserves_calibration_and_unfinished_work(tmp_path, camera, rooms):
    initial = Layout.load(ROOT / f"config/geometry/{camera}.json", camera)
    d = Drawing(initial)
    assert d.layout() == initial
    assert {f["id"] for f in d.figures if f["kind"] == "approach"} == {f"aprox-{i}" for i in rooms}
    d.selected = next(i for i, f in enumerate(d.figures) if f["kind"] == "approach")
    d.remember()
    d.active["points"].append([0.2, 0.3])
    write_draft(tmp_path, d)
    recovered = read_draft(tmp_path / "draft.json", camera)
    assert recovered.active == d.active
    frame = np.zeros((200, 320, 3), np.uint8)
    with pytest.raises(ValueError):
        save_revision(tmp_path, recovered, frame)
    assert not list(tmp_path.glob("revision-*"))
    recovered.restore()
    assert recovered.layout() == initial
    first = save_revision(tmp_path, recovered, frame)
    second = save_revision(tmp_path, recovered, frame)
    assert first != second
    assert Layout.load(first / "geometry.json", camera) == initial
    assert cv2.imread(str(first / "preview.jpg")).any()
    d.undo()
    assert d.layout() == initial


def test_timer_starts_only_after_ready_and_extends_from_pause_or_expiry():
    now = [0.0]
    t = Countdown(lambda: now[0])
    now[0] = 75
    assert t.remaining == 300
    t.start()
    now[0] += 270
    assert t.remaining == 30
    t.pause()
    now[0] += 100
    assert t.remaining == 30
    t.extend()
    assert t.remaining == 330 and t.deadline is None
    t.start()
    now[0] += 400
    assert t.remaining == 0
    t.extend()
    assert t.remaining == 300 and t.deadline is not None


@pytest.mark.skipif(not os.getenv("HOTEL_TEST_GUI"), reason="requiere escritorio Tk")
@pytest.mark.parametrize("camera", ["A1", "A5"])
def test_desktop_gestures_and_expiry(tmp_path, camera):
    root = tk.Tk()
    root.withdraw()
    try:
        session = tmp_path / camera
        e = open_session(root, camera, np.full((320, 640, 3), 70, np.uint8), session)
        root.update()
        assert e.clock_label.cget("text") == "05:00"

        def mouse(event, x, y):
            e.canvas.event_generate(
                event, x=round(e.origin[0] + x * e.span[0]), y=round(e.origin[1] + y * e.span[1])
            )
            root.update()

        e.clear()
        for x in (0.2, 0.8):
            mouse("<Button-1>", x, 0.4)
            mouse("<ButtonRelease-1>", x, 0.4)
        mouse("<Button-1>", 0.5, 0.4)
        mouse("<B1-Motion>", 0.5, 0.6)
        mouse("<ButtonRelease-1>", 0.5, 0.6)
        assert all(abs(y - 0.6) < 0.01 for _, y in e.model.active["points"])
        e.undo()
        assert all(abs(y - 0.4) < 0.01 for _, y in e.model.active["points"])
        e.invert()
        e.toggle()
        e.save()
        assert list(session.glob("revision-*/preview.jpg"))
        i = next(i for i, f in enumerate(e.model.figures) if f["kind"] == "approach")
        e.listbox.selection_clear(0, "end")
        e.listbox.selection_set(i)
        e.listbox.event_generate("<<ListboxSelect>>")
        root.update()
        for x, y in ((0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.5, 0.2)):
            mouse("<Button-1>", x, y)
            mouse("<ButtonRelease-1>", x, y)
        assert len(e.model.active["points"]) == 4
        mouse("<Button-3>", 0.5, 0.2)
        assert len(e.model.active["points"]) == 3
        mouse("<Button-1>", 0.2, 0.2)
        mouse("<B1-Motion>", 0.3, 0.3)
        mouse("<ButtonRelease-1>", 0.3, 0.3)
        assert e.model.active["points"][0] == pytest.approx([0.3, 0.3], abs=0.01)
        e.toggle_time()
        e.timer.left = 29
        e.win.after_cancel(e.tick_id)
        e.tick()
        assert "30 segundos" in e.message.get()
        e.timer.left = 0
        e.win.after_cancel(e.tick_id)
        e.tick()
        assert e.clock_label.cget("text") == "00:00" and e.win.winfo_viewable()
        assert json.loads((session / "draft.json").read_text())["selected"] == i
        e.extend()
        assert e.timer.remaining > 299
        e.close()
        assert read_draft(session / "draft.json", camera).active["points"]
    finally:
        root.destroy()
