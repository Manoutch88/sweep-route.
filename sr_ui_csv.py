"""sr_ui_csv.py — Onglet 📥📤 Import/Export CSV."""
import streamlit as st
from sr_persistence import ContactManager, AddressBookManager

def _render_tab_csv():
    st.markdown("#### 📤 Export"); col1, col2 = st.columns(2)
    with col1:
        if st.session_state.address_book: st.download_button("📥 Télécharger CSV", data=AddressBookManager.export_to_csv(), file_name="carnet.csv", mime="text/csv", use_container_width=True)
        else: st.info("Vide")
    with col2:
        if st.button("💾 Exporter vers Import_Clients.csv", use_container_width=True, type="primary"):
            ok, msg = AddressBookManager.export_to_import_csv()
            if ok: st.success(msg)
            else: st.error(msg)
    st.markdown("---"); st.markdown("#### 📥 Import"); st.download_button("📋 Modèle", data=AddressBookManager.get_csv_template(), file_name="modele.csv", mime="text/csv")
    up = st.file_uploader("Fichier CSV", type=["csv"], key="up_csv")
    if up:
        st.caption("Choisir :"); c1, c2 = st.columns(2)
        if c1.button("➕ Ajouter", key="b_csv_a", use_container_width=True, type="primary"):
            i, e = AddressBookManager.import_from_csv(AddressBookManager.decode_csv_bytes(up.getvalue()))
            if i > 0: st.success(f"✅ {i} importé(s)")
            [st.caption(f"⚠️ {err}") for err in e[:5]]; st.rerun()
        if c2.button("🔄 Remplacer", key="b_csv_r", use_container_width=True):
            if st.session_state.get("_c_r_csv"):
                st.session_state.address_book = []; ContactManager.invalidate_index(); i, e = AddressBookManager.import_from_csv(AddressBookManager.decode_csv_bytes(up.getvalue())); st.session_state["_c_r_csv"] = False
                if i > 0: st.success(f"✅ {i} importé(s)")
                [st.caption(f"⚠️ {err}") for err in e[:5]]; st.rerun()
            else: st.session_state["_c_r_csv"] = True; st.warning("⚠️ Action irréversible. Re-cliquez.")
