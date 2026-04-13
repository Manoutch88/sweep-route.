# sr_agenda.py — Agenda hebdomadaire avec timeline inline et dialog par créneau (positionné gauche)
"""
sr_agenda.py — Agenda hebdomadaire avec timeline inline
Lundi→Samedi · 7h00→19h00 · créneaux de 15 minutes
Timeline intégrée à la page ; chaque ligne de créneau ouvre un dialog de planification
positionné à gauche pour laisser la timeline visible.
"""

import streamlit as st
import json
import os
import math
from datetime import date, timedelta
from typing import Dict, List, Tuple, Optional

from sr_core import (
    _norm_addr, _h, DEFAULT_INTERVENTION_TYPE, SPM, SPH, HAS_DIALOG
)
from sr_persistence import RouteManager, ContactManager
from sr_state import StateManager

# ==========================================================
# CONSTANTES TIMELINE
# ==========================================================
JOURS        = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi"]
AGENDA_FILE  = "agenda_semaines.json"

SLOT_START   = 7  * SPH + 30 * SPM  # 27 000 s  -> 07h30
SLOT_END     = 19 * SPH + 30 * SPM  # 70 200 s  -> 19h30
SLOT_STEP    = 15 * SPM    # 900 s     -> 15 min
N_SLOTS      = (SLOT_END - SLOT_START) // SLOT_STEP   # 48 creneaux

# Couleurs par type d'intervention
_ITYPE_COLORS = {
    "Rapide":    "rgba( 46,204,113,.30)",
    "Standard":  "rgba( 52,152,219,.30)",
    "Difficile": "rgba(231, 76, 60,.30)",
    "Conduits":  "rgba(155, 89,182,.30)",
}
_ITYPE_BORDERS = {
    "Rapide":    "#2ecc71",
    "Standard":  "#3498db",
    "Difficile": "#e74c3c",
    "Conduits":  "#9b59b6",
}


def _slot_to_secs(idx: int) -> int:
    return SLOT_START + idx * SLOT_STEP


def _secs_to_slot(secs: int) -> int:
    return (secs - SLOT_START) // SLOT_STEP


def _fmt_time(secs: int) -> str:
    return f"{secs // SPH:02d}:{(secs % SPH) // SPM:02d}"


def _itype_style(itype: str) -> Tuple[str, str]:
    key = itype.split("_")[0] if itype else "Standard"
    return (
        _ITYPE_COLORS.get(key,  "rgba(52,152,219,.30)"),
        _ITYPE_BORDERS.get(key, "#3498db"),
    )


# ==========================================================
# AGENDA MANAGER — persistance disque
# ==========================================================
class AgendaManager:

    _CACHE_KEY = "_agenda_file_cache"

    @staticmethod
    def _load_all() -> dict:
        """Charge le fichier agenda avec cache en session_state (FIX9)."""
        cached = st.session_state.get(AgendaManager._CACHE_KEY)
        if cached is not None:
            return cached
        if os.path.exists(AGENDA_FILE):
            try:
                with open(AGENDA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    st.session_state[AgendaManager._CACHE_KEY] = data
                    return data
            except Exception:
                return {}
        return {}

    @staticmethod
    def _save_all(data: dict):
        """Sauvegarde les données de l'agenda avec robustesse (Windows)."""
        try:
            # On écrit directement dans le fichier pour éviter les erreurs de permission lors de l'os.replace
            with open(AGENDA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            # Invalider le cache après écriture
            st.session_state.pop(AgendaManager._CACHE_KEY, None)
        except Exception as e:
            st.error(f"Erreur lors de la sauvegarde de l'agenda : {e}")

    @staticmethod
    def batch_add_clients(year: int, week: int, jour: str, clients: List[dict]) -> int:
        """Ajoute plusieurs clients d'un coup pour éviter les multiples écritures disque."""
        data = AgendaManager._load_all()
        k    = AgendaManager.week_key(year, week)
        data.setdefault(k, {j: [] for j in JOURS})
        data[k].setdefault(jour, [])
        
        existing = {_norm_addr(c.get("address", "")) for c in data[k][jour]}
        count = 0
        for client in clients:
            c_norm = _norm_addr(client.get("address", ""))
            if c_norm not in existing:
                data[k][jour].append(client)
                existing.add(c_norm)
                count += 1
        
        if count > 0:
            AgendaManager._save_all(data)
        return count

    @staticmethod
    def import_from_planning(target_date: date, points: list, arrival_times: list) -> int:
        """Importe un résultat d'optimisation (liste de points + heures) dans l'agenda."""
        iso = target_date.isocalendar()
        y, w, d_idx = iso[0], iso[1], iso[2] - 1
        if d_idx >= 6: return 0 # Dimanche non géré
        
        jour_fr = JOURS[d_idx]
        data = AgendaManager._load_all()
        k = AgendaManager.week_key(y, w)
        data.setdefault(k, {j: [] for j in JOURS})
        data[k].setdefault(jour_fr, [])
        
        count = 0
        # On ne traite que les clients (pas le départ/arrivée)
        # arrival_times correspond à l'ordre dans result.order
        
        # next_avail_s permet de garantir qu'on ne chevauche pas le client précédent 
        # à cause de l'arrondi (FIX conflict planning->agenda)
        next_avail_s = SLOT_START
        
        for i, pt in enumerate(points):
            # L'heure d'arrivée est en secondes depuis 00:00
            arr_s = arrival_times[i]
            # Arrondi au créneau de 15 min le plus proche
            slot_s = round(arr_s / SLOT_STEP) * SLOT_STEP
            
            # Sécurité : on ne peut pas commencer avant que le précédent ne soit fini
            # (dans le système de créneaux de l'agenda)
            if slot_s < next_avail_s:
                slot_s = next_avail_s
                
            # On s'assure de rester dans les clous 7h-19h
            slot_s = max(SLOT_START, min(slot_s, SLOT_END - SLOT_STEP))
            
            payload = {
                "name":               pt.name or "",
                "address":            pt.address or "",
                "intervention_type":  pt.intervention_type or "Standard_45",
                "notes":              pt.notes or "",
                "service_duration":   pt.service_duration or 2700,
                "time_mode":          "Heure précise",
                "target_time":        arr_s, # HEURE EXACTE SANS AUCUN ARRONDI
                "slot_time":          slot_s,
                "phone":              getattr(pt, "phone", ""),
                "available_weekdays": getattr(pt, "available_weekdays", ""),
            }
            # Éviter doublons sur la même adresse dans la même journée
            c_norm = _norm_addr(pt.address)
            data[k][jour_fr] = [c for c in data[k][jour_fr] if _norm_addr(c.get("address","")) != c_norm]
            data[k][jour_fr].append(payload)
            count += 1
            
            # Calcul du prochain créneau disponible pour le client suivant
            dur = pt.service_duration or 2700
            next_avail_s = math.ceil((slot_s + dur) / SLOT_STEP) * SLOT_STEP
            
        if count > 0:
            AgendaManager._save_all(data)
        return count

    @staticmethod
    def week_key(year: int, week: int) -> str:
        return f"{year}-S{week:02d}"

    @staticmethod
    def parse_key(key: str) -> Tuple[int, int]:
        try:
            y, w = key.split("-S")
            return int(y), int(w)
        except Exception:
            return 0, 0

    @staticmethod
    def get_week(year: int, week: int) -> Dict[str, List[dict]]:
        data  = AgendaManager._load_all()
        saved = data.get(AgendaManager.week_key(year, week), {})
        return {j: list(saved.get(j, [])) for j in JOURS}

    @staticmethod
    def add_client(year: int, week: int, jour: str, client: dict) -> bool:
        data = AgendaManager._load_all()
        k    = AgendaManager.week_key(year, week)
        data.setdefault(k, {j: [] for j in JOURS})
        data[k].setdefault(jour, [])
        existing = {_norm_addr(c.get("address", "")) for c in data[k][jour]}
        if _norm_addr(client.get("address", "")) in existing:
            return False
        data[k][jour].append(client)
        AgendaManager._save_all(data)
        return True

    @staticmethod
    def update_client(year: int, week: int, jour: str,
                      addr_norm: str, updates: dict):
        data = AgendaManager._load_all()
        k    = AgendaManager.week_key(year, week)
        if k not in data or jour not in data[k]:
            return
        for c in data[k][jour]:
            if _norm_addr(c.get("address", "")) == addr_norm:
                c.update(updates)
                break
        AgendaManager._save_all(data)

    @staticmethod
    def remove_client(year: int, week: int, jour: str, addr_norm: str):
        data = AgendaManager._load_all()
        k    = AgendaManager.week_key(year, week)
        if k in data and jour in data[k]:
            data[k][jour] = [
                c for c in data[k][jour]
                if _norm_addr(c.get("address", "")) != addr_norm
            ]
            AgendaManager._save_all(data)

    @staticmethod
    def clear_week(year: int, week: int):
        data = AgendaManager._load_all()
        data.pop(AgendaManager.week_key(year, week), None)
        AgendaManager._save_all(data)

    @staticmethod
    def list_weeks() -> List[str]:
        return sorted(AgendaManager._load_all().keys(), reverse=True)


# ==========================================================
# UTILITAIRES SEND
# ==========================================================
def _week_dates(year: int, week: int) -> List[date]:
    jan4  = date(year, 1, 4)
    start = jan4 + timedelta(weeks=week - 1, days=-jan4.weekday())
    return [start + timedelta(days=i) for i in range(6)]


def _dest_options() -> List[str]:
    return ["📋 En cours"] + RouteManager.list_saves()


def _do_send_one(client: dict, dest: str) -> Tuple[bool, str]:
    label = client.get("name") or client.get("address", "?")[:30]
    c_out = dict(client)
    if c_out.get("slot_time") is not None:
        c_out["time_mode"]   = "Heure précise"
        c_out["target_time"] = int(c_out["slot_time"])
    if dest == "📋 En cours":
        StateManager.add_contact_to_route(c_out)
        StateManager.commit(do_rerun=False)
        return True, f"✅ **{_h(label)}** ajouté à la planification en cours."
    ok, msg = RouteManager.add_client_to_save(dest, c_out)
    return ok, msg


def _do_send_day(clients: List[dict], dest: str) -> Tuple[bool, str]:
    if not clients:
        return False, "Aucun client ce jour."
    if dest == "📋 En cours":
        for c in clients:
            c_out = dict(c)
            if c_out.get("slot_time") is not None:
                c_out["time_mode"]   = "Heure précise"
                c_out["target_time"] = int(c_out["slot_time"])
            StateManager.add_contact_to_route(c_out)
        StateManager.commit(do_rerun=False)
        return True, f"✅ {len(clients)} client(s) ajoutés à la planification en cours."
    added, skipped, msg = RouteManager.add_clients_to_save(dest, clients)
    return (added > 0 or skipped == len(clients)), msg


def _build_slots_map(clients: List[dict]) -> Dict[int, Tuple[dict, bool]]:
    occupied: Dict[int, Tuple[dict, bool]] = {}
    scheduled = sorted(
        (c for c in clients if c.get("slot_time") is not None),
        key=lambda c: c["slot_time"]
    )
    for c in scheduled:
        start_idx = _secs_to_slot(int(c["slot_time"]))
        if start_idx < 0 or start_idx >= N_SLOTS:
            continue
        n_used = max(1, math.ceil(c.get("service_duration", 45 * SPM) / SLOT_STEP))
        for offset in range(n_used):
            s = start_idx + offset
            if s < N_SLOTS and s not in occupied:
                occupied[s] = (c, offset == 0)
    return occupied


def _contact_labels(contacts: list) -> List[str]:
    return (
        ["🔍 Choisir dans le carnet…"]
        + [
            f"{c.get('name', 'Sans nom')} — {c.get('address', '')[:40]}"
            for c in contacts
        ]
    )


def _assign_to_slot(year: int, week: int, jour: str,
                    contact: dict, slot_secs: int):
    """Ajoute ou met à jour un contact sur un créneau précis."""
    c_norm = _norm_addr(contact.get("address", ""))
    payload = {
        "name":               contact.get("name", ""),
        "address":            contact.get("address", ""),
        "intervention_type":  contact.get("intervention_type", DEFAULT_INTERVENTION_TYPE),
        "notes":              contact.get("notes", ""),
        "service_duration":   contact.get("service_duration", 45 * SPM),
        "time_mode":          "Heure précise",
        "target_time":        slot_secs,
        "slot_time":          slot_secs,
        "phone":              contact.get("phone", ""),
        "available_weekdays": contact.get("available_weekdays", ""),
        "_pref_time_mode":    contact.get("time_mode", "Libre"),
        "_pref_time":         contact.get("preferred_time"),
    }
    added = AgendaManager.add_client(year, week, jour, payload)
    if not added:
        AgendaManager.update_client(
            year, week, jour, c_norm,
            {
                "slot_time":          slot_secs,
                "target_time":        slot_secs,
                "time_mode":          "Heure précise",
                "phone":              contact.get("phone", ""),
                "available_weekdays": contact.get("available_weekdays", ""),
                "_pref_time_mode":    contact.get("time_mode", "Libre"),
                "_pref_time":         contact.get("preferred_time"),
            }
        )


# ==========================================================
# EXPORT ICS
# ==========================================================
def _generate_weekly_ics(year: int, week: int, dates: List[date], week_data: Dict[str, List[dict]]) -> str:
    """Génère un fichier .ics pour toute la semaine de l'agenda."""
    from datetime import datetime

    def _esc(s: str) -> str:
        return str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Tournees4Me//FR",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:Agenda S{week:02d}-{year}",
        "X-WR-TIMEZONE:Europe/Paris"
    ]

    now_stamp = datetime.now().strftime("%Y%m%dT%H%M%SZ")

    for jour, d in zip(JOURS, dates):
        clients = week_data.get(jour, [])
        date_str = d.strftime("%Y%m%d")

        for idx, c in enumerate(clients):
            slot_t = c.get("slot_time")
            if slot_t is None:
                continue

            dur     = c.get("service_duration", 45 * SPM)
            start_s = int(slot_t)
            end_s   = start_s + dur

            def to_dt(sec):
                h, m = (sec // 3600) % 24, (sec % 3600) // 60
                return f"{date_str}T{h:02d}{m:02d}00"

            name  = c.get("name") or c.get("address", "?")[:30]
            addr  = c.get("address", "")
            phone = c.get("phone", "")
            itype = c.get("intervention_type", "Standard").split("_")[0]
            notes = c.get("notes", "")

            uid     = f"agenda-{year}-S{week}-{jour}-{idx}@local"
            summary = _esc(f"{name} - {addr}")

            desc_parts = []
            if name:  desc_parts.append(f"Client: {_esc(name)}")
            if phone: desc_parts.append(f"Tél: {_esc(phone)}")
            desc_parts.append(f"Type: {_esc(itype)}")
            if notes: desc_parts.append(f"Notes: {_esc(notes)}")

            lines += [
                "BEGIN:VEVENT",
                f"DTSTAMP:{now_stamp}",
                f"UID:{uid}",
                f"DTSTART:{to_dt(start_s)}",
                f"DTEND:{to_dt(end_s)}",
                f"SUMMARY:{summary}",
                f"DESCRIPTION:{'\\n'.join(desc_parts)}",
                f"LOCATION:{_esc(addr)}",
                "END:VEVENT"
            ]

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


# ==========================================================
# CSS — DIALOG POSITIONNÉ À GAUCHE
# ==========================================================
_SLOT_DIALOG_CSS = """
<style>
/* Backdrop très transparent pour laisser voir la timeline */
div[data-testid="stDialog"] {
    background: rgba(0, 0, 0, 0.08) !important;
    align-items:     flex-start !important;
    justify-content: flex-start !important;
}
/* Panneau dialog : gauche, largeur ~34vw, collé en haut */
div[data-testid="stDialog"] div[role="dialog"] {
    width:      34vw    !important;
    max-width:  430px   !important;
    min-width:  290px   !important;
    margin:     3.5rem 0 0 1.5vw !important;
    border-radius: 10px !important;
    box-shadow: 4px 4px 24px rgba(0,0,0,0.25) !important;
}
/* Couleur du texte pour le bouton Assigner (bouton primaire) */
body div[data-testid="stDialog"] div[role="dialog"] button[data-testid="baseButton-primary"] p,
body div[data-testid="stDialog"] div[role="dialog"] button[data-testid="baseButton-primary"] span,
body div[data-testid="stDialog"] div[role="dialog"] button[data-testid="baseButton-primary"] {
    color: #000000 !important;
    font-weight: bold !important;
}
</style>
"""


# ==========================================================
# DIALOG PAR CRÉNEAU (positionné à gauche)
# ==========================================================
def _render_slot_dialog(year: int, week: int, jour: str, d: date,
                        slot_idx: int, slot_secs: int,
                        client: Optional[dict],
                        day_clients: Optional[List[dict]] = None):
    """Ouvre un dialog de planification pour un créneau précis.
    Créneau occupé  → actions : envoyer / attente / déplacer / libérer.
    Créneau libre   → sélection d'un contact à assigner.
    """
    if not HAS_DIALOG:
        st.warning("⚠️ Streamlit ≥ 1.35 requis pour les fenêtres flottantes.")
        return

    # PRIORITÉ À TARGET_TIME POUR L'AFFICHAGE EXACT
    exact_secs = client.get("target_time", slot_secs) if client else slot_secs
    time_lbl   = _fmt_time(exact_secs)
    dlg_title  = (
        f"🕐 {time_lbl}  —  {jour}"
        if client else
        f"➕ Assigner  —  {jour} {time_lbl}"
    )

    @st.dialog(dlg_title)
    def _dlg():
        # CSS de positionnement gauche (injecté à l'intérieur du dialog)
        st.markdown(_SLOT_DIALOG_CSS, unsafe_allow_html=True)

        def _close():
            st.session_state.pop("ag_open_slot", None)

        # ── Créneau OCCUPÉ ─────────────────────────────────────────────────
        if client:
            c_name  = client.get("name") or client.get("address", "?")[:30]
            dur     = client.get("service_duration", 45 * SPM)
            itype   = client.get("intervention_type", "Standard_45")
            c_norm  = _norm_addr(client.get("address", ""))
            bg, bd  = _itype_style(itype)
            end_s   = exact_secs + dur
            itype_s = itype.split("_")[0]

            # Carte d'identité du créneau
            st.markdown(
                f"<div style='background:{bg};border-left:4px solid {bd};"
                f"border-radius:7px;padding:10px 14px;margin-bottom:8px'>"
                f"<b style='font-size:1.1em'>{_h(c_name)}</b><br>"
                f"<span style='color:#555;font-size:0.92em'>"
                f"🕐 {time_lbl} → {_fmt_time(min(end_s, SLOT_END))}"
                f"  ·  ⏱ {dur // 60} min  ·  {_h(itype_s)}"
                f"</span></div>",
                unsafe_allow_html=True
            )
            phone = client.get("phone", "")
            addr  = client.get("address", "")
            notes = client.get("notes", "")
            if phone: st.caption(f"📞 {phone}")
            if addr:  st.caption(f"📍 {addr[:70]}")
            if notes: st.caption(f"📝 {notes[:90]}")

            st.markdown("---")

            # ── Envoi ──────────────────────────────────────────────────
            dest_opts = _dest_options()
            dest = st.selectbox(
                "Destination",
                dest_opts,
                key=f"sdlg_dest_{jour}_{slot_idx}",
                label_visibility="collapsed",
            )
            c1, c2 = st.columns(2)
            if c1.button("🚀 Envoyer", key=f"sdlg_send_{jour}_{slot_idx}",
                         use_container_width=True, type="primary"):
                ok, msg = _do_send_one(client, dest)
                st.session_state["_agenda_flash"] = (ok, msg)
                _close(); st.rerun()

            if c2.button("⏳ File d'attente", key=f"sdlg_wait_{jour}_{slot_idx}",
                         use_container_width=True):
                from sr_persistence import WaitlistManager
                WaitlistManager.add(client)
                wd = AgendaManager.get_week(year, week)
                wd[jour] = [c for c in wd[jour]
                             if _norm_addr(c.get("address", "")) != c_norm]
                all_d = AgendaManager._load_all()
                all_d[AgendaManager.week_key(year, week)] = wd
                AgendaManager._save_all(all_d)
                st.session_state["_agenda_flash"] = (True, f"⏳ {_h(c_name)} mis en attente.")
                _close(); st.rerun()

            # ── Modifier l'heure (même jour) ───────────────────────────
            st.markdown("**🕐 Modifier l'heure**")
            _creneau_opts = [SLOT_START + k * SLOT_STEP for k in range(N_SLOTS)]
            _cur_idx = min(slot_idx, len(_creneau_opts) - 1)
            same_secs = st.selectbox(
                "Heure (Créneau)", _creneau_opts,
                index=_cur_idx, format_func=_fmt_time,
                key=f"sdlg_sameh_{jour}_{slot_idx}",
                label_visibility="collapsed",
            )
            if st.button("✅ Confirmer l'heure",
                         key=f"sdlg_samemv_{jour}_{slot_idx}",
                         use_container_width=True):
                # On préserve les minutes si on reste sur le même créneau,
                # sinon on aligne sur le nouveau créneau choisi.
                final_t = same_secs
                if (same_secs // SLOT_STEP) == (slot_secs // SLOT_STEP):
                    final_t = exact_secs

                AgendaManager.update_client(
                    year, week, jour, c_norm,
                    {
                        "slot_time":   same_secs,
                        "target_time": final_t,
                        "time_mode":   "Heure précise",
                    }
                )
                st.session_state["_agenda_flash"] = (
                    True,
                    f"🕐 {_h(c_name)} → {jour} à {_fmt_time(final_t)}"
                )
                _close(); st.rerun()

            # ── Déplacer vers un autre jour ────────────────────────────
            st.markdown("**📅 Déplacer vers**")
            mv_c1, mv_c2  = st.columns(2)
            autres_jours  = [j for j in JOURS if j != jour]
            mv_jour = mv_c1.selectbox(
                "Jour", autres_jours,
                key=f"sdlg_mvj_{jour}_{slot_idx}",
                label_visibility="collapsed",
            )
            _def_idx = min(slot_idx, len(_creneau_opts) - 1)
            mv_secs = mv_c2.selectbox(
                "Heure (Créneau)", _creneau_opts,
                index=_def_idx, format_func=_fmt_time,
                key=f"sdlg_mvh_{jour}_{slot_idx}",
                label_visibility="collapsed",
            )
            if st.button("✅ Confirmer le déplacement",
                         key=f"sdlg_mv_{jour}_{slot_idx}",
                         use_container_width=True):
                wd = AgendaManager.get_week(year, week)
                wd[jour] = [c for c in wd[jour]
                             if _norm_addr(c.get("address", "")) != c_norm]
                
                # On préserve les minutes lors du changement de jour
                # si le créneau horaire est identique au créneau d'origine.
                final_mv_t = mv_secs
                if (mv_secs // SLOT_STEP) == (slot_secs // SLOT_STEP):
                    final_mv_t = mv_secs + (exact_secs % SLOT_STEP)

                mv_client = dict(client)
                mv_client.update({
                    "slot_time":   mv_secs,
                    "target_time": final_mv_t,
                    "time_mode":   "Heure précise",
                })
                wd.setdefault(mv_jour, [])
                wd[mv_jour] = [c for c in wd[mv_jour]
                                if _norm_addr(c.get("address", "")) != c_norm]
                wd[mv_jour].append(mv_client)
                all_d = AgendaManager._load_all()
                all_d[AgendaManager.week_key(year, week)] = wd
                AgendaManager._save_all(all_d)
                st.session_state["_agenda_flash"] = (
                    True,
                    f"📅 {_h(c_name)} → {mv_jour} à {_fmt_time(final_mv_t)}"
                )
                _close(); st.rerun()

            st.markdown("---")

            # ── Actions de retrait ─────────────────────────────────────
            act1, act2 = st.columns(2)
            if act1.button("✖ Libérer créneau",
                          key=f"sdlg_free_{jour}_{slot_idx}",
                          use_container_width=True,
                          help="Enlève l'horaire mais garde le client dans la journée"):
                wd = AgendaManager.get_week(year, week)
                # On met à jour pour retirer slot_time
                for c in wd[jour]:
                    if _norm_addr(c.get("address", "")) == c_norm:
                        c["slot_time"] = None
                        break
                all_d = AgendaManager._load_all()
                all_d[AgendaManager.week_key(year, week)] = wd
                AgendaManager._save_all(all_d)
                st.session_state["_agenda_flash"] = (True, f"✖ {_h(c_name)} libéré (sans horaire).")
                _close(); st.rerun()
                
            if act2.button("🗑️ Supprimer",
                          key=f"sdlg_del_{jour}_{slot_idx}",
                          use_container_width=True,
                          help="Retire complètement le client de cette journée"):
                AgendaManager.remove_client(year, week, jour, addr_norm=c_norm)
                st.session_state["_agenda_flash"] = (True, f"🗑️ {_h(c_name)} retiré de l'agenda.")
                _close(); st.rerun()

        # ── Créneau LIBRE ──────────────────────────────────────────────────
        else:
            st.markdown(f"**Créneau disponible — {time_lbl}**")

            # Calcul des créneaux occupés du jour pour filtrage
            _occupied_secs = set()
            for _c in (day_clients or []):
                _st = _c.get("slot_time")
                if _st is not None:
                    _n = max(1, math.ceil(_c.get("service_duration", 45 * SPM) / SLOT_STEP))
                    for _o in range(_n):
                        _occupied_secs.add(int(_st) + _o * SLOT_STEP)

            # Ajout de la pause déjeuner dans les zones occupées
            p_en = st.session_state.get("opt_pause_enabled", False)
            p_s = st.session_state.get("opt_pause_start", 12 * SPH)
            p_e = st.session_state.get("opt_pause_end", 13 * SPH)
            if p_en:
                curr_p = p_s
                while curr_p < p_e:
                    _occupied_secs.add(curr_p)
                    curr_p += SLOT_STEP

            # Filtrage intelligent : au moins 50 minutes de libre
            _all_free = []
            MIN_WINDOW = 50 * SPM
            for s in range(SLOT_START, SLOT_END - MIN_WINDOW + 1, SLOT_STEP):
                # On vérifie si toute la plage [s, s + 50min] est libre
                window_free = True
                for offset in range(0, MIN_WINDOW, SLOT_STEP):
                    if (s + offset) in _occupied_secs:
                        window_free = False
                        break
                if window_free:
                    _all_free.append(s)

            if not _all_free:
                _all_free = [slot_secs]

            # Formattage avec indication Matin/APM
            def _fmt_with_period(s):
                time_str = _fmt_time(s)
                if p_en:
                    return f"{time_str} ({'Matin' if s < p_s else 'Après-midi'})"
                return time_str

            _default_idx = 0
            for _i, _s in enumerate(_all_free):
                if _s >= slot_secs:
                    _default_idx = _i
                    break

            chosen_secs = st.selectbox(
                "Heure de début (plage > 50min)", _all_free,
                index=_default_idx,
                format_func=_fmt_with_period,
                key=f"sdlg_qh_{jour}_{slot_idx}",
                label_visibility="collapsed",
            )
            if p_en:
                st.caption(f"☕ Pause déjeuner : {_fmt_time(p_s)} – {_fmt_time(p_e)}")

            # --- Sélection du contact ---
            reserved = [c for c in (day_clients or []) if c.get("slot_time") is None]
            contacts = st.session_state.get("address_book", [])
            res_labels = [f"⭐ [RÉSERVÉ] {r.get('name','Sans nom')} — {r.get('address','')[:35]}" for r in reserved]
            book_labels = [f"{c.get('name','Sans nom')} — {c.get('address','')[:35]}" for c in contacts]
            all_labels = ["🔍 Choisir un contact…"] + res_labels + book_labels
            
            sel = st.selectbox(
                "Contact à assigner", all_labels,
                key=f"sdlg_pick_{jour}_{slot_idx}",
                label_visibility="collapsed",
            )
            
            if sel != all_labels[0]:
                sel_idx = all_labels.index(sel) - 1
                if sel_idx < len(reserved):
                    contact = reserved[sel_idx]
                else:
                    contact = contacts[sel_idx - len(reserved)]

                addr = contact.get("address", "")
                if not addr or addr == "ADRESSE_INCONNUE":
                    st.error("🚫 **Action bloquée** : Ce client n'a pas d'adresse renseignée.")
                else:
                    dur_m = contact.get("service_duration", 45 * SPM) // 60
                    end_preview = chosen_secs + contact.get("service_duration", 45 * SPM)
                    st.caption(
                        f"⏱ {dur_m} min  ·  "
                        f"{contact.get('intervention_type','').split('_')[0]}  ·  "
                        f"{_fmt_time(chosen_secs)} → {_fmt_time(min(end_preview, SLOT_END))}"
                    )

                    # ── Détection chevauchement ───────────────
                    _overlap_name  = None
                    _adjacent_name = None

                    for _c in (day_clients or []):
                        _st  = _c.get("slot_time")
                        if _st is None: continue
                        _st  = int(_st)
                        _dur = _c.get("service_duration", 45 * SPM)
                        _end = _st + _dur
                        _cn  = _c.get("name") or _c.get("address", "?")[:20]
                        if _st < chosen_secs < _end:
                            _overlap_name = f"{_cn} ({_fmt_time(_st)}→{_fmt_time(_end)})"
                            break
                        if chosen_secs == _end:
                            _adjacent_name = f"{_cn} ({_fmt_time(_st)}→{_fmt_time(_end)})"

                    if _overlap_name:
                        st.warning(f"⚠️ Chevauchement avec **{_overlap_name}**")
                    elif _adjacent_name:
                        st.warning(f"⚠️ Départ immédiatement après **{_adjacent_name}**")

                    if st.button("✅ Assigner ce contact",
                                 key=f"sdlg_assign_{jour}_{slot_idx}",
                                 use_container_width=True, type="primary"):
                        _assign_to_slot(year, week, jour, contact, chosen_secs)
                        _close(); st.rerun()

            if st.button("Annuler", key=f"sdlg_cancel_{jour}_{slot_idx}",
                         use_container_width=True):
                _close(); st.rerun()

    _dlg()


# ==========================================================
# RENDU PRINCIPAL DE L'ONGLET
# ==========================================================
def _render_tab_agenda():
    _col_title, _col_btn = st.columns([6, 1])
    _col_title.markdown("#### 📆 Agenda hebdomadaire")
    _col_btn.link_button("📅 Vacances", "https://www.vacances-scolaires-education.fr", use_container_width=True)

    st.markdown(
        """<style>
        [data-testid="stHorizontalBlock"] [data-testid="stColumn"] button { min-height: 22px !important; height: 22px !important; padding: 0px 4px !important; margin: 0 !important; }
        [data-testid="stHorizontalBlock"] [data-testid="stColumn"] button p { font-size: 15px !important; line-height: 1 !important; white-space: nowrap !important; overflow: hidden !important; text-overflow: ellipsis !important; }
        .flash-msg { animation: flashout 3.5s forwards; }
        @keyframes flashout { 0% { opacity: 1; } 85% { opacity: 1; } 100% { opacity: 0; height: 0; margin: 0; padding: 0; overflow: hidden; } }
        </style>""",
        unsafe_allow_html=True,
    )

    if "_agenda_flash" in st.session_state:
        ok, msg = st.session_state.pop("_agenda_flash")
        bg, border, text = ("rgba(40, 167, 69, 0.15)", "rgba(40, 167, 69, 0.3)", "#155724") if ok else ("rgba(255, 193, 7, 0.15)", "rgba(255, 193, 7, 0.3)", "#856404")
        st.markdown(f"<div class='flash-msg' style='color:{text}; background-color:{bg}; padding: 14px 18px; border-radius: 8px; border: 1px solid {border}; margin-bottom: 16px; font-weight: 500;'>{'✅' if ok else '⚠️'} {msg}</div>", unsafe_allow_html=True)

    today = date.today()
    iso   = today.isocalendar()
    st.session_state.setdefault("agenda_year", iso[0])
    st.session_state.setdefault("agenda_week", iso[1])

    _yy, _ww = iso[0], iso[1]
    _all_opts = []
    # Passé
    _yp, _wp = _yy, _ww
    _past = []
    for _ in range(16):
        _wp -= 1
        if _wp < 1: _yp -= 1; _wp = date(_yp, 12, 28).isocalendar()[1]
        _dr = _week_dates(_yp, _wp)
        _past.append({"y": _yp, "w": _wp, "lbl": f"S{_wp:02d}/{_yp} ({_dr[0].strftime('%d/%m')}→{_dr[-1].strftime('%d/%m')}) 🕓"})
    _all_opts = list(reversed(_past))
    # Courante
    _dr_c = _week_dates(_yy, _ww)
    _jours_fr = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    _mois_fr  = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
    _today_fmt = f"{_jours_fr[today.weekday()]} {today.day} {_mois_fr[today.month-1]} {today.year}"
    _all_opts.append({"y": _yy, "w": _ww, "lbl": f"🗓 S{_ww:02d}/{_yy} ({_dr_c[0].strftime('%d/%m')}→{_dr_c[-1].strftime('%d/%m')}) — Aujourd'hui ({_today_fmt})"})
    # Futur
    _yf, _wf = _yy, _ww
    for _ in range(104):
        _wf += 1
        if _wf > date(_yf, 12, 28).isocalendar()[1]: _yf += 1; _wf = 1
        _dr = _week_dates(_yf, _wf)
        _all_opts.append({"y": _yf, "w": _wf, "lbl": f"S{_wf:02d}/{_yf} ({_dr[0].strftime('%d/%m')}→{_dr[-1].strftime('%d/%m')})"})

    cur_idx = next((i for i, o in enumerate(_all_opts) if o["y"] == st.session_state.agenda_year and o["w"] == st.session_state.agenda_week), 16)
    nav1, nav2, nav3, _ = st.columns([4, 1.6, 1.3, 0.9])
    sel_opt = nav1.selectbox("Semaine", options=_all_opts, index=cur_idx, format_func=lambda x: x["lbl"], label_visibility="collapsed", key="agenda_week_sel")
    year, week = sel_opt["y"], sel_opt["w"]
    st.session_state["agenda_year"], st.session_state["agenda_week"] = year, week
    dates, week_data = _week_dates(year, week), AgendaManager.get_week(year, week)

    _is_current = (year == iso[0] and week == iso[1])
    if nav2.button("🗓 Voir cette semaine", use_container_width=True, disabled=_is_current, key="ag_today_btn_fixed"):
        st.session_state["agenda_year"], st.session_state["agenda_week"] = iso[0], iso[1]
        st.session_state.pop("agenda_week_sel", None); st.rerun()

    dest_global = nav3.selectbox("Destination", _dest_options(), key="ag_dest_global", label_visibility="collapsed")

    # Confirmation de vidage
    _clear_target = st.session_state.get("_ag_confirm_clear_day")
    if _clear_target:
        _cy, _cw, _cj = _clear_target
        st.warning(f"Vider tout le **{_cj}** ?")
        cc1, cc2 = st.columns(2)
        if cc1.button("✅ Oui", type="primary", use_container_width=True, key="ag_clear_yes"):
            w_data = AgendaManager.get_week(_cy, _cw); w_data[_cj] = []
            all_data = AgendaManager._load_all(); all_data[AgendaManager.week_key(_cy, _cw)] = w_data; AgendaManager._save_all(all_data)
            st.session_state.pop("_ag_confirm_clear_day", None); st.session_state["_agenda_flash"] = (True, f"📅 {_cj} vidé."); st.rerun()
        if cc2.button("Annuler", use_container_width=True, key="ag_clear_no"): st.session_state.pop("_ag_confirm_clear_day", None); st.rerun()
        st.stop()

    st.download_button(f"💾 Exporter S{week:02d}/{year} (.ics)", data=_generate_weekly_ics(year, week, dates, week_data), file_name=f"agenda_S{week:02d}_{year}.ics", mime="text/calendar", use_container_width=True)
    st.markdown("<hr style='margin:6px 0'>", unsafe_allow_html=True)

    day_group = st.radio("Groupe", options=["Lundi-Mercredi", "Jeudi-Samedi"], index=0, horizontal=True, label_visibility="collapsed", key="agenda_day_group")
    display_jours, display_dates = (JOURS[:3], dates[:3]) if day_group == "Lundi-Mercredi" else (JOURS[3:6], dates[3:6])

    _combined = ContactManager.get_combined_contacts()
    cols = st.columns(3)

    for col_idx, (jour, d) in enumerate(zip(display_jours, display_dates)):
        clients, slots_map = week_data.get(jour, []), _build_slots_map(week_data.get(jour, []))
        unscheduled = [c for c in clients if c.get("slot_time") is None]
        conflicts = [c for c in clients if c.get("slot_time") is not None and _secs_to_slot(int(c["slot_time"])) in slots_map and slots_map[_secs_to_slot(int(c["slot_time"]))][0] != c]

        with cols[col_idx]:
            hd_l, hd_m, hd_r, hd_x = st.columns([2, 1, 1, 1])
            hd_l.markdown(f"<div style='background:#ebebf2;padding:2px 8px;border-radius:7px;border:1px solid #2a2a4a;line-height:1.35'><b style='color:#de104d'>{jour}</b><br><span style='font-size:0.9em;color:#5510de'>{d.strftime('%d/%m')}</span></div>", unsafe_allow_html=True)
            if hd_m.button("📤", key=f"send_{jour}", help="Envoyer tout", disabled=(not clients)):
                ok, msg = _do_send_day(clients, dest_global)
                st.session_state["_agenda_flash"] = (ok, msg); st.rerun()
            if hd_r.button("🗑️", key=f"clr_{jour}", help="Vider", disabled=(not clients)):
                st.session_state["_ag_confirm_clear_day"] = (year, week, jour); st.rerun()
            if hd_x.button("🔄", key=f"reopt_{jour}", help="Ré-optimiser", disabled=(not clients)):
                _added = 0
                for _c in clients:
                    if _c.get("address"): StateManager.add_point(_c["address"], name=_c.get("name","")); _pt = StateManager.points()[-1]; _pt.notes, _pt.intervention_type, _pt.service_duration, _pt.time_mode, _pt.target_time = _c.get("notes",""), _c.get("intervention_type","St_50"), _c.get("service_duration",2700), _c.get("time_mode","Libre"), _c.get("target_time"); _added += 1
                if _added:
                    w_data = AgendaManager.get_week(year, week); w_data[jour] = []; all_d = AgendaManager._load_all(); all_d[AgendaManager.week_key(year, week)] = w_data; AgendaManager._save_all(all_d)
                    StateManager.request_reoptimize(); StateManager.commit(do_rerun=False); st.session_state["_agenda_flash"] = (True, f"🔄 {jour} envoyé vers planif."); st.rerun()

            if clients:
                st.markdown(f"<div style='font-size:0.85em;color:gray;'>👤 {len(clients)} · ⏱ {sum(c.get('service_duration',2700) for c in clients)//60}min</div>", unsafe_allow_html=True)
                if unscheduled:
                    with st.popover(f"⚠️ {len(unscheduled)} non planifié(s)", use_container_width=True):
                        for u in unscheduled:
                            c1, c2 = st.columns([4, 1]); c1.write(f"• {u.get('name','?')}"); 
                            if c2.button("🗑️", key=f"del_u_{jour}_{_norm_addr(u.get('address',''))}"): AgendaManager.remove_client(year, week, jour, _norm_addr(u.get('address',''))); st.rerun()
                if conflicts:
                    with st.popover(f"🟠 {len(conflicts)} conflits", use_container_width=True):
                        for conf in conflicts:
                            st.write(f"• **{_fmt_time(int(conf['slot_time']))}** : {conf.get('name','?')}"); 
                            if st.button(f"Gérer {conf.get('name','?')[:10]}", key=f"fix_{jour}_{_norm_addr(conf.get('address',''))}"):
                                st.session_state["ag_open_slot"] = {"jour": jour, "d": d.isoformat(), "idx": _secs_to_slot(int(conf["slot_time"])), "secs": int(conf["slot_time"]), "client": conf}; st.rerun()

            for i in range(N_SLOTS):
                slot_secs = _slot_to_secs(i); time_lbl, is_hour = _fmt_time(slot_secs), (slot_secs % SPH == 0)
                if is_hour and i > 0: st.markdown("<hr style='margin:1px 0;opacity:.5'>", unsafe_allow_html=True)
                if i in slots_map:
                    slot_client, is_first = slots_map[i]
                    if is_first:
                        exact_t = slot_client.get("target_time", slot_secs)
                        if st.button(f"{_fmt_time(exact_t)} | {slot_client.get('name','?')[:15]}", key=f"occ_{jour}_{i}", use_container_width=True):
                            st.session_state["ag_open_slot"] = {"jour": jour, "d": d.isoformat(), "idx": i, "secs": slot_secs, "client": slot_client}; st.rerun()
                    else:
                        _, bd2 = _itype_style(slots_map[i][0].get("intervention_type", "Standard_45"))
                        st.markdown(f"<div style='border-left:3px solid {bd2};height:9px;margin-left:5px;opacity:.35'></div>", unsafe_allow_html=True)
                else:
                    if is_hour:
                        tc, bc = st.columns([4, 1]); tc.markdown(f"<span style='color:#e319a3;font-size:0.8em;font-weight:bold'>{time_lbl}</span>", unsafe_allow_html=True)
                        if bc.button("＋", key=f"free_{jour}_{i}", use_container_width=True):
                            st.session_state["ag_open_slot"] = {"jour": jour, "d": d.isoformat(), "idx": i, "secs": slot_secs, "client": None}; st.rerun()
                    else: st.markdown("<div style='border-left:1px solid #333;height:5px;margin-left:7px;opacity:.12'></div>", unsafe_allow_html=True)

    slot_info = st.session_state.get("ag_open_slot")
    if slot_info:
        try: _render_slot_dialog(year, week, slot_info["jour"], date.fromisoformat(slot_info["d"]), slot_info["idx"], slot_info["secs"], slot_info.get("client"), day_clients=week_data.get(slot_info["jour"], []))
        except Exception as _err:
            if "Only one dialog" not in str(_err): raise

    from sr_persistence import WaitlistManager
    week_wait = [w for w in WaitlistManager.load() if w.get("target_week") == week and w.get("target_year") == year]
    if week_wait:
        st.markdown("---"); st.markdown("### ⏳ En attente cette semaine")
        cols_w = st.columns(min(len(week_wait), 4))
        for idx_w, w in enumerate(week_wait):
            with cols_w[idx_w % 4]: st.info(f"📍 **{w['client'].get('name') or w['client']['address'][:20]}**")
