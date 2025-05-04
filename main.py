# video_trimmer.py
"""
Video‑Trimmer ‑ простое настольное приложение на PySide6 (Qt 6) для обрезки видео.

Зависимости (ASCII‑дефисы!):
    pip install PySide6 moviepy imageio-ffmpeg proglog

Возможности
-----------
* Открыть любой ролик, который «понимает» FFmpeg.
* Задать время начала и конца (HH:MM:SS или HH:MM:SS.mmm).
* Выбрать файл, куда сохранить результат.
* Обрезать в фоновом потоке; состояние отрисовывается в ProgressBar.
* Поддержка Windows, macOS, Linux; тест: Python 3.11 + MoviePy 1.0.3.

Запуск:
    python video_trimmer.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal, Slot, Qt
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

# Проверяем наличие MoviePy заранее, чтобы выдать читаемое сообщение

from moviepy import VideoFileClip  # type: ignore


from proglog import ProgressBarLogger  # для кастомного логгера

TIME_PATTERN = re.compile(r"^(\d+):(\d{2}):(\d{2}(?:\.\d{1,3})?)$")


def to_seconds(timecode: str) -> float:
    """HH:MM:SS(.ms) → seconds (float)."""
    m = TIME_PATTERN.fullmatch(timecode.strip())
    if not m:
        raise ValueError("Timecode must be HH:MM:SS or HH:MM:SS.mmm")
    h, mnt, sec = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(sec)


def seconds_to_timecode(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02}:{m:02}:{s:06.3f}" if s % 1 else f"{h:02}:{m:02}:{int(s):02}"


class QtLogger(ProgressBarLogger):
    """MoviePy → Qt  (обновляет QProgressBar)."""

    def __init__(self, qt_signal):
        super().__init__()
        self.qt_signal = qt_signal  # Signal(int)

    def callback(self, **changes):
        super().callback(**changes)  # важно: обновляет self.bars
        if not self.bars:  # ещё нет данных
            return

        completed = sum(
            min(b["index"], b["total"] or b["index"]) for b in self.bars.values()
        )
        grand_total = sum((b["total"] or b["index"]) for b in self.bars.values()) or 1
        percent = int(completed / grand_total * 100)
        self.qt_signal.emit(percent)


class TrimWorker(QThread):
    """Фоновая обрезка видео."""

    progress = Signal(int)  # 0‑100
    finished = Signal(bool, str)  # success, message

    def __init__(self, src: Path, dst: Path, start: float, end: float, parent=None):
        super().__init__(parent)
        self.src = src
        self.dst = dst
        self.start_time = start
        self.end_time = end

    def run(self):
        try:
            clip = VideoFileClip(str(self.src))
            if self.end_time > clip.duration:
                self.end_time = clip.duration
            sub: VideoFileClip = clip.subclipped(self.start_time, self.end_time)
            logger = QtLogger(self.progress)

            sub.write_videofile(
                str(self.dst),  # .mp4
                logger=logger,
            )
            self.finished.emit(True, "Done!")
        except Exception as e:
            self.finished.emit(False, str(e))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Video Trimmer")
        self.setMinimumSize(500, 260)
        central = QWidget()
        layout = QVBoxLayout(central)
        self.setCentralWidget(central)

        # Source file row
        file_row = QHBoxLayout()
        browse_btn = QPushButton("Open …")
        browse_btn.clicked.connect(self.browse_file)
        self.file_label = QLabel("<i>No file chosen</i>")
        self.file_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        file_row.addWidget(browse_btn)
        file_row.addWidget(self.file_label)
        layout.addLayout(file_row)

        # Time row
        time_row = QHBoxLayout()
        self.start_edit = QLineEdit("00:00:00")
        self.end_edit = QLineEdit("00:00:00")
        time_row.addWidget(QLabel("Start:"))
        time_row.addWidget(self.start_edit)
        time_row.addWidget(QLabel("End:"))
        time_row.addWidget(self.end_edit)
        layout.addLayout(time_row)

        # Output row
        out_row = QHBoxLayout()
        out_browse = QPushButton("Save As …")
        out_browse.clicked.connect(self.browse_output)
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText("Output file path")
        out_row.addWidget(out_browse)
        out_row.addWidget(self.out_edit)
        layout.addLayout(out_row)

        # Progress + Trim
        self.progress = QProgressBar(maximum=100)
        self.progress.setValue(0)
        self.trim_btn = QPushButton("Trim")
        self.trim_btn.clicked.connect(self.start_trim)
        layout.addWidget(self.progress)
        layout.addWidget(self.trim_btn)

        self.video_path: Optional[Path] = None
        self.worker: Optional[TrimWorker] = None

    # ────────────────────────── UI Slots ──────────────────────────
    @Slot()
    def browse_file(self):
        fname, _ = QFileDialog.getOpenFileName(
            self,
            "Choose a video",
            "",
            "Video Files (*.mp4 *.mov *.mkv *.avi);;All Files (*)",
        )
        if fname:
            self.video_path = Path(fname)
            self.file_label.setText(fname)
            try:
                dur = VideoFileClip(fname).duration
                self.end_edit.setText(seconds_to_timecode(dur))
            except Exception:
                pass

    @Slot()
    def browse_output(self):
        fname, _ = QFileDialog.getSaveFileName(
            self, "Save trimmed video as", "", "MP4 Video (*.mp4)"
        )
        if fname:
            self.out_edit.setText(
                fname if fname.lower().endswith(".mp4") else fname + ".mp4"
            )

    @Slot()
    def start_trim(self):
        if not self.video_path or not self.video_path.exists():
            self.error("Please select a source video first.")
            return
        output = Path(self.out_edit.text().strip())
        if not output:
            self.error("Please specify an output file path.")
            return
        try:
            start_sec = to_seconds(self.start_edit.text())
            end_sec = to_seconds(self.end_edit.text())
        except ValueError as e:
            self.error(str(e))
            return
        if end_sec <= start_sec:
            self.error("End time must be greater than start time.")
            return

        # Prepare UI & worker
        self.trim_btn.setEnabled(False)
        self.progress.setValue(0)
        self.worker = TrimWorker(self.video_path, output, start_sec, end_sec)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.finished.connect(self.on_finished)
        self.worker.start()

    @Slot(bool, str)
    def on_finished(self, ok: bool, msg: str):
        self.trim_btn.setEnabled(True)
        if ok:
            self.progress.setValue(100)
            QMessageBox.information(self, "Success", "Trimming complete!")
        else:
            self.progress.setValue(0)
            self.error(f"Failed: {msg}")

    # ────────────────────────── Helpers ──────────────────────────
    def error(self, message: str):
        QMessageBox.critical(self, "Error", message)


def main() -> None:  # noqa: D401
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
