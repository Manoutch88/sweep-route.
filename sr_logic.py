import streamlit as st
import time
import math
import threading
import requests
from typing import List, Tuple, Optional, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed

from sr_core import (
    Config, DeliveryPoint, Contact, RouteConfig, RouteResult,
    _norm_addr, _cap_cache, _RE_ORDINAL, _RE_RTA_TO_DOT, _RE_DU, _RE_DES,
    _RE_POSTCODE, TimeUtils, TW, OSRM_URL, MAP_CENTER, WORK_START, WORK_END, SPH, SPM
)
from sr_persistence import GeoCache, OSRMCache

# ==========================================================
# GEOCODING
# ==========================================================
class Geo:
    """Géocodage : API Gouv FR (1er) → Photon (2e) → Nominatim (3e)"""
    GOUV_URL      = "https://api-adresse.data.gouv.fr/search/"
    PHOTON_URL    = "https://photon.komoot.io/api/"
    NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
    HEADERS = {
        "User-Agent": "Tournees4Me/1.0 (ramonage planning)",
        "Accept-Language": "fr,fr-FR;q=0.9",
    }
    _session: Optional["requests.Session"] = None
    _session_lock = threading.RLock()
    _session_initialized = False
    _cache_lock   = threading.Lock()

    @staticmethod
    def _get_session():
        # ❌ FIX BUG #5 : Double-check lock robuste
        if Geo._session is None:
            with Geo._session_lock:
                if Geo._session is None:
                    import requests
                    Geo._session = requests.Session()
                    Geo._session.headers.update(Geo.HEADERS)
        return Geo._session

    @staticmethod
    def _init_session_early():
        """Initialiser la session AVANT les threads (appelé dans StateManager.init())"""
        if not Geo._session_initialized:
            with Geo._session_lock:
                if not Geo._session_initialized:
                    Geo._get_session()
                    Geo._session_initialized = True

    @staticmethod
    def normalize_address(address: str) -> List[str]:
        variants = [address]
        normalized = address
        if _RE_ORDINAL.search(normalized):
            normalized = _RE_ORDINAL.sub(r'\1e', normalized)
            variants.append(normalized)
        if 'rta' in normalized.lower():
            if 'R.T.A' not in normalized:
                var_with_dots = _RE_RTA_TO_DOT.sub('R.T.A', normalized)
                if var_with_dots != normalized: variants.append(var_with_dots)
            if 'R.T.A' in normalized:
                var_no_dots = normalized.replace('R.T.A', 'RTA').replace('r.t.a', 'rta')
                if var_no_dots != normalized: variants.append(var_no_dots)
        if ' du ' in normalized.lower(): variants.append(_RE_DU.sub('des', normalized))
        elif ' des ' in normalized.lower(): variants.append(_RE_DES.sub('du', normalized))
        seen, unique_variants = set(), []
        for v in variants:
            v_lower = v.lower()
            if v_lower not in seen: seen.add(v_lower); unique_variants.append(v)
        return unique_variants

    @staticmethod
    def _fetch(address: str) -> Tuple[Optional[Tuple[float, float]], Optional[str]]:
        variants, last_err = Geo.normalize_address(address.strip()), None
        for variant in variants:
            coords, err = Geo._gouv(variant)
            if coords: return coords, None
            if err: last_err = err
        for variant in variants:
            coords, err = Geo._photon(variant)
            if coords: return coords, None
            if err: last_err = err
        for variant in variants:
            coords, err = Geo._nominatim(variant)
            if coords: return coords, None
            if err: last_err = err
        return None, last_err

    @staticmethod
    def get(address: str) -> Optional[Tuple[float, float]]:
        if not address or not address.strip(): return None
        key, cache = _norm_addr(address), st.session_state.setdefault("coord_cache", {})
        # Cache positif ET négatif : None en cache = adresse déjà tentée sans succès
        # dans cette session → on évite de retraverser les 3 APIs à chaque rerun.
        if key in cache: return cache[key]
        coords, err = Geo._fetch(address)
        if coords:
            # Stocker sous la clé principale ET sous toutes les variantes normalisées
            # (ex : "R.T.A" et "RTA" donnent des clés _norm_addr distinctes — on unifie).
            cache[key] = coords
            for variant in Geo.normalize_address(address):
                cache.setdefault(_norm_addr(variant), coords)
            _cap_cache(cache, max_size=Config.MAX_GEO_CACHE)
            GeoCache.save()
        else:
            # Cache négatif : None en mémoire uniquement (non persisté sur disque).
            cache[key] = None
            st.session_state.last_error = err or f"Adresse introuvable : '{address}'."
        return coords

    @staticmethod
    def batch_geocode(addresses: List[str], max_workers: int = Config.GEO_MAX_WORKERS, progress_cb=None) -> Dict[str, Optional[Tuple[float, float]]]:
        """Géocodage par lot avec isolation stricte des threads."""
        cache = st.session_state.setdefault("coord_cache", {})
        result, to_fetch = {}, []
        for addr in addresses:
            key = _norm_addr(addr)
            if key in cache: result[addr] = cache[key]
            else: to_fetch.append(addr)
        if not to_fetch: return result

        total, done = len(addresses), len(result)
        errors: List[str] = []
        
        # FIX #7 : les écritures dans `cache` depuis les threads workers sont protégées
        # par le GIL CPython (dict.__setitem__ est atomique). L'ordre d'insertion (FIFO
        # pour _cap_cache) n'est toutefois pas garanti entre threads concurrents.
        # En pratique cela ne cause pas de corruption, mais _cap_cache peut éviter des
        # entrées légèrement différentes de celles attendues. Acceptable pour un cache LRU-like.
        # Pour garantir un ordre strict, utiliser un threading.Lock autour des écritures.
        with ThreadPoolExecutor(max_workers=min(max_workers, len(to_fetch), Config.GEO_MAX_WORKERS)) as executor:
            future_to_addr = {executor.submit(Geo._fetch, addr): addr for addr in to_fetch}
            for future in as_completed(future_to_addr):
                addr = future_to_addr[future]
                try:
                    coords, fetch_err = future.result()
                    result[addr] = coords
                    # Mise à jour sécurisée du cache dans le thread principal
                    if coords:
                        cache[_norm_addr(addr)] = coords
                        for variant in Geo.normalize_address(addr):
                            cache.setdefault(_norm_addr(variant), coords)
                    else:
                        cache[_norm_addr(addr)] = None
                        if fetch_err: errors.append(fetch_err)
                except Exception as e:
                    errors.append(f"{addr}: {e}")
                
                done += 1
                if progress_cb: progress_cb(done, total)

        if errors:
            st.session_state.last_error = "Géocodage partiel : " + ", ".join(errors[:3])
        
        if to_fetch:
            _cap_cache(cache, max_size=Config.MAX_GEO_CACHE)
            GeoCache.save()
        return result

    # Bounding box approximative de la zone de travail (Vosges + départements voisins)
    # lat_min, lat_max, lon_min, lon_max — couvre 88, 57, 67, 68, 54, 55, 52, 70
    _BBOX = (47.4, 49.5, 5.5, 8.3)

    @staticmethod
    def _in_bbox(lat: float, lon: float) -> bool:
        lat_min, lat_max, lon_min, lon_max = Geo._BBOX
        return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max

    @staticmethod
    def _gouv(address: str) -> Tuple[Optional[Tuple[float, float]], Optional[str]]:
        try:
            # Extraire le code postal de l'adresse pour contraindre la recherche
            pc_match = _RE_POSTCODE.search(address)
            params: dict = {"q": address, "limit": 5}
            if pc_match:
                params["postcode"] = pc_match.group(0)
            r = Geo._get_session().get(Geo.GOUV_URL, params=params, timeout=Config.GEO_TIMEOUT_SHORT)
            if r.status_code == 200:
                features = r.json().get("features", [])
                if not features:
                    return None, f"Gouv : aucun résultat pour : {address}"
                # Prioriser les résultats dans la bounding box de la zone de travail
                for feat in features:
                    lon_f = float(feat["geometry"]["coordinates"][0])
                    lat_f = float(feat["geometry"]["coordinates"][1])
                    if Geo._in_bbox(lat_f, lon_f):
                        return (lat_f, lon_f), None
                # Si aucun résultat dans la bbox, vérifier si le code postal correspond
                if pc_match:
                    target_pc = pc_match.group(0)
                    for feat in features:
                        props = feat.get("properties", {})
                        if props.get("postcode", "") == target_pc:
                            lon_f = float(feat["geometry"]["coordinates"][0])
                            lat_f = float(feat["geometry"]["coordinates"][1])
                            return (lat_f, lon_f), None
                # En dernier recours, retourner le premier résultat
                return (float(features[0]["geometry"]["coordinates"][1]), float(features[0]["geometry"]["coordinates"][0])), None
            else:
                return None, f"Gouv HTTP {r.status_code} pour : {address}"
        except Exception as e:
            return None, f"Gouv erreur : {e}"

    @staticmethod
    def _photon(address: str) -> Tuple[Optional[Tuple[float, float]], Optional[str]]:
        try:
            time.sleep(Config.PHOTON_DELAY)
            # Centrer la recherche sur la zone de travail (Vosges) via le paramètre location_bias
            params = {
                "q": address, "limit": 5, "lang": "fr",
                "lat": MAP_CENTER[0], "lon": MAP_CENTER[1],
                "zoom": 9  # rayon ~50 km autour du centre
            }
            r = Geo._get_session().get(Geo.PHOTON_URL, params=params, timeout=Config.GEO_TIMEOUT_SHORT)
            if r.status_code == 200:
                features = r.json().get("features", [])
                if not features:
                    return None, f"Photon : aucun résultat pour : {address}"
                # Prioriser les résultats dans la bounding box
                for feat in features:
                    lon_f = float(feat["geometry"]["coordinates"][0])
                    lat_f = float(feat["geometry"]["coordinates"][1])
                    if Geo._in_bbox(lat_f, lon_f):
                        return (lat_f, lon_f), None
                # Fallback : vérifier le code postal si présent dans l'adresse
                pc_match = _RE_POSTCODE.search(address)
                if pc_match:
                    target_pc = pc_match.group(0)
                    for feat in features:
                        props = feat.get("properties", {})
                        if props.get("postcode", "") == target_pc:
                            lon_f = float(feat["geometry"]["coordinates"][0])
                            lat_f = float(feat["geometry"]["coordinates"][1])
                            return (lat_f, lon_f), None
                return (float(features[0]["geometry"]["coordinates"][1]), float(features[0]["geometry"]["coordinates"][0])), None
            else:
                return None, f"Photon HTTP {r.status_code} pour : {address}"
        except Exception as e:
            return None, f"Photon erreur : {e}"

    @staticmethod
    def _nominatim(address: str) -> Tuple[Optional[Tuple[float, float]], Optional[str]]:
        try:
            time.sleep(Config.NOMINATIM_DELAY)
            lat_min, lat_max, lon_min, lon_max = Geo._BBOX
            # viewbox + bounded=1 : on contraint Nominatim à la zone de travail (Vosges + alentours)
            params = {
                "q": address, "format": "json", "limit": 5,
                "addressdetails": 1, "accept-language": "fr",
                "countrycodes": "fr",
                "viewbox": f"{lon_min},{lat_max},{lon_max},{lat_min}",
                "bounded": 1,
            }
            r = Geo._get_session().get(Geo.NOMINATIM_URL, params=params, timeout=Config.GEO_TIMEOUT_LONG)
            if r.status_code == 200:
                data = r.json()
                for item in data:
                    lat_f, lon_f = float(item["lat"]), float(item["lon"])
                    if Geo._in_bbox(lat_f, lon_f):
                        return (lat_f, lon_f), None
                return None, f"Nominatim (zone) : aucun résultat pour : {address}"
            return None, f"Nominatim HTTP {r.status_code} pour : {address}"
        except Exception as e:
            return None, f"Nominatim: {e}"

    @staticmethod
    def reverse(lat: float, lon: float) -> Optional[str]:
        try:
            r = Geo._get_session().get("https://nominatim.openstreetmap.org/reverse", params={"lat": lat, "lon": lon, "format": "json", "accept-language": "fr", "zoom": 18}, timeout=Config.GEO_TIMEOUT_SHORT)
            if r.status_code == 200:
                data = r.json()
                if "display_name" in data:
                    addr, parts = data.get("address", {}), []
                    if addr.get("house_number"): parts.append(addr["house_number"])
                    if addr.get("road"): parts.append(addr["road"])
                    city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("hamlet", "")
                    postcode = addr.get("postcode", "")
                    if postcode and city: parts.append(f"{postcode} {city}")
                    elif city: parts.append(city)
                    return " ".join(parts) if parts else data["display_name"]
        except Exception: pass
        return None

    @staticmethod
    def is_incomplete_address(address: str) -> bool:
        return not bool(_RE_POSTCODE.search(address))

    _SUGGEST_CACHE_MAX = 50
    @staticmethod
    def _suggest_cache() -> dict: 
        return st.session_state.setdefault("_addr_search_cache", {})

    @staticmethod
    def search_address_suggestions(partial_address: str, limit: int = 5) -> List[dict]:
        if len(partial_address.strip()) < 5: return []
        cache_key, cache = _norm_addr(partial_address) + f"|{limit}", Geo._suggest_cache()
        if cache_key in cache: return cache[cache_key]
        suggestions = []
        try:
            # FIX #8 : ce sleep est obligatoire (ToS Nominatim : 1 req/s max).
            # Il bloque le thread principal Streamlit ~1 s — limitation connue et acceptée.
            # Pour une UX non-bloquante, déplacer cet appel dans un thread via st.spinner.
            time.sleep(Config.NOMINATIM_DELAY)
            r = Geo._get_session().get(Geo.NOMINATIM_URL, params={"q": f"{partial_address}, France", "format": "json", "limit": limit, "addressdetails": 1, "countrycodes": "fr", "accept-language": "fr"}, timeout=Config.GEO_TIMEOUT_LONG)
            if r.status_code == 200:
                for result in r.json():
                    addr, parts = result.get("address", {}), []
                    if addr.get("house_number"): parts.append(addr["house_number"])
                    if addr.get("road"): parts.append(addr["road"])
                    city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality") or ""
                    postcode = addr.get("postcode", "")
                    if postcode and city: parts.append(f"{postcode} {city}")
                    elif city: parts.append(city)
                    suggestions.append({"display_name": " ".join(parts) if parts else result.get("display_name", ""), "full_display": result.get("display_name", ""), "lat": float(result["lat"]), "lon": float(result["lon"]), "postcode": postcode, "city": city, "road": addr.get("road", ""), "house_number": addr.get("house_number", "")})
        except Exception as e: st.warning(f"Erreur recherche suggestions : {e}")
        if len(cache) >= Geo._SUGGEST_CACHE_MAX:
            try: del cache[next(iter(cache))]
            except StopIteration: pass
        cache[cache_key] = suggestions
        return suggestions

# ==========================================================
# ROUTING
# ==========================================================
class OSRM:
    """Cache OSRM par paire de points."""
    _session: Optional["requests.Session"] = None
    _session_lock = threading.Lock()

    @staticmethod
    def haversine(c1, c2) -> float:
        lat1, lon1 = math.radians(c1[0]), math.radians(c1[1])
        lat2, lon2 = math.radians(c2[0]), math.radians(c2[1])
        a = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
        return 2 * 6371000 * math.asin(math.sqrt(a))

    @staticmethod
    def _get_session():
        if OSRM._session is None:
            with OSRM._session_lock:
                if OSRM._session is None:
                    import requests
                    from requests.adapters import HTTPAdapter
                    from urllib3.util.retry import Retry
                    s = requests.Session()
                    retries = Retry(total=3, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504], raise_on_status=False)
                    s.mount("http://", HTTPAdapter(max_retries=retries))
                    s.mount("https://", HTTPAdapter(max_retries=retries))
                    OSRM._session = s
        return OSRM._session

    @staticmethod
    def _pt(coord) -> tuple: 
        return (round(coord[0], 6), round(coord[1], 6))

    @staticmethod
    def matrix(coords) -> Optional[Tuple[list, list]]:
        n = len(coords)
        # Garde-fou indépendant : matrix() peut être appelé directement sans passer
        # par Optimizer.optimize(), qui possède sa propre vérification.
        if n > Config.OSRM_MAX_COORDS:
            raise ValueError(f"OSRM.matrix() : trop de coordonnées ({n} > {Config.OSRM_MAX_COORDS})")
        dist_cache, dur_cache = st.session_state.setdefault("_osrm_pair_dist", {}), st.session_state.setdefault("_osrm_pair_dur",  {})
        pts = [OSRM._pt(c) for c in coords]
        try:
            missing_indices = set()
            for i in range(n):
                for j in range(n):
                    if (pts[i], pts[j]) not in dist_cache: missing_indices.add(i); missing_indices.add(j)
            if missing_indices:
                sub_idx = sorted(missing_indices)
                sub_coords = [coords[i] for i in sub_idx]
                r = OSRM._get_session().get(f"{OSRM_URL}/table/v1/driving/{';'.join(f'{lon},{lat}' for lat, lon in sub_coords)}?annotations=distance,duration", timeout=Config.OSRM_TIMEOUT)
                r.raise_for_status()
                d = r.json()
                if "distances" not in d or "durations" not in d: raise ValueError("Réponse OSRM invalide")
                sub_pts = [pts[i] for i in sub_idx]
                for si, pi in enumerate(sub_pts):
                    for sj, pj in enumerate(sub_pts):
                        dist_cache[(pi, pj)], dur_cache[(pi, pj)] = d["distances"][si][sj], d["durations"][si][sj]
                _cap_cache(dist_cache, max_size=Config.MAX_OSRM_CACHE)
                _cap_cache(dur_cache, max_size=Config.MAX_OSRM_CACHE)
                OSRMCache.save()
            # OSRM a répondu correctement : on s'assure que le flag crow-flies est levé.
            st.session_state.pop("_is_crow_flies", None)
        except Exception:
            st.session_state["_is_crow_flies"] = True
            for i in range(n):
                for j in range(n):
                    if (pts[i], pts[j]) not in dist_cache:
                        dist = OSRM.haversine(coords[i], coords[j])
                        # Vitesse réaliste de 35 km/h configurée dans Config
                        dist_cache[(pts[i], pts[j])], dur_cache[(pts[i], pts[j])] = dist, dist / Config.CROW_FLIES_SPEED
        return ([[dist_cache.get((pts[i], pts[j]), 0.0) for j in range(n)] for i in range(n)], [[dur_cache.get((pts[i], pts[j]), 0.0) for j in range(n)] for i in range(n)])

    @staticmethod
    def route_geometry(coords: list) -> Optional[list]:
        if not coords or len(coords) < 2: return None
        cache_key, cached = tuple((round(c[0], 5), round(c[1], 5)) for c in coords), st.session_state.get("_route_geometry_cache")
        if cached and cached.get("key") == cache_key: return cached["geometry"]
        try:
            r = OSRM._get_session().get(f"{OSRM_URL}/route/v1/driving/{';'.join(f'{c[1]},{c[0]}' for c in coords)}", params={"overview": "full", "geometries": "geojson", "steps": "false"}, timeout=Config.OSRM_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if data.get("code") != "Ok": return None
            geometry = [(pt[1], pt[0]) for pt in data["routes"][0]["geometry"]["coordinates"]]
            st.session_state["_route_geometry_cache"] = {"key": cache_key, "geometry": geometry}
            return geometry
        except Exception as e: st.session_state.last_error = f"OSRM route: {e}"; return None

# ==========================================================
# OPTIMIZER
# ==========================================================
class Optimizer:
    @staticmethod
    def cheapest_insertion(candidates: List[int], start: int, end: int, cost_matrix) -> List[int]:
        if not candidates: return []
        if len(candidates) == 1: return list(candidates)
        seed_node = max(candidates, key=lambda n: cost_matrix[start][n])
        route, remaining = [start, seed_node, end], set(c for c in candidates if c != seed_node)
        while remaining:
            best_cost, best_node, best_pos = float("inf"), -1, -1
            for node in remaining:
                for pos in range(1, len(route)):
                    a, b = route[pos - 1], route[pos]
                    delta = cost_matrix[a][node] + cost_matrix[node][b] - cost_matrix[a][b]
                    if delta < best_cost: best_cost, best_node, best_pos = delta, node, pos
            route.insert(best_pos, best_node)
            remaining.discard(best_node)
        return route[1:-1]

    @staticmethod
    def two_opt_delta(chain: List[int], start: int, end: int, cost_matrix, max_iter: int = 3) -> Tuple[List[int], bool]:
        if len(chain) < 2: return chain, False
        route, n, any_improved, improved, iters = [start] + list(chain) + [end], len(chain)+2, False, True, 0
        while improved and iters < max_iter:
            improved = False
            iters += 1
            for i in range(1, n - 2):
                ri_prev, ri = route[i - 1], route[i]
                for j in range(i + 2, n - 1):
                    rj, rj_next = route[j], route[j + 1]
                    if (cost_matrix[ri_prev][rj] + cost_matrix[ri][rj_next] - cost_matrix[ri_prev][ri] - cost_matrix[rj][rj_next]) < -1e-9:
                        route[i:j + 1], improved, any_improved = route[i:j + 1][::-1], True, True
                        ri = route[i]
        return route[1:-1], any_improved

    @staticmethod
    def or_opt_1(chain: List[int], start: int, end: int, cost_matrix) -> Tuple[List[int], bool]:
        if len(chain) < 4: return chain, False
        route, n, any_improved, improved = [start] + list(chain) + [end], len(chain)+2, False, True
        while improved:
            improved = False
            for i in range(1, n - 1):
                node, prev_i, next_i = route[i], route[i - 1], route[i + 1]
                removal_gain = cost_matrix[prev_i][node] + cost_matrix[node][next_i] - cost_matrix[prev_i][next_i]
                for j in range(1, n - 2):
                    if j == i or j == i - 1: continue
                    a, b = route[j], route[j + 1]
                    insert_cost = cost_matrix[a][node] + cost_matrix[node][b] - cost_matrix[a][b]
                    if removal_gain - insert_cost > 1e-9:
                        route.pop(i)
                        route.insert((j if j < i else j - 1) + 1, node)
                        improved = any_improved = True
                        break
                if improved: break
        return route[1:-1], any_improved

    @staticmethod
    def _params() -> dict:
        return {
            "pause_start": st.session_state.get("opt_pause_start", Config.PAUSE_DEFAULT_START),
            "pause_end": st.session_state.get("opt_pause_end", Config.PAUSE_DEFAULT_END),
            "pause_enabled": st.session_state.get("opt_pause_enabled", False)
        }

    @staticmethod
    def held_karp(candidates: List[int], start: int, end: int, dist, svc, points, start_time, p_params) -> List[int]:
        from itertools import combinations
        n = len(candidates)
        if n > 15:
            raise ValueError(f"held_karp appelé avec {n} nœuds — utilisez l'heuristique pour n > 11")
        if n <= 1: return list(candidates)

        PAUSE_ON  = p_params["pause_enabled"]
        PAUSE_S   = p_params["pause_start"]
        PAUSE_E   = p_params["pause_end"]
        PARK      = Config.PARKING_TIME

        def _next_arr(prev_arr: int, prev_node_local: int, next_node_local: int) -> int:
            """Calcule l'heure d'arrivée au nœud suivant en reproduisant fidèlement
            la logique de _compute_times : Règle A (chevauchement pause sur service),
            parking, puis Règle B (arrivée pendant la pause)."""
            svc_prev = svc[prev_node_local] if prev_node_local < len(svc) else 0
            # Regle A (bloquante) : si le service precedent debute avant la pause mais la traverse
            # -> sa fin est repoussee a apres la pause
            if PAUSE_ON and svc_prev > 0 and prev_arr < PAUSE_S and (prev_arr + svc_prev) > PAUSE_S:
                fin_prev = PAUSE_E + (prev_arr + svc_prev - PAUSE_S)
            else:
                fin_prev = prev_arr + svc_prev
            travel = dist[prev_node_local][next_node_local]
            # Parking uniquement entre deux clients (pas depuis le depart fictif ni vers le retour)
            parking = PARK if (travel > 0 and prev_node_local != start) else 0
            t = fin_prev + travel + parking
            # Regle B : si on arrive pendant la pause bloquante, on attend la fin
            if PAUSE_ON and PAUSE_S <= t < PAUSE_E:
                t = PAUSE_E
            # Regle A bis : si le prochain service debute avant la pause mais la traverse -> repousser
            svc_next = svc[next_node_local] if next_node_local < len(svc) else 0
            if PAUSE_ON and svc_next > 0 and t < PAUSE_S and (t + svc_next) > PAUSE_S:
                t = PAUSE_E
            return int(t)

        local, size, dp = [start] + list(candidates), len(candidates) + 1, {}
        for k in range(1, size):
            arr_k = _next_arr(start_time, local[0], local[k])
            lo_k, hi_k = TW.get(points[local[k]-1])
            if arr_k < lo_k: arr_k = lo_k
            # Si après ajustement lo, le service chevauche la pause, repousser à PAUSE_E
            if PAUSE_ON and arr_k < PAUSE_E and (arr_k + svc[local[k]]) > PAUSE_S:
                arr_k = PAUSE_E
                if arr_k < lo_k: arr_k = lo_k
            dp[(1 << k, k)] = (1_000_000 + (arr_k - hi_k) * 10 if arr_k > hi_k else 0, float(dist[local[0]][local[k]]), arr_k, 0)
        for subset_size in range(2, size):
            for subset in combinations(range(1, size), subset_size):
                bits = 0
                for b in subset: bits |= (1 << b)
                for k in subset:
                    prev_bits, best_val = bits & ~(1 << k), None
                    for m in subset:
                        if m == k: continue
                        state = dp.get((prev_bits, m))
                        if state is None: continue
                        prev_pen, prev_dist, prev_arr, _ = state
                        arr_k = _next_arr(prev_arr, local[m], local[k])
                        lo_k, hi_k = TW.get(points[local[k]-1])
                        if arr_k < lo_k: arr_k = lo_k
                        if PAUSE_ON and arr_k < PAUSE_E and (arr_k + svc[local[k]]) > PAUSE_S:
                            arr_k = PAUSE_E
                            if arr_k < lo_k: arr_k = lo_k
                        val = (prev_pen + (1_000_000 + (arr_k - hi_k) * 10 if arr_k > hi_k else 0), prev_dist + dist[local[m]][local[k]], arr_k, m)
                        if best_val is None or val < best_val: best_val = val
                    if best_val is not None: dp[(bits, k)] = best_val
        full_bits, best_final, best_last = (1 << size) - 2, None, None
        for k in range(1, size):
            state = dp.get((full_bits, k))
            if state is None: continue
            pen, d_total, arr_last, _ = state
            val = (pen, d_total + dist[local[k]][end], arr_last, k)
            if best_final is None or val < best_final: best_final, best_last = val, k
        if best_last is None: return list(candidates)
        path, curr, bits = [], best_last, full_bits
        for _ in range(n):
            path.append(local[curr])
            _, _, _, prev = dp[(bits, curr)]
            bits &= ~(1 << curr)
            curr = prev
        path.reverse()
        return path

    @staticmethod
    def optimize(config: RouteConfig, points: List[DeliveryPoint], precomputed_mats=None) -> Optional["RouteResult"]:
        start_pt, optim_points = next((p for p in points if p.is_start), None), [p for p in points if not p.is_start and not p.is_end]
        n_pts, p_params = len(optim_points), Optimizer._params()
        working_cfg = RouteConfig(start_address=config.start_address, start_coordinates=config.start_coordinates, start_time=config.start_time, start_service_duration=start_pt.service_duration if start_pt else config.start_service_duration, end_address=config.end_address, end_coordinates=config.end_coordinates)
        tour_hash = hash((working_cfg.start_address, working_cfg.end_address, working_cfg.start_time, working_cfg.start_service_duration, tuple((p.address, p.time_mode, p.target_time, p.intervention_type, p.service_duration) for p in optim_points), p_params["pause_enabled"], p_params["pause_start"], p_params["pause_end"]))
        cached = st.session_state.get("_optim_cache")
        if cached and cached[0] == tour_hash: return cached[1]
        all_coords = [working_cfg.start_coordinates] + [p.coordinates for p in optim_points] + [working_cfg.end_coordinates]
        if len(all_coords) > Config.OSRM_MAX_COORDS: st.session_state.last_error = f"Max {Config.OSRM_MAX_COORDS} points"; return None
        mats = precomputed_mats if precomputed_mats is not None else OSRM.matrix(all_coords)
        if mats is None: return None
        dist_m, dur_s = mats
        
        # LOGIQUE DE DÉPART :
        # 1. Si un point de la liste est marqué 'is_start', il est déjà utilisé via start_pt
        # 2. Sinon, si 'Partir du domicile' est décoché, on ignore le coût du premier trajet
        has_manual_start = any(p.is_start for p in points)
        if not has_manual_start and not st.session_state.get("use_fixed_start", True):
            # FIX #1 : copier les matrices avant mutation pour ne pas corrompre le cache
            # partagé (_last_mats / precomputed_mats). La mutation en place des listes
            # originales affectait silencieusement les appels ultérieurs (move_result_node,
            # re-optimisation automatique).
            dist_m = [list(row) for row in dist_m]
            dur_s  = [list(row) for row in dur_s]
            # On ignore le coût du premier trajet (départ "gratuit" depuis le point de départ fictif)
            for j in range(len(dur_s[0])):
                dur_s[0][j] = 0.0
                dist_m[0][j] = 0.0
            # Mise à jour des pénalités pour les heuristiques
            if n_pts > 11:
                penalized = [list(row) for row in dur_s]
                for i in range(n_pts + 1):
                    for j in range(1, n_pts + 1):
                        if i == j: continue
                        pj = optim_points[j-1]
                        _, hi_j = TW.get(pj)
                        if working_cfg.start_time + dur_s[0][j] > hi_j: penalized[i][j] += 1_000_000
        
        #svc = [working_cfg.start_service_duration] + [p.service_duration for p in optim_points] + [0]
        # Le départ (index 0) a toujours une durée d'intervention de 0 pour ne pas retarder le 1er client.
        svc = [0] + [p.service_duration for p in optim_points] + [0]
        all_indices, end_node = list(range(1, n_pts + 1)), n_pts + 1
        # FIX #2 : initialiser penalized avant les branches pour éviter un NameError
        # si la logique évolue (ex : n_pts > 11 mais use_fixed_start=False déjà traité).
        penalized = None
        if n_pts == 0:
            best_chain = []
        elif n_pts <= 11:
            best_chain = Optimizer.held_karp(all_indices, 0, end_node, dur_s, svc, optim_points, working_cfg.start_time, p_params)
        else:
            # Pour n > 11, on construit une matrice de coût effectif qui intègre :
            # 1. La pénalité de dépassement de fenêtre horaire (hi_j)
            # 2. Le délai de pause bloquante : si un trajet i→j fait arriver pendant la pause,
            #    le coût réel inclut l'attente jusqu'à PAUSE_END.
            penalized = [list(row) for row in dur_s]
            PAUSE_ON = p_params["pause_enabled"]
            PAUSE_S  = p_params["pause_start"]
            PAUSE_E  = p_params["pause_end"]
            PARK     = Config.PARKING_TIME
            for i in range(n_pts + 1):
                for j in range(1, n_pts + 1):
                    if i == j: continue
                    pj = optim_points[j-1]
                    _, hi_j = TW.get(pj)
                    # Pénalité fenêtre horaire
                    if working_cfg.start_time + dur_s[0][j] > hi_j:
                        penalized[i][j] += 1_000_000
                    # Surcoût pause : si arriver à ce nœud depuis i force une attente,
                    # on l'ajoute au coût pour que l'heuristique évite cet enchaînement
                    if PAUSE_ON:
                        arr_j_from_i = working_cfg.start_time + dur_s[i][j] + PARK
                        if PAUSE_S <= arr_j_from_i < PAUSE_E:
                            penalized[i][j] += (PAUSE_E - arr_j_from_i)
            best_chain, _ = Optimizer.two_opt_delta(Optimizer.cheapest_insertion(all_indices, 0, end_node, penalized), 0, end_node, penalized, max_iter=100)
        arrivals = Optimizer._compute_times([0] + best_chain + [end_node], working_cfg.start_time, dur_s, svc, optim_points)
        total_dist = sum(float(dist_m[([0] + best_chain + [end_node])[k]][([0] + best_chain + [end_node])[k+1]]) for k in range(len(best_chain) + 1))
        result = RouteResult(order=[0] + best_chain + [end_node], total_distance=total_dist, total_time=arrivals[-1]-working_cfg.start_time, arrival_times=arrivals, is_approximation=(n_pts > 11), initial_distance=total_dist, tour_hash=tour_hash)
        st.session_state["_optim_cache"] = (tour_hash, result)
        return result

    @staticmethod
    def _compute_times(order, start_time, dur_s, svc, points):
        n_pts, p_ = len(points), Optimizer._params()
        PAUSE_START, PAUSE_END, USE_PAUSE = p_["pause_start"], p_["pause_end"], p_["pause_enabled"]
        arrivals, t = [], int(start_time)
        
        for step, node in enumerate(order):
            service_dur = svc[node] if node < len(svc) else 0

            # REGLE B : arrivée pendant la pause -> on attend la fin
            if USE_PAUSE and PAUSE_START <= t < PAUSE_END:
                t = PAUSE_END

            # REGLE A (bloquante) : service débutant avant la pause mais la traversant
            # -> repousser le début a apres la pause (meme logique que Regle B)
            if USE_PAUSE and service_dur > 0 and t < PAUSE_START and (t + service_dur) > PAUSE_START:
                t = PAUSE_END

            # Gestion de la fenêtre horaire du client
            if 0 < node <= n_pts:
                lo, _ = TW.get(points[node - 1])
                if t < lo: t = lo

            # Enregistrement de l'heure d'arrivée
            arrivals.append(t)

            # FIN DE SERVICE : simple, la pause est deja integree dans t si necessaire
            fin_t = t + service_dur

            if step < len(order) - 1:
                travel_duration = int(dur_s[node][order[step + 1]])
                # On n'ajoute le parking que si ce n'est pas le retour final
                is_last_leg = (step == len(order) - 2)
                parking = Config.PARKING_TIME if (travel_duration > 0 and not is_last_leg) else 0
                t = fin_t + travel_duration + parking
                
        return arrivals

# ==========================================================
# VALIDATOR
# ==========================================================
class Validator:
    @staticmethod
    def check_point_time(p: DeliveryPoint) -> Tuple[bool, Optional[str]]:
        if p.time_mode == "Heure précise":
            if p.target_time is None: return False, "Heure cible non spécifiée"
            if not (WORK_START <= p.target_time <= WORK_END): return False, f"Heure {p.target_time // SPH:02d}:{(p.target_time % SPH) // SPM:02d} hors plage 08h-18h"
        return True, None

    @staticmethod
    def check_setup(config: RouteConfig, points: List[DeliveryPoint]) -> Tuple[bool, Optional[str]]:
        if not config.start_address: return False, "Adresse de départ manquante"
        if not config.start_coordinates: return False, "Départ non géocodé"
        if not config.end_address: return False, "Adresse de retour manquante"
        if not config.end_coordinates: return False, "Retour non géocodé"
        if not points: return False, "Aucun point d'arrêt"
        for p in points:
            if not p.coordinates: return False, f"Non géocodé: {p.address}"
        for i, p in enumerate(points):
            ok, err = Validator.check_point_time(p)
            if not ok: return False, f"Point {i+1}: {err}"
        return True, None
