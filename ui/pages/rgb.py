"""Страница подсветки: управление всеми RGB-устройствами через OpenRGB."""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QSlider, QColorDialog, QCheckBox, QGridLayout, QButtonGroup,
)

from core import rgb as rgblib
from core.context import AppContext

from .. import theme
from ..widgets import (
    Card, Badge, SectionTitle, page_header, scroll_page,
    primary_button, ghost_button, dim_label, glow, clear_layout,
)

# Готовые цвета для быстрого выбора
SWATCHES = [
    ("Красный", (255, 0, 0)), ("Оранжевый", (255, 90, 0)),
    ("Жёлтый", (255, 200, 0)), ("Зелёный", (0, 255, 60)),
    ("Бирюзовый", (0, 220, 220)), ("Голубой", (0, 140, 255)),
    ("Синий", (30, 40, 255)), ("Фиолетовый", (140, 0, 255)),
    ("Розовый", (255, 40, 160)), ("Белый", (255, 255, 255)),
]


class ConnectWorker(QThread):
    done = Signal(bool, str)

    def __init__(self, client: rgblib.OpenRGBClient):
        super().__init__()
        self.client = client

    def run(self):
        ok, msg = self.client.connect()
        self.done.emit(ok, msg)


class RgbPage(QWidget):
    """Единое управление подсветкой всего компьютера."""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.client = ctx.rgb_client
        self.effects = ctx.effects
        self.color = (0, 200, 255)
        self.device_boxes: list[tuple[QCheckBox, int]] = []
        self.worker = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area, lay = scroll_page()
        outer.addWidget(area)

        lay.addWidget(page_header(
            "Подсветка",
            "Единое управление подсветкой платы, памяти, видеокарты, кулера, "
            "клавиатуры и мыши — через открытый сервер OpenRGB."))

        lay.addWidget(self._connection_card())
        self.devices_card = self._devices_card()
        lay.addWidget(self.devices_card)
        lay.addWidget(self._effect_card())
        lay.addWidget(self._sync_card())
        lay.addStretch()

        self._try_autoconnect()

    # ------------------------------------------------------------ карточки

    def _connection_card(self) -> Card:
        card = Card("Соединение с OpenRGB")
        row = QHBoxLayout()
        self.connect_btn = primary_button("Подключиться")
        self.connect_btn.clicked.connect(self._connect)
        row.addWidget(self.connect_btn)

        if rgblib.openrgb_installed():
            launch = ghost_button("Запустить OpenRGB")
            launch.clicked.connect(self._launch_openrgb)
            row.addWidget(launch)
        row.addStretch()

        self.conn_badge = Badge("не подключено", theme.TEXT_FAINT)
        row.addWidget(self.conn_badge)
        card.add_layout(row)

        self.conn_status = QLabel(
            "OpenRGB — бесплатная открытая программа, которая умеет говорить с "
            "подсветкой большинства плат, памяти, видеокарт и периферии. "
            "КРЕМЕНЬ подключается к её серверу и рисует эффекты сам.")
        self.conn_status.setObjectName("Dim")
        self.conn_status.setWordWrap(True)
        card.add(self.conn_status)
        return card

    def _devices_card(self) -> Card:
        card = Card("Устройства", "Отметь то, чем управлять.")
        self.devices_box = QVBoxLayout()
        card.add_layout(self.devices_box)
        self.devices_empty = QLabel("Устройств пока нет — подключись к OpenRGB.")
        self.devices_empty.setObjectName("Dim")
        card.add(self.devices_empty)
        return card

    def _effect_card(self) -> Card:
        card = Card("Эффект")

        grid = QGridLayout()
        grid.setSpacing(8)
        self.effect_group = QButtonGroup(self)
        for i, (key, title) in enumerate(rgblib.EFFECTS.items()):
            btn = QPushButton(title)
            btn.setObjectName("Preset")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setMinimumHeight(46)
            btn.clicked.connect(lambda _c=False, k=key: self._set_effect(k))
            self.effect_group.addButton(btn)
            grid.addWidget(btn, i // 4, i % 4)
            if key == "static":
                btn.setChecked(True)
        card.add_layout(grid)

        card.add(SectionTitle("Цвет"))
        swatch_row = QHBoxLayout()
        swatch_row.setSpacing(6)
        for name, rgb in SWATCHES:
            btn = QPushButton()
            btn.setFixedSize(34, 34)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(name)
            btn.setStyleSheet(
                f"background: rgb{rgb}; border: 1px solid {theme.BORDER_LIGHT}; "
                "border-radius: 17px;")
            btn.clicked.connect(lambda _c=False, c=rgb: self._set_color(c))
            swatch_row.addWidget(btn)

        custom = ghost_button("Свой цвет…")
        custom.clicked.connect(self._pick_color)
        swatch_row.addWidget(custom)
        swatch_row.addStretch()
        card.add_layout(swatch_row)

        sliders = QGridLayout()
        sliders.addWidget(QLabel("Яркость", objectName="Dim"), 0, 0)
        self.brightness = QSlider(Qt.Horizontal)
        self.brightness.setRange(5, 100)
        self.brightness.setValue(100)
        self.brightness.valueChanged.connect(self._on_brightness)
        sliders.addWidget(self.brightness, 0, 1)
        self.brightness_label = QLabel("100%")
        self.brightness_label.setObjectName("Dim")
        sliders.addWidget(self.brightness_label, 0, 2)

        sliders.addWidget(QLabel("Скорость", objectName="Dim"), 1, 0)
        self.speed = QSlider(Qt.Horizontal)
        self.speed.setRange(10, 300)
        self.speed.setValue(100)
        self.speed.valueChanged.connect(self._on_speed)
        sliders.addWidget(self.speed, 1, 1)
        self.speed_label = QLabel("1.0×")
        self.speed_label.setObjectName("Dim")
        sliders.addWidget(self.speed_label, 1, 2)
        sliders.setColumnStretch(1, 1)
        card.add_layout(sliders)

        self.preview = QLabel("Предпросмотр цвета")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(46)
        card.add(self.preview)
        self._update_preview()

        self.effect_status = QLabel("")
        self.effect_status.setObjectName("Faint")
        self.effect_status.setWordWrap(True)
        card.add(self.effect_status)
        return card

    def _sync_card(self) -> Card:
        card = Card(
            "Умные сцены",
            "Подсветка может реагировать на то, что происходит с железом.")
        row = QHBoxLayout()
        row.setSpacing(10)

        temp_btn = ghost_button("🌡  Цвет по температуре GPU")
        temp_btn.setToolTip("Синий — холодно, красный — горячо. Видно нагрев боковым зрением.")
        temp_btn.clicked.connect(lambda: self._set_effect("temperature"))
        row.addWidget(temp_btn)

        load_btn = ghost_button("📊  Полоса загрузки GPU")
        load_btn.setToolTip("Светодиоды заполняются по мере нагрузки на видеокарту.")
        load_btn.clicked.connect(lambda: self._set_effect("load"))
        row.addWidget(load_btn)

        off_btn = ghost_button("🌙  Выключить всё")
        off_btn.clicked.connect(lambda: self._set_effect("off"))
        row.addWidget(off_btn)
        row.addStretch()
        card.add_layout(row)

        card.add(dim_label(
            "Эффекты считаются самим приложением и отправляются кадрами, поэтому "
            "работают одинаково на устройствах разных производителей — даже там, "
            "где родная программа умеет только статичный цвет."))
        return card

    # ------------------------------------------------------------ соединение

    def _try_autoconnect(self):
        if rgblib.openrgb_running():
            self._connect()
        elif not rgblib.openrgb_installed():
            self.conn_status.setText(
                "OpenRGB на компьютере не найден.\n\n"
                "Скачай его с openrgb.org, установи и запусти от имени администратора. "
                "В настройках включи «Enable SDK Server» — после этого вернись сюда "
                "и нажми «Подключиться». Программа бесплатная и с открытым кодом.")

    def _connect(self):
        self.connect_btn.setEnabled(False)
        self.connect_btn.setText("Подключаюсь…")
        self.worker = ConnectWorker(self.client)
        self.worker.done.connect(self._on_connected)
        self.worker.start()

    def _on_connected(self, ok: bool, msg: str):
        self.connect_btn.setEnabled(True)
        self.connect_btn.setText("Переподключиться" if ok else "Подключиться")
        self.conn_status.setText(msg)
        if ok:
            self.conn_badge.setText(f"подключено · протокол v{self.client.protocol}")
            self.conn_badge.set_color(theme.OK)
            self._fill_devices()
        else:
            self.conn_badge.setText("не подключено")
            self.conn_badge.set_color(theme.DANGER)

    def _launch_openrgb(self):
        if rgblib.launch_openrgb():
            self.conn_status.setText(
                "Запускаю OpenRGB… Дай ему пару секунд и нажми «Подключиться». "
                "Если подсветка не найдётся, запусти OpenRGB от имени администратора.")

    def _fill_devices(self):
        clear_layout(self.devices_box)
        self.device_boxes.clear()

        if not self.client.devices:
            self.devices_empty.setText(
                "OpenRGB подключён, но устройств не видит. Обычно помогает запуск "
                "OpenRGB от имени администратора и закрытие фирменной программы "
                "подсветки (Mystic Light, Aura, iCUE) — они держат устройства занятыми.")
            return

        self.devices_empty.setVisible(False)
        for dev in self.client.devices:
            row = QHBoxLayout()
            box = QCheckBox(f"{dev.name}")
            box.setChecked(True)
            box.setStyleSheet("font-weight: 600;")
            box.toggled.connect(lambda state, d=dev: self._toggle_device(d, state))
            row.addWidget(box, 1)
            row.addWidget(Badge(dev.type_name, theme.ACCENT_2))
            row.addWidget(Badge(f"{dev.led_count} LED", theme.TEXT_FAINT))
            self.devices_box.addLayout(row)
            self.device_boxes.append((box, dev.index))

    def _toggle_device(self, device, enabled: bool):
        device.enabled = enabled

    # ------------------------------------------------------------ эффекты

    def _set_color(self, rgb: tuple[int, int, int]):
        self.color = rgb
        self.effects.color = rgb
        self._update_preview()
        self._push()

    def _pick_color(self):
        current = QColor(*self.color)
        chosen = QColorDialog.getColor(current, self, "Выбери цвет подсветки")
        if chosen.isValid():
            self._set_color((chosen.red(), chosen.green(), chosen.blue()))

    def _update_preview(self):
        r, g, b = self.color
        self.preview.setStyleSheet(
            f"background: rgb({r},{g},{b}); border-radius: 10px; "
            f"color: {'#000' if (r + g + b) > 400 else '#fff'}; font-weight: 700;")
        self.preview.setText(f"RGB {r}, {g}, {b}")

    def _on_brightness(self, value: int):
        self.brightness_label.setText(f"{value}%")
        self.effects.brightness = value / 100
        self._push()

    def _on_speed(self, value: int):
        self.speed_label.setText(f"{value / 100:.1f}×")
        self.effects.speed = value / 100

    def _set_effect(self, key: str):
        self.effects.effect = key
        for btn, name in zip(self.effect_group.buttons(), rgblib.EFFECTS.keys()):
            btn.setChecked(name == key)
        self._push()
        self.effect_status.setText(f"Эффект: {rgblib.EFFECTS.get(key, key)}")

    def _push(self):
        """Применяет текущий эффект к устройствам."""
        if not self.client.connected:
            self.effect_status.setText(
                "Настройки запомнены, но применить их некуда — сначала подключись к OpenRGB.")
            return
        animated = self.effects.effect in ("breathing", "rainbow", "wave",
                                           "temperature", "load")
        if animated:
            if not self.effects.running:
                self.effects.start()
        else:
            if self.effects.running:
                self.effects.stop()
            for dev in self.client.devices:
                if dev.enabled:
                    self.client.set_custom_mode(dev.index)
            self.effects.apply_once()

    # ------------------------------------------------------------ данные датчиков

    def tick(self, reading: dict):
        """Передаёт эффектам свежие показания датчиков."""
        if self.effects.effect == "temperature":
            self.effects.sensor_value = reading.get("gpu_temp") or reading.get("cpu_temp") or 0
        elif self.effects.effect == "load":
            self.effects.sensor_value = reading.get("gpu_load") or reading.get("cpu_load") or 0
