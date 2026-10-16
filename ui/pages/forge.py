"""Кузница — капстоун-страница: перестраивает компьютер и доказывает результат.

Ни одна другая программа так не делает: обычная «оптимизация» обещает ускорение
и уходит. Кузница снимает живые показатели ДО, перестраивает систему под
выбранную задачу на глазах, снимает показатели ПОСЛЕ и выдаёт «паспорт ковки» —
разницу в цифрах, которые можно проверить. Если разница в пределах погрешности,
она честно так и скажет.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal, QRectF, QTimer, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QConicalGradient
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QApplication, QMessageBox,
)

from core import forge as forge_core
from core import profiles as profiles_core
from core.context import AppContext

from .. import theme
from ..widgets import (
    Card, Badge, page_header, scroll_page, primary_button, ghost_button, dim_label,
)


# Профили, которые имеет смысл «ковать» под измеримый результат
_FORGE_TARGETS = ["gaming", "esports", "silent", "eco"]


class ForgeWorker(QThread):
    """Замер до → перестройка → замер после. Всё в фоне, интерфейс не висит."""
    stage = Signal(str, int)                 # текст этапа, процент
    finished_forge = Signal(dict)

    def __init__(self, ctx: AppContext, profile):
        super().__init__()
        self.ctx = ctx
        self.profile = profile

    def run(self):
        try:
            self.stage.emit("Снимаю показатели ДО перестройки…", 8)
            before = forge_core.measure(quick=False)

            self.stage.emit("Перестраиваю компьютер под профиль…", 30)
            applied = profiles_core.apply_profile(
                self.ctx, self.profile,
                progress=lambda t: self.stage.emit(t, 55))

            self.stage.emit("Даю системе устояться…", 78)
            self.msleep(1500)

            self.stage.emit("Снимаю показатели ПОСЛЕ…", 85)
            after = forge_core.measure(quick=False)

            gains = forge_core.compare(before, after)
            passport = forge_core.passport_text(self.profile.title, gains, applied)
            self.stage.emit("Готово.", 100)
            self.finished_forge.emit({
                "gains": gains, "applied": applied, "passport": passport,
                "verdict": forge_core.verdict(gains),
            })
        except Exception as exc:
            self.finished_forge.emit({"error": str(exc)})


class Anvil(QWidget):
    """Живой круг ковки: пока идёт работа — вращается и пульсирует искрой."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(150, 150)
        self.angle = 0
        self.percent = 0
        self.active = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._spin)

    def start(self):
        self.active = True
        self._timer.start(30)

    def stop(self):
        self.active = False
        self._timer.stop()
        self.update()

    def set_percent(self, percent: int):
        self.percent = percent
        self.update()

    def _spin(self):
        self.angle = (self.angle + 6) % 360
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(14, 14, 122, 122)

        p.setPen(QPen(QColor(theme.BORDER), 8))
        p.drawArc(rect, 0, 360 * 16)

        if self.active:
            grad = QConicalGradient(rect.center(), -self.angle)
            grad.setColorAt(0.0, QColor(theme.ACCENT))
            grad.setColorAt(0.5, QColor(theme.ACCENT_2))
            grad.setColorAt(1.0, QColor(theme.ACCENT))
            p.setPen(QPen(grad, 8, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(rect, -self.angle * 16, 110 * 16)
        else:
            p.setPen(QPen(QColor(theme.ACCENT), 8, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(rect, 90 * 16, -int(360 * 16 * self.percent / 100))

        p.setPen(QColor(theme.TEXT))
        p.setFont(QFont("Segoe UI", 24, QFont.Bold))
        p.drawText(rect, Qt.AlignCenter, f"{self.percent}%" if not self.active else "🔨")
        p.end()


class ForgePage(QWidget):
    """Ковка профиля с доказательством результата."""

    forged = Signal()

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.profile = next(p for p in profiles_core.PROFILES if p.key == "gaming")
        self.worker: ForgeWorker | None = None
        self._passport = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, self.lay = scroll_page()
        outer.addWidget(area)

        self.lay.addWidget(page_header(
            "Кузница",
            "Не верит на слово. Замеряет компьютер, перестраивает его под задачу "
            "и показывает разницу в цифрах — до и после. Всё обратимо профилем "
            "«Как с завода»."))

        self.lay.addWidget(self._forge_card())
        self.result_card = self._result_card()
        self.result_card.setVisible(False)
        self.lay.addWidget(self.result_card)
        self.lay.addStretch()

    # ------------------------------------------------------------ кузница

    def _forge_card(self) -> Card:
        card = Card()
        row = QHBoxLayout()
        row.setSpacing(24)

        self.anvil = Anvil()
        row.addWidget(self.anvil, 0, Qt.AlignTop)

        right = QVBoxLayout()
        right.setSpacing(12)

        right.addWidget(dim_label("Под какую задачу ковать:"))
        picker = QHBoxLayout()
        picker.setSpacing(8)
        self.buttons: dict[str, ghost_button] = {}
        for key in _FORGE_TARGETS:
            profile = next(p for p in profiles_core.PROFILES if p.key == key)
            btn = ghost_button(f"{profile.icon}  {profile.title}")
            btn.setCheckable(True)
            btn.setChecked(key == "gaming")
            btn.clicked.connect(lambda _c=False, k=key: self._pick(k))
            picker.addWidget(btn)
            self.buttons[key] = btn
        picker.addStretch()
        right.addLayout(picker)

        self.stage_label = QLabel("Готов ковать. Нажми — и не трогай компьютер "
                                  "полминуты, дай снять чистый замер.")
        self.stage_label.setObjectName("Dim")
        self.stage_label.setWordWrap(True)
        right.addWidget(self.stage_label)

        actions = QHBoxLayout()
        self.forge_btn = primary_button("🔨  Ковать")
        self.forge_btn.clicked.connect(self._start)
        actions.addWidget(self.forge_btn)
        actions.addStretch()
        right.addLayout(actions)
        right.addStretch()

        row.addLayout(right, 1)
        card.add_layout(row)
        return card

    def _pick(self, key: str):
        self.profile = next(p for p in profiles_core.PROFILES if p.key == key)
        for k, btn in self.buttons.items():
            btn.setChecked(k == key)

    def _start(self):
        if self.worker and self.worker.isRunning():
            return
        self.forge_btn.setEnabled(False)
        for btn in self.buttons.values():
            btn.setEnabled(False)
        self.result_card.setVisible(False)
        self.anvil.start()
        self.stage_label.setText("Разжигаю горн…")

        self.worker = ForgeWorker(self.ctx, self.profile)
        self.worker.stage.connect(self._on_stage)
        self.worker.finished_forge.connect(self._on_done)
        self.worker.start()

    def _on_stage(self, text: str, percent: int):
        self.stage_label.setText(text)
        self.anvil.set_percent(percent)

    def _on_done(self, result: dict):
        self.anvil.stop()
        self.anvil.set_percent(100)
        self.forge_btn.setEnabled(True)
        for btn in self.buttons.values():
            btn.setEnabled(True)

        if result.get("error"):
            self.stage_label.setText("Что-то пошло не так.")
            QMessageBox.warning(self, "Кузница", f"Ковка прервалась:\n{result['error']}")
            return

        self.stage_label.setText("Ковка завершена. Паспорт ниже.")
        self._render_result(result)
        self.result_card.setVisible(True)
        self.forged.emit()

    # ------------------------------------------------------------ результат

    def _result_card(self) -> Card:
        card = Card("Паспорт ковки",
                    "Замерено до и после перестройки. Ничего не приукрашено.")
        self.verdict_label = QLabel("")
        self.verdict_label.setWordWrap(True)
        self.verdict_label.setStyleSheet(
            f"color: {theme.TEXT}; background: {theme.SURFACE_2}; "
            f"border: 1px solid {theme.ACCENT}; border-radius: 10px; "
            "padding: 12px; font-size: 15px; font-weight: 600;")
        card.add(self.verdict_label)

        self.gains_box = QVBoxLayout()
        self.gains_box.setSpacing(8)
        card.add_layout(self.gains_box)

        row = QHBoxLayout()
        row.addStretch()
        copy_btn = ghost_button("Скопировать паспорт")
        copy_btn.clicked.connect(self._copy)
        row.addWidget(copy_btn)
        card.add_layout(row)
        return card

    def _render_result(self, result: dict):
        self.verdict_label.setText(result["verdict"])
        self._passport = result["passport"]

        while self.gains_box.count():
            item = self.gains_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for g in result["gains"]:
            block = QWidget()
            bl = QVBoxLayout(block)
            bl.setContentsMargins(12, 10, 12, 10)
            bl.setSpacing(3)
            block.setStyleSheet(
                f"background: {theme.SURFACE_2}; border: 1px solid {theme.BORDER}; "
                "border-radius: 10px;")
            top = QHBoxLayout()
            title = QLabel(g.title)
            title.setStyleSheet("font-weight: 600; font-size: 13px;")
            top.addWidget(title)
            top.addStretch()
            sign = "+" if g.delta_percent >= 0 else ""
            color = theme.OK if g.better and abs(g.delta_percent) >= 1 else \
                theme.DANGER if not g.better else theme.TEXT_DIM
            top.addWidget(Badge(f"{sign}{g.delta_percent:.0f}%", color))
            bl.addLayout(top)

            detail = QLabel(f"{g.before}  →  {g.after}   ·   {g.unit_note}")
            detail.setObjectName("Faint")
            bl.addWidget(detail)
            self.gains_box.addWidget(block)

    def _copy(self):
        QApplication.clipboard().setText(self._passport)
        QMessageBox.information(self, "Кузница", "Паспорт ковки скопирован.")
