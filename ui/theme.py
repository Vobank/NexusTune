"""Тёмная тема оформления приложения со сменными палитрами.

Цвета лежат в обычных модульных константах, потому что на них ссылаются
десятки виджетов. Смена палитры переписывает эти константы и пересобирает
таблицу стилей: всё оформление окна меняется сразу, а виджеты, которые взяли
цвет себе в инлайновый стиль при создании, догоняют после перезапуска.
"""
from __future__ import annotations

# ---------------------------------------------------------------- палитры

PALETTES = {
    "flint": {
        "title": "Кремень", "hint": "холодная сталь и голубая искра",
        "BG": "#0b0e14", "SURFACE": "#141924", "SURFACE_2": "#1b2231",
        "BORDER": "#242c3d", "BORDER_LIGHT": "#2f3a50",
        "TEXT": "#e6edf3", "TEXT_DIM": "#8b98ad", "TEXT_FAINT": "#5a6678",
        "ACCENT": "#00d0ff", "ACCENT_2": "#7b5cff", "ACCENT_DIM": "#0a5c72",
        "OK": "#3fd07f", "WARN": "#ffb648", "DANGER": "#ff5f5f",
    },
    "magma": {
        "title": "Магма", "hint": "раскалённый металл в горне",
        "BG": "#0d0a09", "SURFACE": "#1a1412", "SURFACE_2": "#241b17",
        "BORDER": "#33241d", "BORDER_LIGHT": "#4a3327",
        "TEXT": "#f5ece6", "TEXT_DIM": "#ad9689", "TEXT_FAINT": "#75625a",
        "ACCENT": "#ff7a29", "ACCENT_2": "#ffc247", "ACCENT_DIM": "#7a3510",
        "OK": "#7fd04a", "WARN": "#ffcf5c", "DANGER": "#ff4d4d",
    },
    "emerald": {
        "title": "Изумруд", "hint": "спокойный зелёный, мягко для глаз",
        "BG": "#080f0c", "SURFACE": "#101c17", "SURFACE_2": "#16261f",
        "BORDER": "#1f352b", "BORDER_LIGHT": "#2b4a3c",
        "TEXT": "#e4f2ea", "TEXT_DIM": "#87a596", "TEXT_FAINT": "#5a7267",
        "ACCENT": "#25e08a", "ACCENT_2": "#4fd6d6", "ACCENT_DIM": "#0d6642",
        "OK": "#3fd07f", "WARN": "#ffc94d", "DANGER": "#ff6b6b",
    },
    "amethyst": {
        "title": "Аметист", "hint": "фиолетовый неон",
        "BG": "#0c0a14", "SURFACE": "#161327", "SURFACE_2": "#1e1935",
        "BORDER": "#2b2347", "BORDER_LIGHT": "#3d3163",
        "TEXT": "#ece7f7", "TEXT_DIM": "#9a8fbd", "TEXT_FAINT": "#685e87",
        "ACCENT": "#b06cff", "ACCENT_2": "#ff6ec7", "ACCENT_DIM": "#5a2b8f",
        "OK": "#4fd68a", "WARN": "#ffc14d", "DANGER": "#ff5f8f",
    },
    "reactor": {
        "title": "Реактор", "hint": "боевой окрас: сталь и аварийный красный",
        "BG": "#0a0708", "SURFACE": "#140d0e", "SURFACE_2": "#1e1315",
        "BORDER": "#33191b", "BORDER_LIGHT": "#4a2327",
        "TEXT": "#f3e6e8", "TEXT_DIM": "#ad8f92", "TEXT_FAINT": "#755a5d",
        "ACCENT": "#ff2d2d", "ACCENT_2": "#ff7a29", "ACCENT_DIM": "#5a0e0e",
        "OK": "#5fd07f", "WARN": "#ffb648", "DANGER": "#ff2d2d",
    },
    "graphite": {
        "title": "Графит", "hint": "строгий монохром без ярких цветов",
        "BG": "#0d0f11", "SURFACE": "#16191d", "SURFACE_2": "#1e2227",
        "BORDER": "#282d33", "BORDER_LIGHT": "#373d45",
        "TEXT": "#e8ecef", "TEXT_DIM": "#98a1ab", "TEXT_FAINT": "#666e77",
        "ACCENT": "#c6d0da", "ACCENT_2": "#8b98a6", "ACCENT_DIM": "#4a535d",
        "OK": "#6fbf8d", "WARN": "#d6b268", "DANGER": "#d97070",
    },
}

DEFAULT_PALETTE = "flint"
_current = DEFAULT_PALETTE

BG = SURFACE = SURFACE_2 = BORDER = BORDER_LIGHT = ""
TEXT = TEXT_DIM = TEXT_FAINT = ""
ACCENT = ACCENT_2 = ACCENT_DIM = ""
OK = WARN = DANGER = ""

# ---------------------------------------------------------------- типографика

# Строгая шкала размеров. Раньше по страницам было раскидано шестнадцать
# разных значений — глазу это читается как неаккуратность. Все размеры теперь
# берутся отсюда, промежуточных значений нет.
FONT = {
    "micro": 10,      # мелкие подписи в оверлее
    "caption": 11,    # пояснения под строками
    "small": 12,      # второстепенный текст
    "body": 13,       # основной текст интерфейса
    "lead": 15,       # заголовки карточек и крупные строки
    "title": 19,      # заголовки блоков
    "page": 26,       # заголовок страницы
    "display": 34,    # крупное число внутри карточки
    "counter": 44,    # счётчик кадров в оверлее
    "hero": 52,       # главное число страницы
}

WEIGHT = {"normal": 400, "medium": 600, "bold": 700, "heavy": 800}


def font_css(size: str = "body", weight: str = "normal",
             color: str | None = None, extra: str = "") -> str:
    """Собирает инлайн-стиль текста по шкале. Никаких случайных пикселей."""
    parts = [f"font-size: {FONT.get(size, FONT['body'])}px",
             f"font-weight: {WEIGHT.get(weight, 400)}"]
    if color:
        parts.append(f"color: {color}")
    css = "; ".join(parts) + ";"
    return css + (" " + extra if extra else "")


def rgba(color: str, alpha: int) -> str:
    """#rrggbb + прозрачность 0-255 -> строка rgba() для таблицы стилей."""
    color = color.lstrip("#")
    r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha})"


def grade_color(key: str) -> str:
    """Цвет по смысловому ключу. Функция, а не словарь: палитра меняется."""
    return {"ok": OK, "warn": WARN, "danger": DANGER}.get(key, ACCENT)


def fps_color(fps: int, target: int = 60) -> str:
    """Цвет для числа кадров относительно цели."""
    if fps >= target * 1.6:
        return OK
    if fps >= target:
        return "#9ae66e"
    if fps >= target * 0.7:
        return WARN
    return DANGER


def temp_color(temp: float) -> str:
    if temp >= 85:
        return DANGER
    if temp >= 75:
        return WARN
    return OK


def load_color(percent: float) -> str:
    if percent >= 90:
        return DANGER
    if percent >= 70:
        return WARN
    return ACCENT


# ---------------------------------------------------------------- таблица стилей


def _build_stylesheet() -> str:
    """Собирает таблицу стилей из текущих цветов палитры."""
    return f"""
* {{
    font-family: "Segoe UI", "Inter", sans-serif;
    color: {TEXT};
}}

QMainWindow, QWidget#Root {{
    background: {BG};
}}

QScrollArea {{
    border: none;
    background: transparent;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {BORDER_LIGHT};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {TEXT_FAINT};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    height: 0px;
    background: transparent;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER_LIGHT};
    border-radius: 5px;
    min-width: 30px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}

/* ---------------- боковое меню ---------------- */

QWidget#Sidebar {{
    background: {SURFACE};
    border-right: 1px solid {BORDER};
}}

QPushButton#NavButton {{
    background: transparent;
    border: none;
    border-radius: 10px;
    padding: 11px 14px;
    text-align: left;
    font-size: 15px;
    color: {TEXT_DIM};
}}
QPushButton#NavButton:hover {{
    background: {SURFACE_2};
    color: {TEXT};
}}
QPushButton#NavButton:checked {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 {rgba(ACCENT, 40)},
                                stop:1 {rgba(ACCENT_2, 20)});
    color: {ACCENT};
    font-weight: 600;
}}

QLabel#Logo {{
    font-size: 19px;
    font-weight: 800;
    letter-spacing: 3px;
    color: {TEXT};
}}
QLabel#LogoSub {{
    font-size: 10px;
    letter-spacing: 0.4px;
    color: {ACCENT};
    font-weight: 600;
}}

/* ---------------- карточки ---------------- */

QFrame#Card {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 14px;
}}
QFrame#CardFlat {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}

QLabel#PageTitle {{
    font-size: 26px;
    font-weight: 700;
}}
QLabel#PageSubtitle {{
    font-size: 13px;
    color: {TEXT_DIM};
}}
QLabel#CardTitle {{
    font-size: 15px;
    font-weight: 600;
}}
QLabel#Dim {{
    color: {TEXT_DIM};
    font-size: 12px;
}}
QLabel#Faint {{
    color: {TEXT_FAINT};
    font-size: 11px;
}}
QLabel#BigNumber {{
    font-size: 34px;
    font-weight: 700;
}}
QLabel#SectionHeader {{
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 1.5px;
    color: {TEXT_FAINT};
}}

/* ---------------- кнопки ---------------- */

QPushButton {{
    background: {SURFACE_2};
    border: 1px solid {BORDER_LIGHT};
    border-radius: 9px;
    padding: 9px 16px;
    font-size: 13px;
}}
QPushButton:hover {{
    background: {BORDER};
    border-color: {TEXT_FAINT};
}}
QPushButton:pressed {{
    background: {BORDER_LIGHT};
}}
QPushButton:disabled {{
    color: {TEXT_FAINT};
    background: {SURFACE};
    border-color: {BORDER};
}}

QPushButton#Primary {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 {ACCENT}, stop:1 {ACCENT_2});
    border: none;
    color: #05202a;
    font-weight: 700;
}}
QPushButton#Primary:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 #3fdcff, stop:1 #9a7dff);
}}
QPushButton#Primary:disabled {{
    background: {BORDER};
    color: {TEXT_FAINT};
}}

QPushButton#Danger {{
    background: transparent;
    border: 1px solid {DANGER};
    color: {DANGER};
}}
QPushButton#Danger:hover {{
    background: {rgba(DANGER, 30)};
}}

QPushButton#Ghost {{
    background: transparent;
    border: 1px solid {BORDER_LIGHT};
    color: {TEXT_DIM};
}}
QPushButton#Ghost:hover {{
    color: {TEXT};
    border-color: {ACCENT};
}}

QPushButton#Preset {{
    background: {SURFACE_2};
    border: 1px solid {BORDER_LIGHT};
    border-radius: 12px;
    padding: 14px;
    text-align: left;
}}
QPushButton#Preset:checked {{
    border: 2px solid {ACCENT};
    background: {rgba(ACCENT, 22)};
}}

/* ---------------- поля ввода ---------------- */

QLineEdit, QComboBox, QSpinBox {{
    background: {SURFACE_2};
    border: 1px solid {BORDER_LIGHT};
    border-radius: 8px;
    padding: 8px 10px;
    selection-background-color: {ACCENT_DIM};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox QAbstractItemView {{
    background: {SURFACE_2};
    border: 1px solid {BORDER_LIGHT};
    selection-background-color: {ACCENT_DIM};
    outline: none;
}}

QListWidget {{
    background: transparent;
    border: none;
    outline: none;
}}
QListWidget::item {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 10px;
    margin-bottom: 6px;
}}
QListWidget::item:selected {{
    border: 1px solid {ACCENT};
    background: {rgba(ACCENT, 20)};
}}

QCheckBox {{
    spacing: 9px;
    font-size: 13px;
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 5px;
    border: 1px solid {BORDER_LIGHT};
    background: {SURFACE_2};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}

QSlider::groove:horizontal {{
    height: 5px;
    background: {BORDER};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {TEXT};
    width: 15px;
    height: 15px;
    margin: -5px 0;
    border-radius: 7px;
}}

QProgressBar {{
    background: {SURFACE_2};
    border: none;
    border-radius: 5px;
    height: 8px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {ACCENT};
    border-radius: 5px;
}}

QTableWidget {{
    background: transparent;
    border: none;
    gridline-color: {BORDER};
}}
QHeaderView::section {{
    background: {SURFACE_2};
    border: none;
    padding: 8px;
    color: {TEXT_DIM};
    font-weight: 600;
}}

QToolTip {{
    background: {SURFACE_2};
    border: 1px solid {ACCENT};
    border-radius: 6px;
    padding: 6px;
    color: {TEXT};
}}

QTextEdit {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 8px;
}}
"""


def apply_palette(name: str) -> str:
    """Переключает палитру и возвращает свежую таблицу стилей."""
    global _current, STYLESHEET
    global BG, SURFACE, SURFACE_2, BORDER, BORDER_LIGHT
    global TEXT, TEXT_DIM, TEXT_FAINT, ACCENT, ACCENT_2, ACCENT_DIM
    global OK, WARN, DANGER

    palette = PALETTES.get(name) or PALETTES[DEFAULT_PALETTE]
    _current = name if name in PALETTES else DEFAULT_PALETTE

    BG, SURFACE, SURFACE_2 = palette["BG"], palette["SURFACE"], palette["SURFACE_2"]
    BORDER, BORDER_LIGHT = palette["BORDER"], palette["BORDER_LIGHT"]
    TEXT, TEXT_DIM, TEXT_FAINT = palette["TEXT"], palette["TEXT_DIM"], palette["TEXT_FAINT"]
    ACCENT, ACCENT_2, ACCENT_DIM = palette["ACCENT"], palette["ACCENT_2"], palette["ACCENT_DIM"]
    OK, WARN, DANGER = palette["OK"], palette["WARN"], palette["DANGER"]

    STYLESHEET = _build_stylesheet()
    return STYLESHEET


def current_palette() -> str:
    return _current


STYLESHEET = apply_palette(DEFAULT_PALETTE)
