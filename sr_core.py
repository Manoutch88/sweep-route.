import streamlit as st
import time
import math
import unicodedata
import html as _html
import dataclasses
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dt_time
from typing import List, Tuple, Optional, Dict, Set
from dataclasses import dataclass, field
import json
import csv
import io
import os
import re

# ── UTILITAIRES DE BASE ──────────────────────────────────────────────────────
def _h(s: str) -> str:
    """Échappe les caractères HTML dans une chaîne utilisateur."""
    return _html.escape(str(s))

def _sort_key_date(d: str):
    """Clé de tri robuste pour dates dd/mm/yyyy → (yyyy, mm, dd) en int."""
    try:
        parts = d.split("/")
        if len(parts) == 3:
            return (int(parts[2]), int(parts[1]), int(parts[0]))
    except (ValueError, IndexError):
        pass
    return (9999, 12, 31)

def _parse_fr_date(s: str) -> str:
    if not s or "/" in s:
        return s
    s_clean = s.strip().lower().rstrip(".,;!")
    months = {
        "janvier": "01", "fevrier": "02", "mars": "03", "avril": "04",
        "mai": "05", "juin": "06", "juillet": "07", "aout": "08",
        "septembre": "09", "octobre": "10", "novembre": "11", "decembre": "12"
    }
    for m_name, m_num in months.items():
        if m_name in s_clean:
            m = re.search(r"(\d{1,2})", s_clean)
            d = f"{int(m.group(1)):02d}" if m else "01"
            y_m = re.search(r"(\d{4})", s_clean)
            y = y_m.group(1) if y_m else str(datetime.now().year)
            return f"{d}/{m_num}/{y}"
    return s

def _norm_addr(s: str) -> str:
    if not s: return ""
    s = "".join(c for c in unicodedata.normalize("NFD", str(s)) if unicodedata.category(c) != "Mn")
    s = s.lower().replace(",", " ").replace(".", " ").replace("-", " ")
    s = re.sub(r"\s+", " ", s).strip()
    # Supprimer les mots de liaison communs pour le matching
    for w in [" rue ", " avenue ", " boulevard ", " chemin ", " impasse ", " route ", " place ", " allee "]:
        s = s.replace(w, " ")
    return s.strip()

def _cap_cache(cache: dict, max_size: int):
    if len(cache) > max_size:
        keys = list(cache.keys())
        for i in range(len(keys) // 5):
            cache.pop(keys[i], None)

# ── CONSTANTES TEMPORELLES ───────────────────────────────────────────────────
SPM = 60
SPH = 3600
WORK_START = 8 * SPH
WORK_END   = 19 * SPH
MIDI       = 12 * SPH
DEBUT_APM  = 13 * SPH + 15 * SPM

# ── REGEX ────────────────────────────────────────────────────────────────────
_RE_POSTCODE = re.compile(r"\b\d{5}\b")
_RE_CITY_EXTRACTOR = re.compile(r"\b\d{5}\s+[A-ZÀ-ÿ\s\-]+\b", re.IGNORECASE)
_RE_ORDINAL = re.compile(r"(\d+)(?:er|re|e|eme)\b", re.IGNORECASE)
_RE_RTA_TO_DOT = re.compile(r"(\d+)h(\d*)", re.IGNORECASE)
_RE_DU = re.compile(r"\bdu\b", re.IGNORECASE)
_RE_DES = re.compile(r"\bdes\b", re.IGNORECASE)
_RE_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_\-]")

# ── TYPES D'INTERVENTION ─────────────────────────────────────────────────────
INTERVENTION_TYPES = {
    "Standard_70": 70 * SPM,
    "Standard_45": 45 * SPM,
    "Expertise_120": 120 * SPM,
    "Standard_30": 30 * SPM,
    "Standard_60": 60 * SPM,
    "Standard_80": 80 * SPM,
    "Standard_90": 90 * SPM
}
INTERVENTION_KEYS = list(INTERVENTION_TYPES.keys())
DEFAULT_INTERVENTION_TYPE = "Standard_45"

# ── CONFIGURATION GLOBALE ────────────────────────────────────────────────────
class Config:
    OSRM_URL             = "http://router.project-osrm.org"
    OSRM_MAX_COORDS      = 40
    OSRM_TIMEOUT         = 10
    GEO_TIMEOUT_SHORT    = 10
    GEO_TIMEOUT_LONG     = 15
    NOMINATIM_DELAY      = 1.0
    PHOTON_DELAY         = 0.15
    GEO_MAX_WORKERS      = 4
    MAX_GEO_CACHE             = 5_000
    MAX_OSRM_CACHE            = 10_000
    GEO_CACHE_WRITE_INTERVAL  = 10
    OSRM_CACHE_WRITE_INTERVAL = 10
    ADDR_SEARCH_LIMIT    = 5
    HIST_PAGE_SIZE       = 10
    CROW_FLIES_SPEED     = 35 / 3.6 # m/s
    PARKING_TIME         = 5 * SPM  # 300 s — marge de stationnement incluse entre deux clients
    PAUSE_DEFAULT_START  = 12 * SPH
    PAUSE_DEFAULT_END    = 13 * SPH + 15 * SPM
    DEFAULT_START_ADDRESS = "9 chemin des pinasses 88530 Le Tholy"
    DEFAULT_END_ADDRESS   = "9 chemin des pinasses 88530 Le Tholy"
    WORK_START = 8 * SPH
    WORK_END   = 19 * SPH

OSRM_URL = Config.OSRM_URL
MAP_CENTER = [48.087277258810744, 6.728637352753757] # Coordonnées personnalisées

# ── CLASSES UTILITAIRES ──────────────────────────────────────────────────────
class TimeUtils:
    @staticmethod
    def to_seconds(t: dt_time) -> int:
        return t.hour * 3600 + t.minute * 60 + t.second
    @staticmethod
    def from_seconds(s: int) -> dt_time:
        s = int(s); return dt_time(hour=(s // 3600) % 24, minute=(s % 3600) // 60, second=s % 60)
    @staticmethod
    def fmt_hm(s: int) -> str:
        s = int(s); return f"{s//3600:02d}:{(s%3600)//60:02d}"

class TW:
    @staticmethod
    def get(p: "DeliveryPoint") -> Tuple[int, int]:
        m = p.time_mode
        if m == "Heure précise":
            t = p.target_time or WORK_START
            return (t - 5 * SPM, t + 5 * SPM)
        if m == "Matin": return (WORK_START, MIDI)
        if m == "Après-midi": return (DEBUT_APM, WORK_END)
        return (WORK_START, WORK_END)
    @staticmethod
    def fmt(lo: int, hi: int) -> str:
        return f"{TimeUtils.fmt_hm(lo)}–{TimeUtils.fmt_hm(hi)}"

def _streamlit_version():
    try:
        from importlib.metadata import version
        v = version("streamlit")
        parts = v.split(".")
        return int(parts[0]), int(parts[1])
    except Exception: return (0, 0)

_ST_MAJOR, _ST_MINOR = _streamlit_version()
HAS_DIALOG   = (_ST_MAJOR, _ST_MINOR) >= (1, 35)
HAS_POPOVER  = (_ST_MAJOR, _ST_MINOR) >= (1, 31)
HAS_FRAGMENT = (_ST_MAJOR, _ST_MINOR) >= (1, 37)

def _import_folium():
    import folium as _folium
    from streamlit_folium import st_folium as _st_folium
    return _folium, _st_folium

# ── DATA CLASSES ─────────────────────────────────────────────────────────────
@dataclass
class DeliveryPoint:
    address: str
    name: str = ""
    coordinates: Optional[Tuple[float, float]] = None
    time_mode: str = "Libre"
    target_time: Optional[int] = None
    intervention_type: str = DEFAULT_INTERVENTION_TYPE
    notes: str = ""
    service_duration: int = 45 * SPM
    is_start: bool = False
    is_end: bool = False
    is_visited: bool = False
    uid: str = field(default_factory=lambda: os.urandom(4).hex())
    _address_norm: str = field(init=False, repr=False)

    def __post_init__(self):
        object.__setattr__(self, "_address_norm", _norm_addr(self.address))
    @property
    def address_norm(self) -> str: return self._address_norm
    def set_address(self, addr: str):
        self.address = addr
        object.__setattr__(self, "_address_norm", _norm_addr(addr))

@dataclass
class Contact:
    name: str
    address: str
    phone: str = ""
    intervention_type: str = DEFAULT_INTERVENTION_TYPE
    notes: str = ""
    service_duration: int = 45 * SPM
    visit_dates: List[str] = field(default_factory=list)
    time_mode: str = "Libre"
    preferred_time: Optional[int] = None
    available_weekdays: str = ""
    preferred_month: int = 0

@dataclass
class RouteConfig:
    start_address: str = Config.DEFAULT_START_ADDRESS
    end_address: str = Config.DEFAULT_END_ADDRESS
    start_time: int = WORK_START
    start_service_duration: int = 0
    start_coordinates: Optional[Tuple[float, float]] = None
    end_coordinates: Optional[Tuple[float, float]] = None

@dataclass
class RouteResult:
    order: List[int]
    arrival_times: List[int]
    total_distance: float
    total_time: int
    is_approximation: bool = False
    initial_distance: float = 0.0
    tour_hash: int = 0
