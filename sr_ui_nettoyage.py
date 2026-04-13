"""sr_ui_nettoyage.py — Onglet 🛠 Nettoyage : fusion et dédoublonnage du carnet d'adresses."""
import streamlit as st
import pandas as pd
import json
import os
from datetime import datetime

from sr_persistence import AddressBookManager, ContactManager

def _parse_date(date_str: str):
    try:
        return datetime.strptime(date_str, "%d/%m/%Y")
    except Exception:
        return datetime.min

def _render_tab_nettoyage():
    st.markdown("### 🛠 Nettoyage et Maintenance")
    
    t_dup, t_bulk = st.tabs(["🔍 Détection de Doublons", "🧹 Nettoyage par lots"])
    
    contacts = st.session_state.address_book
    if not contacts:
        with t_dup:
            st.info("Le carnet d'adresses est vide.")
        with t_bulk:
            st.info("Le carnet d'adresses est vide.")
        return
        
    df = pd.DataFrame(contacts)

    # ==========================================================
    # ONGLET 1 : DÉTECTION DE DOUBLONS
    # ==========================================================
    with t_dup:
        st.markdown("#### 🔍 Détection automatique")
        
        # Détection variations d'adresse (même nom, adresses différentes)
        names_multi_addr = df.groupby('name')['address'].nunique()
        names_multi_addr = names_multi_addr[names_multi_addr > 1].index.tolist()

        # Détection doublons exacts (même nom ET même adresse)
        exact_dup_counts = df.groupby(['name', 'address']).size().reset_index(name='count')
        exact_dups = exact_dup_counts[exact_dup_counts['count'] > 1]
        
        m1, m2 = st.columns(2)
        m1.metric("🟡 Clients multi-adresses", len(names_multi_addr))
        m2.metric("🔴 Doublons exacts", len(exact_dups))

        all_target_names = sorted(set(names_multi_addr + exact_dups['name'].unique().tolist()))

        if not all_target_names:
            st.success("✅ Aucun doublon évident détecté.")
        else:
            name_to_clean = st.selectbox("Sélectionner un client à traiter", ["—"] + all_target_names)
            
            if name_to_clean != "—":
                client_rows = df[df['name'] == name_to_clean].copy()
                st.write(f"Entrées trouvées pour **{name_to_clean}** :")
                
                # Sélection des lignes à fusionner
                client_rows['_merge'] = False
                edited_rows = st.data_editor(
                    client_rows,
                    column_config={
                        "_merge": st.column_config.CheckboxColumn("Fusionner ?", default=False),
                        "visit_dates": st.column_config.ListColumn("Passages")
                    },
                    disabled=["name", "address", "visit_dates"],
                    hide_index=True,
                    use_container_width=True,
                    key=f"editor_dup_{name_to_clean}"
                )
                
                to_merge = edited_rows[edited_rows['_merge']]
                
                if len(to_merge) >= 2:
                    st.markdown("---")
                    st.markdown("**Configuration de la fusion**")
                    c1, c2, c3 = st.columns(3)
                    final_name = c1.text_input("Nom final", value=name_to_clean)
                    final_addr = c2.text_input("Adresse finale", value=to_merge.iloc[0]['address'])
                    final_phone = c3.text_input("Téléphone final", value=to_merge.iloc[0]['phone'] if 'phone' in to_merge.columns else "")
                    
                    if st.button("✨ Fusionner les entrées sélectionnées", type="primary", use_container_width=True):
                        # Logique de fusion
                        new_book = [c for c in contacts if not (
                            c['name'] == name_to_clean and 
                            c['address'] in to_merge['address'].values
                        )]
                        
                        # Fusion des dates et notes
                        all_dates = set()
                        all_notes = []
                        for _, row in to_merge.iterrows():
                            if isinstance(row['visit_dates'], list):
                                for d in row['visit_dates']: all_dates.add(d)
                            note = str(row.get('notes', '')).strip()
                            if note and note not in all_notes: all_notes.append(note)
                        
                        final_row_dict = to_merge.iloc[0].to_dict()
                        final_entry = {k: (None if pd.isna(v) else v) for k, v in final_row_dict.items()}
                        final_entry.update({
                            "name": final_name,
                            "address": final_addr,
                            "phone": final_phone,
                            "visit_dates": sorted(list(all_dates), key=_parse_date),
                            "notes": " / ".join(all_notes)
                        })
                        if '_merge' in final_entry: del final_entry['_merge']
                        
                        new_book.append(final_entry)
                        st.session_state.address_book = new_book
                        AddressBookManager.set_dirty()
                        AddressBookManager.save_to_file(sync_import_csv=True)
                        ContactManager.invalidate_index()
                        st.success("✅ Fusion terminée !")
                        st.rerun()

    # ==========================================================
    # ONGLET 2 : NETTOYAGE PAR LOTS
    # ==========================================================
    with t_bulk:
        st.markdown("#### 🧹 Suppression et édition rapide")
        st.caption("Utilisez ce tableau pour supprimer rapidement des lignes ou corriger des erreurs massives.")
        
        # On affiche un éditeur simplifié pour le nettoyage
        clean_df = df.copy()
        
        edited_clean_df = st.data_editor(
            clean_df,
            column_config={
                "visit_dates": st.column_config.ListColumn("Historique", width="small"),
            },
            num_rows="dynamic",
            use_container_width=True,
            key="bulk_cleaner"
        )
        
        if st.button("💾 Sauvegarder le nettoyage", type="primary"):
            st.session_state.address_book = edited_clean_df.where(pd.notnull(edited_clean_df), None).to_dict('records')
            AddressBookManager.set_dirty()
            AddressBookManager.save_to_file(sync_import_csv=True)
            ContactManager.invalidate_index()
            st.success("✅ Modifications enregistrées !")
            st.rerun()

    # ==========================================================
    # SECTION : CACHE GÉOLOCALISATION
    # ==========================================================
    st.markdown("---")
    st.markdown("### 🗺️ Cache de géolocalisation")
    st.caption(
        "Si une adresse a été mal géolocalisée (ex : ville homonyme dans un autre département), "
        "videz le cache pour forcer un nouveau géocodage."
    )
    n_cache = len(st.session_state.get("coord_cache", {}))
    st.info(f"📦 Entrées en cache : **{n_cache}**")

    col_g1, col_g2 = st.columns(2)

    # Vider une adresse spécifique
    addr_to_clear = col_g1.text_input(
        "Adresse à recalculer", placeholder="24 rue de Lattre de Tassigny 88120 Vagney",
        key="geo_clear_addr"
    )
    if col_g1.button("🔄 Recalculer cette adresse", use_container_width=True, disabled=not addr_to_clear.strip()):
        from sr_core import _norm_addr
        key = _norm_addr(addr_to_clear)
        cache = st.session_state.get("coord_cache", {})
        removed = 0
        for k in list(cache.keys()):
            if key in k or k in key:
                del cache[k]
                removed += 1
        # Aussi supprimer la clé exacte
        cache.pop(key, None)
        from sr_persistence import GeoCache
        GeoCache.save()
        st.success(f"✅ {removed or 1} entrée(s) supprimée(s) du cache. L'adresse sera re-géocodée au prochain calcul.")
        st.rerun()

    # Vider tout le cache
    if col_g2.button("🗑️ Vider tout le cache géo", use_container_width=True):
        st.session_state["_confirm_clear_geocache"] = True

    if st.session_state.get("_confirm_clear_geocache"):
        st.warning("⚠️ Toutes les coordonnées mises en cache seront supprimées. Êtes-vous sûr ?")
        c1, c2 = st.columns(2)
        if c1.button("✅ Confirmer", key="geo_clear_confirm", type="primary", use_container_width=True):
            st.session_state["coord_cache"] = {}
            from sr_persistence import GeoCache
            GeoCache.save()
            st.session_state.pop("_confirm_clear_geocache", None)
            st.success("✅ Cache géo vidé. Les adresses seront re-géocodées à la prochaine optimisation.")
            st.rerun()
        if c2.button("Annuler", key="geo_clear_cancel", use_container_width=True):
            st.session_state.pop("_confirm_clear_geocache", None)
            st.rerun()
