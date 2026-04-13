import streamlit as st
import os
import atexit
from sr_persistence import CacheCleaner

# Enregistrement du vidage automatique du cache à la fermeture
atexit.register(CacheCleaner.clear_python_cache)

def _read_version():
    if os.path.exists("version.txt"):
        with open("version.txt", "r", encoding="utf-8") as f:
            return f.read().strip()
    return "SW#1004-1#"

st.set_page_config(layout="wide", page_title=f"♨️{_read_version()}")

from datetime import datetime

# ── IMPORTS DES MODULES ──────────────────────────────────────────────────────
from sr_core import (
    _h, Config, OSRM_URL, MAP_CENTER, WORK_START, WORK_END, MIDI, DEBUT_APM,
    SPH, SPM
)
from sr_persistence import RouteManager
from sr_logic import OSRM, Optimizer
from sr_state import StateManager
from sr_ui import (
    UI, _render_tab_tournee, _render_sidebar
)
from sr_ui_contacts import _render_tab_contacts
from sr_ui_sauvegardes import _render_tab_sauvegardes
from sr_ui_csv import _render_tab_csv
from sr_ui_waitlist import _render_tab_waitlist
from sr_agenda import _render_tab_agenda
from sr_ui_nettoyage import _render_tab_nettoyage
from sr_ui_import_vcard import _render_tab_import_vcard

# ==========================================================
# CONFIGURATION
# ==========================================================
def main():
    try:
        StateManager.init()
        
        # Style CSS global
        st.markdown("""
            <style>
            button[data-testid='baseButton-headerNoPadding'] { display: none !important; } 
            .main .block-container { max-width: 1300px !important; padding: 1rem 2rem !important; } 
            html, body, [class*='css'] { font-size: 0.95rem !important; } 
            div[data-testid='stExpander'] { margin-bottom: 0.5rem !important; } 
            .stMetricValue { font-size: 1.3rem !important; } 
            
            /* Amélioration visuelle du menu latéral */
            section[data-testid="stSidebar"] {
                background-color: #f8f9fa;
                border-right: 1px solid #e0e0e0;
            }
            [data-testid="stSidebarUserContent"] [data-testid="stVerticalBlock"] {
                gap: 1.1rem !important;
            }
            [data-testid="stSidebar"] .stMarkdown p {
                margin-bottom: 0.1rem !important;
            }
            [data-testid="stSidebar"] hr {
                margin: 0.5rem 0 !important;
            }
            /* Réduction des paddings des widgets dans la sidebar */
            [data-testid="stSidebar"] div[data-testid="stVerticalBlock"] > div {
                margin-bottom: -0.2rem !important;
            }
            .st-emotion-cache-16ids99 p {
                font-weight: 600 !important;
            }
            </style>
            """, unsafe_allow_html=True)

        # ── Navigation latérale ─────────────────────────────────────────────
        with st.sidebar:
            st.markdown("<h1 style='text-align:center; color:#1a73e8; margin-top: -70px;'>♨️ SweepRoute</h1>", unsafe_allow_html=True)

            
            menu = st.radio(
                "Menu Principal",
                [
                    "🪄 Planification", 
                    "📂 Sauvegardes", 
                    "📅 Agenda", 
                    "📓 Carnet d'adresses",
                    "⌛ File d'attente", 
                    "🛠 Nettoyage & Maintenance", 
                    "⬇⬆ Import / Export CSV", 
                    "☎ Import vCard"
                ],
                index=0
            )
            st.markdown("---")
            
            # Appel de la sidebar contextuelle originale (paramètres de tournée)
            _render_sidebar()
            
        # ── En-tête de page ────────────────────────────────────────────────
        if st.session_state.last_error: 
            st.warning(f"⚠️ {st.session_state.last_error}")
            st.session_state.last_error = None

        # ── Ré-optimisation automatique ────────────────────────────────────
        if StateManager.pop_reopt_pending() and not StateManager.is_manual_plan():
            cfg, pts = StateManager.config(), StateManager.points()
            optim_pts = [p for p in pts if not p.is_start and not p.is_end]
            all_coords = [cfg.start_coordinates] + [p.coordinates for p in optim_pts] + [cfg.end_coordinates]
            if all(c is not None for c in all_coords):
                with st.spinner("🔄 Optimisation..."):
                    mats = OSRM.matrix(all_coords)
                    if mats:
                        result = Optimizer.optimize(cfg, pts, precomputed_mats=mats)
                        if result:
                            st.session_state.optimized_result = result
                            StateManager.set_last_mats(mats)

        # ── Restauration autosave ─────────────────────────────────────────
        if st.session_state.get("_autosave_available"):
            info = st.session_state["_autosave_available"]
            st.warning(f"💾 Une tournée en cours a été détectée ({info['n_pts']} points, {info['saved_at'][:16].replace('T',' ')}).")
            c1, c2 = st.columns(2)
            if c1.button("↺ Restaurer la session", use_container_width=True, type="primary"):
                RouteManager.load(RouteManager.AUTOSAVE_NAME)
                del st.session_state["_autosave_available"]
                st.rerun()
            if c2.button("✗ Ignorer", use_container_width=True):
                del st.session_state["_autosave_available"]
                st.rerun()

        # ── Affichage du contenu selon le menu ──────────────────────────────
        if menu == "🪄 Planification":
            _render_tab_tournee()
        elif menu == "📂 Sauvegardes":
            _render_tab_sauvegardes()
        elif menu == "📅 Agenda":
            _render_tab_agenda()
        elif menu == "📓 Carnet d'adresses":
            _render_tab_contacts()
        elif menu == "⌛ File d'attente":
            _render_tab_waitlist()
        elif menu == "🛠 Nettoyage & Maintenance":
            _render_tab_nettoyage()
        elif menu == "⬇⬆ Import / Export CSV":
            _render_tab_csv()
        elif menu == "☎ Import vCard":
            _render_tab_import_vcard()

        st.markdown("<div style='text-align:center;color:gray;font-size:0.9em;margin-top:4em'>SweepRoute &nbsp;·&nbsp; ⚙ Optimisation temps réel &nbsp;·&nbsp; ⚠️ Respect des contraintes horaires</div>", unsafe_allow_html=True)

    except Exception as e:
        st.error(f"❌ Erreur critique: {_h(str(e))}")
        import traceback
        st.code(traceback.format_exc())

if __name__ == "__main__":
    main()
