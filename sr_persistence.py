import streamlit as st
import re
import json
import os
import csv
import io
import dataclasses
from datetime import datetime
from typing import List, Tuple, Optional, Dict

from sr_core import (
    Config, DeliveryPoint, Contact, RouteConfig, RouteResult,
    _norm_addr, _cap_cache, _sort_key_date, _parse_fr_date,
    INTERVENTION_TYPES, DEFAULT_INTERVENTION_TYPE, SPM, _RE_CITY_EXTRACTOR,
    _RE_SAFE_NAME, WORK_START
)

# ==========================================================
# HISTORY MANAGER
# ==========================================================
class HistoryManager:
    """Redirige les dates de passage vers le carnet d'adresses (source unique de vérité).
    
    Le fichier historique_clients.json est abandonné.
    Les visit_dates sont maintenant stockées directement dans chaque entrée du carnet.
    Ce wrapper maintient la compatibilité des appels existants.
    """

    @staticmethod
    def save_to_file():
        """Délègue la sauvegarde au carnet d'adresses."""
        AddressBookManager.save_to_file(sync_import_csv=True)

    @staticmethod
    def load_from_file():
        """No-op : l'historique est dans le carnet. La migration est gérée par AddressBookManager."""
        return 0

    @staticmethod
    def add_visit(p: DeliveryPoint, date_str: str):
        """Ajoute une date de passage dans l'entrée du carnet correspondante."""
        ss = st.session_state
        book = ss.get("address_book", [])
        hist_key = (_norm_addr(p.name), _norm_addr(p.address))
        for contact in book:
            if (_norm_addr(contact.get("name", "")), _norm_addr(contact.get("address", ""))) == hist_key:
                # FIX #4 — On ne met à jour intervention_type/service_duration que si le point de route
                # porte une valeur non-vide. Les notes du carnet (plus récentes et plus complètes)
                # ne sont JAMAIS écrasées par les notes de la route (qui datent du moment de l'ajout).
                if p.intervention_type:
                    contact["intervention_type"] = p.intervention_type
                if p.service_duration:
                    contact["service_duration"] = p.service_duration
                # contact["notes"] intentionnellement NON mis à jour :
                # les notes du carnet sont la source de vérité (éditées manuellement).
                dates = contact.setdefault("visit_dates", [])
                if date_str not in dates:
                    dates.append(date_str)
                AddressBookManager.set_dirty()
                AddressBookManager.save_to_file(sync_import_csv=True)
                ContactManager.invalidate_index()
                return
        # Client absent du carnet → on ne crée pas d'entrée automatiquement
        # (le carnet est la source de vérité et est maintenu manuellement)

    @staticmethod
    def delete_entry(address: str, name: str = "", only_history: bool = False):
        """Supprime le contact du carnet (et donc ses dates de passage)."""
        addr_key = _norm_addr(address)
        name_key = _norm_addr(name)
        if not only_history:
            ContactManager.delete_contact_by_key(addr_key, name_key)

# ==========================================================
# CONTACT MANAGER
# ==========================================================
class ContactManager:
    """Gère le carnet d'adresses et ses index de recherche."""
    
    @staticmethod
    def build_index():
        book = st.session_state.address_book
        name_idx, addr_idx, composite_idx = {}, {}, {}
        full_list = []
        for i, c in enumerate(book):
            nl = _norm_addr(c["name"])
            al = _norm_addr(c["address"])
            # first-wins : on ne remplace pas une entrée existante.
            # Les doublons (homonymes, adresses partagées) sont conservés dans
            # composite_idx et full_list ; name_idx/addr_idx sont des index
            # "meilleure correspondance unique" destinés aux look-ups rapides.
            if nl not in name_idx:
                name_idx[nl] = (i, c)
            if al not in addr_idx:
                addr_idx[al] = (i, c)
            composite_idx[(nl, al)] = (i, c)
            full_list.append((i, nl, al, c))
        st.session_state["_contact_name_idx"]      = name_idx
        st.session_state["_contact_addr_idx"]      = addr_idx
        st.session_state["_contact_composite_idx"] = composite_idx
        st.session_state["_contact_index"]         = full_list

    @staticmethod
    def invalidate_index():
        for k in ("_contact_index", "_contact_name_idx",
                  "_contact_addr_idx", "_contact_composite_idx"):
            st.session_state.pop(k, None)
        st.session_state.pop("_csv_cache", None)

    @staticmethod
    def get_index():
        if "_contact_index" not in st.session_state: ContactManager.build_index()
        return st.session_state["_contact_index"]

    @staticmethod
    def get_index_by_addr():
        if "_contact_addr_idx" not in st.session_state: ContactManager.build_index()
        return st.session_state["_contact_addr_idx"]

    @staticmethod
    def get_name_by_addr(address: str) -> str:
        """Récupère le nom d'un client par son adresse dans le carnet."""
        if not address: return ""
        idx = ContactManager.get_index_by_addr()
        match = idx.get(_norm_addr(address))
        return match[1].get("name", "") if match else ""

    @staticmethod
    def add_contact(contact: Contact):
        st.session_state.address_book.append(dataclasses.asdict(contact))
        ContactManager.invalidate_index()
        AddressBookManager.set_dirty()
        AddressBookManager.save_to_file(sync_import_csv=True)

    @staticmethod
    def update_contact(index: int, **kwargs):
        """Met à jour un contact — crée les champs manquants si nécessaire."""
        if 0 <= index < len(st.session_state.address_book):
            contact = st.session_state.address_book[index]
            # Sauvegarde des anciennes clés pour la synchro file d'attente
            old_name = contact.get("name", "")
            old_addr = contact.get("address", "")
            
            # Synchronisation automatique de intervention_type si service_duration change
            if "service_duration" in kwargs and "intervention_type" not in kwargs:
                new_dur = kwargs["service_duration"]
                # Chercher une correspondance exacte dans INTERVENTION_TYPES
                from sr_core import INTERVENTION_TYPES, SPM
                found = False
                for k, v in INTERVENTION_TYPES.items():
                    if v == new_dur:
                        kwargs["intervention_type"] = k
                        found = True
                        break
                if not found:
                    # Générer un type dynamique si pas de correspondance (ex: Standard_70)
                    kwargs["intervention_type"] = f"Standard_{new_dur // SPM}"

            for k, v in kwargs.items():
                contact[k] = v
            
            ContactManager.invalidate_index()
            AddressBookManager.set_dirty()
            AddressBookManager.save_to_file(sync_import_csv=True)
            
            # Synchronisation automatique de la file d'attente
            WaitlistManager.sync_contact_update(old_name, old_addr, contact)

    @staticmethod
    def find_duplicate(name: str, address: str, exclude_index: int = -1, exact_match: bool = False) -> Optional[int]:
        """Cherche un contact identique (nom+adresse) dans le carnet.
        Si exact_match=True, compare les chaînes brutes (strip).
        Sinon, utilise la normalisation standard.
        """
        if exact_match:
            n_target, a_target = name.strip(), address.strip()
            for i, c in enumerate(st.session_state.get("address_book", [])):
                if i == exclude_index: continue
                if c.get("name", "").strip() == n_target and c.get("address", "").strip() == a_target:
                    return i
        else:
            norm_n, norm_a = _norm_addr(name), _norm_addr(address)
            for i, c in enumerate(st.session_state.get("address_book", [])):
                if i == exclude_index: continue
                if _norm_addr(c.get("name", "")) == norm_n and _norm_addr(c.get("address", "")) == norm_a:
                    return i
        return None

    @staticmethod
    def merge_contacts(target_idx: int, source_idx: int):
        """Fusionne deux contacts (le source est supprimé après fusion).
        
        FIX #4 : précision sur la sécurité de l'index après pop.
        - Si target_idx < source_idx : target_idx est résolu en objet avant le pop,
          donc la fusion est correcte. Le pop décale les indices > source_idx,
          mais target est déjà une référence directe — aucun problème.
        - Si target_idx > source_idx : le pop décalerait target_idx, mais on n'utilise
          plus target_idx après le pop (on travaille sur la référence objet `t`).
        Dans les deux cas : appeler ContactManager.invalidate_index() avant toute
        utilisation ultérieure des index (ce qui est fait ici).
        """
        book = st.session_state.get("address_book", [])
        if 0 <= target_idx < len(book) and 0 <= source_idx < len(book) and target_idx != source_idx:
            t, s = book[target_idx], book[source_idx]
            
            # Sauvegarde des anciennes clés de la source pour la synchro file d'attente
            old_source_name = s.get("name", "")
            old_source_addr = s.get("address", "")
            
            # Fusion des dates
            vd_t = t.setdefault("visit_dates", [])
            for d in s.get("visit_dates", []):
                if d and d not in vd_t: vd_t.append(d)
            # Fusion des notes
            if s.get("notes"):
                if t.get("notes"):
                    if s["notes"] not in t["notes"]: t["notes"] += " | " + s["notes"]
                else: t["notes"] = s["notes"]
            # Téléphone (si manquant sur cible)
            if s.get("phone") and not t.get("phone"): t["phone"] = s["phone"]
            
            # Suppression du source (attention à l'index si target_idx > source_idx)
            book.pop(source_idx)
            ContactManager.invalidate_index()
            AddressBookManager.set_dirty()
            AddressBookManager.save_to_file(sync_import_csv=True)
            
            # Synchronisation : les entrées de la file qui pointaient vers 'source' 
            # pointent maintenant vers 'target' (avec ses nouvelles données fusionnées)
            WaitlistManager.sync_contact_update(old_source_name, old_source_addr, t)
            
            return True
        return False

    @staticmethod
    def delete_contact_by_key(addr_key: str, name_key: str):
        st.session_state.address_book = [
            c for c in st.session_state.get("address_book", [])
            if not (_norm_addr(c.get("address", "")) == addr_key
                    and _norm_addr(c.get("name", "")) == name_key)
        ]
        ContactManager.invalidate_index()
        AddressBookManager.set_dirty()
        AddressBookManager.save_to_file(sync_import_csv=True)

    @staticmethod
    def get_combined_contacts():
        """Retourne les contacts du carnet avec leurs dates de passage.
        
        Après refactoring, le carnet est la source unique de vérité :
        plus besoin de fusionner deux fichiers.
        """
        book = st.session_state.get("address_book", [])
        result = []
        for c in book:
            entry = dict(c)
            entry.setdefault("visit_dates", [])
            result.append(entry)
        return result

    @staticmethod
    def get_all_cities():
        """Extrait les localités uniques (CP + VILLE) du carnet d'adresses."""
        cities = set()
        for c in st.session_state.get("address_book", []):
            addr = c.get("address", "")
            match = _RE_CITY_EXTRACTOR.search(addr)
            if match:
                city_val = match.group(0).strip().upper()
                city_val = re.sub(r'\s+', ' ', city_val)
                cities.add(city_val)
        return sorted(list(cities))

# ==========================================================
# BASE PERSISTENCE
# ==========================================================
class BasePersistence:
    """Logique commune pour la sauvegarde atomique et le chargement JSON avec cache."""
    
    @staticmethod
    @st.cache_data(show_spinner=False)
    def cached_load(file_path: str) -> Optional[any]:
        """Charge un fichier JSON avec le cache Streamlit (lecture seule)."""
        if not os.path.exists(file_path):
            return None
        try:
            if os.path.getsize(file_path) == 0:
                return None
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    @staticmethod
    def atomic_save(file_path: str, data: any) -> bool:
        """Sauvegarde atomique : écrit dans un .tmp puis remplace le fichier cible."""
        tmp = file_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2 if ".json" in file_path else None)
            os.replace(tmp, file_path)
            # Invalider le cache de lecture après une écriture réussie
            BasePersistence.cached_load.clear(file_path)
            return True
        except Exception as e:
            if os.path.exists(tmp):
                try: os.remove(tmp)
                except: pass
            st.error(f"Erreur écriture disque ({os.path.basename(file_path)}) : {e}")
            return False

    @staticmethod
    def safe_load(file_path: str) -> Optional[any]:
        """Lecture directe sans cache (utilisé pour les données critiques ou mutables)."""
        if not os.path.exists(file_path):
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

# ==========================================================
# GEO CACHE — persistance disque du cache de géocodage
# ==========================================================
class GeoCache:
    """Cache géocodage persistant sur disque (coord_cache.json)."""
    CACHE_FILE     = "coord_cache.json"

    @staticmethod
    def load():
        # Utilisation du cache Streamlit pour le chargement initial
        disk_data = BasePersistence.cached_load(GeoCache.CACHE_FILE)
        if disk_data:
            merged = {k: tuple(v) for k, v in disk_data.items()
                      if isinstance(v, list) and len(v) == 2}
            current = st.session_state.get("coord_cache", {})
            merged.update(current)
            st.session_state["coord_cache"] = merged
            st.session_state["_geocache_size_disk"] = len(merged)
        else:
            st.session_state.setdefault("coord_cache", {})
            st.session_state["_geocache_size_disk"] = 0

    @staticmethod
    def save(force: bool = False):
        cache = st.session_state.setdefault("coord_cache", {})
        _cap_cache(cache, max_size=Config.MAX_GEO_CACHE)
        size_disk = st.session_state.get("_geocache_size_disk", 0)
        
        # On ne sauvegarde que si forcé ou si le cache a grossi significativement
        if not force and (len(cache) - size_disk) < Config.GEO_CACHE_WRITE_INTERVAL:
            return

        serializable = {k: list(v) for k, v in cache.items() if v is not None}
        if BasePersistence.atomic_save(GeoCache.CACHE_FILE, serializable):
            st.session_state["_geocache_size_disk"] = len(serializable)

# ==========================================================
# OSRM CACHE — persistance disque des paires distance/durée
# ==========================================================
class OSRMCache:
    """Cache OSRM persistant sur disque (osrm_cache.json)."""
    CACHE_FILE     = "osrm_cache.json"

    @staticmethod
    def _key_to_str(k: tuple) -> str:
        return f"{k[0][0]},{k[0][1]}|{k[1][0]},{k[1][1]}"

    @staticmethod
    def _str_to_key(s: str) -> tuple:
        try:
            a, b = s.split("|")
            la, lo = a.split(",")
            la2, lo2 = b.split(",")
            return ((float(la), float(lo)), (float(la2), float(lo2)))
        except: return None

    @staticmethod
    def load():
        data = BasePersistence.cached_load(OSRMCache.CACHE_FILE)
        if not data: return
        
        dist_cache = st.session_state.setdefault("_osrm_pair_dist", {})
        dur_cache  = st.session_state.setdefault("_osrm_pair_dur",  {})
        
        for s, v in data.get("dist", {}).items():
            key = OSRMCache._str_to_key(s)
            if key: dist_cache[key] = float(v)
        for s, v in data.get("dur", {}).items():
            key = OSRMCache._str_to_key(s)
            if key: dur_cache[key] = float(v)
            
        st.session_state["_osrm_cache_size_disk"] = len(dist_cache)

    @staticmethod
    def save(force: bool = False):
        dist_cache = st.session_state.get("_osrm_pair_dist", {})
        dur_cache  = st.session_state.get("_osrm_pair_dur",  {})
        size_disk  = st.session_state.get("_osrm_cache_size_disk", 0)
        
        if not force and (len(dist_cache) - size_disk) < Config.OSRM_CACHE_WRITE_INTERVAL:
            return
            
        _cap_cache(dist_cache, max_size=Config.MAX_OSRM_CACHE)
        _cap_cache(dur_cache,  max_size=Config.MAX_OSRM_CACHE)
        
        data = {
            "dist": {OSRMCache._key_to_str(k): v for k, v in dist_cache.items()},
            "dur":  {OSRMCache._key_to_str(k): v for k, v in dur_cache.items()},
        }
        if BasePersistence.atomic_save(OSRMCache.CACHE_FILE, data):
            st.session_state["_osrm_cache_size_disk"] = len(dist_cache)

# ==========================================================
# UI PREFERENCES MANAGER
# ==========================================================
class UIPreferencesManager:
    """Gère les préférences d'affichage utilisateur (ex: colonnes masquées)."""
    PREFS_FILE = "ui_prefs.json"

    @staticmethod
    def load() -> dict:
        if "ui_prefs" not in st.session_state:
            data = BasePersistence.safe_load(UIPreferencesManager.PREFS_FILE)
            st.session_state["ui_prefs"] = data if data else {}
        return st.session_state["ui_prefs"]

    @staticmethod
    def set(key: str, value: any):
        prefs = UIPreferencesManager.load()
        prefs[key] = value
        BasePersistence.atomic_save(UIPreferencesManager.PREFS_FILE, prefs)

    @staticmethod
    def get(key: str, default: any = None) -> any:
        return UIPreferencesManager.load().get(key, default)

class AddressBookManager:
    """Gère la persistance du carnet d'adresses"""
    SAVE_FILE = "carnet_adresses.json"
    
    @staticmethod
    def set_dirty():
        """Marque le carnet comme ayant besoin d'une sauvegarde disque."""
        st.session_state["_book_dirty"] = True

    @staticmethod
    def is_dirty() -> bool:
        return st.session_state.get("_book_dirty", False)

    @staticmethod
    def save_to_file(sync_import_csv: bool = False, force: bool = False):
        """Sauvegarde différée : n'écrit sur disque que si dirty ou force."""
        if not force and not AddressBookManager.is_dirty():
            return True
            
        try:
            contacts = st.session_state.get("address_book", [])
            if BasePersistence.atomic_save(AddressBookManager.SAVE_FILE, contacts):
                st.session_state["_book_dirty"] = False
                if sync_import_csv:
                    AddressBookManager.export_to_import_csv()
                return True
            return False
        except Exception as e:
            st.error(f"Erreur sauvegarde carnet : {e}")
            return False
    
    @staticmethod
    def load_from_file():
        # On utilise cached_load pour éviter de relire le JSON si le fichier n'a pas changé sur disque
        data = BasePersistence.cached_load(AddressBookManager.SAVE_FILE)
        if data is not None:
            # Copie profonde pour éviter de modifier le cache directement
            contacts = json.loads(json.dumps(data)) 
            for c in contacts:
                for k, v in list(c.items()):
                    if isinstance(v, float) and v != v: c[k] = None # Sanitize NaN
                if c.get("intervention_type") not in INTERVENTION_TYPES:
                    c["intervention_type"] = DEFAULT_INTERVENTION_TYPE
                c.setdefault("visit_dates", [])
                li = c.pop("last_intervention", None)
                if li and li not in c["visit_dates"]:
                    c["visit_dates"].append(li)
            st.session_state.address_book = contacts
            st.session_state["_book_dirty"] = False
            ContactManager.invalidate_index()
            AddressBookManager._migrate_history_if_needed()
            WaitlistManager.sync_all(contacts)
            return len(contacts)
        return 0

    @staticmethod
    def _migrate_history_if_needed():
        """Migration one-time : fusionne historique_clients.json dans le carnet.
        
        Si le fichier historique existe, on rapatrie les visit_dates dans les
        entrées correspondantes du carnet, puis on renomme le fichier en .migrated.
        Les clients présents dans l'historique mais absents du carnet sont ignorés
        (le carnet est la source de vérité).
        """
        HIST_FILE = "historique_clients.json"
        if not os.path.exists(HIST_FILE):
            return
        try:
            with open(HIST_FILE, 'r', encoding='utf-8') as f:
                history = json.load(f)
            if not history:
                os.rename(HIST_FILE, HIST_FILE + ".migrated")
                return
            book = st.session_state.get("address_book", [])
            book_idx = {
                (_norm_addr(c.get("name", "")), _norm_addr(c.get("address", ""))): c
                for c in book
            }
            for h in history:
                key = (_norm_addr(h.get("name", "")), _norm_addr(h.get("address", "")))
                dates = h.get("visit_dates", [])
                if not dates and h.get("last_intervention"):
                    dates = [h["last_intervention"]]
                if key in book_idx:
                    existing = book_idx[key].setdefault("visit_dates", [])
                    for d in dates:
                        if d and d not in existing:
                            existing.append(d)
            AddressBookManager.save_to_file(sync_import_csv=True)
            ContactManager.invalidate_index()
            # os.replace est plus robuste sur Windows (écrase si destination existe)
            os.replace(HIST_FILE, HIST_FILE + ".migrated")
        except Exception:
            pass  # Migration silencieuse — sera retentée au prochain démarrage
    
    @staticmethod
    def _build_csv_rows(contacts: list) -> list:
        """FIX #9 : logique commune aux deux exports CSV (carnet → bytes, carnet → fichier).
        Retourne une liste de tuples (visit_date, name, address, notes, itype, duration, phone).
        Avant ce fix, les deux méthodes dupliquaient exactement la même boucle, créant un
        risque de divergence silencieuse lors de futures modifications du format CSV.
        """
        rows = []
        for contact in contacts:
            name     = contact.get("name", "")
            addr     = contact.get("address", "")
            phone    = contact.get("phone", "")
            duration = contact.get("service_duration", 2700) // 60
            itype    = contact.get("intervention_type", "")
            notes    = contact.get("notes", "")
            dates    = contact.get("visit_dates", [])
            if dates:
                for d in sorted(dates, key=_sort_key_date):
                    rows.append((d, name, addr, notes, itype, duration, phone))
            else:
                rows.append(("", name, addr, notes, itype, duration, phone))
        return rows

    @staticmethod
    def export_to_csv() -> bytes:
        if "_csv_cache" in st.session_state:
            return st.session_state["_csv_cache"]
        contacts = st.session_state.get("address_book", [])
        if not contacts: return b""
        output = io.StringIO()
        header = ["visit_date", "name", "address", "notes", "intervention_type", "service_duration", "phone"]
        writer = csv.writer(output, delimiter=';')
        writer.writerow(header)
        for row in AddressBookManager._build_csv_rows(contacts):
            writer.writerow(row)
        result = output.getvalue().encode('utf-8-sig')
        st.session_state["_csv_cache"] = result
        return result

    @staticmethod
    def export_to_import_csv() -> Tuple[bool, str]:
        """Exporte le carnet vers Import_Clients.csv.
        
        Stratégie :
        1. Écrire le carnet actuel dans Import_Clients.csv
        2. Retourner (succès, message)
        """
        try:
            IMPORT_FILE = "Import_Clients.csv"
            contacts = st.session_state.get("address_book", [])
            if not contacts:
                return False, "❌ Carnet vide. Rien à exporter."
            output = io.StringIO()
            header = ["visit_date", "name", "address", "notes", "intervention_type", "service_duration", "phone"]
            writer = csv.writer(output, delimiter=';')
            writer.writerow(header)
            for row in AddressBookManager._build_csv_rows(contacts):
                writer.writerow(row)
            with open(IMPORT_FILE, 'w', encoding='utf-8-sig') as f:
                f.write(output.getvalue())
            msg = f"✅ Export réussi ! {len(contacts)} contact(s) sauvegardés dans **Import_Clients.csv**"
            return True, msg
        except Exception as e:
            return False, f"❌ Erreur export : {str(e)}"
    @staticmethod
    def _clean_phone(phone: str) -> str:
        """Nettoie et formate le numéro de téléphone (format FR simple)."""
        if not phone: return ""
        # Garde uniquement les chiffres
        digits = re.sub(r"\D", "", str(phone))
        if len(digits) == 10 and digits.startswith("0"):
            return f"{digits[:2]} {digits[2:4]} {digits[4:6]} {digits[6:8]} {digits[8:10]}"
        return phone # Retourne tel quel si format inconnu

    @staticmethod
    def import_from_csv(csv_content: str) -> Tuple[int, List[str]]:
        """Import CSV avec validations strictes et rapports d'erreurs détaillés."""
        try:
            lines = [l for l in csv_content.splitlines() if l.strip()]
            if not lines: return 0, ["Fichier CSV vide."]
            
            cleaned_content = "\n".join(lines)
            delimiter = ';' if ';' in cleaned_content else ','
            reader = csv.DictReader(io.StringIO(cleaned_content), delimiter=delimiter)
            
            if reader.fieldnames is None: 
                return 0, ["Fichier CSV illisible (en-tête manquant)."]
                
            actual_fields = {f.strip().lower() for f in reader.fieldnames if f}
            if not (actual_fields & {"address", "adress"}): 
                return 0, [f"Colonne 'address' obligatoire manquante. Colonnes trouvées : {list(actual_fields)}"]
            
            errors, by_addr = [], {}
            for i, row in enumerate(reader, start=2):
                try:
                    # Normalisation des noms de colonnes
                    row = {(k.strip().lower() if k else k): (v.strip() if v else v) for k, v in row.items()}
                    
                    name = row.get("name", "").strip()
                    base_addr = (row.get("address") or row.get("adress") or "").strip()
                    
                    if not base_addr and not name:
                        errors.append(f"Ligne {i}: Nom et Adresse vides, ligne sautée.")
                        continue
                    
                    # Validation spécifique
                    phone = AddressBookManager._clean_phone(row.get("phone", ""))
                    
                    key = (_norm_addr(name), _norm_addr(base_addr))
                    raw_date = _parse_fr_date(row.get("visit_date", "").strip())
                    
                    # Validation format date DD/MM/YYYY
                    if raw_date and not re.match(r"^\d{2}/\d{2}/\d{4}$", raw_date):
                        errors.append(f"Ligne {i}: Date '{raw_date}' mal formatée, ignorée.")
                        raw_date = ""

                    itype = row.get("intervention_type", "").strip()
                    if itype not in INTERVENTION_TYPES: 
                        itype = DEFAULT_INTERVENTION_TYPE
                    
                    try: 
                        duration_sec = int(row.get("service_duration", 45)) * 60
                    except: 
                        duration_sec = 2700
                        
                    if key not in by_addr:
                        by_addr[key] = {
                            "name": name, "address": base_addr, "phone": phone,
                            "intervention_type": itype, "notes": row.get("notes", "").strip(),
                            "service_duration": duration_sec, "visit_dates": [],
                            # FIX #7 — champs contact initialisés à leur valeur par défaut
                            # pour garantir la cohérence avec le dataclass Contact
                            "time_mode": "Libre", "preferred_time": None,
                            "available_weekdays": "", "preferred_month": 0,
                        }
                    
                    entry = by_addr[key]
                    if raw_date and raw_date not in entry["visit_dates"]: 
                        entry["visit_dates"].append(raw_date)
                        
                except Exception as e: 
                    errors.append(f"Ligne {i}: Erreur inattendue : {e}")

            if not by_addr: 
                return 0, errors

            # Transaction finale
            book = st.session_state.address_book
            book_idx = {(_norm_addr(c.get("name", "")), _norm_addr(c["address"])): c for c in book}
            imported_count = 0

            for key, entry in by_addr.items():
                vd = entry.pop("visit_dates", [])
                if key in book_idx:
                    existing_c = book_idx[key]
                    if entry.get("phone"): existing_c["phone"] = entry["phone"]
                    if entry.get("notes"): existing_c["notes"] = entry["notes"]
                    evd = existing_c.setdefault("visit_dates", [])
                    for d in vd:
                        if d not in evd: evd.append(d)
                else:
                    new_entry = dict(entry)
                    new_entry["visit_dates"] = vd
                    book.append(new_entry)
                    imported_count += 1

            st.session_state.address_book = book
            AddressBookManager.set_dirty()
            AddressBookManager.save_to_file(sync_import_csv=True)
            ContactManager.invalidate_index()
            return imported_count, errors
            
        except Exception as e:
            return 0, [f"Erreur fatale lors de l'import : {str(e)}"]
    
    @staticmethod
    def get_csv_template() -> bytes:
        content = "visit_date;name;address;notes;intervention_type;service_duration;phone\n15/01/2025;DUPONT;12 RUE DE LA PAIX 88000 EPINAL;POELE BOIS;Standard_45;60;0601020304"
        return content.encode('utf-8-sig')

# ==========================================================
# ROUTE MANAGER
# ==========================================================
class RouteManager:
    """Sauvegarde et recharge une tournée complète (points + config) en JSON."""
    SAVE_DIR      = "tournees_sauvegardees"
    AUTOSAVE_NAME = "_autosave"

    @staticmethod
    def _ensure_dir(): os.makedirs(RouteManager.SAVE_DIR, exist_ok=True)

    @staticmethod
    def autosave():
        try:
            if st.session_state.get("delivery_points"):
                RouteManager.save(RouteManager.AUTOSAVE_NAME)
        except Exception as e:
            # ❌ FIX BUG #7 : Logger les erreurs au lieu de les ignorer silencieusement
            st.warning(f"⚠️ Autosave échoué : {e}")

    @staticmethod
    def save(name: str) -> bool:
        try:
            RouteManager._ensure_dir()
            safe_name = _RE_SAFE_NAME.sub("_", name)
            cfg, pts = st.session_state.route_config, st.session_state.delivery_points
            data = {
                "version": 1, "saved_at": datetime.today().isoformat(timespec="seconds"),
                "config": {
                    "start_address": cfg.start_address, "start_time": cfg.start_time,
                    "start_service_duration": cfg.start_service_duration, "end_address": cfg.end_address,
                },
                "points": [{
                    "address": p.address, "name": p.name, "time_mode": p.time_mode,
                    "target_time": p.target_time, "intervention_type": p.intervention_type,
                    "notes": p.notes, "service_duration": p.service_duration,
                    "is_start": p.is_start, "is_end": p.is_end, "uid": p.uid,
                } for p in pts],
            }
            path = os.path.join(RouteManager.SAVE_DIR, f"{safe_name}.json")
            # FIX #3 : écriture atomique (tmp + os.replace) pour éviter un fichier tronqué
            # en cas d'interruption (crash Streamlit, coupure réseau, etc.).
            # Avant ce fix, save() utilisait open(..., "w") directement, contrairement à
            # add_client_to_save() qui utilisait déjà le pattern atomique.
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
            return True
        except Exception as e:
            st.error(f"Erreur sauvegarde tournée : {e}"); return False

    @staticmethod
    def list_saves() -> List[str]:
        RouteManager._ensure_dir()
        return sorted([f[:-5] for f in os.listdir(RouteManager.SAVE_DIR) if f.endswith(".json") and f[:-5] != RouteManager.AUTOSAVE_NAME])

    @staticmethod
    def load(name: str) -> bool:
        try:
            safe_name = _RE_SAFE_NAME.sub("_", name)
            path = os.path.join(RouteManager.SAVE_DIR, f"{safe_name}.json")
            with open(path, "r", encoding="utf-8") as f: data = json.load(f)
            cfg, pts = st.session_state.route_config, []
            c = data.get("config", {})
            cfg.start_address = c.get("start_address", ""); cfg.start_time = c.get("start_time", WORK_START)
            cfg.start_service_duration = c.get("start_service_duration", 45 * SPM); cfg.end_address = c.get("end_address", "")
            cfg.start_coordinates, cfg.end_coordinates = None, None
            for pd in data.get("points", []):
                # Sanitize NaN
                for k, v in list(pd.items()):
                    if isinstance(v, float) and v != v: pd[k] = None
                pts.append(DeliveryPoint(
                    address=pd.get("address", ""), name=pd.get("name", ""), time_mode=pd.get("time_mode", "Libre"),
                    target_time=pd.get("target_time"), intervention_type=pd.get("intervention_type", DEFAULT_INTERVENTION_TYPE),
                    notes=pd.get("notes", ""), service_duration=pd.get("service_duration", 45 * SPM),
                    is_start=pd.get("is_start", False), is_end=pd.get("is_end", False),
                    uid=pd.get("uid", os.urandom(4).hex())
                ))
            st.session_state.delivery_points, st.session_state.optimized_result = pts, None
            RouteManager.refresh_route_data(st.session_state.delivery_points)
            st.session_state.pop("si_start", None); st.session_state.pop("si_end", None)
            return True
        except Exception as e:
            st.error(f"Erreur chargement tournée : {e}"); return False

    @staticmethod
    def delete(name: str) -> bool:
        try:
            safe_name = _RE_SAFE_NAME.sub("_", name)
            os.remove(os.path.join(RouteManager.SAVE_DIR, f"{safe_name}.json"))
            return True
        except Exception as e:
            st.error(f"Erreur suppression : {e}"); return False

    @staticmethod
    def add_client_to_save(name: str, client: dict) -> tuple:
        try:
            RouteManager._ensure_dir()
            safe_name = _RE_SAFE_NAME.sub("_", name.strip())
            path = os.path.join(RouteManager.SAVE_DIR, f"{safe_name}.json")
            # is_new déterminé ICI, avant toute écriture, pour éviter la race TOCTOU.
            # L'écriture se fait ensuite via tmp + os.replace (atomique).
            is_new = not os.path.exists(path)
            if not is_new:
                with open(path, "r", encoding="utf-8") as f: data = json.load(f)
            else:
                data = {
                    "version": 1, "saved_at": datetime.today().isoformat(timespec="seconds"),
                    "config": {"start_address": Config.DEFAULT_END_ADDRESS, "start_time": Config.WORK_START, "start_service_duration": 45 * SPM, "end_address": Config.DEFAULT_END_ADDRESS},
                    "points": [],
                }
            if _norm_addr(client.get("address", "")) in {_norm_addr(p.get("address", "")) for p in data.get("points", [])}:
                return (False, f"« {client.get('name') or client.get('address', '?')} » est déjà dans « {safe_name} ».")
            data["points"].append({
                "address": client.get("address", ""), "name": client.get("name", ""), "time_mode": client.get("time_mode", "Libre"),
                "target_time": client.get("target_time"), "intervention_type": client.get("intervention_type", DEFAULT_INTERVENTION_TYPE),
                "notes": client.get("notes", ""), "service_duration": client.get("service_duration", 45 * SPM), "is_start": False, "is_end": False,
                "uid": os.urandom(4).hex()
            })
            data["saved_at"] = datetime.today().isoformat(timespec="seconds")
            # Écriture atomique : on ne modifie le fichier de destination qu'après
            # une écriture réussie dans le fichier temporaire (cohérent avec tous les
            # autres managers du projet).
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
            return (True, f"✅ Sauvegarde « {safe_name} » {'créée' if is_new else 'mise à jour'} ({len(data['points'])} arrêt(s)).")
        except Exception as e: return (False, f"Erreur sauvegarde : {e}")

    @staticmethod
    def add_clients_to_save(name: str, clients: list) -> tuple:
        try:
            RouteManager._ensure_dir()
            safe_name = _RE_SAFE_NAME.sub("_", name.strip())
            path = os.path.join(RouteManager.SAVE_DIR, f"{safe_name}.json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f: data = json.load(f)
            else:
                data = {
                    "version": 1, "saved_at": datetime.today().isoformat(timespec="seconds"),
                    "config": {"start_address": Config.DEFAULT_END_ADDRESS, "start_time": Config.WORK_START, "start_service_duration": 45 * SPM, "end_address": Config.DEFAULT_END_ADDRESS},
                    "points": [],
                }
            existing_norms = {_norm_addr(p.get("address", "")) for p in data.get("points", [])}
            added, skipped = 0, 0
            for client in clients:
                client_norm = _norm_addr(client.get("address", ""))
                if client_norm in existing_norms: skipped += 1; continue
                data["points"].append({
                    "address": client.get("address", ""), "name": client.get("name", ""), "time_mode": client.get("time_mode", "Libre"),
                    "target_time": client.get("target_time"), "intervention_type": client.get("intervention_type", DEFAULT_INTERVENTION_TYPE),
                    "notes": client.get("notes", ""), "service_duration": client.get("service_duration", 45 * SPM), "is_start": False, "is_end": False,
                    "uid": os.urandom(4).hex()
                })
                existing_norms.add(client_norm); added += 1
            data["saved_at"] = datetime.today().isoformat(timespec="seconds")
            # FIX #3 — écriture atomique alignée sur le reste du projet (évite corruption en cas de crash)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
            return (added, skipped, f"✅ Sauvegarde « {safe_name} » mise à jour — {added} client(s) ajouté(s) ({len(data['points'])} arrêt(s) au total).")
        except Exception as e: return (0, 0, f"Erreur sauvegarde par lot : {e}")

    @staticmethod
    def refresh_route_data(points: List[DeliveryPoint]) -> int:
        """Met à jour les informations des points d'une tournée à partir du carnet d'adresses."""
        book_idx = ContactManager.get_index_by_addr()
        updated_count = 0
        for p in points:
            addr_norm = _norm_addr(p.address)
            if addr_norm in book_idx:
                _, contact = book_idx[addr_norm]
                # Mise à jour des données métier sans écraser la planification
                p.name = contact.get("name", p.name)
                p.notes = contact.get("notes", p.notes)
                p.service_duration = contact.get("service_duration", p.service_duration)
                p.intervention_type = contact.get("intervention_type", p.intervention_type)
                updated_count += 1
        return updated_count

    @staticmethod
    def to_ics(result: "RouteResult", cfg: "RouteConfig", points: List["DeliveryPoint"], tour_date: "datetime") -> str:
        def _ics_escape(s: str) -> str:
            s = str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")
            return s
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Tournees4Me//FR", "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Tournée de ramonage", "X-WR-TIMEZONE:Europe/Paris"]
        date_str, now_dt_stamp = tour_date.strftime("%Y%m%d"), datetime.now().strftime("%Y%m%dT%H%M%SZ")
        optim_points = [p for p in points if not p.is_start and not p.is_end]
        start_pt, end_pt = next((p for p in points if p.is_start), None), next((p for p in points if p.is_end), None)
        n_pts = len(optim_points)
        def to_dt(seconds): h, m = (seconds // 3600) % 24, (seconds % 3600) // 60; return f"{date_str}T{h:02d}{m:02d}00"
        addr_idx = ContactManager.get_index_by_addr()
        name_idx = {_norm_addr(c.get("name","")): c for c in st.session_state.get("address_book", [])}
        for step, node in enumerate(result.order):
            p = None
            if node == 0: p = start_pt
            elif 0 < node <= n_pts: p = optim_points[node - 1]
            elif node == n_pts + 1: p = end_pt
            if not p or ((node == 0 or node == n_pts + 1) and p.service_duration <= 0): continue
            p_name = p.name or ContactManager.get_name_by_addr(p.address)
            start, end = result.arrival_times[step], result.arrival_times[step] + p.service_duration
            uid = f"tournee4me-{date_str}-{step}-{node}@local"
            summary = _ics_escape(f"{p_name + ' - ' if p_name else ''}{p.address}")
            desc_parts = []
            if p_name: desc_parts.append(f"Client: {_ics_escape(p_name)}")
            phone, ph_match = "", addr_idx.get(_norm_addr(p.address))
            if ph_match and ph_match[1].get("phone"): phone = ph_match[1]["phone"]
            elif p_name:
                ph_name_match = name_idx.get(_norm_addr(p_name))
                if ph_name_match and ph_name_match.get("phone"): phone = ph_name_match["phone"]
            if phone: desc_parts.append(f"Tél: {_ics_escape(phone)}")
            desc_parts.append(f"Type: {_ics_escape(p.intervention_type)}")
            if p.notes: desc_parts.append(f"Notes: {_ics_escape(p.notes)}")
            lines += ["BEGIN:VEVENT", f"DTSTAMP:{now_dt_stamp}", f"UID:{uid}", f"DTSTART:{to_dt(start)}", f"DTEND:{to_dt(end)}", f"SUMMARY:{summary}", f"DESCRIPTION:{'\\n'.join(desc_parts)}", f"LOCATION:{_ics_escape(p.address)}", "END:VEVENT"]
        lines.append("END:VCALENDAR")
        return "\r\n".join(lines)

class WaitlistManager:
    WAITLIST_FILE  = 'clients_en_attente.json'
    _SS_CACHE_KEY  = "waitlist"

    # ── Gestion d'état ───────────────────────────────────────────────────
    @staticmethod
    def set_dirty():
        st.session_state["_waitlist_dirty"] = True

    @staticmethod
    def is_dirty() -> bool:
        return st.session_state.get("_waitlist_dirty", False)

    # ── I/O ──────────────────────────────────────────────────────────────
    @staticmethod
    def load() -> List[dict]:
        """Charge la file d'attente depuis le cache session ou le fichier JSON."""
        if WaitlistManager._SS_CACHE_KEY in st.session_state:
            return st.session_state[WaitlistManager._SS_CACHE_KEY]
        try:
            if os.path.exists(WaitlistManager.WAITLIST_FILE):
                with open(WaitlistManager.WAITLIST_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    waitlist = data if isinstance(data, list) else []
            else:
                waitlist = []
        except Exception:
            waitlist = []
        # Migration : assigner un uid aux items qui n'en ont pas (ajoutés par batch_add avant le fix)
        import hashlib
        dirty = False
        for item in waitlist:
            c = item.get('client', {})
            for k, v in list(c.items()):
                if isinstance(v, float) and v != v: c[k] = None # Sanitize NaN
            if not item.get('uid'):
                c = item.get('client', {})
                unique_str = f"{c.get('name','')}{c.get('address','')}{item.get('added_at','')}"
                item['uid'] = hashlib.md5(unique_str.encode()).hexdigest()[:12]
                dirty = True
        st.session_state[WaitlistManager._SS_CACHE_KEY] = waitlist
        if dirty:
            WaitlistManager.set_dirty()
            WaitlistManager.save()
        return waitlist

    @staticmethod
    def add(client_data: dict):
        import hashlib
        waitlist = WaitlistManager.load()
        # Création d'un UID unique pour éviter les bugs d'index
        unique_str = f"{client_data.get('name','')}{client_data.get('address','')}{datetime.now().isoformat()}"
        uid = hashlib.md5(unique_str.encode()).hexdigest()[:12]
        
        waitlist.append({
            'uid': uid, # <--- Nouvel identifiant unique
            'client': client_data,
            'added_at': datetime.now().isoformat()
        })
        WaitlistManager.set_dirty()
        WaitlistManager.save() 

    @staticmethod
    def save(force: bool = False):
        """Sauvegarde différée de la file d'attente."""
        if not force and not WaitlistManager.is_dirty():
            return
            
        try:
            data = st.session_state.get(WaitlistManager._SS_CACHE_KEY, [])
            if BasePersistence.atomic_save(WaitlistManager.WAITLIST_FILE, data):
                st.session_state["_waitlist_dirty"] = False
        except Exception as e:
            st.error(f"Erreur lors de la sauvegarde de la file d'attente : {e}")

    @staticmethod
    def batch_add(clients_data: List[dict]):
        import hashlib
        waitlist = WaitlistManager.load()
        now = datetime.now().isoformat()
        for c in clients_data:
            unique_str = f"{c.get('name','')}{c.get('address','')}{now}{len(waitlist)}"
            uid = hashlib.md5(unique_str.encode()).hexdigest()[:12]
            waitlist.append({
                'uid': uid,
                'client': c,
                'added_at': now
            })
        WaitlistManager.set_dirty()
        WaitlistManager.save()

    @staticmethod
    def remove(uid: str):
        """Supprime l'item dont le uid correspond. Refuse de supprimer si uid est vide
        (évite de laisser des items sans uid bloquer ou provoquer des suppressions en masse)."""
        if not uid:
            return
        waitlist = WaitlistManager.load()
        st.session_state[WaitlistManager._SS_CACHE_KEY] = [item for item in waitlist if item.get('uid') != uid]
        WaitlistManager.set_dirty()
        WaitlistManager.save()

    @staticmethod
    def update(uid: str, new_client_data: dict) -> bool:
        """Met à jour les données d'un client identifié par son uid."""
        if not uid:
            return False
        waitlist = WaitlistManager.load()
        for item in waitlist:
            if item.get('uid') == uid:
                item['client'] = dict(new_client_data)
                WaitlistManager.set_dirty()
                WaitlistManager.save()
                return True
        return False

    @staticmethod
    def clear():
        """Vide intégralement la file d'attente."""
        st.session_state[WaitlistManager._SS_CACHE_KEY] = []
        WaitlistManager.set_dirty()
        WaitlistManager.save()

    @staticmethod
    def sync_contact_update(old_name: str, old_addr: str, new_client_data: dict):
        waitlist = WaitlistManager.load()
        changed = False
        norm_old_n, norm_old_a = _norm_addr(old_name), _norm_addr(old_addr)
        
        for item in waitlist:
            c = item.get('client', {})
            if (_norm_addr(c.get('name', '')) == norm_old_n and 
                _norm_addr(c.get('address', '')) == norm_old_a):
                item['client'] = dict(new_client_data)
                changed = True
        
        if changed:
            WaitlistManager.set_dirty()
            WaitlistManager.save()

    @staticmethod
    def sync_all(book: List[dict]):
        waitlist = WaitlistManager.load()
        if not waitlist: return
        
        book_map = {(_norm_addr(c.get("name","")), _norm_addr(c.get("address",""))): c for c in book}
        changed = False
        for item in waitlist:
            c = item.get("client", {})
            key = (_norm_addr(c.get("name","")), _norm_addr(c.get("address","")))
            if key in book_map:
                item["client"] = dict(book_map[key])
                changed = True
        
        if changed:
            WaitlistManager.set_dirty()
            WaitlistManager.save()

    # ── Sauvegardes indépendantes de la file d'attente ───────────────────
    SNAPSHOT_DIR = "waitlist_snapshots"

    @staticmethod
    def _ensure_snapshot_dir():
        os.makedirs(WaitlistManager.SNAPSHOT_DIR, exist_ok=True)

    @staticmethod
    def list_snapshots() -> List[str]:
        WaitlistManager._ensure_snapshot_dir()
        files = [f[:-5] for f in os.listdir(WaitlistManager.SNAPSHOT_DIR) if f.endswith('.json')]
        return sorted(files, reverse=True)

    @staticmethod
    def save_snapshot(name: str) -> Tuple[bool, str]:
        WaitlistManager._ensure_snapshot_dir()
        waitlist = WaitlistManager.load()
        if not waitlist:
            return False, "La file d'attente est vide."
        safe = re.sub(r'[^A-Za-z0-9_\-\. ]', '_', name).strip()
        if not safe:
            return False, "Nom invalide."
        path = os.path.join(WaitlistManager.SNAPSHOT_DIR, f"{safe}.json")
        try:
            tmp = path + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({"name": safe, "saved_at": datetime.now().isoformat(), "items": waitlist}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return True, f"✅ File sauvegardée sous « {safe} »."
        except Exception as e:
            return False, f"Erreur : {e}"

    @staticmethod
    def load_snapshot(name: str) -> Tuple[bool, str]:
        path = os.path.join(WaitlistManager.SNAPSHOT_DIR, f"{name}.json")
        if not os.path.exists(path):
            return False, "Sauvegarde introuvable."
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            items = data.get("items", [])
            existing = WaitlistManager.load()
            existing_addrs = {_norm_addr(i["client"].get("address","")) for i in existing}
            added = 0
            for item in items:
                addr = _norm_addr(item.get("client", {}).get("address", ""))
                if addr not in existing_addrs:
                    existing.append(item)
                    existing_addrs.add(addr)
                    added += 1
            # FIX #2 — save() prenait `existing` (List) comme argument `force` (bool),
            # ce qui provoquait un skip silencieux de la sauvegarde quand la liste était vide.
            # existing est déjà la référence en session_state (mutation en place via append).
            WaitlistManager.set_dirty()
            WaitlistManager.save()
            return True, f"✅ {added} client(s) ajouté(s) depuis « {name} »."
        except Exception as e:
            return False, f"Erreur : {e}"

    @staticmethod
    def delete_snapshot(name: str) -> Tuple[bool, str]:
        path = os.path.join(WaitlistManager.SNAPSHOT_DIR, f"{name}.json")
        if os.path.exists(path):
            os.remove(path)
            return True, f"Sauvegarde « {name} » supprimée."
        return False, "Introuvable."

    @staticmethod
    def save_client_to_snapshot(name: str, client_item: dict) -> Tuple[bool, str]:
        """Ajoute un seul client à une sauvegarde de file (crée si inexistante)."""
        WaitlistManager._ensure_snapshot_dir()
        safe = re.sub(r'[^A-Za-z0-9_\-\. ]', '_', name).strip()
        if not safe:
            return False, "Nom invalide."
        path = os.path.join(WaitlistManager.SNAPSHOT_DIR, f"{safe}.json")
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                data = {"name": safe, "saved_at": datetime.now().isoformat(), "items": []}
        else:
            data = {"name": safe, "saved_at": datetime.now().isoformat(), "items": []}
        # Éviter doublon par adresse normalisée
        existing_addrs = {_norm_addr(i.get("client", {}).get("address", "")) for i in data["items"]}
        addr = _norm_addr(client_item.get("client", {}).get("address", ""))
        if addr and addr in existing_addrs:
            return False, f"Ce client est déjà dans la sauvegarde « {safe} »."
        data["items"].append(client_item)
        data["saved_at"] = datetime.now().isoformat()
        try:
            tmp = path + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            name_label = client_item.get("client", {}).get("name") or client_item.get("client", {}).get("phone", "?")
            return True, f"✅ {name_label} ajouté à la sauvegarde « {safe} »."
        except Exception as e:
            return False, f"Erreur : {e}"

# ==========================================================
# CACHE CLEANER
# ==========================================================
class CacheCleaner:
    """Utilitaire de nettoyage des caches Python et fichiers temporaires."""
    @staticmethod
    def clear_python_cache():
        import shutil, pathlib
        root = pathlib.Path(__file__).parent
        # Nettoyage récursif
        for d in root.rglob("__pycache__"):
            if d.is_dir(): shutil.rmtree(d, ignore_errors=True)
        for f in root.rglob("*.pyc"): f.unlink(missing_ok=True)
