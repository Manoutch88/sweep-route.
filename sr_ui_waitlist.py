"""sr_ui_waitlist.py — Onglet ⏳ File d'attente."""
import streamlit as st
import io
from datetime import datetime
from typing import List

from sr_core import (
    _norm_addr, _h,
    DEFAULT_INTERVENTION_TYPE, SPM,
    HAS_DIALOG,
    _RE_CITY_EXTRACTOR,
)
from sr_persistence import (
    ContactManager, AddressBookManager, RouteManager, WaitlistManager,
)
from sr_state import StateManager

# ==========================================================
# GÉNÉRATION PDF IMPRIMABLE
# ==========================================================
_MOIS_PDF = ['','Janvier','Février','Mars','Avril','Mai','Juin',
             'Juillet','Août','Septembre','Octobre','Novembre','Décembre']

def _ensure_reportlab():
    """Installe reportlab automatiquement si le module est absent."""
    try:
        import reportlab  # noqa: F401
    except ImportError:
        import subprocess, sys
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "reportlab", "--quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )


def _generate_waitlist_pdf(waitlist: list) -> bytes:
    """Génère un PDF imprimable de la file d'attente avec une fiche complète par client."""
    _ensure_reportlab()
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    )
    from reportlab.lib.enums import TA_CENTER

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18*mm, rightMargin=18*mm,
        topMargin=18*mm,  bottomMargin=18*mm,
        title="File d'attente SweepRoute", author="SweepRoute"
    )
    W = A4[0] - 36*mm  # largeur utile

    # ── Styles ────────────────────────────────────────────────────────────
    base = getSampleStyleSheet()
    def _sty(name, parent='Normal', **kw):
        return ParagraphStyle(name, parent=base[parent], **kw)

    from reportlab.lib.fonts import addMapping
    sty_title  = _sty('SR_T',    'Title',  fontSize=18, textColor=colors.HexColor('#1a237e'), spaceAfter=2*mm)
    sty_sub    = _sty('SR_S',    fontSize=9,  textColor=colors.HexColor('#666666'), spaceAfter=4*mm)
    sty_name   = _sty('SR_N',    fontSize=11, fontName='Helvetica-Bold', textColor=colors.HexColor('#1a237e'))
    sty_label  = _sty('SR_L',    fontSize=8,  fontName='Helvetica-Bold', textColor=colors.HexColor('#555555'))
    sty_val    = _sty('SR_V',    fontSize=9,  textColor=colors.HexColor('#222222'))
    sty_warn   = _sty('SR_W',    fontSize=9,  fontName='Helvetica-Bold', textColor=colors.HexColor('#e67e22'))
    sty_note   = _sty('SR_NOTE', fontSize=8.5, textColor=colors.HexColor('#444444'),
                      leftIndent=3*mm, backColor=colors.HexColor('#f5f5f5'))
    sty_footer = _sty('SR_F',    fontSize=7.5, textColor=colors.HexColor('#aaaaaa'), alignment=TA_CENTER)
    sty_num    = _sty('SR_NUM',  fontSize=8,  fontName='Helvetica-Bold',
                      textColor=colors.white, alignment=TA_CENTER)

    story = []
    now_str = datetime.now().strftime("%d/%m/%Y a %H:%M")

    # ── En-tête ───────────────────────────────────────────────────────────
    story.append(Paragraph("File d'attente — SweepRoute", sty_title))
    story.append(Paragraph(
        f"Imprime le {now_str}   |   {len(waitlist)} client(s) en attente",
        sty_sub
    ))
    story.append(HRFlowable(width="100%", thickness=1.5,
                             color=colors.HexColor('#1a237e'), spaceAfter=5*mm))

    col_w = (W - 4*mm) / 2

    def _cell(lbl, val):
        return [Paragraph(lbl, sty_label), Paragraph(str(val) if val else "—", sty_val)]

    def _grid_row(left, right):
        t = Table(
            [[left[0], left[1], right[0], right[1]]],
            colWidths=[22*mm, col_w - 22*mm, 22*mm, col_w - 22*mm]
        )
        t.setStyle(TableStyle([
            ('VALIGN',       (0,0), (-1,-1), 'TOP'),
            ('TOPPADDING',   (0,0), (-1,-1), 1*mm),
            ('BOTTOMPADDING',(0,0), (-1,-1), 1*mm),
            ('LEFTPADDING',  (0,0), (-1,-1), 1.5*mm),
        ]))
        return t

    # ── Fiches clients ────────────────────────────────────────────────────
    for i, item in enumerate(waitlist):
        c       = item.get("client", {})
        name    = (c.get("name")    or "").strip()
        phone   = (c.get("phone")   or "").strip()
        address = (c.get("address") or "").strip()
        notes   = (c.get("notes")   or "").strip()
        itype   = (c.get("intervention_type") or "").strip()
        svc     = c.get("service_duration") or 0
        tm      = (c.get("time_mode") or "Libre").strip()
        pt      = c.get("preferred_time")
        wd      = (c.get("available_weekdays") or "").strip()
        pm      = c.get("preferred_month") or 0
        unknown = c.get("address_unknown", False) or address == "ADRESSE_INCONNUE"
        added   = (item.get("added_at") or "")[:10]

        label   = name or phone or "Contact sans nom"
        svc_mn  = svc // 60 if svc else 0
        heure_str = ""
        if tm == "Heure precise" and pt is not None:
            heure_str = f" — {pt // 3600:02d}:{(pt % 3600) // 60:02d}"
        mois_val   = _MOIS_PDF[pm] if pm and 1 <= pm <= 12 else "—"
        horaire_v  = f"{tm}{heure_str}" if tm != "Libre" else "Libre"
        addr_par   = Paragraph("Adresse inconnue", sty_warn) if unknown else Paragraph(address, sty_val)
        num_bg     = colors.HexColor('#e67e22') if unknown else colors.HexColor('#1a237e')

        # Bandeau titre de la fiche
        badge = Table(
            [[Paragraph(str(i + 1), sty_num), Paragraph(label, sty_name)]],
            colWidths=[7*mm, W - 7*mm]
        )
        badge.setStyle(TableStyle([
            ('BACKGROUND',   (0,0), (0,0), num_bg),
            ('VALIGN',       (0,0), (-1,-1), 'MIDDLE'),
            ('TOPPADDING',   (0,0), (-1,-1), 1.5*mm),
            ('BOTTOMPADDING',(0,0), (-1,-1), 1.5*mm),
            ('LEFTPADDING',  (0,0), (0,0),   2*mm),
            ('LEFTPADDING',  (1,0), (1,0),   3*mm),
        ]))
        story.append(badge)
        story.append(Spacer(1, 1.5*mm))

        # Grille 4 rangées × 2 colonnes
        grid = Table(
            [
                [_grid_row([Paragraph("Telephone", sty_label), Paragraph(phone or "—", sty_val)],
                           [Paragraph("Adresse",   sty_label), addr_par])],
                [_grid_row(_cell("Intervention", itype),
                           _cell("Duree", f"{svc_mn} min" if svc_mn else "—"))],
                [_grid_row(_cell("Horaire souhaite",  horaire_v),
                           _cell("Jours disponibles", wd or "—"))],
                [_grid_row(_cell("Mois prefere",    mois_val),
                           _cell("En attente depuis", added or "—"))],
            ],
            colWidths=[W]
        )
        grid.setStyle(TableStyle([
            ('BOX',          (0,0), (-1,-1), 0.5, colors.HexColor('#cccccc')),
            ('ROWBACKGROUNDS',(0,0),(-1,-1), [colors.HexColor('#f9f9f9'), colors.white]),
            ('LEFTPADDING',  (0,0), (-1,-1), 2*mm),
            ('RIGHTPADDING', (0,0), (-1,-1), 2*mm),
        ]))
        story.append(grid)

        if notes:
            story.append(Spacer(1, 1*mm))
            story.append(Paragraph(f"Notes : {notes}", sty_note))

        story.append(Spacer(1, 4*mm))
        if i < len(waitlist) - 1:
            story.append(HRFlowable(width="100%", thickness=0.4,
                                     color=colors.HexColor('#dddddd'), spaceAfter=3*mm))

    # ── Pied de page ──────────────────────────────────────────────────────
    story.append(Spacer(1, 5*mm))
    story.append(HRFlowable(width="100%", thickness=0.5,
                             color=colors.HexColor('#cccccc'), spaceAfter=2*mm))
    story.append(Paragraph(
        f"SweepRoute — File d'attente imprimee le {now_str}",
        sty_footer
    ))

    doc.build(story)
    buf.seek(0)
    return buf.read()

# ── Dialog info client ────────────────────────────────────────────────────────
if HAS_DIALOG:
    @st.dialog("ℹ️ Informations client", width="small")
    def _show_client_info_dialog(item: dict):
        _MOIS = ['','Janvier','Février','Mars','Avril','Mai','Juin',
                 'Juillet','Août','Septembre','Octobre','Novembre','Décembre']
        _TM_ICON = {'Matin': '🌅', 'Après-midi': '🌆', 'Heure précise': '⏰'}

        c        = item.get('client', {})
        uid      = item.get('uid', '')
        added_at = item.get('added_at', '')

        name    = c.get('name', '') or ''
        phone   = c.get('phone', '') or ''
        address = c.get('address', '') or ''
        notes   = c.get('notes', '') or ''
        itype   = c.get('intervention_type', '') or ''
        svc     = c.get('service_duration', 0)
        tm      = c.get('time_mode', 'Libre') or 'Libre'
        pt      = c.get('preferred_time') or c.get('target_time')
        wd      = c.get('available_weekdays', '') or ''
        pm      = c.get('preferred_month', 0) or 0
        unknown = c.get('address_unknown', False) or address == "ADRESSE_INCONNUE"

        # Mode édition
        edit_mode = st.toggle("📝 Mode édition", key=f"wl_edit_mode_{uid}")

        if not edit_mode:
            # ── Identité ──────────────────────────────────────────────────────────
            st.markdown(f"### 👤 {_h(name) if name else '_Sans nom_'}")
            if phone:
                st.markdown(f"📞 [{_h(phone)}](tel:{phone.replace(' ','')})")

            st.markdown("---")

            # ── Adresse ───────────────────────────────────────────────────────────
            col1, col2 = st.columns(2)
            with col1:
                st.markdown("**📍 Adresse**")
                if unknown:
                    st.warning("⚠️ Adresse inconnue")
                else:
                    st.write(address)

            # ── Intervention ──────────────────────────────────────────────────────
            with col2:
                st.markdown("**🔧 Intervention**")
                if itype:
                    st.write(itype)
                if svc:
                    st.write(f"⏱ Durée : {svc // 60} min")

            st.markdown("---")

            # ── Disponibilités ────────────────────────────────────────────────────
            col3, col4 = st.columns(2)
            with col3:
                st.markdown("**🗓 Disponibilités**")
                if wd:
                    st.write(wd)
                else:
                    st.caption("Non renseignées")
                if pm and 1 <= pm <= 12:
                    st.write(f"📅 Mois préféré : {_MOIS[pm]}")

            with col4:
                st.markdown("**🕐 Horaire**")
                if tm != 'Libre':
                    icon = _TM_ICON.get(tm, '🕐')
                    heure_str = ""
                    if tm == 'Heure précise' and pt is not None:
                        heure_str = f" — {pt // 3600:02d}:{(pt % 3600) // 60:02d}"
                    st.write(f"{icon} {tm}{heure_str}")
                else:
                    st.caption("Libre")

            # ── Notes ─────────────────────────────────────────────────────────────
            if notes:
                st.markdown("---")
                st.markdown("**📝 Notes**")
                st.info(notes)
        else:
            # ── FORMULAIRE D'ÉDITION ──────────────────────────────────────────
            st.markdown("### 📝 Modifier les informations")
            
            new_name = st.text_input("Nom", value=name)
            new_phone = st.text_input("Téléphone", value=phone)
            
            new_svc_min = st.number_input("⏱ Durée d'intervention (min)", 
                                         value=svc // 60, min_value=1, max_value=480, step=5)
            
            new_notes = st.text_area("📝 Notes", value=notes, height=100)
            
            if st.button("✅ Enregistrer les modifications", type="primary", use_container_width=True):
                c['name'] = new_name
                c['phone'] = new_phone
                c['service_duration'] = new_svc_min * 60
                c['notes'] = new_notes
                
                if WaitlistManager.update(uid, c):
                    st.success("Modifications enregistrées !")
                    st.rerun()
                else:
                    st.error("Erreur lors de la mise à jour.")

        # ── Métadonnées ───────────────────────────────────────────────────────
        if added_at:
            st.markdown("---")
            st.caption(f"🕓 Ajouté en file d'attente le {added_at[:10]}")

else:
    # Fallback si Streamlit < 1.35 : bloc session_state
    def _show_client_info_dialog(item: dict):
        st.session_state["_wl_info_client"] = item

def _render_tab_waitlist():
    from datetime import date as _date
    _JOURS_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

    st.subheader("⏳ Clients en attente de planification")
    waitlist = WaitlistManager.load()

    # ── Barre de sauvegarde / chargement ─────────────────────────────────
    with st.expander("💾 Sauvegardes de la file d'attente", expanded=False):
        snapshots = WaitlistManager.list_snapshots()

        # ── Section 1 : Sauvegarder la file actuelle ──────────────────────
        st.markdown("**💾 Sauvegarder la file actuelle**")
        snap_name = st.text_input("Nom de la sauvegarde", key="wl_snap_name",
                                   placeholder="ex: Attente_Janvier")
        if st.button("💾 Sauvegarder", key="wl_snap_save",
                     use_container_width=True, disabled=not snap_name.strip()):
            ok, msg = WaitlistManager.save_snapshot(snap_name.strip())
            (st.success if ok else st.warning)(msg)
            st.rerun()

        st.markdown("<hr style='margin:10px 0'>", unsafe_allow_html=True)

        # ── Section 2 : Charger / supprimer ───────────────────────────────
        st.markdown("**📂 Charger / Supprimer**")
        if snapshots:
            sel_snap = st.selectbox("Sauvegarde", snapshots,
                                    key="wl_snap_sel",
                                    label_visibility="collapsed")

            # Destination du chargement
            _route_saves = RouteManager.list_saves()
            _dest_opts   = ["⏳ File d'attente", "📋 Planification en cours"] + _route_saves
            _snap_dest   = st.selectbox("Vers", _dest_opts,
                                         key="wl_snap_dest",
                                         label_visibility="collapsed")

            sl1, sl2 = st.columns(2)
            if sl1.button("📂 Charger", key="wl_snap_load",
                          use_container_width=True, type="primary"):
                import os as _os, json as _json
                _path = _os.path.join(WaitlistManager.SNAPSHOT_DIR, f"{sel_snap}.json")
                try:
                    with open(_path, 'r', encoding='utf-8') as _f:
                        _snap_data = _json.load(_f)
                    _items = _snap_data.get("items", [])
                except Exception as _e:
                    st.error(f"Erreur lecture : {_e}")
                    _items = []

                if _items:
                    if _snap_dest == "⏳ File d'attente":
                        ok, msg = WaitlistManager.load_snapshot(sel_snap)
                        (st.success if ok else st.warning)(msg)

                    elif _snap_dest == "📋 Planification en cours":
                        _added = 0
                        for _it in _items:
                            _cl = _it.get("client", {})
                            _addr = _cl.get("address", "")
                            _unknown = _cl.get("address_unknown", False) or _addr == "ADRESSE_INCONNUE"
                            if _unknown:
                                continue
                            StateManager.add_point(_addr, name=_cl.get("name",""))
                            _pt = StateManager.points()[-1]
                            _pt.notes             = _cl.get("notes", "")
                            _pt.intervention_type = _cl.get("intervention_type", DEFAULT_INTERVENTION_TYPE)
                            _pt.service_duration  = _cl.get("service_duration", 45 * SPM)
                            _pt.time_mode         = _cl.get("time_mode", "Libre")
                            _pt.target_time       = _cl.get("target_time")
                            _added += 1
                        if _added:
                            StateManager.commit(do_rerun=False)
                            st.success(f"✅ {_added} client(s) ajouté(s) à la planification.")
                        else:
                            st.warning("Aucun client avec adresse connue à ajouter.")

                    else:
                        _clients = [_it.get("client", {}) for _it in _items]
                        _n_a, _n_s, _msg = RouteManager.add_clients_to_save(_snap_dest, _clients)
                        (st.success if _n_a > 0 else st.warning)(_msg)

                    st.rerun()
                else:
                    st.warning("Sauvegarde vide.")

            if sl2.button("🗑️ Supprimer", key="wl_snap_del",
                          use_container_width=True):
                st.session_state["_wl_snap_confirm_del"] = sel_snap
            if st.session_state.get("_wl_snap_confirm_del"):
                _sd = st.session_state["_wl_snap_confirm_del"]
                st.warning(f"Supprimer la sauvegarde « {_sd} » ?")
                sd1, sd2 = st.columns(2)
                if sd1.button("✅ Oui", key="wl_snap_del_ok",
                              type="primary", use_container_width=True):
                    ok, msg = WaitlistManager.delete_snapshot(_sd)
                    st.session_state.pop("_wl_snap_confirm_del", None)
                    (st.success if ok else st.warning)(msg)
                    st.rerun()
                if sd2.button("Annuler", key="wl_snap_del_no",
                              use_container_width=True):
                    st.session_state.pop("_wl_snap_confirm_del", None)
                    st.rerun()
        else:
            st.caption("Aucune sauvegarde.")

    if not waitlist:
        st.info("La file d'attente est vide.")
        return

    # ── Barre d'actions : compteur + 4 boutons sur la même ligne ─────────
    _col_info, _col_save, _col_pdf, _col_dl, _col_clear = st.columns([3, 2, 2, 2, 2])

    _col_info.caption(f"📋 {len(waitlist)} client(s) en attente — à planifier selon disponibilités")

    # Bouton Sauvegarder (ouvre / ferme le panneau de sauvegarde rapide)
    if _col_save.button("💾", key="wl_quick_save_tog",
                        use_container_width=True,
                        help="Sauvegarder la file d'attente actuelle"):
        st.session_state["_wl_quick_save_open"] = not st.session_state.get("_wl_quick_save_open", False)
        st.rerun()

    # Bouton Créer PDF (génère et stocke en session)
    if _col_pdf.button("🖨️", key="wl_print_btn",
                       use_container_width=True,
                       help="Générer un PDF imprimable de la file d'attente"):
        try:
            with st.spinner("Génération du PDF…"):
                _pdf_bytes = _generate_waitlist_pdf(waitlist)
            _fname = f"file_attente_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
            st.session_state["_wl_pdf_ready"] = (_pdf_bytes, _fname)
            st.rerun()
        except Exception as _e:
            st.error(f"Erreur génération PDF : {_e}")

    # Bouton Télécharger (visible uniquement si un PDF a été généré)
    if st.session_state.get("_wl_pdf_ready"):
        _pdf_bytes, _fname = st.session_state["_wl_pdf_ready"]
        _col_dl.download_button(
            label="📥",
            data=_pdf_bytes,
            file_name=_fname,
            mime="application/pdf",
            use_container_width=True,
            key="wl_print_dl",
            help="Télécharger le PDF généré"
        )
    else:
        _col_dl.button("📥", disabled=True,
                       use_container_width=True, key="wl_dl_disabled",
                       help="Cliquez sur 'Créer un PDF' avant de télécharger")

    # Bouton Vider la file d'attente
    if _col_clear.button("🗑️", key="wl_clear_tog",
                         use_container_width=True,
                         help="Supprimer tous les clients de la file d'attente"):
        st.session_state["_wl_clear_confirm"] = not st.session_state.get("_wl_clear_confirm", False)
        st.session_state["_wl_quick_save_open"] = False
        st.rerun()

    # ── Panneau de confirmation : vider la file ───────────────────────────
    if st.session_state.get("_wl_clear_confirm", False):
        with st.container():
            st.markdown(
                "<div style='background:rgba(255,80,80,0.07);border-left:3px solid #e74c3c;"
                "border-radius:6px;padding:10px 14px;margin:6px 0'>",
                unsafe_allow_html=True
            )
            st.warning(
                f"⚠️ Confirmer la suppression des **{len(waitlist)} client(s)** "
                "de la file d'attente ? Cette action est irréversible."
            )
            _cc1, _cc2 = st.columns(2)
            if _cc1.button("✅ Oui, vider", key="wl_clear_ok",
                           use_container_width=True, type="primary"):
                WaitlistManager.clear()
                st.session_state.pop("_wl_clear_confirm", None)
                st.success("✅ File d'attente vidée.")
                st.rerun()
            if _cc2.button("Annuler", key="wl_clear_cancel",
                           use_container_width=True):
                st.session_state.pop("_wl_clear_confirm", None)
                st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)

    # Panneau sauvegarde rapide (dépliable sous la barre)
    if st.session_state.get("_wl_quick_save_open", False):
        with st.container():
            st.markdown(
                "<div style='background:rgba(255,200,0,0.07);border-left:3px solid #f0b400;"
                "border-radius:6px;padding:10px 14px;margin:6px 0'>",
                unsafe_allow_html=True
            )
            _qs_name = st.text_input("Nom de la sauvegarde", key="wl_qs_name",
                                      placeholder="ex: Attente_Avril")
            _qs1, _qs2 = st.columns(2)
            if _qs1.button("✅ Sauvegarder", key="wl_qs_ok",
                           use_container_width=True, type="primary",
                           disabled=not _qs_name.strip()):
                ok, msg = WaitlistManager.save_snapshot(_qs_name.strip())
                (st.success if ok else st.warning)(msg)
                st.session_state["_wl_quick_save_open"] = False
                st.rerun()
            if _qs2.button("Annuler", key="wl_qs_cancel", use_container_width=True):
                st.session_state["_wl_quick_save_open"] = False
                st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("---")

    # ── ACTIONS PAR LOT (Options 1, 2, 3) ────────────────────────────────
    st.markdown("### ⬆️ Clients a envoyer vers la planification")
    
    # Initialisation de l'état de sélection
    if "wl_selected" not in st.session_state:
        st.session_state["wl_selected"] = {} # index -> bool

    # Filtres de sélection (Option 3)
    f1, f2, f3, f4, f5 = st.columns([2.9, 2.9, 2.9, 2.9, 2])
    
    if f1.button("☑️", key="wl_bulk_all", use_container_width=True, help="Tout cocher"):
        for i in range(len(waitlist)): 
            st.session_state["wl_selected"][i] = True
            st.session_state[f"wl_sel_cb_{i}"] = True
        st.rerun()
    
    if f2.button("⬜", key="wl_bulk_none", use_container_width=True, help="Tout décocher"):
        st.session_state["wl_selected"] = {}
        for i in range(len(waitlist)):
            st.session_state[f"wl_sel_cb_{i}"] = False
        st.rerun()

    if f3.button("🌅", key="wl_bulk_matin", use_container_width=True, help="Cocher tous les clients 'Matin'"):
        for i, item in enumerate(waitlist):
            is_m = item.get("client", {}).get("time_mode") == "Matin"
            st.session_state["wl_selected"][i] = is_m
            st.session_state[f"wl_sel_cb_{i}"] = is_m
        st.rerun()

    if f4.button("🌆", key="wl_bulk_apm", use_container_width=True, help="Cocher tous les clients 'Après-midi'"):
        for i, item in enumerate(waitlist):
            is_a = item.get("client", {}).get("time_mode") == "Après-midi"
            st.session_state["wl_selected"][i] = is_a
            st.session_state[f"wl_sel_cb_{i}"] = is_a
        st.rerun()

    if f5.button("🗑️", key="wl_bulk_clear_btn", use_container_width=True, help="Vider toute la file d'attente"):
        st.session_state["_wl_confirm_clear_all"] = True
        st.rerun()

    if st.session_state.get("_wl_confirm_clear_all", False):
        st.warning(f"⚠️ Supprimer **{len(waitlist)} client(s)** de la file d'attente ?")
        _cc1, _cc2 = st.columns(2)
        if _cc1.button("✅ Oui, tout vider", key="wl_clear_all_yes", type="primary", use_container_width=True):
            WaitlistManager.clear()
            st.session_state["wl_selected"] = {}
            st.session_state.pop("_wl_confirm_clear_all", None)
            st.success("🗑️ File d'attente vidée.")
            st.rerun()
        if _cc2.button("Annuler", key="wl_clear_all_no", use_container_width=True):
            st.session_state.pop("_wl_confirm_clear_all", None)
            st.rerun()

    # Destination et Envoi (Option 3 suite)
    with st.container(border=True):
        c_dest1, c_dest2, c_dest3 = st.columns([2, 2, 4])

        target_date = c_dest1.date_input("Date cible pour l'envoi", value=_date.today(), key="wl_bulk_date", format="DD/MM/YYYY")

        # Nombre de sélectionnés
        n_sel = sum(1 for v in st.session_state["wl_selected"].values() if v)

        c_dest2.markdown(f"<div style='padding-top:35px; font-weight:bold; color:#3498db;'>{n_sel} client(s) sélectionné(s)</div>", unsafe_allow_html=True)

        # Ajout d'un décalage pour aligner les boutons avec le champ de saisie
        c_dest3.markdown("<div style='padding-top:28px;'></div>", unsafe_allow_html=True)
        b_col1, b_col2 = c_dest3.columns(2, gap="medium")
        with b_col1:
            if st.button("⬆️📅", key="wl_bulk_send", type="primary", use_container_width=True, disabled=(n_sel == 0), help="Envoyer vers l'agenda"):
                # ... (reste du code inchangé)
                from sr_agenda import AgendaManager
                iso_b = target_date.isocalendar()
                y_b, w_b, d_idx_b = iso_b[0], iso_b[1], iso_b[2] - 1
                if d_idx_b >= 6:
                    st.error("Impossible : l'agenda ne couvre pas le dimanche.")
                else:
                    j_fr_b = _JOURS_FR[d_idx_b]
                    to_send_clients = []
                    to_remove_indices = []
                    for i, is_sel in st.session_state["wl_selected"].items():
                        if is_sel and i < len(waitlist):
                            to_send_clients.append(dict(waitlist[i]["client"]))
                            to_remove_indices.append(i)
                    if to_send_clients:
                        sent_count = AgendaManager.batch_add_clients(y_b, w_b, j_fr_b, to_send_clients)
                        if sent_count > 0:
                            for i in sorted(to_remove_indices, reverse=True): WaitlistManager.remove(i)
                            st.session_state["wl_selected"] = {}
                            st.success(f"✅ {sent_count} client(s) envoyé(s) vers le {j_fr_b} {target_date.strftime('%d/%m/%Y')}")
                            st.rerun()

        with b_col2:
            if st.button("⬆️📜", key="wl_bulk_to_plan", use_container_width=True, disabled=(n_sel == 0), help="Envoyer vers la planification"):
                # ... (reste du code inchangé)
                added_p = 0
                to_remove_p = []
                for i, is_sel in st.session_state["wl_selected"].items():
                    if is_sel and i < len(waitlist):
                        c = waitlist[i]["client"]
                        if not c.get("address") or c.get("address")=="ADRESSE_INCONNUE": continue
                        StateManager.add_point(c['address'], name=c.get('name',''))
                        pt = StateManager.points()[-1]
                        pt.service_duration = c.get('service_duration', 45*60)
                        pt.intervention_type = c.get('intervention_type', 'Standard_45')
                        pt.notes = c.get('notes', '')
                        added_p += 1
                        to_remove_p.append(i)
                if added_p > 0:
                    for i in sorted(to_remove_p, reverse=True): WaitlistManager.remove(i)
                    StateManager.commit(do_rerun=False)
                    st.session_state["wl_selected"] = {}
                    st.success(f"✅ {added_p} clients ajoutés à la planification.")
                    st.rerun()

    st.markdown("---")

    # ── Tri par ville / code postal ───────────────────────────────────────
    def _extract_city(item: dict) -> str:
        addr = item.get("client", {}).get("address", "") or ""
        m = _RE_CITY_EXTRACTOR.search(addr)
        if m:
            return m.group(0).strip().upper()
        return "— VILLE INCONNUE"

    def _extract_postcode(item: dict) -> str:
        from sr_core import _RE_POSTCODE as _RE_CP
        addr = item.get("client", {}).get("address", "") or ""
        m = _RE_CP.search(addr)
        return m.group(0) if m else "— CP INCONNU"

    _tog_col1, _tog_col2 = st.columns(2)
    sort_by_city = _tog_col1.toggle("🏙️ Regrouper par ville", key="wl_sort_city", value=False)
    sort_by_cp   = _tog_col2.toggle("📮 Regrouper par code postal", key="wl_sort_cp", value=False)

    # sort_by_cp prime sur sort_by_city si les deux sont actifs
    from itertools import groupby
    if sort_by_cp:
        waitlist_sorted = sorted(waitlist, key=_extract_postcode)
        groups = [("cp:" + cp, list(items)) for cp, items in groupby(waitlist_sorted, key=_extract_postcode)]
    elif sort_by_city:
        waitlist_sorted = sorted(waitlist, key=_extract_city)
        groups = [("city:" + city, list(items)) for city, items in groupby(waitlist_sorted, key=_extract_city)]
    else:
        groups = [("", waitlist)]

    # ── Grille 2 colonnes via HTML + items en pleine largeur ─────────────
    # On rend les fiches par paires dans des colonnes Streamlit,
    # mais chaque fiche est autonome (carte HTML + boutons en dessous)
    # pour éviter la compression du contenu dans les expanders imbriqués.

    for group_label, city_items in groups:
        _is_cp_group   = group_label.startswith("cp:")
        _is_city_group = group_label.startswith("city:")
    for group_label, city_items in groups:
        if group_label.startswith("cp:"):
           _label_text = group_label[3:]
        elif group_label.startswith("city:"):
           _label_text = group_label[5:]
        else:
           _label_text = group_label

        if (_is_city_group or _is_cp_group) and _label_text:
            # Bandeau groupe + case à cocher par lot
            group_safe = _label_text.replace(" ", "_").replace("—", "X")
            ck_group_key = f"wl_group_all_{group_safe}"
            city_indices = [waitlist.index(it) for it in city_items]
            all_checked = all(st.session_state.get(f"wl_sel_cb_{i}", False) for i in city_indices)

            if _is_cp_group:
                _icon, _color, _bg, _tc = "📮", "#27ae60", "rgba(39,174,96,0.10)", "#1a5c2e"
            else:
                _icon, _color, _bg, _tc = "🏙️", "#3498db", "rgba(52,152,219,0.10)", "#1a5276"

            col_city_lbl, col_city_cb = st.columns([9, 1])
            col_city_lbl.markdown(
                f"<div style='background:{_bg};border-left:4px solid {_color};"
                f"border-radius:6px;padding:6px 14px;margin:10px 0 2px 0;"
                f"font-weight:700;font-size:1.05em;color:{_tc}'>{_icon} {_label_text}"
                f" <span style='font-size:0.8em;font-weight:400;color:#555'>({len(city_items)})</span></div>",
                unsafe_allow_html=True
            )
            def _toggle_group(indices=city_indices, ck=ck_group_key):
                new_val = st.session_state[ck]
                for i in indices:
                    st.session_state["wl_selected"][i] = new_val
                    st.session_state[f"wl_sel_cb_{i}"] = new_val
            col_city_cb.markdown("<div style='padding-top:12px'></div>", unsafe_allow_html=True)
            col_city_cb.checkbox(
                "Tout", key=ck_group_key,
                value=all_checked,
                on_change=_toggle_group,
                help=f"Cocher/décocher tous les clients — {_label_text}"
            )

        items_pairs = [city_items[i:i+2] for i in range(0, len(city_items), 2)]

        for pair in items_pairs:
            pair_cols = st.columns(len(pair))
            for col, (idx, item) in zip(pair_cols,
                                         [(waitlist.index(it), it) for it in pair]):
              with col:
                c            = item["client"]
                item_uid     = item.get("uid", "")
                name_label   = c.get('name', '') or ''
                phone        = c.get('phone', '') or ''
                _is_unknown  = c.get('address_unknown', False) or c.get('address', '') == "ADRESSE_INCONNUE"
                added        = item.get('added_at', '')[:10] or ''

                if name_label:
                    _ident = name_label
                elif phone:
                    _ident = phone
                else:
                    _ident = "Contact sans nom"

                addr_line = "⚠️ Adresse inconnue" if _is_unknown else c['address']

                # ── Carte HTML compacte ───────────────────────────────────────
                _phone_html = (
                    f"<a href='tel:{phone.replace(' ','')}' style='color:#2196F3;"
                    f"font-weight:600;text-decoration:none'>📞 {_h(phone)}</a><br>"
                ) if phone else ""
                _name_html  = f"👤 <b>{_h(name_label)}</b><br>" if name_label else ""
                _addr_html  = (
                    f"<span style='color:#e67e22'>⚠️ Adresse inconnue</span>"
                    if _is_unknown else
                    f"<span style='color:#888;font-size:0.88em'>📍 {_h(addr_line[:55])}</span>"
                )
                
                _svc_val = c.get('service_duration', 0)
                _svc_html = f" <span style='color:#3498db;font-size:0.85em;margin-left:8px'>⏱ <b>{_svc_val // 60} min</b></span>" if _svc_val else ""

                _notes_html = (
                    f"<br><span style='color:#999;font-size:0.82em'>"
                    f"📝 {_h(c['notes'][:70])}{'…' if len(c['notes'])>70 else ''}</span>"
                ) if c.get('notes') else ""
                _date_html  = (
                    f"<br><span style='color:#bbb;font-size:0.78em'>🕓 {added}</span>"
                ) if added else ""
                _border = "#e67e22" if _is_unknown else "#3498db"

                # Case à cocher de sélection (Option 1)
                def _on_change_cb(i=idx):
                    # Mise à jour de l'état global lors d'un clic individuel
                    st.session_state["wl_selected"][i] = st.session_state[f"wl_sel_cb_{i}"]

                # Initialisation de la clé si elle n'existe pas encore
                if f"wl_sel_cb_{idx}" not in st.session_state:
                    st.session_state[f"wl_sel_cb_{idx}"] = st.session_state["wl_selected"].get(idx, False)

                st.checkbox("Sélectionner", 
                            key=f"wl_sel_cb_{idx}",
                            label_visibility="collapsed",
                            on_change=_on_change_cb)

                st.markdown(
                    f"<div style='border-left:3px solid {_border};"
                    f"background:rgba(52,152,219,0.04);border-radius:6px;"
                    f"padding:8px 10px;margin-bottom:4px;line-height:1.5'>"
                    f"{_phone_html}{_name_html}{_addr_html}{_svc_html}{_notes_html}{_date_html}"
                    f"</div>",
                    unsafe_allow_html=True
                )

                # ── Boutons principaux ────────────────────────────────────────
                b_plan, b_info, b_sav, b_del = st.columns(4)
                _plan_disabled = _is_unknown
                _plan_help = "⚠️ Complétez l'adresse avant de planifier" if _is_unknown else None

                if b_info.button("ℹ️", key=f"wl_info_{idx}",
                                 use_container_width=True,
                                 help="Voir les informations du client"):
                    _show_client_info_dialog(item)

                if b_plan.button("📅", key=f"reint_open_{idx}",
                                 use_container_width=True, type="primary",
                                 disabled=_plan_disabled, help="Planifier"):
                    _plan_help_key_form = f"_wf_open_{idx}"
                    st.session_state[key_form] = not st.session_state.get(key_form, False)
                    st.session_state.pop(f"_wf_save_open_{idx}", None)
                    st.rerun()

                if b_sav.button("💾", key=f"wl_ind_save_tog_{idx}",
                                use_container_width=True,
                                help="Sauvegarder dans une tournée agenda"):
                    _sk = f"_wf_save_open_{idx}"
                    st.session_state[_sk] = not st.session_state.get(_sk, False)
                    st.session_state.pop(f"_wf_open_{idx}", None)
                    st.rerun()

                if b_del.button("🗑️", key=f"del_w_{idx}",
                                use_container_width=True, help="Retirer de la file"):
                    WaitlistManager.remove(item_uid)
                    st.rerun()

                # ── Formulaire de sauvegarde individuelle (file d'attente) ──────
                if st.session_state.get(f"_wf_save_open_{idx}", False):
                    st.markdown(
                        "<div style='background:rgba(255,200,0,0.07);"
                        "border-left:3px solid #f0b400;border-radius:6px;"
                        "padding:10px 14px;margin-top:6px'>",
                        unsafe_allow_html=True
                    )
                    st.markdown("**💾 Sauvegarder dans une sauvegarde de file d'attente**")
                    _snap_list = WaitlistManager.list_snapshots()
                    _sav_new = st.text_input("Nouvelle sauvegarde", key=f"wf_sav_new_{idx}",
                                              placeholder="Nom…")
                    if _snap_list:
                        _sav_exist = st.selectbox("Ou existante", ["—"] + _snap_list,
                                                   key=f"wf_sav_ex_{idx}",
                                                   label_visibility="collapsed")
                    else:
                        _sav_exist = "—"
                    sv_ok, sv_can = st.columns(2)
                    if sv_ok.button("✅ Sauvegarder", key=f"wf_sav_ok_{idx}",
                                    use_container_width=True, type="primary"):
                        _dest_name = _sav_new.strip() or (_sav_exist if _sav_exist != "—" else "")
                        if _dest_name:
                            _ok_s, _msg_s = WaitlistManager.save_client_to_snapshot(
                                _dest_name, item  # item = {client: …, added_at: …}
                            )
                            (st.success if _ok_s else st.warning)(_msg_s)
                            st.session_state.pop(f"_wf_save_open_{idx}", None)
                            st.rerun()
                        else:
                            st.error("Choisissez ou saisissez un nom de sauvegarde.")
                    if sv_can.button("Annuler", key=f"wf_sav_can_{idx}",
                                     use_container_width=True):
                        st.session_state.pop(f"_wf_save_open_{idx}", None)
                        st.rerun()
                    st.markdown("</div>", unsafe_allow_html=True)

                # ── Formulaire de planification (affiché si ouvert) ───────────
                if st.session_state.get(f"_wf_open_{idx}", False):
                    st.markdown(
                        "<div style='background:rgba(100,180,255,0.07);"
                        "border-left:3px solid #3498db;border-radius:6px;"
                        "padding:10px 14px;margin-top:6px'>",
                        unsafe_allow_html=True
                    )
                    st.markdown("**Choisir une date de réintégration**")

                    chosen_date = st.date_input(
                        "Date",
                        value=_date.today(),
                        key=f"wf_date_{idx}",
                        label_visibility="collapsed",
                        format="DD/MM/YYYY"
                    )

                    # Calcul automatique semaine / jour
                    iso        = chosen_date.isocalendar()
                    year_iso   = iso[0]
                    week_iso   = iso[1]
                    jour_idx   = iso[2] - 1          # 0=Lundi … 6=Dimanche
                    jour_fr    = _JOURS_FR[jour_idx]
                    week_label = f"S{week_iso:02d} {year_iso} — {jour_fr} {chosen_date.strftime('%d/%m/%Y')}"
                    st.info(f"📆 {week_label}")

                    if jour_idx >= 6:
                        st.warning("⚠️ Dimanche — l'agenda ne couvre pas ce jour.")

                    # Ajout d'une option pour modifier la durée avant réintégration
                    new_svc_min = st.number_input("⏱ Durée d'intervention (min)", 
                                                 value=c.get('service_duration', 45*60) // 60, 
                                                 min_value=1, max_value=480, step=5,
                                                 key=f"wf_svc_{idx}")

                    dest_opts = ["📅 Agenda"] + ["📋 Tournée en cours"] + RouteManager.list_saves()
                    dest = st.selectbox("Destination", dest_opts,
                                        key=f"wf_dest_{idx}",
                                        label_visibility="collapsed")

                    cf_ok, cf_can = st.columns(2)
                    if cf_ok.button("✅ Confirmer", key=f"wf_ok_{idx}",
                                    use_container_width=True, type="primary"):
                        c['service_duration'] = new_svc_min * 60
                        if dest == "📅 Agenda":
                            # ─ Ajout dans l'agenda ─────────────────────────────
                            if jour_idx >= 6:
                                st.error("Impossible : l'agenda ne couvre pas le dimanche.")
                            else:
                                try:
                                    from sr_agenda import AgendaManager
                                    ok_ag = AgendaManager.add_client(
                                        year_iso, week_iso, jour_fr, dict(c)
                                    )
                                    if ok_ag:
                                        WaitlistManager.remove(item_uid)
                                        st.session_state.pop(f"_wf_open_{idx}", None)
                                        st.success(
                                            f"✅ {name_label} ajouté à l'agenda "
                                            f"({jour_fr} {chosen_date.strftime('%d/%m/%Y')})"
                                        )
                                        st.rerun()
                                    else:
                                        st.warning("⚠️ Ce client est déjà dans l'agenda ce jour.")
                                except Exception as e:
                                    st.error(f"Erreur agenda : {e}")

                        elif dest == "📋 Tournée en cours":
                            # ─ Ajout dans la tournée active ────────────────────
                            StateManager.add_point(
                                c['address'],
                                name=c.get('name', ''),
                                coords=c.get('coordinates')
                            )
                            pt = StateManager.points()[-1]
                            pt.notes             = c.get('notes', '')
                            pt.intervention_type = c.get('intervention_type', DEFAULT_INTERVENTION_TYPE)
                            pt.service_duration  = c.get('service_duration', 45 * SPM)
                            WaitlistManager.remove(item_uid)
                            st.session_state.pop(f"_wf_open_{idx}", None)
                            StateManager.commit()

                        else:
                            # ─ Ajout dans une tournée sauvegardée ──────────────
                            ok_s, msg_s = RouteManager.add_client_to_save(dest, c)
                            if ok_s:
                                WaitlistManager.remove(item_uid)
                                st.session_state.pop(f"_wf_open_{idx}", None)
                                st.success(msg_s)
                                st.rerun()
                            else:
                                st.warning(msg_s)

                    if cf_can.button("Annuler", key=f"wf_can_{idx}",
                                     use_container_width=True):
                        st.session_state.pop(f"_wf_open_{idx}", None)
                        st.rerun()

                    st.markdown("</div>", unsafe_allow_html=True)
