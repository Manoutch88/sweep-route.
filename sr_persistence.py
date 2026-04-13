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

class CacheCleaner:
    @staticmethod
    def clear_python_cache():
        pass

def get_gsheets_conn():
    try:
        return st.connection("gsheets", type=GSheetsConnection)
    except Exception as e:
        st.error(f"Erreur connexion Google Sheets: {e}")
        return None

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
        for k in ("_contact_index", "_contact_
