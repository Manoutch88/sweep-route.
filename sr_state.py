import streamlit as st
import dataclasses
import os
import json
from datetime import datetime
from typing import List, Tuple, Optional, Dict

from sr_core import (
    Config, DeliveryPoint, Contact, RouteConfig, RouteResult,
    _norm_addr, WORK_START, DEFAULT_INTERVENTION_TYPE, SPM
)
from sr_persistence import (
    HistoryManager, ContactManager, AddressBookManager,
    RouteManager, GeoCache, OSRMCache
)
from sr_logic import Optimizer

# ==========================================================
# STATE MANAGER
# ==========================================================
class StateManager:
    @staticmethod
    def init():
        defaults = {
            "delivery_points": [],
            "route_config": RouteConfig(),
            "coord_cache": {},
            "map_zoom_target": None,
            "optimized_result": None,
            "last_error": None,
            "map_click_address": None,
            "map_click_coords": None,
            "map_click_queue": [],
            "address_book": [],
            "address_suggestions": {},
            "opt_pause_start":  Config.PAUSE_DEFAULT_START,
            "opt_pause_end":    Config.PAUSE_DEFAULT_END,
            "opt_pause_enabled": False,
            "opt_parking_time": Config.PARKING_TIME // SPM,
            # L'historique est désormais enregistré uniquement via le bouton
            # "✅ Valider et enregistrer dans l'historique" dans l'onglet Planification.
            "_mode_test": False,
            "contact_sort_col": "name",
            "contact_sort_desc": False,
            "use_fixed_start": True
            }
        for k, v in defaults.items():
            if k not in st.session_state: st.session_state[k] = v
        
        # Synchronisation de la config avec le state
        Config.PARKING_TIME = st.session_state.opt_parking_time * SPM

        if "address_book_loaded" not in st.session_state:
            from sr_logic import Geo
            from sr_persistence import RouteManager, WaitlistManager
            Geo._init_session_early()
            AddressBookManager.load_from_file()
            WaitlistManager.load()  # Chargement initial de la file d'attente
            GeoCache.load()
            OSRMCache.load()
            
            # ── Détection automatique de l'autosave au démarrage ───────────────
            # Si le fichier d'autosave existe et n'est pas déjà chargé
            autosave_path = os.path.join(RouteManager.SAVE_DIR, f"{RouteManager.AUTOSAVE_NAME}.json")
            if os.path.exists(autosave_path) and not st.session_state.delivery_points:
                try:
                    with open(autosave_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        pts = data.get("points", [])
                        if len(pts) > 0:
                            st.session_state["_autosave_available"] = {
                                "n_pts": len(pts),
                                "saved_at": datetime.fromtimestamp(os.path.getmtime(autosave_path)).isoformat()
                            }
                except Exception:
                    pass  # Autosave illisible — on ignore silencieusement

            st.session_state.address_book_loaded = True

    @staticmethod
    def add_point(address, name="", coords=None):
        if not name: name = ContactManager.get_name_by_addr(address)
        st.session_state.delivery_points.append(DeliveryPoint(address=address, name=name, coordinates=coords))
        StateManager.invalidate_optim_cache()

    @staticmethod
    def add_from_history(h: dict):
        coords = h.get("coordinates")
        if not coords: coords = st.session_state.coord_cache.get(_norm_addr(h.get("address", "")))
        name = h.get("name", "")
        if not name: name = ContactManager.get_name_by_addr(h.get("address", ""))
        StateManager.add_point(h.get("address", ""), name=name, coords=coords)
        pts = st.session_state.delivery_points
        if pts:
            pts[-1].intervention_type = h.get("intervention_type", DEFAULT_INTERVENTION_TYPE)
            pts[-1].notes             = h.get("notes", "")
            pts[-1].time_mode         = h.get("time_mode", "Libre")
            pts[-1].service_duration  = h.get("service_duration", 2700)
            if pts[-1].time_mode == "Heure précise" and pts[-1].target_time is None:
                _ai_idx = ContactManager.get_index_by_addr()
                _ai_match = _ai_idx.get(_norm_addr(pts[-1].address))
                if _ai_match: pts[-1].target_time = _ai_match[1].get("preferred_time")
        StateManager.invalidate_optim_cache()

    @staticmethod
    def add_contact_to_route(contact_dict: dict):
        name = contact_dict.get("name", "")
        if not name: name = ContactManager.get_name_by_addr(contact_dict["address"])
        point = DeliveryPoint(
            address=contact_dict["address"], name=name, coordinates=contact_dict.get("coordinates"),
            intervention_type=contact_dict.get("intervention_type", DEFAULT_INTERVENTION_TYPE),
            notes=contact_dict.get("notes", ""), service_duration=contact_dict.get("service_duration", 45 * SPM),
            time_mode=contact_dict.get("time_mode", "Libre"), target_time=contact_dict.get("preferred_time"),
        )
        st.session_state.delivery_points.append(point)
        StateManager.invalidate_optim_cache()

    @staticmethod
    def remove_point(i):
        if 0 <= i < len(st.session_state.delivery_points):
            p = st.session_state.delivery_points.pop(i)
            StateManager._clear_point_widgets(p.uid)
        st.session_state["_clear_addr_widgets"] = True
        st.rerun()

    @staticmethod
    def update_point(i, **kw):
        p = st.session_state.delivery_points[i]
        for k, v in kw.items():
            if k == "address": p.set_address(v)
            elif hasattr(p, k): setattr(p, k, v)
        # st.session_state["_clear_addr_widgets"] = True # Plus besoin de vider tout
        st.rerun()

    @staticmethod
    def _clear_point_widgets(uid: str):
        for prefix in ("mode_", "svc_", "notes_", "itype_", "tgt_h_", "tgt_m_", "prev_type_", "addr_", "is_start_", "is_end_", "ti_"):
            st.session_state.pop(f"{prefix}{uid}", None)

    @staticmethod
    def _clear_address_widget_keys():
        """Vide tous les widgets liés aux points (nettoyage global)."""
        for k in list(st.session_state.keys()):
            for prefix in ("mode_", "svc_", "notes_", "itype_", "tgt_h_", "tgt_m_", "prev_type_", "addr_", "is_start_", "is_end_", "ti_"):
                if k.startswith(prefix):
                    st.session_state.pop(k, None)

    @staticmethod
    def points() -> List[DeliveryPoint]: 
        return st.session_state.delivery_points

    @staticmethod
    def config() -> RouteConfig: 
        return st.session_state.route_config

    @staticmethod
    def update_config(**kw):
        c = st.session_state.route_config
        for k, v in kw.items():
            if hasattr(c, k): setattr(c, k, v)

    @staticmethod
    def commit(do_rerun: bool = True):
        if st.session_state.optimized_result is not None:
            cfg, pts = st.session_state.route_config, st.session_state.delivery_points
            if bool(cfg.start_coordinates) and bool(cfg.end_coordinates) and bool(pts) and all(p.coordinates for p in pts):
                st.session_state["_reopt_pending"] = True
            else: st.session_state.optimized_result = None
        RouteManager.autosave()
        if do_rerun: st.rerun()

    @staticmethod
    def save_to_history():
        today_str = datetime.today().strftime("%d/%m/%Y")
        for p in st.session_state.delivery_points:
            HistoryManager.add_visit(p, today_str)
        ContactManager.invalidate_index()

    @staticmethod
    def move_result_node(step: int, direction: int):
        result, mats = st.session_state.get("optimized_result"), StateManager.get_last_mats()
        if not result or not mats: return
        order, target = list(result.order), step + direction
        if target <= 0 or target >= len(order) - 1: return
        order[step], order[target] = order[target], order[step]
        cfg, pts = StateManager.config(), StateManager.points()
        optim_pts = [p for p in pts if not p.is_start and not p.is_end]
        _start_pt = next((p for p in pts if p.is_start), None)
        new_arrivals = Optimizer._compute_times(order, cfg.start_time, mats[1], [_start_pt.service_duration if _start_pt else cfg.start_service_duration] + [p.service_duration for p in optim_pts] + [0], optim_pts)
        st.session_state.optimized_result = dataclasses.replace(result, order=order, arrival_times=new_arrivals, total_distance=sum(float(mats[0][order[k]][order[k+1]]) for k in range(len(order) - 1)), total_time=new_arrivals[-1]-cfg.start_time, is_approximation=True)

    @staticmethod
    def move_point_up(i: int):
        pts = st.session_state.delivery_points
        if i > 0: pts[i], pts[i - 1] = pts[i - 1], pts[i]; StateManager.commit()

    @staticmethod
    def move_point_down(i: int):
        pts = st.session_state.delivery_points
        if i < len(pts) - 1: pts[i], pts[i + 1] = pts[i + 1], pts[i]; StateManager.commit()

    @staticmethod
    def is_duplicate_address(address: str) -> bool: 
        return any(p.address_norm == _norm_addr(address) for p in st.session_state.delivery_points)

    @staticmethod
    def is_duplicate_contact(name: str, address: str) -> bool:
        if "_contact_composite_idx" not in st.session_state: ContactManager.build_index()
        return (_norm_addr(name), _norm_addr(address)) in st.session_state["_contact_composite_idx"]

    @staticmethod
    def auto_add_to_book(points: List["DeliveryPoint"], arrival_times: List[int], order: List[int], today_str: str):
        book_addrs, missing = {_norm_addr(c["address"]) for c in st.session_state.address_book}, []
        for step, node in enumerate(order):
            if node <= 0 or node > len(points): continue
            p = points[node - 1]
            if _norm_addr(p.address) not in book_addrs: missing.append({"address": p.address, "intervention_type": p.intervention_type, "notes": p.notes, "service_duration": p.service_duration, "last_intervention": today_str})
        st.session_state["_auto_add_candidates"] = missing

    @staticmethod
    def clear_points() -> None:
        st.session_state.delivery_points, st.session_state.optimized_result = [], None
        # FIX #1 — boucle réintégrée dans la méthode (était au niveau classe → ne s'exécutait jamais au bon moment)
        for k in ("_last_mats", "_optim_cache", "_manual_plan", "_reopt_pending"):
            st.session_state.pop(k, None)
        
    @staticmethod
    def request_reoptimize() -> None: 
        st.session_state.pop("_optim_cache", None)
        st.session_state.optimized_result, st.session_state["_reopt_pending"] = None, True

    @staticmethod
    def pop_reopt_pending() -> bool: 
        return bool(st.session_state.pop("_reopt_pending", False))

    @staticmethod
    def invalidate_optim_cache() -> None: 
        st.session_state.pop("_optim_cache", None)

    @staticmethod
    def set_last_mats(mats) -> None: 
        st.session_state["_last_mats"] = mats

    @staticmethod
    def get_last_mats(): 
        return st.session_state.get("_last_mats")

    @staticmethod
    def set_manual_plan(value: bool) -> None: 
        st.session_state["_manual_plan"] = value

    @staticmethod
    def is_manual_plan() -> bool: 
        return bool(st.session_state.get("_manual_plan", False))

    @staticmethod
    def get_auto_add_candidates() -> list: 
        return st.session_state.get("_auto_add_candidates", [])

    @staticmethod
    def set_auto_add_candidates(candidates: list) -> None: 
        st.session_state["_auto_add_candidates"] = candidates
