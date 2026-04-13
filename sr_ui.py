import streamlit as st
import os
import json
from datetime import datetime, time as dt_time
from typing import List, Tuple, Optional, Dict

from sr_core import (
    Config, DeliveryPoint, Contact, RouteConfig, RouteResult,
    _norm_addr, _h, _sort_key_date, _parse_fr_date, _RE_SAFE_NAME, _RE_POSTCODE,
    INTERVENTION_TYPES, DEFAULT_INTERVENTION_TYPE, INTERVENTION_KEYS,
    TimeUtils, TW, MAP_CENTER, HAS_DIALOG, HAS_POPOVER, HAS_FRAGMENT,
    SPH, SPM, _import_folium
)
from sr_persistence import (
    HistoryManager, ContactManager, AddressBookManager,
    RouteManager, WaitlistManager
)
from sr_logic import Geo, OSRM, Optimizer, Validator
from sr_state import StateManager

# Compatibilité Python < 3.9 : évite la syntaxe @(expr) (PEP 614)
_maybe_fragment = st.fragment if HAS_FRAGMENT else (lambda f: f)

# ==========================================================
# UI CLASS
# ==========================================================
class UI:
    @staticmethod
    def _get_warning_nodes(result: RouteResult, points: List[DeliveryPoint]) -> dict:
        if not result: return {}
        warnings, optim_points = {}, [p for p in points if not p.is_start and not p.is_end]
        n_optim, p_ = len(optim_points), Optimizer._params()
        PAUSE_S, PAUSE_E, USE_PAUSE = p_["pause_start"], p_["pause_end"], p_.get("pause_enabled", False)
        fmt = lambda s: f"{s//3600:02d}:{(s%3600)//60:02d}"
        for step, node in enumerate(result.order):
            if 0 < node <= n_optim:
                pt, arr = optim_points[node - 1], result.arrival_times[step]
                fin, msgs = arr + pt.service_duration, []
                if not USE_PAUSE:
                    if arr < PAUSE_S < fin: msgs.append(f"⏸️ service à cheval sur la pause ({fmt(PAUSE_S)}–{fmt(PAUSE_E)})")
                    elif PAUSE_S <= arr < PAUSE_E: msgs.append(f"⏸️ arrivée pendant la pause ({fmt(arr)})")
                if pt.time_mode == "Heure précise":
                    if pt.target_time is not None:
                        lo, _ = TW.get(pt)
                        if arr > lo + 5 * 60: msgs.append(f"⏰ retard {(arr-lo)//60}min sur {fmt(lo)}")
                    else: msgs.append("⚠️ Heure précise sans heure cible définie")
                elif pt.time_mode == "Matin" and arr >= PAUSE_E: msgs.append(f"🌅 matin mais arrivée à {fmt(arr)}")
                elif pt.time_mode == "Après-midi" and arr < PAUSE_S: msgs.append(f"🌆 APM mais arrivée à {fmt(arr)}")
                if msgs: warnings[node] = " · ".join(msgs)
        return warnings

    @staticmethod
    def _edit_point(idx: int, address: str):
        if HAS_DIALOG:
            @st.dialog("✏️ Modifier l'adresse")
            def _dlg():
                new_addr = st.text_input("Adresse", value=address, key="edit_addr_input")
                if new_addr and Geo.is_incomplete_address(new_addr): st.caption("💡 Pas de code postal — géocodage peut échouer")
                col_ok, col_no = st.columns(2)
                with col_ok:
                    if st.button("✅ Valider", type="primary", use_container_width=True, key="dlg_edit_ok"):
                        if new_addr.strip() and new_addr.strip() != address:
                            pts = StateManager.points(); pts[idx].set_address(new_addr.strip()); pts[idx].coordinates = None; StateManager.commit()
                        else: st.rerun()
                with col_no:
                    if st.button("Annuler", use_container_width=True, key="dlg_edit_no"): st.rerun()
            _dlg()
        else:
            key_edit = f"_pending_edit_{idx}"
            if st.session_state.get(key_edit):
                new_addr = st.text_input("Nouvelle adresse", value=address, key=f"edit_addr_inline_{idx}")
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("✅ Valider", key=f"edit_ok_{idx}", type="primary", use_container_width=True):
                        if new_addr.strip() and new_addr.strip() != address:
                            pts = StateManager.points(); pts[idx].set_address(new_addr.strip()); pts[idx].coordinates = None
                        st.session_state.pop(key_edit, None); StateManager.commit()
                with c2:
                    if st.button("Annuler", key=f"edit_no_{idx}", use_container_width=True): st.session_state.pop(key_edit, None); st.rerun()
            else: st.session_state[key_edit] = True; st.rerun()

    @staticmethod
    def _confirm_delete_point(idx: int, address: str):
        if HAS_DIALOG:
            @st.dialog("🗑️ Supprimer ce point ?")
            def _dlg():
                st.write(f"**{address}**"); st.caption("Cette action supprimera le point de la tournée en cours.")
                col_ok, col_no = st.columns(2)
                with col_ok:
                    if st.button("✅ Confirmer", type="primary", use_container_width=True, key="dlg_del_ok"): StateManager.remove_point(idx); StateManager.commit()
                with col_no:
                    if st.button("Annuler", use_container_width=True, key="dlg_del_no"): st.rerun()
            _dlg()
        else:
            key_confirm = f"_pending_del_{idx}"
            if st.session_state.get(key_confirm):
                st.warning(f"Confirmer la suppression de **{address[:40]}** ?")
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("✅ Oui, supprimer", key=f"dlg_del_ok_{idx}", type="primary", use_container_width=True): st.session_state.pop(key_confirm, None); StateManager.remove_point(idx); StateManager.commit()
                with c2:
                    if st.button("Annuler", key=f"dlg_del_no_{idx}", use_container_width=True): st.session_state.pop(key_confirm, None); st.rerun()
            else: st.session_state[key_confirm] = True; st.rerun()

    @staticmethod
    def _confirm_delete_save(save_name: str):
        if HAS_DIALOG:
            @st.dialog("🗑️ Supprimer la sauvegarde ?")
            def _dlg():
                st.write(f"**« {save_name} »**"); st.caption("Cette action est irréversible.")
                col_ok, col_no = st.columns(2)
                with col_ok:
                    if st.button("✅ Supprimer", type="primary", use_container_width=True, key="dlg_save_ok"): RouteManager.delete(save_name); st.rerun()
                with col_no:
                    if st.button("Annuler", use_container_width=True, key="dlg_save_no"): st.rerun()
            _dlg()
        else:
            if RouteManager.delete(save_name): st.rerun()

    # Google Maps Web URL API : 10 waypoints intermédiaires maximum.
    GMAPS_MAX_WAYPOINTS = 10

    @staticmethod
    def export_google_maps_url(result: RouteResult, cfg: RouteConfig, points: List[DeliveryPoint]) -> Tuple[str, Optional[str]]:
        """Retourne (url, warning_msg).
        warning_msg est non-None si la tournée dépasse la limite de waypoints Google Maps.
        """
        all_coords = [cfg.start_coordinates] + [p.coordinates for p in points] + [cfg.end_coordinates]
        ordered_coords = [all_coords[node] for node in result.order if all_coords[node]]
        if len(ordered_coords) < 2: return "", None
        waypoints = ordered_coords[1:-1]
        warning = None
        if len(waypoints) > UI.GMAPS_MAX_WAYPOINTS:
            warning = (
                f"⚠️ Google Maps accepte 10 étapes intermédiaires maximum. "
                f"Seules les {UI.GMAPS_MAX_WAYPOINTS} premières sur {len(waypoints)} sont incluses dans le lien."
            )
            waypoints = waypoints[:UI.GMAPS_MAX_WAYPOINTS]
        url = (
            f"https://www.google.com/maps/dir/?api=1"
            f"&origin={ordered_coords[0][0]},{ordered_coords[0][1]}"
            f"&destination={ordered_coords[-1][0]},{ordered_coords[-1][1]}"
        )
        if waypoints:
            url += f"&waypoints={'|'.join(f'{c[0]},{c[1]}' for c in waypoints)}"
        return url + "&travelmode=driving", warning

    @staticmethod
    def _edit_contact_dialog(h: dict, h_name: str, h_addr: str, open_key: str = None):
        display_title = h_name or h_addr[:35]
        if HAS_DIALOG:
            @st.dialog(f"✏️ {display_title}")
            def _dlg():
                new_name  = st.text_input("Nom",  value=h.get("name", ""),  key="dlg_c_name")
                new_phone = st.text_input("Tél",  value=h.get("phone", ""), key="dlg_c_phone")
                _is_unknown = h.get("address_unknown", False) or h.get("address","") == "ADRESSE_INCONNUE"
                if _is_unknown:
                    st.warning("⚠️ Adresse inconnue — renseignez-la ci-dessous pour pouvoir planifier ce contact.")
                new_addr_unknown = st.checkbox(
                    "⚠️ Adresse inconnue",
                    value=_is_unknown,
                    key="dlg_c_addr_unk",
                    help="Décochez et renseignez l'adresse pour activer ce contact dans la planification."
                )
                if new_addr_unknown:
                    new_addr = "ADRESSE_INCONNUE"
                    st.info("📋 Ce contact restera en attente d'adresse.")
                else:
                    _addr_default = "" if _is_unknown else h.get("address", "")
                    new_addr = st.text_input("Adresse", value=_addr_default, key="dlg_c_addr")
                cur_itype = h.get("intervention_type", DEFAULT_INTERVENTION_TYPE)
                new_itype = st.selectbox("Type", INTERVENTION_KEYS, index=INTERVENTION_KEYS.index(cur_itype) if cur_itype in INTERVENTION_KEYS else 1, key="dlg_c_itype")
                new_svc_min = st.number_input("Min", min_value=5, max_value=480, step=5, value=max(5, h.get("service_duration", 3600) // 60), key="dlg_c_svc")
                new_notes = st.text_area("Notes", value=h.get("notes", ""), height=80, key="dlg_c_notes")
                _TM_OPTS = ["Libre", "Matin", "Après-midi", "Heure précise"]
                _cur_tm = h.get("time_mode", "Libre")
                new_time_mode = st.selectbox("Dispo", _TM_OPTS, index=_TM_OPTS.index(_cur_tm) if _cur_tm in _TM_OPTS else 0, key="dlg_c_tmode")
                new_preferred_time = h.get("preferred_time")
                if new_time_mode == "Heure précise":
                    _def_pt = dt_time(7, 45)
                    if new_preferred_time is not None:
                        try: _def_pt = dt_time(new_preferred_time // 3600, (new_preferred_time % 3600) // 60)
                        except (ValueError, TypeError): pass  # Heure stockée invalide — on garde 08:00 par défaut
                    _pt_val = st.time_input("Heure", value=_def_pt, key="dlg_c_pt"); new_preferred_time = _pt_val.hour * SPH + _pt_val.minute * SPM
                else: new_preferred_time = None
                new_weekdays = st.text_input("Jours", value=h.get("available_weekdays", ""), key="dlg_c_wd")
                _MOIS_DLG = ["Non défini","Janvier","Février","Mars","Avril","Mai","Juin","Juillet","Août","Septembre","Octobre","Novembre","Décembre"]
                new_pmonth = st.selectbox("Relance", range(len(_MOIS_DLG)), index=min(int(h.get("preferred_month", 0)), 12), format_func=lambda x: _MOIS_DLG[x], key="dlg_c_pm")
                cur_dates = sorted([d for d in h.get("visit_dates", []) if d], key=_sort_key_date)
                kept_dates = st.multiselect(f"Dates ({len(cur_dates)})", options=cur_dates, default=cur_dates, key="dlg_c_dates")
                new_date_str = st.text_input("Ajouter date", value="", key="dlg_c_newdate")
                st.markdown("---")
                col_save, col_del = st.columns([3, 1])
                with col_save:
                    old_name_norm, old_addr_norm = _norm_addr(h_name), _norm_addr(h_addr)
                    if "_contact_composite_idx" not in st.session_state: ContactManager.build_index()
                    book_idx = -1
                    if (old_name_norm, old_addr_norm) in st.session_state["_contact_composite_idx"]:
                        book_idx, _ = st.session_state["_contact_composite_idx"][(old_name_norm, old_addr_norm)]
                    elif old_addr_norm in st.session_state.get("_contact_addr_idx", {}):
                        book_idx, _ = st.session_state["_contact_addr_idx"][old_addr_norm]
                    elif old_name_norm in st.session_state.get("_contact_name_idx", {}):
                        book_idx, _ = st.session_state["_contact_name_idx"][old_name_norm]

                    # Clé unique pour session_state basée sur l'identité originale
                    state_key = f"{old_name_norm}_{old_addr_norm}"
                    m_t = st.session_state.get(f"m_t_{state_key}")
                    if m_t is not None:
                        st.warning("⚠️ Un client avec ce nom et cette adresse existe déjà.")
                        if st.button("🤝 Fusionner", type="primary", use_container_width=True, key="dlg_c_merge"):
                            data = st.session_state.get(f"m_d_{state_key}")
                            if data:
                                if book_idx != -1:
                                    # FIX bug 3+4 : mettre à jour le contact édité, puis fusionner
                                    # le doublon (m_t) DANS le contact édité (book_idx = cible à conserver)
                                    ContactManager.update_contact(book_idx, **data)
                                    if ContactManager.merge_contacts(book_idx, m_t):
                                        st.toast("✅ Fusion et synchronisation CSV réussies")
                                else:
                                    # Contact introuvable par index : l'ajouter proprement (bug 5)
                                    import dataclasses as _dc
                                    _cf = {f.name for f in _dc.fields(Contact)}
                                    new_entry = {k: v for k, v in data.items() if k in _cf}
                                    new_dict = _dc.asdict(Contact(**new_entry))
                                    new_dict["visit_dates"] = data.get("visit_dates", [])
                                    st.session_state.address_book.append(new_dict)
                                    ContactManager.invalidate_index(); AddressBookManager.save_to_file(sync_import_csv=True)
                            st.session_state.pop(f"m_t_{state_key}", None); st.session_state.pop(f"m_d_{state_key}", None)
                            if open_key: st.session_state.pop(open_key, None)
                            st.rerun()
                        if st.button("Annuler", use_container_width=True, key="dlg_c_merge_can"):
                            st.session_state.pop(f"m_t_{state_key}", None); st.session_state.pop(f"m_d_{state_key}", None)
                            if open_key: st.session_state.pop(open_key, None)
                            st.rerun()
                    elif st.button("💾 Enregistrer", type="primary", use_container_width=True, key="dlg_c_save"):
                        merged_dates, nd = list(kept_dates), new_date_str.strip()
                        if nd: nd = _parse_fr_date(nd)
                        if nd and nd not in merged_dates: merged_dates.append(nd)
                        
                        new_name_s, new_addr_s = new_name.strip(), new_addr.strip()
                        dupe_idx = ContactManager.find_duplicate(new_name_s, new_addr_s, exclude_index=book_idx, exact_match=True)
                        
                        data = {
                            "name": new_name_s, "address": new_addr_s, "phone": new_phone.strip(),
                            "intervention_type": new_itype, "service_duration": new_svc_min * 60,
                            "notes": new_notes.strip(), "visit_dates": merged_dates,
                            "time_mode": new_time_mode, "preferred_time": new_preferred_time,
                            "available_weekdays": new_weekdays.strip(), "preferred_month": int(new_pmonth),
                            "address_unknown": new_addr_unknown,
                        }
                        
                        if dupe_idx is not None:
                            st.session_state[f"m_t_{state_key}"] = dupe_idx
                            st.session_state[f"m_d_{state_key}"] = data
                            st.rerun()  # dialog reste ouvert (open_key conservé intentionnellement)
                        else:
                            if book_idx != -1:
                                ContactManager.update_contact(book_idx, **data)
                                st.toast("✅ Enregistré et synchronisé CSV")
                            else:
                                # FIX bug 5 : Contact() ne connaît pas visit_dates
                                import dataclasses as _dc
                                _cf = {f.name for f in _dc.fields(Contact)}
                                new_entry = {k: v for k, v in data.items() if k in _cf}
                                new_dict = _dc.asdict(Contact(**new_entry))
                                new_dict["visit_dates"] = data.get("visit_dates", [])
                                st.session_state.address_book.append(new_dict)
                                ContactManager.invalidate_index(); AddressBookManager.save_to_file(sync_import_csv=True)
                                st.toast("✅ Nouveau contact créé et synchronisé CSV")
                            
                            if _norm_addr(new_addr_s) != old_addr_norm: st.session_state.setdefault("coord_cache", {}).pop(old_addr_norm, None); ContactManager.invalidate_index()
                            if open_key: st.session_state.pop(open_key, None)
                            st.rerun()
                with col_del:
                    if st.button("🗑️", use_container_width=True, key="dlg_c_del"):
                        HistoryManager.delete_entry(h_addr, h_name)
                        if open_key: st.session_state.pop(open_key, None)
                        st.rerun()
            _dlg()
        else:
            with st.expander("✏️ Gérer ce client", expanded=False):
                old_name_norm, old_addr_norm = _norm_addr(h_name), _norm_addr(h_addr)
                if "_contact_composite_idx" not in st.session_state: ContactManager.build_index()
                book_idx = -1
                if (old_name_norm, old_addr_norm) in st.session_state["_contact_composite_idx"]:
                    book_idx, _ = st.session_state["_contact_composite_idx"][(old_name_norm, old_addr_norm)]
                elif old_addr_norm in st.session_state.get("_contact_addr_idx", {}):
                    book_idx, _ = st.session_state["_contact_addr_idx"][old_addr_norm]
                elif old_name_norm in st.session_state.get("_contact_name_idx", {}):
                    book_idx, _ = st.session_state["_contact_name_idx"][old_name_norm]
                state_key = f"{old_name_norm}_{old_addr_norm}"

                # FIX bug 2 : le bloc fusion doit être HORS du st.form (st.button interdit dans un form)
                m_t = st.session_state.get(f"m_t_{state_key}")
                if m_t is not None:
                    st.warning("⚠️ Un client avec ce nom et cette adresse existe déjà.")
                    if st.button("🤝 Fusionner", type="primary", use_container_width=True, key=f"merge_fb_{state_key}"):
                        data = st.session_state.get(f"m_d_{state_key}")
                        if data:
                            if book_idx != -1:
                                # FIX bug 3+4 : book_idx = cible à conserver, m_t = doublon à supprimer
                                ContactManager.update_contact(book_idx, **data)
                                if ContactManager.merge_contacts(book_idx, m_t):
                                    st.toast("✅ Fusion réussie")
                            else:
                                # FIX bug 5 : Contact() ne connaît pas visit_dates
                                import dataclasses as _dc
                                _cf = {f.name for f in _dc.fields(Contact)}
                                new_entry = {k: v for k, v in data.items() if k in _cf}
                                new_dict = _dc.asdict(Contact(**new_entry))
                                new_dict["visit_dates"] = data.get("visit_dates", [])
                                st.session_state.address_book.append(new_dict)
                                ContactManager.invalidate_index(); AddressBookManager.save_to_file(sync_import_csv=True)
                        st.session_state.pop(f"m_t_{state_key}", None); st.session_state.pop(f"m_d_{state_key}", None)
                        st.rerun()
                    if st.button("Annuler", use_container_width=True, key=f"can_merge_fb_{state_key}"):
                        st.session_state.pop(f"m_t_{state_key}", None); st.session_state.pop(f"m_d_{state_key}", None)
                        st.rerun()
                else:
                    with st.form(key=f"form_fb_{abs(hash((h_name, h_addr)))}"):
                        new_name, new_addr, new_phone = st.text_input("Nom", value=h.get("name", "")), st.text_input("Adresse", value=h.get("address", "")), st.text_input("Tél", value=h.get("phone", ""))
                        new_itype = st.selectbox("Type", INTERVENTION_KEYS, index=INTERVENTION_KEYS.index(h.get("intervention_type", DEFAULT_INTERVENTION_TYPE)) if h.get("intervention_type") in INTERVENTION_KEYS else 1)
                        new_svc_min = st.number_input("Min", min_value=5, value=max(5, h.get("service_duration", 3600) // 60))
                        new_notes = st.text_area("Notes", value=h.get("notes", ""))
                        _tm_opts2 = ["Libre","Matin","Après-midi","Heure précise"]
                        _cur_tm2 = h.get("time_mode", "Libre")
                        _tm_idx2 = _tm_opts2.index(_cur_tm2) if _cur_tm2 in _tm_opts2 else 0
                        new_tm = st.selectbox("Dispo", _tm_opts2, index=_tm_idx2)
                        new_pt_fb = h.get("preferred_time")
                        if new_tm == "Heure précise":
                            _dft = dt_time(7, 45)
                            if new_pt_fb is not None:
                                try: _dft = dt_time(new_pt_fb//3600, (new_pt_fb%3600)//60)
                                except (ValueError, TypeError): pass  # Heure stockée invalide — on garde 08:00 par défaut
                            _ptv = st.time_input("Heure", value=_dft); new_pt_fb = _ptv.hour*3600 + _ptv.minute*60
                        else: new_pt_fb = None
                        new_wd = st.text_input("Jours", value=h.get("available_weekdays", ""))
                        new_pm = st.selectbox("Relance", range(13), index=min(int(h.get("preferred_month", 0)), 12))
                        cur_dates = sorted([d for d in h.get("visit_dates", []) if d], key=_sort_key_date)
                        kept_dates = st.multiselect("Dates", options=cur_dates, default=cur_dates)
                        new_ds = st.text_input("Ajouter date", value="")
                        col_s, col_d = st.columns([3, 1])
                        sub, del_c = col_s.form_submit_button("💾 Enregistrer", use_container_width=True), col_d.form_submit_button("🗑️", use_container_width=True)

                        if del_c: HistoryManager.delete_entry(h_addr, h_name); st.rerun()
                        elif sub:
                            merged = list(kept_dates); nd = _parse_fr_date(new_ds.strip())
                            if nd and nd not in merged: merged.append(nd)
                            new_name_s, new_addr_s = new_name.strip(), new_addr.strip()
                            dupe_idx = ContactManager.find_duplicate(new_name_s, new_addr_s, exclude_index=book_idx, exact_match=True)
                            data = {
                                "name": new_name_s, "address": new_addr_s, "phone": new_phone.strip(),
                                "intervention_type": new_itype, "service_duration": new_svc_min * 60,
                                "notes": new_notes.strip(), "visit_dates": merged,
                                "time_mode": new_tm, "preferred_time": new_pt_fb,
                                "available_weekdays": new_wd.strip(), "preferred_month": int(new_pm)
                            }
                            if dupe_idx is not None:
                                st.session_state[f"m_t_{state_key}"] = dupe_idx
                                st.session_state[f"m_d_{state_key}"] = data
                                st.rerun()
                            else:
                                if book_idx != -1:
                                    ContactManager.update_contact(book_idx, **data)
                                    st.toast("✅ Enregistré")
                                else:
                                    # FIX bug 5 : Contact() ne connaît pas visit_dates
                                    import dataclasses as _dc
                                    _cf = {f.name for f in _dc.fields(Contact)}
                                    new_entry = {k: v for k, v in data.items() if k in _cf}
                                    new_dict = _dc.asdict(Contact(**new_entry))
                                    new_dict["visit_dates"] = data.get("visit_dates", [])
                                    st.session_state.address_book.append(new_dict)
                                    ContactManager.invalidate_index(); AddressBookManager.save_to_file(sync_import_csv=True)
                                    st.toast("✅ Nouveau contact créé")
                                if _norm_addr(new_addr_s) != old_addr_norm: st.session_state.setdefault("coord_cache", {}).pop(old_addr_norm, None); ContactManager.invalidate_index()
                                st.rerun()

    @staticmethod
    @_maybe_fragment
    def address_list():
        if st.session_state.pop("_clear_addr_widgets", False): StateManager._clear_address_widget_keys()
        st.markdown("<a name='liste-a-planifier'></a>", unsafe_allow_html=True)
        st.subheader("✏️ Liste en cours de planification"); cfg, points, result = StateManager.config(), StateManager.points(), st.session_state.get("optimized_result")
        arrival_by_node = {node: result.arrival_times[step] for step, node in enumerate(result.order)} if result else {}
        fmt_t = lambda s: f"{s//3600:02d}:{(s%3600)//60:02d}"
        optim_points, _start_pt_al = [p for p in points if not p.is_start and not p.is_end], next((p for p in points if p.is_start), None)
        n_optim, _start_svc_al = len(optim_points), _start_pt_al.service_duration if _start_pt_al else cfg.start_service_duration
        _warning_nodes = UI._get_warning_nodes(result, points) if result else {}
        st.markdown("<div style='display:flex; font-weight:bold; color:#888; font-size:0.85rem; margin-bottom:5px'><div style='flex:0.6'>#</div><div style='flex:5.0'>📍 Intervention / Client</div><div style='flex:0.8; text-align:center'>🏠</div><div style='flex:0.8; text-align:center'>🏁</div><div style='flex:1.8; text-align:center'>🕒 Créneau</div><div style='flex:0.8; text-align:center'>⏱</div><div style='flex:1.8; text-align:center'>🕑 Passage</div><div style='flex:2.6'></div></div>", unsafe_allow_html=True)
        display_sequence, used_indices = [], set()
        if result:
            for step, node in enumerate(result.order):
                if node == 0:
                    start_pt_idx = next((idx for idx, p in enumerate(points) if p.is_start), -1)
                    if start_pt_idx != -1: used_indices.add(start_pt_idx); display_sequence.append(("client", points[start_pt_idx], step, node, start_pt_idx))
                    else: display_sequence.append(("system", cfg.start_address, step, node, -1))
                elif node == n_optim + 1:
                    end_pt_idx = next((idx for idx, p in enumerate(points) if p.is_end), -1)
                    if end_pt_idx != -1: used_indices.add(end_pt_idx); display_sequence.append(("client", points[end_pt_idx], step, node, end_pt_idx))
                    else: display_sequence.append(("system", cfg.end_address, step, node, -1))
                else:
                    # Sécurité : vérifier que l'index du nœud est valide pour optim_points
                    if 1 <= node <= len(optim_points):
                        p_target = optim_points[node - 1]
                        # Utilisation de l'uid pour retrouver l'index d'origine de manière fiable
                        try:
                            orig_idx = next(idx for idx, p in enumerate(points) if p.uid == p_target.uid)
                            used_indices.add(orig_idx)
                            display_sequence.append(("client", p_target, step, node, orig_idx))
                        except StopIteration:
                            pass # Point disparu entre temps
            for i, p in enumerate(points):
                if i not in used_indices: display_sequence.append(("client", p, 99, -1, i))
        else:
            for i, p in enumerate(points): display_sequence.append(("client", p, i, -1, i))
        for seq_idx, (stype, sdata, step, node, orig_idx) in enumerate(display_sequence):
            is_client, p, arr = (stype == "client"), (sdata if stype == "client" else None), (result.arrival_times[step] if (result and node != -1) else None)
            c_idx, c_addr, c_start, c_end, c_tw, c_dur, c_arr, c_btns = st.columns([0.6, 5.0, 0.8, 0.8, 1.8, 0.8, 1.8, 2.6])
            with c_idx:
                if stype == "system": st.write("🏠" if node == 0 else "🏁")
                elif result:
                    if node == 0: st.write("🏠")
                    elif node == n_optim + 1: st.write("🏁")
                    elif node == -1: st.write("⏳")
                    else: st.write(f"**{step}**")
                else: st.write(f"**{orig_idx+1}**")
            with c_addr:
                if is_client:
                    display_name = p.name or ContactManager.get_name_by_addr(p.address)
                    is_long = (p.service_duration >= 70 * SPM)
                    name_style = "color:#d32f2f; font-weight:900;" if is_long else ""
                    name_prefix = f"<span style='{name_style}'>{_h(display_name)}</span> · " if display_name else ""
                    
                    warn_msg = _warning_nodes.get(node, "")
                    if warn_msg: st.markdown(f"<span style='color:#f0a500;font-weight:bold'>⚠️ {name_prefix}{_h(p.address)}</span>", unsafe_allow_html=True); st.caption(warn_msg)
                    else: st.markdown(f"{name_prefix}{_h(p.address)}", unsafe_allow_html=True)
                else: st.write(f"**{'🏠 DÉPART' if node == 0 else '🏁 RETOUR'}** : {sdata}")
            with c_start:
                if is_client:
                    if st.checkbox(" ", value=p.is_start, key=f"is_start_{p.uid}"):
                        if not p.is_start:
                            for pt in points: pt.is_start = False
                            p.is_start, cfg.start_address, cfg.start_coordinates, cfg.start_service_duration = True, p.address, p.coordinates, p.service_duration; StateManager.commit()
                    elif p.is_start: p.is_start, cfg.start_address, cfg.start_coordinates, cfg.start_service_duration = False, "", None, 60*SPM; StateManager.commit()
            with c_end:
                if is_client:
                    if st.checkbox(" ", value=p.is_end, key=f"is_end_{p.uid}"):
                        if not p.is_end:
                            for pt in points: pt.is_end = False
                            p.is_end, cfg.end_address, cfg.end_coordinates = True, p.address, p.coordinates; StateManager.commit()
                    elif p.is_end: p.is_end, cfg.end_address, cfg.end_coordinates = False, Config.DEFAULT_END_ADDRESS, None; StateManager.commit()
            with c_tw:
                if is_client: lo, hi = TW.get(p); icons = {"Heure précise":"🕒","Matin":"🌅","Après-midi":"🌆","Libre":"🔄"}; st.write(f"{icons.get(p.time_mode, '🔄')} {(TW.fmt(lo, hi) if p.time_mode != 'Libre' else 'Libre')}")
            with c_dur:
                if is_client: st.caption(f"{p.service_duration//SPM}m")
                elif node == 0 and _start_svc_al > 0: st.caption(f"{_start_svc_al//SPM}m")
            with c_arr:
                if arr is not None: st.write(f"🕐 **{fmt_t(arr)}**")
                else: st.caption("—")
            with c_btns:
                b1, b2, b3, b4 = st.columns(4)
                with b1:
                    if result:
                        client_steps = [s for s, nd in enumerate(result.order) if 0 < nd <= n_optim]
                        if st.button("↑", key=f"up_{seq_idx}", disabled=not is_client or (step in client_steps and client_steps.index(step) == 0)): StateManager.move_result_node(step, -1); st.rerun()
                    else:
                        if st.button("↑", key=f"up_{orig_idx}", disabled=orig_idx==0): StateManager.move_point_up(orig_idx)
                with b2:
                    if result:
                        client_steps = [s for s, nd in enumerate(result.order) if 0 < nd <= n_optim]
                        if st.button("↓", key=f"dn_{seq_idx}", disabled=not is_client or (step in client_steps and client_steps.index(step) == len(client_steps)-1)): StateManager.move_result_node(step, 1); st.rerun()
                    else:
                        if st.button("↓", key=f"dn_{orig_idx}", disabled=orig_idx==len(points)-1): StateManager.move_point_down(orig_idx)
                with b3:
                    if is_client and st.button("✏️", key=f"edit_{p.uid}"): UI._edit_point(orig_idx, p.address)
                with b4:
                    if is_client and st.button("🗑️", key=f"del_{p.uid}"): UI._confirm_delete_point(orig_idx, p.address)

            if is_client:
                i = p.uid; exp_label = (f"📍 {_h(p.name)} — " if p.name else "📍 ") + (f"{p.address[:28]}…" if len(p.address) > 28 else p.address)
                with st.expander(exp_label):
                    new_address_val = st.text_input("Adresse", value=p.address, key=f"addr_{i}")
                    if not bool(_RE_POSTCODE.search(new_address_val)): st.caption("⚠️ Pas de code postal")
                    if new_address_val.strip() != p.address.strip():
                        new_addr_clean, addr_l_old, addr_idx = new_address_val.strip(), _norm_addr(p.address), ContactManager.get_index_by_addr()
                        def _apply_to_tour(): st.session_state.setdefault("coord_cache", {}).pop(_norm_addr(addr_l_old), None); StateManager.update_point(orig_idx, address=new_addr_clean, coordinates=None); st.session_state.pop(f"addr_{i}", None)
                        def _apply_to_carnet(o_idx, contact): st.session_state.setdefault("coord_cache", {}).pop(_norm_addr(contact["address"]), None); st.session_state.address_book[o_idx]["address"] = new_addr_clean; ContactManager.invalidate_index(); AddressBookManager.save_to_file()
                        if addr_l_old in addr_idx:
                            orig_idx_c, contact_c = addr_idx[addr_l_old]; st.caption(f"📒 Contact : **{contact_c['name']}**")
                            c_t, c_c, c_tc = st.columns(3)
                            if c_t.button("✅ Tournée", key=f"val_t_{i}", use_container_width=True): _apply_to_tour(); StateManager.commit()
                            if c_c.button("💾 Carnet", key=f"val_c_{i}", use_container_width=True): _apply_to_carnet(orig_idx_c, contact_c); st.session_state.pop(f"addr_{i}", None); st.rerun()
                            if c_tc.button("🔄 Partout", key=f"val_tc_{i}", use_container_width=True, type="primary"): _apply_to_carnet(orig_idx_c, contact_c); _apply_to_tour(); StateManager.commit()
                        else:
                            c_t2, c_add2 = st.columns([1, 2]); candidates = StateManager.get_auto_add_candidates()
                            if c_t2.button("✅ Tournée", key=f"val_to_{i}", use_container_width=True): _apply_to_tour(); StateManager.commit()
                            with c_add2:
                                cand_name = st.text_input("Nom", key=f"opt_cn_{i}", label_visibility="collapsed")
                                if st.button("➕ Partout", key=f"val_tc2_{i}", use_container_width=True, type="primary"):
                                    if cand_name.strip(): st.session_state.address_book.append({"name": cand_name.strip(), "address": new_addr_clean, "phone": "", "intervention_type": p.intervention_type, "notes": p.notes, "service_duration": p.service_duration, "last_intervention": datetime.today().strftime("%d/%m/%Y")}); ContactManager.invalidate_index(); AddressBookManager.save_to_file(); StateManager.set_auto_add_candidates([c for c in candidates if _norm_addr(c["address"]) != addr_l_old]); _apply_to_tour(); StateManager.commit()
                                    else: st.warning("Nom requis")
                    elif _norm_addr(p.address) not in ContactManager.get_index_by_addr():
                        candidates2 = StateManager.get_auto_add_candidates(); is_cand2 = any(_norm_addr(c["address"]) == _norm_addr(p.address) for c in candidates2)
                        with st.expander("📒 Ajouter au carnet" + (" ✨" if is_cand2 else ""), expanded=is_cand2):
                            cn3, cph3 = st.columns(2)
                            cand_name3, cand_phone3 = cn3.text_input("Nom", key=f"cn_s_{i}"), cph3.text_input("Tél", key=f"cp_s_{i}")
                            if st.button("➕ Enregistrer", key=f"ca_s_{i}", use_container_width=True, type="primary"):
                                if cand_name3.strip(): st.session_state.address_book.append({"name": cand_name3.strip(), "address": p.address, "phone": cand_phone3.strip(), "intervention_type": p.intervention_type, "notes": p.notes, "service_duration": p.service_duration, "last_intervention": datetime.today().strftime("%d/%m/%Y")}); ContactManager.invalidate_index(); AddressBookManager.save_to_file(); StateManager.set_auto_add_candidates([c for c in candidates2 if _norm_addr(c["address"]) != _norm_addr(p.address)]); st.rerun()
                                else: st.warning("Nom requis")
                    st.markdown("<hr style='margin:6px 0;border-color:#333'>", unsafe_allow_html=True)
                    _tm_opts = ["Libre", "Heure précise", "Matin", "Après-midi"]
                    _tm_idx = _tm_opts.index(p.time_mode) if p.time_mode in _tm_opts else 0
                    mode = st.selectbox("Mode horaire", _tm_opts, index=_tm_idx, key=f"mode_{i}")
                    tgt = None
                    if mode == "Heure précise":
                        ti2 = st.time_input("Heure cible", value=datetime.strptime(f"{p.target_time//SPH:02d}:{(p.target_time%SPH)//SPM:02d}", "%H:%M").time() if p.target_time is not None else dt_time(7, 45), key=f"ti_{i}")
                        tgt = ti2.hour * SPH + ti2.minute * SPM if ti2 else WORK_START
                        if mode == "Heure précise" and tgt is not None:
                            _ps, _pe = st.session_state.get("opt_pause_start", Config.PAUSE_DEFAULT_START), st.session_state.get("opt_pause_end", Config.PAUSE_DEFAULT_END)
                            if _ps <= tgt < _pe: st.warning(f"⚠️ {tgt//SPH:02d}:{(tgt%SPH)//SPM:02d} est dans la pause ({_ps//SPH:02d}h{(_ps%SPH)//SPM:02d}–{_pe//SPH:02d}h{(_pe%SPH)//SPM:02d})")
                    lo, hi = TW.get(DeliveryPoint(address="", time_mode=mode, target_time=tgt)) if (mode != "Heure précise" or tgt is not None) else (0,0)
                    st.caption(f"{ {'Heure précise':'⏰','Matin':'🌅','Après-midi':'🌆','Libre':'🕐'}.get(mode,'🕐') } Fenêtre : {TW.fmt(lo, hi) if (mode != 'Heure précise' or tgt is not None) else 'Saisissez une heure'}")
                    if f"prev_type_{i}" not in st.session_state: st.session_state[f"prev_type_{i}"] = p.intervention_type
                    itype = st.selectbox("Type", INTERVENTION_KEYS, index=INTERVENTION_KEYS.index(p.intervention_type) if p.intervention_type in INTERVENTION_TYPES else 1, key=f"itype_{i}")
                    if itype != p.intervention_type: StateManager.update_point(orig_idx, intervention_type=itype, service_duration=INTERVENTION_TYPES.get(itype, 60*SPM)); st.session_state[f"prev_type_{i}"] = itype; st.rerun()
                    svc_min, notes_val = st.slider("Durée (min)", 15, 180, value=max(15, p.service_duration//SPM), step=5, key=f"svc_{i}_{p.intervention_type}"), st.text_area("Notes", value=p.notes, height=60, key=f"notes_{i}")
                    if p.time_mode != mode or p.target_time != tgt or p.intervention_type != itype or p.notes != notes_val or p.service_duration != svc_min * SPM: StateManager.update_point(orig_idx, time_mode=mode, target_time=tgt, intervention_type=itype, notes=notes_val, service_duration=svc_min*SPM); StateManager.commit()

    @staticmethod
    def _build_folium_map(cfg, points, show_route, folium):
        from folium import plugins
        
        # 1. Fond OpenStreetMap Standard (Haut Contraste)
        m = folium.Map(
            location=MAP_CENTER, 
            zoom_start=13, 
            tiles="OpenStreetMap"
        )
        m.options['doubleClickZoom'] = False
        plugins.Fullscreen(position='topleft', title='Plein écran', title_cancel='Quitter').add_to(m)
        
        all_coords_to_fit = []
        
        # Helper pour icônes numérotées
        def get_numbered_icon(number, color, is_visited=False):
            opac = "0.5" if is_visited else "1.0"
            border = "2px solid white" if not is_visited else "1px solid gray"
            return folium.DivIcon(html=f"""
                <div style="
                    background-color: {color};
                    border: {border};
                    border-radius: 50%;
                    color: white;
                    font-weight: bold;
                    font-size: 12px;
                    width: 24px;
                    height: 24px;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    opacity: {opac};
                    box-shadow: 0 0 10px rgba(0,0,0,0.5);
                ">{number}</div>""")

        # Départ
        if cfg.start_coordinates:
            folium.Marker(
                cfg.start_coordinates, 
                popup=f"🏠 DÉPART: {cfg.start_address}",
                tooltip="Départ",
                icon=folium.Icon(color="green", icon="play")
            ).add_to(m)
            all_coords_to_fit.append(cfg.start_coordinates)

        # Points de livraison
        seen_coords = []
        
        # 🟢 SYNCHRO NUMÉROS CARTE/LISTE
        # Si un itinéraire est affiché, on récupère l'ordre de passage (step) pour chaque point
        step_map = {}
        if show_route and st.session_state.optimized_result:
            result = st.session_state.optimized_result
            optim_points = [p for p in points if not p.is_start and not p.is_end]
            for step, node in enumerate(result.order):
                if 0 < node <= len(optim_points):
                    p_target = optim_points[node - 1]
                    step_map[p_target.uid] = step

        for i, p in enumerate(points):
            if p.coordinates:
                # --- LOGIQUE DE DÉCALAGE (Jitter) ---
                lat, lon = p.coordinates
                for existing_lat, existing_lon in seen_coords:
                    if abs(lat - existing_lat) < 0.00015 and abs(lon - existing_lon) < 0.00015:
                        import math, random
                        angle = random.uniform(0, 2 * math.pi); dist = 0.00018
                        lat += dist * math.cos(angle); lon += dist * math.sin(angle)
                
                display_coords = (lat, lon)
                seen_coords.append(display_coords)
                all_coords_to_fit.append(display_coords)
                
                lo, hi = TW.get(p); time_info = TW.fmt(lo, hi)
                
                # Couleur selon mode horaire
                if p.is_visited: m_color = "#808080"
                else: m_color = {"Heure précise":"#ff4b4b","Matin":"#00c0f2","Après-midi":"#ffa500","Libre":"#1f77b4"}.get(p.time_mode, "#1f77b4")
                
                # Le numéro affiché : soit le step de l'itinéraire, soit l'index + 1
                display_num = step_map.get(p.uid, i + 1)
                
                # Popup HTML stylé
                popup_html = f"""
                <div style="font-family: sans-serif; min-width: 150px;">
                    <b style="color:{m_color}; font-size: 1.1em;">#{display_num} {p.name or 'Client'}</b><br>
                    <div style="margin-top:5px; color:#333;">
                        📍 {p.address[:60]}<br>
                        🕒 <b>{time_info}</b><br>
                        ⏱ Durée: {p.service_duration//SPM} min
                    </div>
                    <hr style="margin:8px 0">
                    <a href="https://www.google.com/maps/dir/?api=1&destination={p.coordinates[0]},{p.coordinates[1]}" 
                       target="_blank" style="text-decoration:none; color:#1a73e8; font-weight:bold;">
                       🚀 Itinéraire Google Maps
                    </a>
                </div>
                """
                
                folium.Marker(
                    display_coords,
                    popup=folium.Popup(popup_html, max_width=300),
                    tooltip=f"<b>#{display_num}</b> {p.name or p.address[:30]}",
                    icon=get_numbered_icon(display_num, m_color, p.is_visited)
                ).add_to(m)
        
        # Retour
        if cfg.end_coordinates:
            folium.Marker(
                cfg.end_coordinates, 
                popup=f"🏁 RETOUR: {cfg.end_address}",
                tooltip="Arrivée",
                icon=folium.Icon(color="orange", icon="stop")
            ).add_to(m)
            all_coords_to_fit.append(cfg.end_coordinates)

        # Itinéraire
        if show_route and st.session_state.optimized_result:
            result, all_coords = st.session_state.optimized_result, ([cfg.start_coordinates] + [p.coordinates for p in points] + [cfg.end_coordinates])
            # FIX : Sécurité sur les index pour éviter IndexError si la liste de points a changé
            route_coords = [all_coords[node] for node in result.order if node < len(all_coords) and all_coords[node] is not None]
            
            if route_coords:
                geometry = OSRM.route_geometry(route_coords)
                line_color = "#ff9e00" if result.is_approximation else "#00d4ff"
                if geometry:
                    folium.PolyLine(geometry, color=line_color, weight=5, opacity=0.8).add_to(m)
                    # Ajout de flèches de direction (plugins AntPath pour l'animation)
                    plugins.AntPath(geometry, color=line_color, weight=5, opacity=0.6, dash_array=[10, 20]).add_to(m)
                else:
                    folium.PolyLine(route_coords, color=line_color, weight=4, opacity=0.8, dash_array="8").add_to(m)

        # Auto-zoom (fit_bounds)
        if all_coords_to_fit:
            m.fit_bounds(all_coords_to_fit, padding=(30, 30))
            
        return m

    @staticmethod
    @st.fragment
    def map(show_route=False):
        # Utilisation de st.fragment pour isoler la carte (si disponible)
        cfg, points, folium, st_folium = StateManager.config(), StateManager.points(), _import_folium()[0], _import_folium()[1]
        cache_key = (cfg.start_coordinates, cfg.end_coordinates, tuple((p.coordinates, p.time_mode, p.address, p.is_visited) for p in points), show_route, st.session_state.optimized_result is not None)
        if st.session_state.get("_map_cache") and st.session_state["_map_cache"][0] == cache_key: m = st.session_state["_map_cache"][1]
        else: m = UI._build_folium_map(cfg, points, show_route, folium); st.session_state["_map_cache"] = (cache_key, m)
        if st.session_state.map_zoom_target:
            target_coords, zoom = Geo.get(st.session_state.map_zoom_target) or MAP_CENTER, 17
            st.session_state.map_zoom_target, m.location, m.zoom_start = None, list(target_coords), zoom
        st.caption("🖱️ Double-cliquez pour sélectionner, puis **Ajouter**")
        map_data = st_folium(m, height=550, width=None, returned_objects=["last_clicked"], key="main_map")
        if map_data and map_data.get("last_clicked"):
            click = map_data["last_clicked"]; lat, lon = float(click.get("lat")), float(click.get("lng"))
            if (round(lat, 5), round(lon, 5)) != st.session_state.get("_map_last_processed_click"): st.session_state["_map_last_processed_click"] = (round(lat, 5), round(lon, 5)); queue = list(st.session_state.get("map_click_queue", [])); queue.append((round(lat, 5), round(lon, 5))); st.session_state["map_click_queue"] = queue
        queue = st.session_state.get("map_click_queue", [])
        if queue:
            st.info(f"📍 **{len(queue)} point(s)** sélectionnés"); 
            for qi, (qlat, qlon) in enumerate(queue):
                qc1, qc2 = st.columns([5, 1]); addr_key = f"map_q_addr_{qi}"
                if addr_key not in st.session_state: center_res = Geo.reverse(qlat, qlon); st.session_state[addr_key] = center_res or f"{qlat}, {qlon}"
                st.session_state[addr_key] = qc1.text_input(f"P{qi+1}", value=st.session_state[addr_key], key=f"map_q_edit_{qi}", label_visibility="collapsed")
                if qc2.button("✖", key=f"map_q_rm_{qi}"): queue.pop(qi); st.session_state["map_click_queue"] = queue; [st.session_state.pop(f"map_q_addr_{j}", None) for j in range(qi, len(queue)+1)]; st.rerun()
            ca, cb = st.columns(2)
            if ca.button("➕ Ajouter", type="primary", use_container_width=True):
                for qi, (qlat, qlon) in enumerate(queue): StateManager.add_point(st.session_state.get(f"map_q_addr_{qi}", f"{qlat}, {qlon}")); StateManager.points()[-1].coordinates = (qlat, qlon)
                st.session_state["map_click_queue"] = []; [st.session_state.pop(k, None) for k in list(st.session_state.keys()) if k.startswith("map_q_addr_") or k.startswith("map_q_edit_")]; StateManager.commit()
            if cb.button("✖ Vider", use_container_width=True): st.session_state["map_click_queue"] = []; [st.session_state.pop(k, None) for k in list(st.session_state.keys()) if k.startswith("map_q_addr_") or k.startswith("map_q_edit_")]; st.rerun()

    @staticmethod
    def results():
        if not st.session_state.optimized_result: st.info("Aucune tournée planifiée."); return
        result, cfg, points = st.session_state.optimized_result, StateManager.config(), StateManager.points()
        
        # 🚨 ALERTE SURCHARGE JOURNÉE
        last_arr = result.arrival_times[-1]
        if last_arr > 19 * SPH:
            st.error(f"⚠️ **SURCHARGE JOURNÉE** : Retour prévu à {TimeUtils.fmt_hm(last_arr)} (dépasse 19h00)")
        elif last_arr > 19 * SPH:
            st.warning(f"🟡 **Alerte Surcharge** : Retour prévu à {TimeUtils.fmt_hm(last_arr)}")

        start_pt, optim_points = next((p for p in points if p.is_start), None), [p for p in points if not p.is_start and not p.is_end]
        if result.tour_hash != hash((cfg.start_address, cfg.end_address, cfg.start_time, start_pt.service_duration if start_pt else cfg.start_service_duration, tuple((p.address, p.time_mode, p.target_time, p.intervention_type, p.service_duration) for p in optim_points), st.session_state.get("opt_pause_enabled", False), st.session_state.get("opt_pause_start", Config.PAUSE_DEFAULT_START), st.session_state.get("opt_pause_end", Config.PAUSE_DEFAULT_END))): st.warning("⚠️ Tournée obsolète. Cliquez sur Planifier."); return
        st.markdown("<div id='ordre-de-passage'></div>", unsafe_allow_html=True); st.markdown("#### Itinéraire")

        # 🟢 NOUVEL INDICATEUR DE QUALITÉ DU ROUTAGE
        if st.session_state.get("_is_crow_flies"):
            st.warning("⚠️ **Mode Dégradé (Vol d'oiseau)** : Le serveur de routage OSRM est indisponible. Les distances et durées sont estimées à vol d'oiseau (vitesse moyenne 35km/h).")

        cm1, cm2, cm3, cm4 = st.columns(4)
        cm1.metric("Distance", f"{result.total_distance/1000:.1f} km")
        cm2.metric("Durée", f"{result.total_time/3600:.1f} h")
        cm3.metric("Arrêts", str(len(points)))
        if result.initial_distance > 0 and result.initial_distance != result.total_distance: cm4.metric("Gain km", f"{(result.initial_distance-result.total_distance)/result.initial_distance*100:.0f}%", delta=f"-{(result.initial_distance-result.total_distance)/1000:.1f}km")
        st.markdown("---"); all_addr, n_pts, _warning_nodes, p_params, pause_shown = [cfg.start_address] + [p.address for p in optim_points] + [cfg.end_address], len(optim_points), UI._get_warning_nodes(result, points), Optimizer._params(), False
        for step, node in enumerate(result.order):
            if node < 0 or node >= len(all_addr): continue
            arr_t = result.arrival_times[step]
            if p_params["pause_enabled"] and not pause_shown and step > 0 and arr_t >= p_params["pause_end"]:
                prev_node = result.order[step-1]; prev_svc = (optim_points[prev_node-1].service_duration if 0 < prev_node <= n_pts else ( (next((p for p in points if p.is_start), None).service_duration if next((p for p in points if p.is_start), None) else cfg.start_service_duration) if prev_node == 0 else 0))
                if result.arrival_times[step-1] + prev_svc <= p_params["pause_end"]: st.info(f"🖲️**Pause déjeuner** ({p_params['pause_start']//3600:02d}:{(p_params['pause_start']%3600)//60:02d} – {p_params['pause_end']//3600:02d}:{(p_params['pause_end']%3600)//60:02d})"); pause_shown = True
            service_start, waiting = arr_t, 0
            if 0 < node <= n_pts:
                p_obj = optim_points[node-1]; lo, hi = TW.get(p_obj)
                if arr_t < lo: service_start, waiting = lo, lo - arr_t
            fin_t = (service_start + optim_points[node-1].service_duration) if 0 < node <= n_pts else ( (arr_t + (start_pt.service_duration if start_pt else cfg.start_service_duration)) if node == 0 and (start_pt.service_duration if start_pt else cfg.start_service_duration) > 0 else None)
            is_client, client_steps = 0 < node <= n_pts, [s for s, nd in enumerate(result.order) if 0 < nd <= n_pts]
            p_res = optim_points[node-1] if is_client else (start_pt if node == 0 else None); p_name = p_res.name if p_res else ""; 
            if not p_name: p_name = ContactManager.get_name_by_addr(all_addr[node])
            
            # --- STYLE NOM CLIENT (LONGUE DURÉE) ---
            is_long = is_client and (p_res.service_duration >= 70 * SPM)
            name_color = "#d32f2f" if is_long else "#1f77b4"
            name_weight = "900" if is_long else "bold"
            
            c1, c2, c3, c4, c5, c6, c7, c8, c9 = st.columns([1, 5, 2, 2, 2, 1, 1, 1, 1]); c1.write("🏠" if node == 0 or node == n_pts + 1 else f"**{step}**")
            _name_span = (f"<span style='color:{name_color};font-weight:{name_weight};font-size:1.1em'>" + _h(p_name) + "</span><br>") if p_name else ""
            _node_prefix = '🏠 **DÉPART :** ' if node == 0 else ('🏁 **RETOUR :** ' if node == n_pts + 1 else '')
            content = f"{_name_span}{_node_prefix}{_h(all_addr[node])}"
            if _warning_nodes.get(node): c2.markdown(f"<span style='color:#f0a500'>{content}</span>", unsafe_allow_html=True); c2.caption(f"⚠️ {_warning_nodes[node]}")
            else: c2.markdown(content, unsafe_allow_html=True)
            if is_client and p_res.notes: c2.caption(f"📝 {p_res.notes}")
            if is_client and p_res:
                _ph = ContactManager.get_index_by_addr().get(_norm_addr(p_res.address)); _ph_s = _ph[1].get("phone") if _ph and _ph[1].get("phone") else ""
                if not _ph_s and p_name:
                    for c in st.session_state.get("address_book", []):
                        if _norm_addr(c.get("name","")) == _norm_addr(p_name):
                            if c.get("phone"): _ph_s = c["phone"]; break
                if _ph_s: c2.caption(f"📞 {_h(_ph_s)}")
            if waiting > 0:
                c3.write(f"🕐 {arr_t//3600:02d}:{(arr_t%3600)//60:02d}")
                c3.caption(f"⌛ attente {waiting//60}min")
                c3.write(f"▶️ **{service_start//3600:02d}:{(service_start%3600)//60:02d}**")
            else:
                c3.write(f"🕐 **{arr_t//3600:02d}:{(arr_t%3600)//60:02d}**")
            
            # ℹ️ Information parking et fourchette client
            if is_client:
                p_m_res = st.session_state.get("opt_parking_time", Config.PARKING_TIME // SPM)
                c3.caption(f"🅿️ +{p_m_res}mn inclues")
                            
            if fin_t is not None: c3.caption(f"🏁 {fin_t//3600:02d}:{(fin_t%3600)//60:02d}")
            if node == 0 and (start_pt.service_duration if start_pt else cfg.start_service_duration) > 0: c4.caption(f"⏱ {(start_pt.service_duration if start_pt else cfg.start_service_duration)//SPM}min")
            elif is_client: c4.caption(f"⏱ {p_res.service_duration//SPM}min")
            if is_client and p_res.time_mode != "Libre":
                lo, hi = TW.get(p_res); c5.caption(f"📅 {TW.fmt(lo, hi)}")
                margin = hi - fin_t if fin_t is not None else None
                TOLERANCE = 5 * 60  # 5 minutes — cohérent avec _get_warning_nodes
                if lo == hi:
                    if service_start <= lo + TOLERANCE: c5.success("✓")
                    else: c5.warning(f"⏰ +{(service_start-lo)//60}min")
                else:
                    if lo <= service_start <= hi + TOLERANCE:
                        c5.success("✓")   
                    else:
                        c5.error("⚠️ Hors fenêtre!")
            if is_client and c6.button("↑", key=f"res_up_{step}", disabled=client_steps.index(step)==0): StateManager.move_result_node(step, -1); st.rerun()
            if is_client and c7.button("↓", key=f"res_dn_{step}", disabled=client_steps.index(step)==len(client_steps)-1): StateManager.move_result_node(step, +1); st.rerun()
            if is_client and c8.button("🗑️", key=f"del_res_{node}"): StateManager.remove_point(node-1); StateManager.request_reoptimize(); st.rerun()
            if is_client and c9.button("⏳", key=f"wl_res_{node}", help="Renvoyer en file d'attente"):
                _p_wl = optim_points[node-1]
                WaitlistManager.add({"name": _p_wl.name, "address": _p_wl.address, "phone": "", "intervention_type": _p_wl.intervention_type, "notes": _p_wl.notes, "service_duration": _p_wl.service_duration, "time_mode": _p_wl.time_mode, "preferred_time": _p_wl.target_time})
                StateManager.remove_point(node-1); StateManager.request_reoptimize(); st.rerun()

        st.markdown("---"); st.subheader("💾 Export ICS"); tour_date = st.date_input("Date", value=datetime.today().date(), key="ics_d_final")
        if tour_date.weekday() == 6: st.warning(f"⚠️ Dimanche ⚠️")
        
        c_exp1, c_exp2 = st.columns(2)
        c_exp1.download_button("📅 Télécharger .ics", data=RouteManager.to_ics(result, cfg, points, datetime(tour_date.year, tour_date.month, tour_date.day)), file_name=f"tournee_{tour_date.strftime('%Y%m%d')}.ics", mime="text/calendar", use_container_width=True)
        
        if c_exp2.button("🗓️ Envoyer vers l'agenda", key="btn_to_agenda", use_container_width=True, help="Inscrire cette tournée directement dans l'agenda hebdomadaire"):
            if HAS_DIALOG:
                @st.dialog("📤 Confirmer l'export vers l'agenda")
                def _confirm_export_dlg():
                    st.warning(f"Voulez-vous inscrire cette tournée à la date du **{tour_date.strftime('%d/%m/%Y')}** ?")
                    st.caption("Cette action remplacera les clients déjà présents à cette date dans l'agenda.")
                    c1, c2 = st.columns(2)
                    if c1.button("✅ Oui, exporter", type="primary", use_container_width=True):
                        from sr_agenda import AgendaManager
                        optim_pts = [p for p in points if not p.is_start and not p.is_end]
                        final_pts, final_times = [], []
                        for step, node in enumerate(result.order):
                            if 0 < node <= len(optim_pts):
                                final_pts.append(optim_pts[node-1])
                                final_times.append(result.arrival_times[step])
                        n_saved = AgendaManager.import_from_planning(tour_date, final_pts, final_times)
                        if n_saved > 0:
                            st.session_state["_export_success_msg"] = f"✅ {n_saved} clients inscrits au {tour_date.strftime('%d/%m/%Y')}"
                            st.rerun()
                    if c2.button("Annuler", use_container_width=True): st.rerun()
                _confirm_export_dlg()
            else:
                # Fallback si st.dialog n'est pas dispo
                st.session_state["_confirm_agenda_export"] = True

        # Affichage du message de succès après rerun (post-dialog)
        if "_export_success_msg" in st.session_state:
            st.success(st.session_state.pop("_export_success_msg"))
            st.toast("Import agenda réussi !")

        StateManager.auto_add_to_book(points, result.arrival_times, result.order, datetime.today().strftime("%d/%m/%Y"))

    @staticmethod
    def _render_search_section():
        with st.expander("🔍 Recherche avancée (GPS / Suggestions)", expanded=False):
            q = st.text_input("Rechercher une adresse précise", placeholder="12 rue des fleurs, Paris…", key="as_q")
            if st.button("🔍 Chercher", key="as_b", use_container_width=True, type="primary", disabled=len(q.strip())<5):
                if q:
                    ck = _norm_addr(q) + f"|{Config.ADDR_SEARCH_LIMIT}"; cached = st.session_state.get("_addr_search_cache", {}).get(ck)
                    if cached: st.session_state.address_suggestions["search"] = cached; st.rerun()
                    else:
                        with st.spinner("Recherche..."):
                            sug = Geo.search_address_suggestions(q, limit=Config.ADDR_SEARCH_LIMIT)
                            if sug: st.session_state.address_suggestions["search"] = sug; st.rerun()
                            else: st.warning("Aucun résultat trouvé.")
            
            if "search" in st.session_state.address_suggestions:
                sug = st.session_state.address_suggestions["search"]; st.caption(f"{len(sug)} résultat(s)")
                for idx, s in enumerate(sug):
                    c_b, c_s = st.columns([8, 1.2])
                    if c_b.button(f"➕ {s['display_name'][:60]}", key=f"s_s_{idx}", use_container_width=True):
                        m = ContactManager.get_index_by_addr().get(_norm_addr(s['display_name']))
                        StateManager.add_point(s['display_name'], name=m[1].get('name', '') if m else "")
                        StateManager.points()[-1].coordinates = (s['lat'], s['lon'])
                        del st.session_state.address_suggestions["search"]
                        StateManager.commit()
                        st.rerun()
                    if c_s.button("💾", key=f"s_sv_{idx}", help="Enregistrer au carnet"): st.session_state[f"_os_s_{idx}"] = True; st.rerun()
                    if st.session_state.get(f"_os_s_{idx}"):
                        m = ContactManager.get_index_by_addr().get(_norm_addr(s['display_name']))
                        _inline_save_form({"address": s['display_name'], "name": m[1].get('name', '') if m else "", "intervention_type": DEFAULT_INTERVENTION_TYPE, "service_duration": 45*SPM, "notes": "", "time_mode": "Libre"}, f"search_{idx}")

# ==========================================================
# RENDU DES ONGLETS
# ==========================================================
def _render_sidebar():
    with st.sidebar:
        st.header("⚙️ Paramètres")
        # 🏠 OPTION DÉMARRAGE DOMICILE
        use_home = st.checkbox("🏠 Partir du domicile", value=st.session_state.get("use_fixed_start", True), help="Si décoché, le trajet entre le domicile et le 1er client sera ignoré.")
        if use_home != st.session_state.get("use_fixed_start"): 
            st.session_state["use_fixed_start"] = use_home
            StateManager.invalidate_optim_cache()
            st.rerun()
        
        st.markdown("---")
        st.markdown("⏲ **Heure de départ**")
        cfg = StateManager.config()
        ts = st.time_input("Départ", value=dt_time(cfg.start_time//3600, (cfg.start_time%3600)//60), label_visibility="collapsed", key="side_depart")
        if ts.hour*3600 + ts.minute*60 != cfg.start_time: StateManager.update_config(start_time=ts.hour*3600+ts.minute*60); StateManager.commit()
        
        if st.button("🗑 Vider la tournée", use_container_width=True, key="side_vider"):
            StateManager.clear_points()
            RouteManager.delete(RouteManager.AUTOSAVE_NAME)
            st.session_state.pop("_autosave_available", None)
            st.rerun()
        
        st.markdown("---")
        with st.expander("💾 Sauvegarder"):
            sn = st.text_input("Nom", key="side_sn_r")
            if st.button("💾 Sauvegarder", key="side_b_sn_r", use_container_width=True):
                if sn.strip():
                    c_sn = _RE_SAFE_NAME.sub('', sn.strip()).replace(' ', '_')
                    if RouteManager.save(c_sn): st.success(f"✅ {c_sn}"); st.rerun()
                else: st.warning("Nom requis")
                
        with st.expander("📂 Restaurer"):
            saves = RouteManager.list_saves()
            if saves:
                sl = st.selectbox("Tournées", saves, label_visibility="collapsed", key="side_sl")
                _pending_del = st.session_state.get("_sidebar_pending_del")
                if _pending_del == sl:
                    st.warning(f"Supprimer **« {sl} »** ?")
                    c_ok, c_no = st.columns(2)
                    if c_ok.button("✅ Oui", key="side_del_confirm", use_container_width=True, type="primary"):
                        RouteManager.delete(sl)
                        st.session_state.pop("_sidebar_pending_del", None)
                        st.rerun()
                    if c_no.button("Annuler", key="side_del_cancel", use_container_width=True):
                        st.session_state.pop("_sidebar_pending_del", None)
                        st.rerun()
                else:
                    cl, cd = st.columns(2)
                    if cl.button("📂 Restaurer", key="side_b_l", use_container_width=True):
                        if RouteManager.load(sl): st.rerun()
                    if cd.button("🗑️", key="side_b_d", use_container_width=True):
                        st.session_state["_sidebar_pending_del"] = sl
                        st.rerun()
            else: st.caption("Aucune sauvegarde")
            
        st.markdown("---")
        with st.expander("🖲️Pause déjeuner", expanded=True):
            en = st.checkbox("⚠️ Bloquer un créneau", value=st.session_state.opt_pause_enabled, key="side_pause_en")
            if en != st.session_state.opt_pause_enabled: st.session_state.opt_pause_enabled = en; StateManager.invalidate_optim_cache(); st.rerun()
            st.markdown("**Début**")
            ps_t = st.time_input("Heure de début", value=dt_time(st.session_state.opt_pause_start//3600, (st.session_state.opt_pause_start%3600)//60), key="side_ps_t", label_visibility="collapsed")
            if ps_t.hour*3600+ps_t.minute*60 != st.session_state.opt_pause_start: st.session_state.opt_pause_start = ps_t.hour*3600+ps_t.minute*60; StateManager.invalidate_optim_cache(); st.rerun()
            st.markdown("**Fin**")
            pe_t = st.time_input("Heure de fin", value=dt_time(st.session_state.opt_pause_end//3600, (st.session_state.opt_pause_end%3600)//60), key="side_pe_t", label_visibility="collapsed")
            if pe_t.hour*3600+pe_t.minute*60 != st.session_state.opt_pause_end: st.session_state.opt_pause_end = pe_t.hour*3600+pe_t.minute*60; StateManager.invalidate_optim_cache(); st.rerun()
        
        with st.expander("🅿️ Stationnement", expanded=True):
            p_min = st.slider("Marge (min)", 0, 30, value=st.session_state.get("opt_parking_time", 5), step=1, key="side_parking_t")
            if p_min != st.session_state.opt_parking_time:
                st.session_state.opt_parking_time = p_min
                Config.PARKING_TIME = p_min * SPM
                StateManager.request_reoptimize()
                st.rerun()

        st.markdown("---")

        # ── Vider le cache __pycache__ ────────────────────────────────────
        if st.button("🗑️ Vider le cache Python", use_container_width=True,
                     key="side_clear_pycache",
                     help="Supprime les dossiers __pycache__ et fichiers .pyc"):
            st.session_state["_pycache_confirm"] = not st.session_state.get("_pycache_confirm", False)
            st.rerun()

        if st.session_state.get("_pycache_confirm", False):
            st.warning("Supprimer tous les `__pycache__` ?")
            _pc1, _pc2 = st.columns(2)
            if _pc1.button("✅ Oui", key="side_pycache_ok",
                           use_container_width=True, type="primary"):
                import shutil, pathlib
                _root = pathlib.Path(__file__).parent
                _deleted = 0
                for _d in _root.rglob("__pycache__"):
                    if _d.is_dir():
                        shutil.rmtree(_d, ignore_errors=True)
                        _deleted += 1
                for _f in _root.rglob("*.pyc"):
                    _f.unlink(missing_ok=True)
                    _deleted += 1
                st.session_state.pop("_pycache_confirm", None)
                st.success(f"✅ {_deleted} élément(s) supprimé(s).")
                st.rerun()
            if _pc2.button("Annuler", key="side_pycache_cancel",
                           use_container_width=True):
                st.session_state.pop("_pycache_confirm", None)
                st.rerun()

        st.markdown("---")
        if st.button("⏻ Quitter & Nettoyer", use_container_width=True, type="primary", 
                     help="Ferme l'application et vide le cache Python"):
             from sr_persistence import CacheCleaner
             CacheCleaner.clear_python_cache()
             st.toast("👋 Fermeture...")
             import time, os
             time.sleep(0.5)
             os._exit(0)

def _render_tab_tournee():
    map_v = st.session_state.get("map_visible", False)
    if st.button("👁 " + ("Masquer" if map_v else "Afficher") + " la carte"): st.session_state["map_visible"] = not map_v; st.rerun()
    if map_v: UI.map(show_route=bool(st.session_state.optimized_result))
    st.markdown("<style>div[data-testid='stButton'] > button {height: 25px !important; padding: 1 !important; } div[data-testid='stButton'] > button[kind='primary'] { background-color: #e4f2f2 !important; color: #004d40 !important; border: none !important; font-weight: bold !important; } div[data-testid='stButton'] > button[kind='primary']:hover { background-color: #d1e8e8 !important; }</style>", unsafe_allow_html=True)
    c_btn, c_geo, _ = st.columns([2.5, 2.5, 10.5])
    run_optim = c_btn.button("🪄 Planifier", type="primary", key="btn_plan", use_container_width=True,help="Optimiser le trajet"); _do_geo = c_geo.button("🌐Géocoder", key="btn_geo", use_container_width=True,help="Géocoder les adresses")
    if _do_geo:
        pts_g, cfg_g = StateManager.points(), StateManager.config()
        with st.spinner("Géo…"):
            g_addrs = ([cfg_g.start_address] if cfg_g.start_address else []) + ([cfg_g.end_address] if cfg_g.end_address else []) + [p.address for p in pts_g]
            g_r = Geo.batch_geocode(g_addrs); 
            if cfg_g.start_address and g_r.get(cfg_g.start_address): StateManager.update_config(start_coordinates=g_r[cfg_g.start_address])
            if cfg_g.end_address and g_r.get(cfg_g.end_address): StateManager.update_config(end_coordinates=g_r[cfg_g.end_address])
            ok, fail = 0, 0
            for p in pts_g:
                if g_r.get(p.address): p.coordinates, ok = g_r[p.address], ok+1
                else: fail += 1
            StateManager.commit(); st.success(f"✅ {ok} 📍")
    if st.session_state.optimized_result: st.markdown("<a href='#liste-a-planifier'><button style='background:#1f77b4;color:white;border:none;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:0.85rem'> Voir liste en cours</button></a>", unsafe_allow_html=True)
    st.markdown("<a name='ajouter-un-client-ou-une-adresse'></a>", unsafe_allow_html=True)
    UI._render_search_section()
    st.caption("➕ Ajouter un client ou une adresse")
    _contacts_add = st.session_state.address_book
    _options_add = ["🔍 Rechercher un contact..."] + [f"{c.get('name', 'Sans nom')} ({c.get('address', '')[:40]})" for c in _contacts_add]
    _cs, _cn, _ca = st.columns([4, 4, 1.2])
    _sel_c = _cs.selectbox("Contact", _options_add, label_visibility="collapsed", key="quick_c_search")
    _new_addr = _cn.text_input("Adresse", placeholder="Ou taper une adresse...", label_visibility="collapsed", key="quick_a_search")
    if _ca.button("➕", type="primary", use_container_width=True, key="btn_q_add"):
        if _sel_c != "🔍 Rechercher un contact...":
            _idx_c = _options_add.index(_sel_c) - 1
            if 0 <= _idx_c < len(_contacts_add):
                StateManager.add_contact_to_route(_contacts_add[_idx_c])
                StateManager.commit()
        elif _new_addr.strip():
            StateManager.add_point(_new_addr.strip())
            StateManager.commit()
    st.markdown("---")

    # ── Validation manuelle de l'historique ─────────────────────────────────
    if st.session_state.optimized_result:
        _hist_done = st.session_state.get("_history_saved_flag", False)
        if _hist_done:
            _hc1, _hc2 = st.columns([5, 1])
            _hc1.success("✅ Tournée enregistrée dans l'historique.")
            if _hc2.button("↩", key="btn_hist_reset", help="Permettre un nouvel enregistrement", use_container_width=True):
                st.session_state.pop("_history_saved_flag", None)
                st.rerun()
        else:
            if st.button(
                "✅ Valider et enregistrer dans l'historique",
                key="btn_save_history",
                type="primary",
                use_container_width=True,
                help="Enregistre la date du jour dans la fiche de chaque client de cette tournée"
            ):
                StateManager.save_to_history()
                st.session_state["_history_saved_flag"] = True
                st.rerun()
        st.markdown("---")

    UI.results(); UI.address_list()
    if run_optim:
        StateManager.invalidate_optim_cache(); st.session_state.pop("_route_geometry_cache", None); st.session_state.optimized_result = None; cfg, pts = StateManager.config(), StateManager.points()
        # On vérifie si un point de départ manuel existe dans la liste
        has_manual_start = any(p.is_start for p in pts)
        use_home_fixed = st.session_state.get("use_fixed_start", True)
        
        # L'adresse de départ effective pour le calcul (Geocoding/Matrix)
        # Si on n'utilise pas le domicile et pas de manuel, on prend quand même le domicile
        # comme point d'origine fictif pour OSRM.
        eff_start_addr = cfg.start_address if (use_home_fixed or has_manual_start) else Config.DEFAULT_END_ADDRESS
        
        if not eff_start_addr or not cfg.end_address or not pts: 
            st.error("Paramètres incomplets (Adresse de retour ou arrêts manquants)")
        else:
            with st.status("🔧 Planification…") as _s:
                needed = ([eff_start_addr] if not cfg.start_coordinates else []) + ([cfg.end_address] if not cfg.end_coordinates else []) + [p.address for p in pts if not p.coordinates]
                g_m = Geo.batch_geocode(needed) if needed else {}
                
                # Mise à jour temporaire des coordonnées de départ pour le calcul
                # (sans forcément modifier cfg.start_address de manière permanente)
                temp_start_coords = cfg.start_coordinates
                if not temp_start_coords:
                    temp_start_coords = g_m.get(eff_start_addr) or g_m.get(_norm_addr(eff_start_addr))
                    if not temp_start_coords: _s.update(label="❌ Départ", state="error"); st.stop()
                
                # On s'assure que cfg a bien les coordonnées de départ (utilisées par Optimizer)
                cfg.start_coordinates = temp_start_coords
                
                if not cfg.end_coordinates:
                    cfg.end_coordinates = g_m.get(cfg.end_address) or g_m.get(_norm_addr(cfg.end_address))
                    if not cfg.end_coordinates: _s.update(label="❌ Retour", state="error"); st.stop()
                failed = [p.address for p in pts if not p.coordinates and not (g_m.get(p.address) or g_m.get(_norm_addr(p.address)))]
                for p in [p for p in pts if not p.coordinates]: p.coordinates = g_m.get(p.address) or g_m.get(_norm_addr(p.address))
                if failed: _s.update(label="❌ Inconnus", state="error"); st.error("\n".join(failed)); st.stop()
                ok, err = Validator.check_setup(cfg, pts)
                if not ok: _s.update(label=f"❌ {err}", state="error"); st.stop()
                pts_o = [p for p in pts if not p.is_start and not p.is_end]; all_c = [cfg.start_coordinates] + [p.coordinates for p in pts_o] + [cfg.end_coordinates]
                m_c = OSRM.matrix(all_c)
                if not m_c: _s.update(label="❌ OSRM", state="error"); st.stop()
                res = Optimizer.optimize(cfg, pts, precomputed_mats=m_c)
                if res:
                    st.session_state.optimized_result = res
                    StateManager.set_last_mats(m_c)
                    StateManager.set_manual_plan(False)
                    _s.update(label="✅ Prêt", state="complete")
                    st.rerun()
                else: _s.update(label="❌ Échec", state="error")

def _cleanup_mgr_keys():
    for k in [k for k in st.session_state if k.startswith("mgr_") or k.startswith("_open_edit_")]: del st.session_state[k]

def _inline_save_form(c_dict, f_key):
    saves = RouteManager.list_saves(); st.markdown("<div style='background:rgba(255,200,0,0.07);padding:6px 10px;border-radius:6px;border-left:3px solid #f0b400;margin:4px 0'>", unsafe_allow_html=True)
    sn_in = st.text_input("Nom", placeholder="Lundi_S42", key=f"_isf_n_{f_key}")
    if saves: st.selectbox("Existant", ["—"] + saves, key=f"_isf_r_{f_key}")
    c_ok, c_can = st.columns(2)
    if c_ok.button("✅", key=f"_isf_o_{f_key}", use_container_width=True, type="primary"):
        sn = sn_in.strip() or (st.session_state.get(f"_isf_r_{f_key}") if st.session_state.get(f"_isf_r_{f_key}") != "—" else "")
        if not sn:
            st.warning("Nom requis")
        else:
            ok, msg = RouteManager.add_client_to_save(sn, c_dict)
            if ok:
                st.success(msg)
                st.session_state.pop(f"_open_save_{f_key}", None)
                st.rerun()
            else:
                st.info(msg)
    if c_can.button("✕", key=f"_isf_c_{f_key}", use_container_width=True): st.session_state.pop(f"_open_save_{f_key}", None); st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)
