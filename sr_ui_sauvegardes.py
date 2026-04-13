"""sr_ui_sauvegardes.py — Onglet 💽 Sauvegardes (Ajouter depuis l'historique)."""
import os
import json
import streamlit as st
from sr_core import (
    _norm_addr, _h, _RE_SAFE_NAME, DEFAULT_INTERVENTION_TYPE, SPM,
)
from sr_persistence import (
    ContactManager, RouteManager,
)
from sr_state import StateManager

def _render_tab_sauvegardes():
    st.markdown("#### 💽 Sauvegardes — Ajouter depuis historique"); saves = RouteManager.list_saves()
    if not saves: st.info("Aucune sauvegarde."); return
    ex_n = {_norm_addr(p.address) for p in StateManager.points()}; combined = ContactManager.get_combined_contacts(); c_map = {(_norm_addr(c.get("name","")), _norm_addr(c.get("address",""))): c for c in combined}
    for sn in saves:
        safe = _RE_SAFE_NAME.sub("_", sn); path = os.path.join(RouteManager.SAVE_DIR, f"{safe}.json")
        try:
            with open(path, "r", encoding="utf-8") as f: data = json.load(f)
        except Exception:
            continue
        pts_d = [p for p in data.get("points", []) if not p.get("is_start") and not p.get("is_end")]; n_pts = len(pts_d)
        with st.expander(f"📁 {sn} — {n_pts} arrêts", expanded=False):
            if not pts_d: st.caption("Vide"); continue
            prefix = f"_s_s_{safe}_"; c_all, c_non, _ = st.columns([1, 1, 4])
            if c_all.button("☑", key=f"s_a_{safe}"): [st.session_state.update({f"{prefix}{i}": True}) for i, p in enumerate(pts_d) if _norm_addr(p.get("address","")) not in ex_n]; st.rerun()
            if c_non.button("☐", key=f"s_n_{safe}"): [st.session_state.update({f"{prefix}{i}": False}) for i in range(len(pts_d))]; st.rerun()
            sel_i = []
            _MOIS = ['','Janvier','Février','Mars','Avril','Mai','Juin',
                     'Juillet','Août','Septembre','Octobre','Novembre','Décembre']
            _TM_ICON = {'Matin': '🌅', 'Après-midi': '🌆', 'Heure précise': '⏰'}

            for ri in range(0, len(pts_d), 2):
                cols = st.columns(2)
                for ci, off in enumerate((0, 1)):
                    idx = ri + off
                    if idx >= len(pts_d): break
                    pd = pts_d[idx]; p_a = pd.get("address", ""); p_n = pd.get("name", ""); p_t = pd.get("intervention_type", "")
                    info = c_map.get((_norm_addr(p_n), _norm_addr(p_a)), {})
                    p_wd = info.get("available_weekdays", ""); p_tm = info.get("time_mode", "Libre"); p_pt = info.get("preferred_time"); p_ph = info.get("phone", ""); p_pm = info.get("preferred_month", 0)
                    if not info: p_tm = pd.get("time_mode") or "Libre"; p_pt = pd.get("target_time")
                    if not p_tm: p_tm = "Libre"
                    already = _norm_addr(p_a) in ex_n; ck = f"{prefix}{idx}"
                    if ck not in st.session_state: st.session_state[ck] = not already
                    with cols[ci]:
                        c_cb, _ = st.columns([1, 7])
                        checked = c_cb.checkbox("sel", key=ck, label_visibility="collapsed", disabled=already)
                        label = f"**{_h(p_n or p_a[:35])}**" + (" _(déjà)_" if already else "")
                        meta = f"<span style='color:gray;font-size:0.8em'>📍 {_h(p_a[:45])}" + (f" · {_h(p_t)}" if p_t else "") + "</span>"
                        av = []
                        if p_wd: av.append(f"🗓 {_h(p_wd)}")
                        if p_tm != "Libre":
                            icon = _TM_ICON.get(p_tm, '🕐'); heure_str = ""
                            if p_tm == 'Heure précise' and p_pt is not None: heure_str = f" ({p_pt // 3600:02d}:{(p_pt % 3600) // 60:02d})"
                            av.append(f"{icon} {_h(p_tm + heure_str)}")
                        if p_ph: av.append(f"📞 {_h(p_ph)}")
                        if p_pm and 1 <= p_pm <= 12: av.append(f"📅 Relance : {_h(_MOIS[p_pm])}")
                        if av: meta += f"<br><span style='color:#4caf50;font-size:0.8em'>{' · '.join(av)}</span>"
                        st.markdown(f"{label}<br>{meta}", unsafe_allow_html=True)
                        if checked and not already: sel_i.append(idx)
                        st.markdown("<hr style='margin:3px 0;border-color:#444'>", unsafe_allow_html=True)

            if st.button(f"🚀 Ajouter {len(sel_i)}", key=f"s_add_{safe}", type="primary", use_container_width=True, disabled=not sel_i):
                for idx in sel_i:
                    pd = pts_d[idx]; StateManager.add_point(pd.get("address", ""), name=pd.get("name", ""))
                    new = StateManager.points()[-1]; new.intervention_type = pd.get("intervention_type", DEFAULT_INTERVENTION_TYPE)
                    new.notes = pd.get("notes", ""); new.service_duration = pd.get("service_duration", 45 * SPM)
                    new.time_mode = pd.get("time_mode", "Libre"); new.target_time = pd.get("target_time")
                for i in range(len(pts_d)): st.session_state.pop(f"{prefix}{i}", None)
                StateManager.commit(); st.success("Ajoutés !"); st.rerun()
    
    st.markdown("---"); st.markdown("#### 🗑️ Gérer les sauvegardes")
    if saves:
        sel_save = st.selectbox("Sélectionner une sauvegarde à supprimer", saves, label_visibility="collapsed", key="semaine_del_sel")
        if st.button("🗑️ Supprimer la sauvegarde", use_container_width=True, key="semaine_del_btn"): st.session_state["_confirm_delete_semaine"] = sel_save
        if "_confirm_delete_semaine" in st.session_state:
            confirm_name = st.session_state["_confirm_delete_semaine"]; st.warning(f"Êtes-vous sûr de vouloir supprimer la sauvegarde « {confirm_name} » ?")
            c1, c2 = st.columns(2)
            if c1.button("✅ Oui, supprimer", key="semaine_del_confirm", type="primary", use_container_width=True): RouteManager.delete(confirm_name); st.session_state.pop("_confirm_delete_semaine", None); st.success(f"Sauvegarde « {confirm_name} » supprimée."); st.rerun()
            if c2.button("Annuler", key="semaine_del_cancel", use_container_width=True): st.session_state.pop("_confirm_delete_semaine", None); st.rerun()
    else: st.caption("Aucune sauvegarde à supprimer.")
