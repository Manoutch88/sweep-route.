import streamlit as st
import re
import json
import os
import csv
import io
import dataclasses
import pandas as pd
from datetime import datetime
from typing import List, Tuple, Optional, Dict
from streamlit_gsheets import GSheetsConnection

from sr_core import (
    Config, DeliveryPoint, Contact, RouteConfig, RouteResult,
    _norm_addr, _cap_cache, _sort_key_date, _parse_fr_date,
    INTERVENTION_TYPES, DEFAULT_INTERVENTION_TYPE, SPM, _RE_CITY_EXTRACTOR,
    _RE_SAFE_NAME, WORK_START
)

# ==========================================================
# CONNEXION GOOGLE SHEETS
# ==========================================================
def get_gsheets_conn():
    try:
        return st.connection("gsheets", type=GSheetsConnection)
    except Exception as e:
        st.error(f"Erreur connexion Google Sheets: {e}")
        return None

# ==========================================================
# HISTORY MANAGER
# ==========================================================
class HistoryManager:
    @staticmethod
    def save_to_file():
        AddressBookManager.save_to_file(force=True)

    @staticmethod
    def load_from_file():
        return 0

    @staticmethod
    def add_visit(p: DeliveryPoint, date_str: str):
        ss = st.session_state
        book = ss.get("address_book", [])
        hist_key = (_norm_addr(p.name), _norm_addr(p.address))
        for contact in book:
            if (_norm_addr(contact.get("name", "")), _norm_addr(contact.get("address", ""))) == hist_key:
                if p.intervention_type: contact["intervention_type"] = p.intervention_type
                if p.service_duration: contact["service_duration"] = p.service_duration
                dates = contact.setdefault("visit_dates", [])
                if date_str not in dates: dates.append(date_str)
                AddressBookManager.set_dirty()
                AddressBookManager.save_to_file()
                ContactManager.invalidate_index()
                return

    @staticmethod
    def delete_entry(address: str, name: str = "", only_history: bool = False):
        if not only_history:
            ContactManager.delete_contact_by_key(_norm_addr(address), _norm_addr(name))

# ==========================================================
# CONTACT MANAGER
# ==========================================================
class ContactManager:
    @staticmethod
    def build_index():
        book = st.session_state.get("address_book", [])
        name_idx, addr_idx, composite_idx = {}, {}, {}
        full_list = []
        for i, c in enumerate(book):
            nl, al = _norm_addr(c.get("name", "")), _norm_addr(c.get("address", ""))
            if nl not in name_idx: name_idx[nl] = (i, c)
            if al not in addr_idx: addr_idx[al] = (i, c)
            composite_idx[(nl, al)] = (i, c)
            full_list.append((i, nl, al, c))
        st.session_state["_contact_name_idx"] = name_idx
        st.session_state["_contact_addr_idx"] = addr_idx
        st.session_state["_contact_composite_idx"] = composite_idx
        st.session_state["_contact_index"] = full_list

    @staticmethod
    def invalidate_index():
        for k in ("_contact_index", "_contact_name_idx", "_contact_addr_idx", "_contact_composite_idx"):
            st.session_state.pop(k, None)

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
        idx = ContactManager.get_index_by_addr()
        match = idx.get(_norm_addr(address))
        return match[1].get("name", "") if match else ""

    @staticmethod
    def add_contact(contact: Contact):
        if "address_book" not in st.session_state: st.session_state.address_book = []
        st.session_state.address_book.append(dataclasses.asdict(contact))
        ContactManager.invalidate_index()
        AddressBookManager.set_dirty()
        AddressBookManager.save_to_file()

    @staticmethod
    def update_contact(index: int, **kwargs):
        book = st.session_state.get("address_book", [])
        if 0 <= index < len(book):
            contact = book[index]
            on, oa = contact.get("name", ""), contact.get("address", "")
            for k, v in kwargs.items(): contact[k] = v
            ContactManager.invalidate_index()
            AddressBookManager.set_dirty()
            AddressBookManager.save_to_file()
            WaitlistManager.sync_contact_update(on, oa, contact)

    @staticmethod
    def find_duplicate(name: str, address: str, exclude_index: int = -1, exact_match: bool = False) -> Optional[int]:
        book = st.session_state.get("address_book", [])
        if exact_match:
            nt, at = name.strip(), address.strip()
            for i, c in enumerate(book):
                if i != exclude_index and c.get("name","").strip() == nt and c.get("address","").strip() == at: return i
        else:
            nn, na = _norm_addr(name), _norm_addr(address)
            for i, c in enumerate(book):
                if i != exclude_index and _norm_addr(c.get("name","")) == nn and _norm_addr(c.get("address","")) == na: return i
        return None

    @staticmethod
    def merge_contacts(target_idx: int, source_idx: int):
        book = st.session_state.get("address_book", [])
        if 0 <= target_idx < len(book) and 0 <= source_idx < len(book):
            t, s = book[target_idx], book[source_idx]
            on, oa = s.get("name", ""), s.get("address", "")
            vd_t = t.setdefault("visit_dates", [])
            for d in s.get("visit_dates", []):
                if d and d not in vd_t: vd_t.append(d)
            if s.get("notes"): t["notes"] = (t.get("notes", "") + " | " + s["notes"]).strip(" | ")
            if s.get("phone") and not t.get("phone"): t["phone"] = s["phone"]
            book.pop(source_idx)
            ContactManager.invalidate_index()
            AddressBookManager.set_dirty()
            AddressBookManager.save_to_file()
            WaitlistManager.sync_contact_update(on, oa, t)
            return True
        return False

    @staticmethod
    def delete_contact_by_key(addr_key: str, name_key: str):
        st.session_state.address_book = [c for c in st.session_state.get("address_book", [])
            if not (_norm_addr(c.get("address", "")) == addr_key and _norm_addr(c.get("name", "")) == name_key)]
        ContactManager.invalidate_index()
        AddressBookManager.set_dirty()
        AddressBookManager.save_to_file()

    @staticmethod
    def get_all_cities():
        cities = set()
        for c in st.session_state.get("address_book", []):
            m = _RE_CITY_EXTRACTOR.search(c.get("address", ""))
            if m: cities.add(re.sub(r'\s+', ' ', m.group(0).strip().upper()))
        return sorted(list(cities))

# ==========================================================
# ADDRESS BOOK MANAGER (GOOGLE SHEETS)
# ==========================================================
class AddressBookManager:
    SHEET_NAME = "Contacts"

    @staticmethod
    def set_dirty(): st.session_state["_book_dirty"] = True

    @staticmethod
    def is_dirty(): return st.session_state.get("_book_dirty", False)

    @staticmethod
    def load_from_file():
        conn = get_gsheets_conn()
        if not conn: return 0
        try:
            df = conn.read(worksheet=AddressBookManager.SHEET_NAME, ttl=0)
            if df is None or df.empty: return 0
            contacts = []
            for _, row in df.iterrows():
                c = {k: (None if pd.isna(v) else v) for k, v in row.to_dict().items()}
                vd_str = str(c.get("visit_date", ""))
                if vd_str and vd_str not in ("None", "nan", ""):
                    c["visit_dates"] = [d.strip() for d in vd_str.split(",") if d.strip()]
                else: c["visit_dates"] = []
                if c.get("intervention_type") not in INTERVENTION_TYPES:
                    c["intervention_type"] = DEFAULT_INTERVENTION_TYPE
                contacts.append(c)
            st.session_state.address_book = contacts
            st.session_state["_book_dirty"] = False
            ContactManager.invalidate_index()
            WaitlistManager.sync_all(contacts)
            return len(contacts)
        except: return 0

    @staticmethod
    def save_to_file(sync_import_csv=False, force=False):
        if not force and not AddressBookManager.is_dirty(): return True
        conn = get_gsheets_conn()
        if not conn: return False
        try:
            contacts = st.session_state.get("address_book", [])
            rows = []
            for c in contacts:
                row = dict(c)
                row["visit_date"] = ", ".join(sorted(row.get("visit_dates", []), key=_sort_key_date))
                if "visit_dates" in row: del row["visit_dates"]
                rows.append(row)
            df = pd.DataFrame(rows)
            cols = ["visit_date", "name", "address", "notes", "intervention_type", "service_duration", "phone"]
            # S'assurer que les colonnes existent
            for col in cols:
                if col not in df.columns: df[col] = ""
            others = [col for col in df.columns if col not in cols]
            df = df[cols + others]
            conn.update(worksheet=AddressBookManager.SHEET_NAME, data=df)
            st.session_state["_book_dirty"] = False
            return True
        except Exception as e:
            st.error(f"Erreur GSheets: {e}")
            return False

    @staticmethod
    def export_to_csv() -> bytes:
        contacts = st.session_state.get("address_book", [])
        output = io.StringIO()
        writer = csv.writer(output, delimiter=';')
        writer.writerow(["visit_date", "name", "address", "notes", "intervention_type", "service_duration", "phone"])
        for c in contacts:
            vd = ", ".join(sorted(c.get("visit_dates", []), key=_sort_key_date))
            writer.writerow([vd, c.get("name",""), c.get("address",""), c.get("notes",""),
                             c.get("intervention_type",""), c.get("service_duration",2700)//60, c.get("phone","")])
        return output.getvalue().encode('utf-8-sig')

    @staticmethod
    def import_from_csv(content: str):
        try:
            lines = [l for l in content.splitlines() if l.strip()]
            reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=';' if ';' in lines[0] else ',')
            book = st.session_state.get("address_book", [])
            added = 0
            for row in reader:
                row = {k.lower().strip(): v.strip() for k, v in row.items() if k}
                addr = row.get("address") or row.get("adress", "")
                if not addr: continue
                book.append({
                    "name": row.get("name", ""), "address": addr, "phone": row.get("phone", ""),
                    "intervention_type": row.get("intervention_type", DEFAULT_INTERVENTION_TYPE),
                    "notes": row.get("notes", ""), "service_duration": int(row.get("service_duration", 45))*60,
                    "visit_dates": [d.strip() for d in row.get("visit_date","").split(",") if d.strip()]
                })
                added += 1
            st.session_state.address_book = book
            AddressBookManager.set_dirty()
            AddressBookManager.save_to_file()
            return added, []
        except Exception as e: return 0, [str(e)]

# ==========================================================
# WAITLIST MANAGER (GOOGLE SHEETS)
# ==========================================================
class WaitlistManager:
    SHEET_NAME = "Waitlist"

    @staticmethod
    def load() -> List[dict]:
        if "waitlist" in st.session_state: return st.session_state["waitlist"]
        conn = get_gsheets_conn()
        if not conn: return []
        try:
            df = conn.read(worksheet=WaitlistManager.SHEET_NAME, ttl=0)
            items = [{"uid": r.uid, "added_at": r.added_at, "client": json.loads(r.client_json)} for r in df.itertuples()]
            st.session_state["waitlist"] = items
            return items
        except: return []

    @staticmethod
    def save():
        conn = get_gsheets_conn()
        if not conn: return
        try:
            wl = st.session_state.get("waitlist", [])
            rows = [{"uid": i["uid"], "added_at": i["added_at"], "client_json": json.dumps(i["client"], ensure_ascii=False)} for i in wl]
            df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["uid", "added_at", "client_json"])
            conn.update(worksheet=WaitlistManager.SHEET_NAME, data=df)
        except: pass

    @staticmethod
    def add(data: dict):
        import hashlib
        wl = WaitlistManager.load()
        uid = hashlib.md5(f"{data.get('address')}{datetime.now()}".encode()).hexdigest()[:12]
        wl.append({'uid': uid, 'client': data, 'added_at': datetime.now().isoformat()})
        WaitlistManager.save()

    @staticmethod
    def remove(uid: str):
        wl = WaitlistManager.load()
        st.session_state["waitlist"] = [i for i in wl if i.get('uid') != uid]
        WaitlistManager.save()

    @staticmethod
    def sync_contact_update(on, oa, nd):
        wl = WaitlistManager.load()
        n, a = _norm_addr(on), _norm_addr(oa)
        for i in wl:
            if _norm_addr(i['client'].get('name')) == n and _norm_addr(i['client'].get('address')) == a:
                i['client'] = dict(nd)
        WaitlistManager.save()

    @staticmethod
    def sync_all(book):
        wl = WaitlistManager.load()
        bm = {(_norm_addr(c.get("name","")), _norm_addr(c.get("address",""))): c for c in book}
        for i in wl:
            k = (_norm_addr(i['client'].get("name","")), _norm_addr(i['client'].get("address","")))
            if k in bm: i['client'] = dict(bm[k])
        WaitlistManager.save()

# ==========================================================
# AUTRES MANAGERS (SANS GSHEETS)
# ==========================================================
class GeoCache:
    @staticmethod
    def load(): st.session_state.setdefault("coord_cache", {})
    @staticmethod
    def save(force=False): pass

class OSRMCache:
    @staticmethod
    def load():
        st.session_state.setdefault("_osrm_pair_dist", {})
        st.session_state.setdefault("_osrm_pair_dur", {})
    @staticmethod
    def save(force=False): pass

class UIPreferencesManager:
    @staticmethod
    def load(): return st.session_state.get("ui_prefs", {})
    @staticmethod
    def set(key, value):
        if "ui_prefs" not in st.session_state: st.session_state.ui_prefs = {}
        st.session_state.ui_prefs[key] = value
    @staticmethod
    def get(key, default=None): return UIPreferencesManager.load().get(key, default)

class RouteManager:
    @staticmethod
    def autosave(): pass
    @staticmethod
    def save(name): return False
    @staticmethod
    def list_saves(): return []
    @staticmethod
    def load(name): return False
    @staticmethod
    def delete(name): return False
    @staticmethod
    def refresh_route_data(pts): return 0
    @staticmethod
    def to_ics(res, cfg, pts, dt): return ""

class CacheCleaner:
    @staticmethod
    def clear_python_cache(): pass
