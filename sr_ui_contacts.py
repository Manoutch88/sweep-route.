"""sr_ui_contacts.py — Onglet 📒 Contacts : gestion complète du carnet d'adresses."""
import streamlit as st
import pandas as pd
from datetime import datetime, time as dt_time
from typing import List

from sr_core import (
    Config, DeliveryPoint, Contact, RouteConfig,
    _norm_addr, _h, _sort_key_date, _parse_fr_date,
    INTERVENTION_TYPES, DEFAULT_INTERVENTION_TYPE, INTERVENTION_KEYS,
    HAS_DIALOG, HAS_POPOVER, SPH, SPM,
)
from sr_persistence import (
    HistoryManager, ContactManager, AddressBookManager, RouteManager, WaitlistManager,
    UIPreferencesManager
)
from sr_logic import Geo
from sr_state import StateManager
from sr_ui import UI, _inline_save_form, _cleanup_mgr_keys  # helpers partagés

def _render_tab_contacts():
    st.markdown("### 📒 Gestion des Contacts")
    
    # Navigation par sous-onglets
    t_search, t_list, t_zone = st.tabs([
        "🔍 Rechercher / Ajouter", 
        "📋 Liste complète (Édition)", 
        "🌍 Par Zone & Période"
    ])

    # ==========================================================
    # ONGLET 1 : RECHERCHE ET AJOUT
    # ==========================================================
    with t_search:
        col_left, col_right = st.columns(2)
        
        with col_left:
            st.markdown("#### ➕ Nouveau contact")
            _nc_v = st.session_state.get("_nc_form_v", 0)
            with st.form(f"f_nc_{_nc_v}"):
                n  = st.text_input("Nom")
                p  = st.text_input("Tél")
                addr_unknown = st.checkbox("⚠️ Adresse inconnue pour l'instant")
                a = st.text_input("Adresse", disabled=addr_unknown)
                
                c_ty, c_tm = st.columns(2)
                ty = c_ty.selectbox("Type", INTERVENTION_KEYS, index=1)
                tm = c_tm.selectbox("Dispo", ["Libre", "Matin", "Après-midi", "Heure précise"], key=f"nc_tm_{_nc_v}")
                
                pt = None
                if tm == "Heure précise":
                    _pt = st.time_input("Heure", value=dt_time(7, 45))
                    pt  = _pt.hour * 3600 + _pt.minute * 60
                
                no = st.text_area("Notes", height=68)
                
                c_wd, c_pm = st.columns(2)
                wd = c_wd.text_input("Jours (ex: Lun, Mar)")
                pm = c_pm.selectbox("Mois relance", range(13), format_func=lambda x: ["Non défini","Jan","Fév","Mar","Avr","Mai","Juin","Juil","Août","Sep","Oct","Nov","Déc"][x])
                
                b1, b2, b3 = st.columns(3)
                if b1.form_submit_button("💾 Créer", use_container_width=True, type="primary"):
                    _handle_new_contact(n, p, a, addr_unknown, ty, no, tm, pt, wd, pm, "create")
                if b2.form_submit_button("💾 + Plan", use_container_width=True):
                    _handle_new_contact(n, p, a, addr_unknown, ty, no, tm, pt, wd, pm, "save")
                if b3.form_submit_button("⏳ + Attente", use_container_width=True):
                    _handle_new_contact(n, p, a, addr_unknown, ty, no, tm, pt, wd, pm, "wait")

        with col_right:
            st.markdown("#### 🔍 Rechercher une adresse")
            q = st.text_input("Saisissez une adresse", placeholder="12 rue…", key="as_q", label_visibility="collapsed")
            if st.button("🔍 Chercher l'adresse", key="as_b", use_container_width=True, type="primary", disabled=len(q.strip())<5):
                if q:
                    ck = _norm_addr(q) + f"|{Config.ADDR_SEARCH_LIMIT}"
                    cached = st.session_state.get("_addr_search_cache", {}).get(ck)
                    if cached: 
                        st.session_state.address_suggestions["search"] = cached
                    else:
                        with st.spinner("Recherche..."):
                            sug = Geo.search_address_suggestions(q, limit=Config.ADDR_SEARCH_LIMIT)
                            if sug: st.session_state.address_suggestions["search"] = sug
                            else: st.warning("Aucun résultat")
            
            if "search" in st.session_state.address_suggestions:
                sug = st.session_state.address_suggestions["search"]
                st.caption(f"{len(sug)} résultat(s)")
                for idx, s in enumerate(sug):
                    c_b, c_s = st.columns([8, 1.2])
                    if c_b.button(f"➕ {s['display_name'][:50]}...", key=f"s_s_{idx}", use_container_width=True):
                        m = ContactManager.get_index_by_addr().get(_norm_addr(s['display_name']))
                        StateManager.add_point(s['display_name'], name=m[1].get('name', '') if m else "")
                        StateManager.points()[-1].coordinates = (s['lat'], s['lon'])
                        del st.session_state.address_suggestions["search"]
                        StateManager.commit()
                    if c_s.button("💾", key=f"s_sv_{idx}", help="Enregistrer dans le carnet"):
                        st.session_state[f"_os_s_{idx}"] = True
                    
                    if st.session_state.get(f"_os_s_{idx}"):
                        m = ContactManager.get_index_by_addr().get(_norm_addr(s['display_name']))
                        _inline_save_form({"address": s['display_name'], "name": m[1].get('name', '') if m else "", 
                                         "intervention_type": DEFAULT_INTERVENTION_TYPE, "service_duration": 45*SPM, 
                                         "notes": "", "time_mode": "Libre"}, f"search_{idx}")

    # ==========================================================
    # ONGLET 2 : LISTE COMPLÈTE (DATA EDITOR)
    # ==========================================================
    with t_list:
        st.markdown("#### 📋 Liste complète des contacts")
        
        # --- Gestion de la visibilité et de l'ordre des colonnes ---
        all_cols_map = {
            "view": "🔍",
            "is_new": "Statut",
            "name": "Nom", "address": "Adresse", "phone": "Tél", 
            "intervention_type": "Type", "service_duration": "Durée", 
            "notes": "Notes", "time_mode": "Dispo", 
            "preferred_time": "Heure cible", "visit_dates": "Historique", 
            "preferred_month": "Relance"
        }
        
        default_order = list(all_cols_map.keys())
        saved_order = UIPreferencesManager.get("contacts_column_order", default_order)
        current_order = [c for c in saved_order if c in all_cols_map]
        for c in all_cols_map:
            if c not in current_order: current_order.append(c)
        
        col_title, col_prefs = st.columns([4, 1])
        with col_prefs:
            if HAS_POPOVER:
                with st.popover("⚙️ Disposition"):
                    st.subheader("Colonnes & Ordre")
                    new_order = list(current_order)
                    visible_cols = UIPreferencesManager.get("contacts_visible_columns", list(all_cols_map.keys()))
                    new_visible = []
                    for i, col_id in enumerate(current_order):
                        col_name = all_cols_map[col_id]
                        c_vis, c_name, c_up, c_dn = st.columns([1, 3, 1, 1])
                        if c_vis.checkbox("", value=(col_id in visible_cols), key=f"check_{col_id}", label_visibility="collapsed"):
                            new_visible.append(col_id)
                        c_name.write(f"**{col_name}**")
                        if c_up.button("↑", key=f"up_{col_id}", disabled=(i == 0)):
                            new_order[i], new_order[i-1] = new_order[i-1], new_order[i]
                            UIPreferencesManager.set("contacts_column_order", new_order)
                            st.rerun()
                        if c_dn.button("↓", key=f"dn_{col_id}", disabled=(i == len(current_order)-1)):
                            new_order[i], new_order[i+1] = new_order[i+1], new_order[i]
                            UIPreferencesManager.set("contacts_column_order", new_order)
                            st.rerun()
                    if set(new_visible) != set(visible_cols):
                        UIPreferencesManager.set("contacts_visible_columns", new_visible)
                        st.rerun()

        st.caption("Cochez 🔍 pour voir les détails d'un client.")
        
        contacts = st.session_state.address_book
        if not contacts:
            st.info("Le carnet d'adresses est vide.")
        else:
            # Préparation des données
            display_contacts = []
            for c in contacts:
                c_copy = dict(c)
                c_copy["view"] = False
                c_copy["select"] = False
                # Conversion de la durée en minutes pour l'affichage
                c_copy["service_duration"] = c_copy.get("service_duration", 2700) // SPM
                # Nouveau client si historique vide
                has_history = "visit_dates" in c and isinstance(c["visit_dates"], list) and len(c["visit_dates"]) > 0
                c_copy["is_new"] = "🆕" if not has_history else ""

                if "visit_dates" in c_copy and isinstance(c_copy["visit_dates"], list):
                    c_copy["visit_dates"] = sorted(c_copy["visit_dates"], key=_sort_key_date, reverse=True)
                display_contacts.append(c_copy)

            df_contacts = pd.DataFrame(display_contacts)

            base_config = {
                "select": st.column_config.CheckboxColumn("☑", help="Sélectionner pour action groupée", width="small"),
                "view": st.column_config.CheckboxColumn("🔍", help="Voir les détails", width="small"),
                "is_new": st.column_config.TextColumn("🆕", width="small", help="Nouveau client (aucun historique)"),
                "name": st.column_config.TextColumn("👤 Noms", required=True),
                "address": st.column_config.TextColumn("🧭Adresses", width="large"),
                "phone": st.column_config.TextColumn("Tél"),
                "intervention_type": st.column_config.SelectboxColumn("Type", options=INTERVENTION_KEYS),
                "service_duration": st.column_config.NumberColumn("Durées (min)", min_value=0, step=5),
                "notes": st.column_config.TextColumn("📝 Notes", width="medium"),                "time_mode": st.column_config.SelectboxColumn("Dispos", options=["Libre", "Matin", "Après-midi", "Heure précise"]),
                "preferred_time": st.column_config.NumberColumn("Heure (s)", help="Secondes depuis minuit"),
                "visit_dates": st.column_config.ListColumn(" 🕧 Historiques", width="small"),
                "preferred_month": st.column_config.NumberColumn("Relances (1-12)"),
            }
            
            visible_cols = UIPreferencesManager.get("contacts_visible_columns", list(all_cols_map.keys()))
            final_column_order = [c for c in current_order if c in visible_cols]
            if "view" not in final_column_order: final_column_order.insert(0, "view")
            if "select" not in final_column_order: final_column_order.insert(0, "select")
            
            if "_editor_version" not in st.session_state: st.session_state["_editor_version"] = 0
            
            edited_df = st.data_editor(
                df_contacts,
                column_config=base_config,
                column_order=final_column_order,
                disabled=["visit_dates", "is_new"],
                num_rows="dynamic",
                use_container_width=True,
                key=f"contacts_editor_v{st.session_state._editor_version}"
            )

            if edited_df["view"].any():
                idx = edited_df["view"].idxmax()
                st.session_state["_popup_contact"] = contacts[idx]
                st.session_state["_editor_version"] += 1
                st.rerun()

            if "_popup_contact" in st.session_state:
                _show_contact_info_popup(st.session_state["_popup_contact"])

            # ── Actions groupées sur la sélection ──────────────────────────
            sel_indices = edited_df[edited_df["select"] == True].index.tolist()
            n_sel = len(sel_indices)
            selected_contacts = [contacts[i] for i in sel_indices if i < len(contacts)]

            st.markdown("---")
            _c_lbl, _c_sav_edit, _c_plan, _c_save, _c_wait  = st.columns([3, 5, 4, 4, 4])
            _c_lbl.markdown(
                f"**{n_sel} client(s) sélectionné(s)**" if n_sel else "_Aucune sélection_",
                help="Cochez la colonne ☑ pour sélectionner des clients"
            )

            if _c_plan.button(
                f"🚀 Planifier ({n_sel})", use_container_width=True,
                type="primary", disabled=not sel_indices,
                key="list_batch_plan"
            ):
                ex_n = {p.address_norm for p in StateManager.points()}
                added = 0
                for client in selected_contacts:
                    if _norm_addr(client.get("address", "")) not in ex_n:
                        StateManager.add_from_history(client)
                        added += 1
                StateManager.commit()
                st.toast(f"✅ {added} client(s) ajouté(s) à la tournée.")

            if _c_save.button(
                f"💽 Sauvegarder ({n_sel})", use_container_width=True,
                type="primary", disabled=not sel_indices, key="list_batch_save"
            ):
                st.session_state["_list_batch_save_open"] = True
                st.rerun()

            if _c_wait.button(
                f"⏳ Attente ({n_sel})", use_container_width=True,
                type="primary", disabled=not sel_indices, key="list_batch_wait"
            ):
                WaitlistManager.batch_add(selected_contacts)
                st.toast(f"⏳ {n_sel} client(s) envoyé(s) en file d'attente.")
                st.rerun()

            if _c_sav_edit.button("💾 Enregistrer Modifs", type="primary", use_container_width=True, key="list_save_edits"):
                df_to_save = edited_df.drop(columns=["view", "select", "is_new"], errors='ignore')
                clean_data = df_to_save.where(pd.notnull(df_to_save), None).to_dict('records')
                
                # Récupération des données brutes avant modif pour comparer
                old_contacts = st.session_state.address_book
                
                for i, _c in enumerate(clean_data):
                    if i >= len(old_contacts): continue
                    old_c = old_contacts[i]
                    
                    # 1. Conversion durée affichée (min) -> interne (sec)
                    new_dur_min = _c.get("service_duration")
                    new_dur_sec = int(new_dur_min) * SPM if new_dur_min is not None else 2700
                    old_dur_sec = old_c.get("service_duration", 2700)
                    
                    # 2. Type d'intervention
                    new_type = _c.get("intervention_type")
                    old_type = old_c.get("intervention_type")
                    
                    # Logique de synchronisation
                    from sr_core import INTERVENTION_TYPES
                    
                    # Si la durée a changé mais pas le type -> on met à jour le type
                    if new_dur_sec != old_dur_sec and new_type == old_type:
                        found = False
                        for k, v in INTERVENTION_TYPES.items():
                            if v == new_dur_sec:
                                _c["intervention_type"] = k
                                found = True
                                break
                        if not found:
                            _c["intervention_type"] = f"Standard_{new_dur_sec // SPM}"
                    
                    # Si le type a changé mais pas la durée -> on met à jour la durée
                    elif new_type != old_type and new_dur_sec == old_dur_sec:
                        if new_type in INTERVENTION_TYPES:
                            _c["service_duration"] = INTERVENTION_TYPES[new_type]
                        else:
                            # Extraction de la durée depuis le nom (ex: Standard_70 -> 70)
                            match = re.search(r"_(\d+)$", new_type)
                            if match:
                                _c["service_duration"] = int(match.group(1)) * SPM
                    else:
                        # On garde la durée calculée en (1)
                        _c["service_duration"] = new_dur_sec

                    # Nettoyage historique
                    if not isinstance(_c.get("visit_dates"), list):
                        _c["visit_dates"] = []

                st.session_state.address_book = clean_data
                AddressBookManager.set_dirty()
                AddressBookManager.save_to_file(sync_import_csv=True)
                ContactManager.invalidate_index()
                st.success("✅ Carnet mis à jour !")
                st.rerun()

            # ── Dialog de sauvegarde groupée ───────────────────────────────
            if st.session_state.get("_list_batch_save_open") and selected_contacts:
                st.markdown("---")
                st.info(f"📦 Sauvegarde de **{len(selected_contacts)}** client(s) dans une tournée")
                lot_s = RouteManager.list_saves()
                col1, col2 = st.columns(2)
                lot_in = col1.text_input("Nouveau nom de tournée", key="list_batch_save_name")
                lot_ex = col2.selectbox("Ou choisir une existante", ["—"] + lot_s, key="list_batch_save_sel")
                b_ok, b_can = st.columns(2)
                if b_ok.button("✅ Confirmer", use_container_width=True, type="primary", key="list_batch_save_ok"):
                    sn = lot_in.strip() or (lot_ex if lot_ex != "—" else "")
                    if sn:
                        _n_a, _n_s, _msg = RouteManager.add_clients_to_save(sn, selected_contacts)
                        st.success(_msg)
                        st.session_state.pop("_list_batch_save_open", None)
                        st.rerun()
                    else:
                        st.warning("Veuillez saisir un nom de tournée.")
                if b_can.button("Annuler", use_container_width=True, key="list_batch_save_cancel"):
                    st.session_state.pop("_list_batch_save_open", None)
                    st.rerun()

    # ==========================================================
    # ONGLET 3 : PAR ZONE & PÉRIODE
    # ==========================================================
    with t_zone:
        _render_zone_period_section()

def _show_contact_info_popup(c: dict):
    """Affiche les détails complets du contact dans un st.dialog."""
    if not HAS_DIALOG: return

    @st.dialog(f"👤 {c.get('name', 'Client sans nom')}")
    def _popup():
        # Utiliser un état local pour la confirmation sans fermer le dialog
        if "_show_confirm" not in st.session_state:
            st.session_state["_show_confirm"] = False

        st.markdown(f"🧭 **Adresse :** `{c.get('address', 'Inconnue')}`")
        st.markdown(f"📞 **Téléphone :** `{c.get('phone', 'Non renseigné')}`")
        st.markdown("---")
        
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**🔧 Intervention**")
            st.caption(f"Type : {c.get('intervention_type', 'Standard')}")
            st.caption(f"Durée : {c.get('service_duration', 2700)//60} min")
        with col2:
            st.markdown("**📅 Préférences**")
            st.caption(f"Dispo : {c.get('time_mode', 'Libre')}")
            st.caption(f"Mois relance : {c.get('preferred_month', 0)}")

        st.markdown("**📝 Notes :**")
        st.info(c.get('notes') or "Aucune note pour ce client.")
        
        st.markdown("**⏳ Historique des passages :**")
        dates = sorted(c.get('visit_dates', []), key=_sort_key_date, reverse=True)
        if dates:
            st.write(", ".join(dates))
        else:
            st.caption("Aucun passage enregistré.")
            
        st.markdown("---")
        
        if st.session_state["_show_confirm"]:
            st.error(f"⚠️ Supprimer définitivement **{c.get('name','ce contact')}** ?")
            b_yes, b_no = st.columns(2)
            if b_yes.button("🔥 Oui, Supprimer", use_container_width=True, type="primary"):
                ContactManager.delete_contact_by_key(_norm_addr(c.get("address", "")), _norm_addr(c.get("name", "")))
                st.session_state.pop("_show_confirm", None)
                st.session_state.pop("_popup_contact", None) # Clear to close dialog
                st.rerun() 
            if b_no.button("Annuler", use_container_width=True):
                st.session_state["_show_confirm"] = False
                # Pas de rerun() ici, le dialog va se re-rendre tout seul
        else:
            ca, cw, cd, cc = st.columns(4)
            if ca.button("🚀Planifier ", use_container_width=True, type="secondary"):
                StateManager.add_from_history(c)
                StateManager.commit()
                st.toast("Ajouté à la tournée")
            if cw.button("⏳Attente", use_container_width=True):
                WaitlistManager.add(c)
                st.toast("Ajouté en attente")
            if cd.button("🗑️Supp", use_container_width=True):
                st.session_state["_show_confirm"] = True
                # Pas de rerun() ici
            if cc.button("Fermer", use_container_width=True):
                st.session_state.pop("_show_confirm", None)
                st.session_state.pop("_popup_contact", None) # Clear to close dialog
                st.rerun()

    _popup()

def _handle_new_contact(n, p, a, addr_unknown, ty, no, tm, pt, wd, pm, action):
    if not addr_unknown and not a.strip():
        st.error("Adresse requise (ou cocher 'Adresse inconnue')")
        return
    if not n.strip() and not p.strip():
        st.error("Nom ou téléphone requis")
        return
        
    _addr = "ADRESSE_INCONNUE" if addr_unknown else a.strip()

    # FIX #5 — vérification de doublon avant création pour éviter les entrées silencieuses
    # (double-clic, re-soumission du formulaire, contact déjà existant)
    dup_idx = ContactManager.find_duplicate(n.strip(), _addr)
    if dup_idx is not None:
        st.warning(f"⚠️ Ce contact existe déjà dans le carnet (ligne {dup_idx + 1}). Aucun doublon créé.")
        return

    new_c = Contact(
        name=n.strip(), address=_addr, phone=p.strip(),
        intervention_type=ty, notes=no.strip(),
        service_duration=INTERVENTION_TYPES.get(ty, 2700),
        time_mode=tm, preferred_time=pt,
        available_weekdays=wd.strip(), preferred_month=int(pm)
    )
    ContactManager.add_contact(new_c)
    
    if addr_unknown:
        for entry in st.session_state.address_book:
            if _norm_addr(entry.get("address","")) == "adresse inconnue" and _norm_addr(entry.get("name","")) == _norm_addr(new_c.name):
                entry["address_unknown"] = True
                break
        AddressBookManager.set_dirty()
        AddressBookManager.save_to_file()

    if action == "save":
        _sk = str(abs(hash((_norm_addr(new_c.name), _norm_addr(new_c.address)))))[:12]
        st.session_state[f"_open_save_{_sk}"] = True
    elif action == "wait":
        WaitlistManager.add({
            "name": new_c.name, "address": _addr, "phone": new_c.phone,
            "intervention_type": new_c.intervention_type, "notes": new_c.notes,
            "service_duration": new_c.service_duration, "address_unknown": addr_unknown,
        })
        st.toast(f"⏳ {new_c.name or _addr} ajouté en attente.")
    else:
        st.toast("✅ Nouveau contact créé !")
    
    st.session_state["_nc_form_v"] = st.session_state.get("_nc_form_v", 0) + 1
    st.rerun()

def _render_zone_period_section():
    target_y = str(datetime.today().year - 1)
    combined = ContactManager.get_combined_contacts()
    
    c1, c2, c3 = st.columns([2, 1, 1])
    mode = c1.radio("Filtrer par", ["🌍 Ville / Zone", "📅 Mois (Année-1)"], horizontal=True)
    target_m = c2.selectbox("Mois cible", range(13), index=datetime.today().month, 
                           format_func=lambda x: ["Tous","Jan","Fév","Mar","Avr","Mai","Jun","Juil","Août","Sep","Oct","Nov","Déc"][x])
    
    zone_q = []
    if mode == "🌍 Ville / Zone":
        zone_q = st.multiselect("Sélectionner une ou plusieurs villes", options=ContactManager.get_all_cities())

    if (zone_q and mode == "🌍 Ville / Zone") or mode == "📅 Mois (Année-1)":
        matches = [h for h in combined if 
                  (mode == "📅 Mois (Année-1)" or any(q.lower() in h.get("address","").lower() for q in zone_q)) and 
                  any(len(d.split("/"))==3 and d.split("/")[2]==target_y and (target_m==0 or int(d.split("/")[1])==target_m) for d in h.get("visit_dates", []))]
        
        # Reset sélection si filtre change
        f_key = f"{mode}_{target_m}_{zone_q}"
        if st.session_state.get("_zs_filter") != f_key:
            st.session_state._zs_filter = f_key
            st.session_state._zs_def = False
            st.session_state._zs_v = st.session_state.get("_zs_v", 0) + 1

        if matches:
            st.markdown(f"**📍 {len(matches)} client(s) trouvé(s)**")

            # --- Sélection par lot ---
            if "_zs_v" not in st.session_state: st.session_state._zs_v = 0
            if "_zs_def" not in st.session_state: st.session_state._zs_def = False
            
            c_sel1, c_sel2, _ = st.columns([1, 1, 4])
            if c_sel1.button("✅ Tout", key="zs_all", use_container_width=True):
                st.session_state._zs_def = True
                st.session_state._zs_v += 1
                st.rerun()
            if c_sel2.button("❌ Aucun", key="zs_none", use_container_width=True):
                st.session_state._zs_def = False
                st.session_state._zs_v += 1
                st.rerun()
            
            # Préparation des données pour sélection par tableau
            match_data = []
            ex_n = {p.address_norm for p in StateManager.points()}
            
            for m in matches:
                match_data.append({
                    "Sélect.": st.session_state._zs_def,
                    "Nom": m.get("name", ""),
                    "Adresse": m.get("address", ""),
                    "Dates d'interventions": sorted(m.get("visit_dates", []), key=_sort_key_date, reverse=True) if m.get("visit_dates") else "—",
                    "Notes": m.get("notes", ""),
                    "En cours": "✅" if _norm_addr(m.get("address","")) in ex_n else ""
                })
            
            edit_matches = st.data_editor(
                pd.DataFrame(match_data),
                column_config={"Sélect.": st.column_config.CheckboxColumn(required=False)},
                disabled=["Nom", "Adresse", "Dates d'interventions", "Notes", "En cours"],
                use_container_width=False,
                hide_index=False,
                key=f"zone_selector_v{st.session_state._zs_v}"
            )
            
            sel_indices = edit_matches[edit_matches["Sélect."]].index.tolist()
            selected_clients = [matches[i] for i in sel_indices]
            
            col_a, col_s, col_w = st.columns(3)
            if col_a.button(f"➕ Ajouter {len(sel_indices)} à la tournée", type="primary", use_container_width=True, disabled=not sel_indices):
                for client in selected_clients:
                    StateManager.add_from_history(client)
                StateManager.commit()
            
            if col_s.button(f"💾 Sauvegarder {len(sel_indices)}", use_container_width=True, disabled=not sel_indices):
                st.session_state["_os_z_lot"] = True
                
            if col_w.button(f"⏳ Mettre en attente {len(sel_indices)}", use_container_width=True, disabled=not sel_indices):
                WaitlistManager.batch_add(selected_clients)
                st.success(f"✅ {len(sel_indices)} client(s) ajouté(s) à la file d'attente.")
                st.rerun()
            
            if st.session_state.get("_os_z_lot"):
                _render_lot_save_dialog(selected_clients)
        else:
            st.warning("Aucun client ne correspond à ces critères pour l'année dernière.")

def _render_lot_save_dialog(clients):
    st.info("📦 Sauvegarde groupée dans une tournée")
    lot_s = RouteManager.list_saves()
    col1, col2 = st.columns(2)
    lot_in = col1.text_input("Nouveau nom de tournée")
    lot_ex = col2.selectbox("Ou choisir une existante", ["—"] + lot_s)
    
    b_ok, b_can = st.columns(2)
    if b_ok.button("✅ Confirmer l'ajout", use_container_width=True, type="primary"):
        sn = lot_in.strip() or (lot_ex if lot_ex != "—" else "")
        if sn:
            _n_a, _n_s, _msg = RouteManager.add_clients_to_save(sn, clients)
            st.success(_msg)
            st.session_state.pop("_os_z_lot", None)
            st.rerun()
    if b_can.button("Annuler", use_container_width=True):
        st.session_state.pop("_os_z_lot", None)
        st.rerun()
