"""Общее состояние приложения, доступное всем страницам."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import expert, optimizer, policies, rgb
from .hwinfo import Machine, detect_machine
from .power import PowerMeter, save_session
from .sensors import Sensors
from .util import load_config, log, save_config

DEFAULT_CONFIG = {
    # ---- энергия и деньги
    "tariff": 4.32,                 # цена киловатт-часа
    "currency": "₴",
    "hours_per_day": 6.0,           # для прогноза расходов
    # ---- автопилот
    "autopilot": False,
    "autopilot_profile": "gaming",
    "autopilot_restore": "stock",
    "active_profile": "",
    # ---- внешний вид
    "palette": "flint",
    "start_page": "dashboard",
    # ---- запуск и поведение
    "autostart": False,
    "start_minimized": False,
    "minimize_to_tray": True,
    "tray_notice_shown": False,
    # ---- датчики и оверлей
    "sensor_interval_ms": 1200,
    "overlay_ping_host": "1.1.1.1",
    "overlay_corner": "top-right",
    "overlay_opacity": 95,
    # ---- безопасность
    "confirm_risky": True,
    "auto_restore_point": True,
}


@dataclass
class AppContext:
    """Единая точка доступа к железу, твикам, энергии и подсветке."""
    machine: Machine = None
    sensors: Sensors = None
    tweaks: list = field(default_factory=list)
    expert_tweaks: list = field(default_factory=list)
    rgb_client: rgb.OpenRGBClient = None
    effects: rgb.EffectEngine = None
    power_meter: PowerMeter = None
    watcher: object = None                       # GameWatcher, создаётся окном
    balancer: object = None                      # SmartBalancer, создаётся страницей процессов
    autooc: object = None                        # SmartOverclock, создаётся страницей разгона
    config: dict = field(default_factory=dict)
    last_reading: dict = field(default_factory=dict)
    games: list = field(default_factory=list)

    @classmethod
    def create(cls) -> "AppContext":
        machine = detect_machine()
        client = rgb.OpenRGBClient()
        config = {**DEFAULT_CONFIG, **load_config()}
        return cls(
            machine=machine,
            sensors=Sensors(machine),
            tweaks=optimizer.build_tweaks(),
            expert_tweaks=(expert.build_expert_tweaks()
                           + policies.build_policy_tweaks()),
            rgb_client=client,
            effects=rgb.EffectEngine(client),
            power_meter=PowerMeter(machine),
            config=config,
        )

    # ------------------------------------------------------------ помощники

    def tweak(self, tweak_id: str):
        for item in self.tweaks:
            if item.id == tweak_id:
                return item
        return None

    def any_tweak(self, tweak_id: str):
        """Твик из обычного каталога или из экспертного."""
        found = self.tweak(tweak_id)
        if found is not None:
            return found
        for item in self.expert_tweaks:
            if item.id == tweak_id:
                return item
        return None

    @property
    def tariff(self) -> float:
        try:
            return float(self.config.get("tariff", 4.32))
        except (TypeError, ValueError):
            return 4.32

    @property
    def currency(self) -> str:
        return str(self.config.get("currency", "₴"))

    def money(self, amount: float) -> str:
        return f"{amount:.2f} {self.currency}"

    def save(self) -> None:
        save_config(self.config)

    def shutdown(self) -> None:
        for background in (self.watcher, self.balancer, self.autooc):
            if background is not None:
                try:
                    background.stop()
                except Exception:
                    pass
        if self.effects and self.effects.running:
            self.effects.stop()
        if self.rgb_client:
            self.rgb_client.disconnect()
        if self.power_meter and self.power_meter.samples:
            try:
                save_session(self.power_meter)
            except Exception as exc:
                log(f"Не удалось сохранить историю потребления: {exc}")
        self.save()
