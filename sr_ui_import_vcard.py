"""sr_ui_import_vcard.py — Onglet ☎ Import vCard : enrichissement des téléphones depuis Google Contacts.

Logique :
  - Format vCard attendu :  FN = "Ville* Nom Prénom"  (séparateur `*`)
  - Extraction : ville_hint = avant `*`, name_part = après `* `
  - Matching fuzzy (difflib, stdlib) sur le champ `name` du JSON
  - Tiebreaker : la ville_hint est cherchée dans l'adresse JSON
  - Résultats classés : ✅ sûr (≥0.82) / 🟡 probable (0.45–0.82) / ❌ non trouvé (<0.45)
  - Seul le champ `phone` est modifié ; aucun autre champ n'est touché.

Améliorations v2 :
  - Persistance du fichier VCF en session (pas de re-upload à chaque rerun)
  - Rafraîchissement automatique du JSON après chaque correction appliquée
  - Matching optimisé : normalisation pré-calculée + index TF-IDF léger (tokens)
"""
import streamlit as st
import json
import os
import unicodedata
import re
import hashlib
from difflib import SequenceMatcher
from functools import lru_cache

from sr_persistence import AddressBookManager, ContactManager

_FILE_PATH     = AddressBookManager.SAVE_FILE
_VCF_CACHE_FILE = "vcf_cache.vcf"   # Persistance disque du dernier VCF importé

# ── Seuils de confiance ────────────────────────────────────────────────────────
SCORE_SURE     = 0.82   # seuil nom seul pour entrer dans "sûr" ou "nom+adresse"
SCORE_PROBABLE = 0.45   # 🟡 Match probable → confirmation manuelle
SCORE_ADDR     = 0.40   # seuil ratio adresse normalisée pour valider un match adresse
# Scores affichés par catégorie
DISPLAY_PERFECT   = 1.00   # ✅ Nom + Adresse + Téléphone exacts
DISPLAY_NAME_ADDR = 0.90   # 🟦 Nom + Adresse exacts, téléphone absent ou différent
# < SCORE_PROBABLE → ❌ non trouvé


# ══════════════════════════════════════════════════════════════════════════════
# Utilitaires texte
# ══════════════════════════════════════════════════════════════════════════════

@lru_cache(maxsize=4096)
def _normalize(s: str) -> str:
    """Minuscules + suppression des accents + suppression des caractères non-alpha.
    Mis en cache pour éviter de recalculer plusieurs fois la même chaîne."""
    s = s.strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")   # retire accents
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@lru_cache(maxsize=4096)
def _tokens(s: str) -> frozenset:
    """Retourne les tokens d'une chaîne normalisée (mis en cache)."""
    return frozenset(_normalize(s).split())


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _norm_phone(p: str) -> str:
    """Utilise le nettoyeur centralisé d'AddressBookManager."""
    return AddressBookManager._clean_phone(p)


def _addr_match(vcf_address: str, vcf_ville: str, json_address: str) -> bool:
    """
    Retourne True si l'adresse vCard correspond suffisamment à l'adresse JSON.
    Deux signaux combinés (OR) :
      1. Ratio textuel normalisé ≥ SCORE_ADDR
      2. Ville vCard présente dans adresse JSON + au moins 1 token de rue en commun
    """
    norm_vcf  = _normalize(vcf_address)
    norm_json = _normalize(json_address)

    # Signal 1 : ratio textuel direct
    if norm_vcf and norm_json:
        r = SequenceMatcher(None, norm_vcf, norm_json).ratio()
        if r >= SCORE_ADDR:
            return True

    # Signal 2 : ville connue + token de rue commun
    if vcf_ville and _normalize(vcf_ville) in norm_json:
        # Cherche si au moins un token numérique ou de rue apparaît dans les deux
        vcf_tok  = set(_normalize(vcf_address).split())
        json_tok = set(norm_json.split())
        # Tokens significatifs : longueur ≥ 3, pas juste la ville
        shared = {t for t in vcf_tok & json_tok if len(t) >= 3
                  and t not in _normalize(vcf_ville).split()}
        if shared:
            return True
        # Si pas de token d'adresse dans la vCard mais ville présente → match partiel
        if not vcf_address.strip():
            return True  # adresse vCard vide : on se fie à la ville seule

    return False


def _score(vcf_name_part: str, vcf_ville: str, json_name: str, json_address: str) -> float:
    """
    Score composite [0‥1] entre un contact vCard et une entrée JSON.
    Version optimisée : early-exit si le score de base est très faible.
    """
    norm_vcf  = _normalize(vcf_name_part)
    norm_json = _normalize(json_name)

    base = SequenceMatcher(None, norm_vcf, norm_json).ratio()

    # Early-exit : si même avec les bonus max on ne dépasse pas SCORE_PROBABLE, on zappe
    if base + 0.25 < SCORE_PROBABLE:
        return base

    # Bonus containment : utile si name_part = "Dupont" et json_name = "Marie Dupont"
    vcf_tok  = _tokens(vcf_name_part)
    json_tok = _tokens(json_name)
    if vcf_tok and vcf_tok.issubset(json_tok):
        base = min(1.0, base + 0.15)
    elif vcf_tok and json_tok and vcf_tok & json_tok:
        overlap = len(vcf_tok & json_tok) / max(len(vcf_tok), 1)
        base = min(1.0, base + 0.08 * overlap)

    # Bonus ville
    if vcf_ville and _normalize(vcf_ville) in _normalize(json_address):
        base = min(1.0, base + 0.10)

    return base


# ══════════════════════════════════════════════════════════════════════════════
# Parser vCard
# ══════════════════════════════════════════════════════════════════════════════

def _parse_vcf(content: str) -> list[dict]:
    """
    Parse un fichier .vcf avec gestion d'erreurs par bloc.
    """
    contacts = []
    try:
        blocks = re.split(r"BEGIN:VCARD", content, flags=re.IGNORECASE)
        for block in blocks:
            if "END:VCARD" not in block.upper():
                continue

            def _field(key_pattern: str) -> str:
                m = re.search(key_pattern + r"[^:]*:(.*)", block, re.IGNORECASE)
                return m.group(1).strip() if m else ""

            fn_raw     = _field(r"FN")
            phone_raw  = _field(r"TEL")
            adr_raw    = _field(r"ADR")
            categories = _field(r"CATEGORIES")

            if not fn_raw:
                continue

            phone = _norm_phone(phone_raw)
            adr_parts = [p.strip() for p in adr_raw.split(";") if p.strip()]
            vcf_address = " ".join(adr_parts)

            if "*" in fn_raw:
                parts = fn_raw.split("*", 1)
                ville_hint = parts[0].strip()
                name_part  = parts[1].strip()
            else:
                ville_hint = ""
                name_part  = fn_raw.strip()

            contacts.append({
                "fn_raw":      fn_raw,
                "name_part":   name_part,
                "ville_hint":  ville_hint,
                "phone":       phone,
                "vcf_address": vcf_address,
                "categories":  categories,
            })
    except Exception as e:
        st.error(f"Erreur lors de la lecture du fichier VCF : {e}")
        return []

    has_clients_tag = any("clients" in c["categories"].lower() for c in contacts)
    if has_clients_tag:
        contacts = [c for c in contacts if "clients" in c["categories"].lower()]

    return contacts


# ══════════════════════════════════════════════════════════════════════════════
# Index inversé pour le matching rapide
# ══════════════════════════════════════════════════════════════════════════════

def _build_index(json_data: list[dict]) -> dict[str, list[int]]:
    """
    Construit un index inversé token → [indices JSON].
    Permet de limiter les comparaisons SequenceMatcher aux candidats pertinents.
    """
    index = {}
    for idx, entry in enumerate(json_data):
        for tok in _tokens(entry.get("name", "")):
            if len(tok) >= 3:  # ignore les tokens trop courts
                index.setdefault(tok, []).append(idx)
    return index


def _candidate_indices(vcf_name_part: str, index: dict, json_data: list[dict], top_k: int = 30) -> list[int]:
    """
    Retourne les indices JSON les plus pertinents via l'index inversé.
    Trie par nombre de tokens communs (vote) avant le SequenceMatcher coûteux.
    Si aucun candidat, retourne tous les indices (fallback).
    """
    vcf_tok = _tokens(vcf_name_part)
    votes = {}
    for tok in vcf_tok:
        if len(tok) >= 3:
            for idx in index.get(tok, []):
                votes[idx] = votes.get(idx, 0) + 1

    if not votes:
        # Fallback : pas de token commun → on compare quand même tout
        return list(range(len(json_data)))

    # Trie par votes décroissants, prend les top_k
    sorted_candidates = sorted(votes.keys(), key=lambda i: -votes[i])
    return sorted_candidates[:top_k]


# ══════════════════════════════════════════════════════════════════════════════
# Moteur de matching
# ══════════════════════════════════════════════════════════════════════════════

def _classify_one(vcf: dict, best_idx: int, best_score: float, json_data: list[dict]) -> tuple[str, float]:
    """
    Détermine la catégorie et le score affiché pour un couple (vCard, JSON entry).
    Retourne (match_type, display_score).
    """
    if best_idx is None or best_score < SCORE_PROBABLE:
        return "not_found", best_score

    if best_score >= SCORE_SURE:
        best_entry = json_data[best_idx]
        addr_ok    = _addr_match(vcf["vcf_address"], vcf["ville_hint"],
                                  best_entry.get("address", ""))
        phone_vcf  = _norm_phone(vcf["phone"])
        phone_json = _norm_phone(best_entry.get("phone", ""))
        phone_ok   = bool(phone_vcf) and bool(phone_json) and phone_vcf == phone_json

        if addr_ok and phone_ok:
            return "perfect",   DISPLAY_PERFECT
        if addr_ok:
            return "name_addr", DISPLAY_NAME_ADDR
        # Nom sûr mais adresse non confirmée → probable
        return "probable", best_score

    return "probable", best_score


def _match_all(vcf_contacts: list[dict], json_data: list[dict]) -> dict:
    """
    Pour chaque contact vCard, trouve le meilleur match dans json_data.

    Étape 1 — Scoring indépendant de chaque vCard contact.
    Étape 2 — Déduplication : un entry JSON ne peut appartenir qu'à UN SEUL
              bloc (le contact vCard avec le meilleur score le "réclame").
              Les concurrents éjectés sont réaffectés à leur prochain candidat
              disponible et reclassifiés.

    Catégories :
      - "perfect"   : Nom ≈ exact + Adresse ≈ exacte + Téléphone identique  → 100 %
      - "name_addr" : Nom ≈ exact + Adresse ≈ exacte + tél absent/différent → 90 %
      - "probable"  : correspondance floue sur le nom seul                   → score réel
      - "not_found" : score < SCORE_PROBABLE
    """
    index = _build_index(json_data)

    # ── Étape 1 : scoring complet ─────────────────────────────────────────────
    raw_items = []
    for vcf in vcf_contacts:
        candidate_idxs = _candidate_indices(vcf["name_part"], index, json_data, top_k=200)

        scored = []
        for idx in candidate_idxs:
            entry = json_data[idx]
            s = _score(vcf["name_part"], vcf["ville_hint"],
                       entry.get("name", ""), entry.get("address", ""))
            scored.append((s, idx))
        scored.sort(key=lambda x: -x[0])

        best_score, best_idx = scored[0] if scored else (0, None)

        # Liste complète de candidats (top 15, score > 0.10)
        all_cands = [
            {"score": s, "idx": i,
             "name": json_data[i].get("name", ""),
             "address": json_data[i].get("address", ""),
             "phone_existing": json_data[i].get("phone", "")}
            for s, i in scored[:15] if s > 0.10
        ]

        match_type, display_score = _classify_one(vcf, best_idx, best_score, json_data)

        raw_items.append({
            "vcf":           vcf,
            "best_idx":      best_idx,
            "score":         best_score,
            "display_score": display_score,
            "match_type":    match_type,
            "all_cands":     all_cands,   # liste complète non filtrée
        })

    # ── Étape 2 : déduplication par best_idx ─────────────────────────────────
    # Trier par priorité de catégorie puis display_score décroissant.
    # Le premier à "réclamer" un JSON idx le garde ; les suivants cherchent
    # leur prochain candidat disponible.
    PRIO = {"perfect": 0, "name_addr": 1, "probable": 2, "not_found": 3}
    raw_items.sort(key=lambda x: (PRIO[x["match_type"]], -x["display_score"]))

    claimed: set[int] = set()   # JSON indices déjà attribués

    perfect, name_addr, probable, not_found = [], [], [], []

    def _add(item: dict) -> None:
        """Trie l'item finalisé dans le bon bucket."""
        buckets = {"perfect": perfect, "name_addr": name_addr,
                   "probable": probable, "not_found": not_found}
        # Candidats affichés = top 15 HORS les JSON entries already claimed
        # par un autre bloc (pour éviter la pollution du selectbox)
        item["candidates"] = [
            c for c in item["all_cands"]
            if c["idx"] not in claimed or c["idx"] == item["best_idx"]
        ][:15]
        del item["all_cands"]
        buckets[item["match_type"]].append(item)

    for item in raw_items:
        best_idx = item["best_idx"]

        if best_idx is None or best_idx not in claimed:
            # Candidat libre → on le réclame
            if best_idx is not None:
                claimed.add(best_idx)
            _add(item)
        else:
            # best_idx déjà pris → chercher le prochain candidat libre
            next_idx, next_score = None, 0.0
            for c in item["all_cands"]:
                if c["idx"] not in claimed:
                    next_idx, next_score = c["idx"], c["score"]
                    break

            item["best_idx"]  = next_idx
            item["score"]     = next_score
            new_type, new_disp = _classify_one(item["vcf"], next_idx, next_score, json_data)
            item["match_type"]    = new_type
            item["display_score"] = new_disp

            if next_idx is not None:
                claimed.add(next_idx)
            _add(item)

    # Tri final de chaque bucket par display_score décroissant
    for lst in (perfect, name_addr, probable):
        lst.sort(key=lambda x: -x["display_score"])

    return {
        "perfect":   perfect,
        "name_addr": name_addr,
        "probable":  probable,
        "not_found": not_found,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Persistance JSON
# ══════════════════════════════════════════════════════════════════════════════

def _load_raw(file_path: str) -> list:
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_raw(data: list, file_path: str) -> bool:
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        st.error(f"Erreur écriture : {e}")
        return False


def _apply_one(json_idx: int, new_phone: str, fn_raw: str = "") -> bool:
    """Sauvegarde un seul numéro de téléphone dans le JSON et rafraîchit la session."""
    try:
        data = _load_raw(_FILE_PATH)
        if 0 <= json_idx < len(data):
            data[json_idx]["phone"] = new_phone
            if _save_raw(data, _FILE_PATH):
                # ── Rafraîchir le JSON en session ────────────────────────────
                st.session_state["vcf_json_data"] = data
                # Forcer le recalcul du matching au prochain rerun
                st.session_state.pop("vcf_results", None)
                # Marquer ce contact comme appliqué (pour le retirer de la liste)
                if fn_raw:
                    applied = st.session_state.setdefault("vcf_applied_set", set())
                    applied.add(fn_raw)
                # Notifier les autres modules
                AddressBookManager.load_from_file()
                ContactManager.invalidate_index()
                st.cache_data.clear()
                return True
    except Exception as e:
        st.error(f"Erreur sauvegarde : {e}")
    return False


# ══════════════════════════════════════════════════════════════════════════════
# Session state — helpers
# ══════════════════════════════════════════════════════════════════════════════

def _get_vcf_content_hash(content: str) -> str:
    return hashlib.md5(content.encode()).hexdigest()


def _save_vcf_to_disk(content: str) -> None:
    """Persiste le contenu VCF sur disque pour survivre aux redémarrages."""
    try:
        tmp = _VCF_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, _VCF_CACHE_FILE)
    except Exception as e:
        st.warning(f"Impossible de sauvegarder le cache VCF sur disque : {e}")


def _load_vcf_from_disk() -> str | None:
    """Charge le VCF depuis le cache disque si disponible."""
    if not os.path.exists(_VCF_CACHE_FILE):
        return None
    try:
        with open(_VCF_CACHE_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def _get_json_mtime() -> float:
    """Retourne le timestamp de modification du fichier JSON."""
    try:
        return os.path.getmtime(_FILE_PATH)
    except OSError:
        return 0.0


def _load_json_cached() -> list:
    """
    Charge le JSON depuis le disque uniquement si le fichier a été modifié
    depuis le dernier chargement (comparaison par mtime).
    """
    current_mtime = _get_json_mtime()
    if (
        "vcf_json_data" not in st.session_state
        or st.session_state.get("vcf_json_mtime", 0) != current_mtime
    ):
        data = _load_raw(_FILE_PATH)
        st.session_state["vcf_json_data"]  = data
        st.session_state["vcf_json_mtime"] = current_mtime
        # Invalider les résultats si le JSON a changé hors de l'onglet
        st.session_state.pop("vcf_results", None)
    return st.session_state["vcf_json_data"]


# ══════════════════════════════════════════════════════════════════════════════
# UI helpers
# ══════════════════════════════════════════════════════════════════════════════

def _render_section_generic(
    items: list,
    json_data: list,
    section_key: str,
    title: str,
    caption: str,
    expander_expanded: bool = False,
    show_apply_button: bool = False,   # True = bouton direct (parfait), False = checkbox
) -> None:
    """
    Rendu générique paginé pour une catégorie de matchs.
    `show_apply_button` :
      - True  → bouton 💾 Appliquer direct (section "parfait" : modification possible)
      - False → checkbox ✔ Confirmer + checkbox 💾 Appliquer (sections inférieures)
    """
    applied_set = st.session_state.get("vcf_applied_set", set())
    filtered_all = [it for it in items if it["vcf"]["fn_raw"] not in applied_set]

    st.markdown(title)
    st.caption(caption)

    col_q, col_pg = st.columns([3, 1])
    search = col_q.text_input("🔍 Rechercher", placeholder="Nom…",
                               key=f"{section_key}_search", label_visibility="collapsed")

    filtered = filtered_all
    if search.strip():
        q = search.strip().lower()
        filtered = [
            it for it in filtered_all
            if q in it["vcf"]["name_part"].lower()
            or q in (it["vcf"]["ville_hint"] or "").lower()
            or any(q in c["name"].lower() for c in it["candidates"])
        ]

    ITEMS_PER_PAGE = 5
    total   = len(filtered)
    n_pages = max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    page    = col_pg.number_input(f"Page / {n_pages}", min_value=1, max_value=n_pages,
                                   value=1, key=f"{section_key}_page",
                                   label_visibility="collapsed")
    start = (page - 1) * ITEMS_PER_PAGE
    end   = start + ITEMS_PER_PAGE

    st.caption(f"{total} résultat(s)")
    st.markdown("---")

    for item in filtered[start:end]:
        vcf        = item["vcf"]
        candidates = item["candidates"]
        score      = item["display_score"]

        key_ck     = f"{section_key}_ck_{vcf['fn_raw']}"
        key_apply  = f"{section_key}_apply_{vcf['fn_raw']}"
        key_sel    = f"{section_key}_sel_{vcf['fn_raw']}"
        key_ph     = f"{section_key}_ph_{vcf['fn_raw']}"
        reset_flag = f"{section_key}_reset_{vcf['fn_raw']}"

        if st.session_state.pop(reset_flag, False):
            st.session_state[key_apply] = False
            st.session_state.pop(key_ck, None)

        with st.expander(
            f"**{vcf['name_part']}**  ·  {vcf['ville_hint'] or '?'}  ·  📞 {vcf['phone']}"
            f"  ·  score : {score:.0%}",
            expanded=expander_expanded,
        ):
            if not candidates:
                st.warning("Aucun candidat trouvé dans le carnet pour ce contact.")
                continue

            cand_labels = [
                f"{c['name']}  —  {c['address'][:50]}  [{c['score']:.0%}]"
                for c in candidates
            ]
            selected_label = st.selectbox(
                "Client cible :", cand_labels, key=key_sel
            )
            chosen_cand = candidates[cand_labels.index(selected_label)]

            existing = chosen_cand["phone_existing"] or "—"
            st.caption(f"📞 Téléphone actuel dans le carnet : **{existing}**")

            if show_apply_button:
                # Section "parfait" : téléphone identique → bouton direct
                col_ph2, col_btn = st.columns([3, 1.5])
                new_phone = col_ph2.text_input(
                    "Téléphone à appliquer", value=vcf["phone"], key=key_ph
                )
                if col_btn.button("💾 Appliquer", key=key_apply, use_container_width=True):
                    if new_phone and _apply_one(chosen_cand["idx"], new_phone, fn_raw=vcf["fn_raw"]):
                        st.success(f"✅ **{chosen_cand['name']}** mis à jour.")
                        st.session_state[reset_flag] = True
                        st.rerun()
            else:
                # Sections inférieures : double confirmation obligatoire
                col_ck2, col_apply2, col_ph2 = st.columns([1.3, 1.5, 2.5])
                confirmed = col_ck2.checkbox("✔ Confirmer ce match", key=key_ck)
                apply_now = col_apply2.checkbox(
                    "💾 Appliquer au carnet", key=key_apply,
                    disabled=not confirmed,
                )
                new_phone = col_ph2.text_input(
                    "Téléphone à appliquer", value=vcf["phone"], key=key_ph
                )
                if confirmed and apply_now and new_phone:
                    if _apply_one(chosen_cand["idx"], new_phone, fn_raw=vcf["fn_raw"]):
                        st.success(f"✅ **{chosen_cand['name']}** mis à jour.")
                        st.session_state[reset_flag] = True
                        st.rerun()


def _render_not_found_section(not_found: list) -> None:
    """Affiche les contacts sans match suffisant (tableau informatif, sans interaction)."""
    applied_set = st.session_state.get("vcf_applied_set", set())
    items = [it for it in not_found if it["vcf"]["fn_raw"] not in applied_set]
    if not items:
        return
    st.markdown("### ❌ Non trouvés")
    st.caption(
        f"{len(items)} contact(s) sans correspondance suffisante dans le carnet "
        f"(score < {SCORE_PROBABLE:.0%}). Aucune modification ne sera appliquée."
    )
    rows = [
        {
            "Contact vCard": f"{it['vcf']['name_part']} ({it['vcf']['ville_hint'] or '?'})",
            "Téléphone vCard": it["vcf"]["phone"],
            "Meilleur score": f"{it['display_score']:.0%}",
        }
        for it in items
    ]
    st.dataframe(rows, use_container_width=True)


def _render_tab_import_vcard():
    st.markdown("#### ☎ Import vCard — Enrichissement des téléphones depuis Google Contacts")
    st.caption(
        "Importe un fichier `.vcf` exporté de Google Contacts (format `Ville* Nom`), "
        "détecte les correspondances avec les clients du carnet, et met à jour uniquement "
        "le champ **téléphone** — sans toucher aux autres données."
    )

    # ── Chargement du JSON (avec cache par mtime) ─────────────────────────────
    if not os.path.exists(_FILE_PATH):
        st.error(f"Fichier `{_FILE_PATH}` introuvable.")
        return

    try:
        json_data = _load_json_cached()
    except Exception as e:
        st.error(f"Impossible de lire le carnet : {e}")
        return

    # ── Upload vCard (persisté en session ET sur disque) ─────────────────────
    uploaded = st.file_uploader(
        "Choisir un fichier vCard (.vcf)",
        type=["vcf", "vcard"],
        help="Export Google Contacts → tous les contacts ou groupe « Clients »",
    )

    # ── Persistance du contenu VCF entre les reruns ──────────────────────────
    if uploaded is not None:
        raw_bytes = uploaded.read()
        try:
            content = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            content = raw_bytes.decode("latin-1")

        new_hash = _get_vcf_content_hash(content)

        # Stocker uniquement si le fichier a changé
        if st.session_state.get("vcf_content_hash") != new_hash:
            st.session_state["vcf_content"]      = content
            st.session_state["vcf_content_hash"] = new_hash
            # Forcer le recalcul du matching car le VCF a changé
            st.session_state.pop("vcf_results", None)
            # ── Persistance disque : survit aux redémarrages ──────────────
            _save_vcf_to_disk(content)

    # Utiliser le contenu en session si disponible (même sans re-upload)
    content = st.session_state.get("vcf_content")

    # ── Fallback : charger depuis le cache disque si session vide ────────────
    if not content:
        disk_content = _load_vcf_from_disk()
        if disk_content:
            content = disk_content
            new_hash = _get_vcf_content_hash(content)
            st.session_state["vcf_content"]      = content
            st.session_state["vcf_content_hash"] = new_hash

    if not content:
        st.info("⬆️ Déposez votre fichier `.vcf` pour commencer.")
        return

    # ── Parse VCF (mis en cache par hash du contenu) ─────────────────────────
    vcf_hash = st.session_state.get("vcf_content_hash", "")
    cache_key = f"vcf_contacts_{vcf_hash}"
    if cache_key not in st.session_state:
        vcf_contacts = _parse_vcf(content)
        st.session_state[cache_key] = vcf_contacts
    else:
        vcf_contacts = st.session_state[cache_key]

    if not vcf_contacts:
        st.warning("Aucun contact exploitable trouvé dans le fichier vCard.")
        return

    st.success(f"✅ {len(vcf_contacts)} contact(s) chargé(s) depuis la vCard.")

    # ── Matching (mis en cache, recalculé si VCF ou JSON ont changé) ─────────
    results_key = "vcf_results"
    if results_key not in st.session_state:
        with st.spinner("⏳ Calcul des correspondances en cours…"):
            results = _match_all(vcf_contacts, json_data)
        st.session_state[results_key] = results
    else:
        results = st.session_state[results_key]

    all_contacts = (
        results["perfect"] + results["name_addr"] +
        results["probable"] + results["not_found"]
    )
    applied_set  = st.session_state.get("vcf_applied_set", set())
    n_total      = len(all_contacts)
    n_applied    = len([it for it in all_contacts if it["vcf"]["fn_raw"] in applied_set])

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("📋 Total vCard",       n_total)
    c2.metric("✅ Déjà sync.",          len(results["perfect"]))
    c3.metric("🟦 Nom + Adresse",     len(results["name_addr"]))
    c4.metric("🟡 À confirmer",       len(results["probable"]))
    c5.metric("✅ Appliqués",          n_applied)

    if st.button("🔄 Recalculer les correspondances",
                 help="Utile après une correction manuelle externe"):
        st.session_state.pop("vcf_results", None)
        st.rerun()

    st.markdown("---")

    # ── ✅ Déjà synchronisés ──────────────────────────────────────────────────
    if results["perfect"]:
        _render_section_generic(
            results["perfect"], json_data,
            section_key="perf",
            title="### ✅ Déjà synchronisés — Nom + Adresse + Téléphone identiques",
            caption=(
                f"{len(results['perfect'])} contact(s) dont le carnet est déjà à jour. "
                "Aucune action requise. Modifiable si besoin (ex : numéro mis à jour dans Google Contacts)."
            ),
            expander_expanded=False,
            show_apply_button=True,
        )
        st.markdown("---")

    # ── 🟦 Match Nom + Adresse ────────────────────────────────────────────────
    if results["name_addr"]:
        _render_section_generic(
            results["name_addr"], json_data,
            section_key="nadr",
            title="### 🟦 Match Nom + Adresse — téléphone à compléter",
            caption=(
                f"{len(results['name_addr'])} contact(s) dont le nom et l'adresse correspondent "
                "mais le téléphone est absent ou différent dans le carnet. "
                "Confirmez puis appliquez."
            ),
            expander_expanded=False,
            show_apply_button=False,
        )
        st.markdown("---")

    # ── 🟡 À confirmer ────────────────────────────────────────────────────────
    if results["probable"]:
        _render_section_generic(
            results["probable"], json_data,
            section_key="prob",
            title="### 🟡 À confirmer — correspondance floue",
            caption=(
                f"{len(results['probable'])} contact(s) avec une correspondance partielle "
                f"(score entre {SCORE_PROBABLE:.0%} et {SCORE_SURE:.0%}). "
                "Choisissez le bon client dans la liste, confirmez, puis appliquez."
            ),
            expander_expanded=False,
            show_apply_button=False,
        )
        st.markdown("---")

    # ── ❌ Non trouvés ────────────────────────────────────────────────────────
    _render_not_found_section(results["not_found"])
