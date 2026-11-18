import os
import json
import math
import time
import requests
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo

VERSION = "1.0"

# ==============================================================================
# APEX QUANT HOCKEY (NHL)
# Misma familia que Apex Quant fútbol, basket y tenis, pero SIN cuotas de casas:
# el bot calcula con su modelo la probabilidad de cada mercado y la CUOTA JUSTA
# (1 / probabilidad). Tú la comparas con Winamax: si Winamax paga al menos un 5 %
# más que la cuota justa (la «cuota mínima»), hay valor.
#
# Fuente gratuita: ESPN (calendario, directo y resultados). No gasta créditos.
# Modelo:
#   1) Ataque y defensa de cada equipo con los goles en tiempo reglamentario
#      (60') de la temporada y la anterior, dando más peso a lo reciente.
#   2) Goles esperados de cada equipo (con ventaja de jugar en casa y cansancio si
#      jugó el día anterior) -> distribución de Poisson -> todos los mercados.
#   3) Si hay empate a los 60', prórroga/penaltis según la fuerza de cada equipo.
# Lo que el modelo NO sabe: qué portero juega, lesiones ni bajas de última hora.
# ==============================================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
APP_URL = os.getenv("APP_URL", "https://apexquantbot.github.io/apex-quant-hockey/")

TZ_MADRID = ZoneInfo("Europe/Madrid")
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"

# Margen que se exige sobre la cuota justa para que haya valor (1.05 = +5 %)
MARGEN_VALOR = float(os.getenv("MARGEN_VALOR", "1.05"))
# Hora (Madrid) a la que se manda la previa de la noche con todas las cuotas justas
PREVIA_HORA = int(os.getenv("PREVIA_HORA", "18"))
# Pronóstico del modelo para cada partido: probabilidad mínima y cuota mínima
PICK_PROB_MIN = float(os.getenv("PICK_PROB_MIN", "0.60"))
PICK_PROB_MAX = float(os.getenv("PICK_PROB_MAX", "0.85"))
PICK_CUOTA_MIN = float(os.getenv("PICK_CUOTA_MIN", "1.30"))
PICKS_MAX_DIA = int(os.getenv("PICKS_MAX_DIA", "6"))      # tope de pronósticos por noche en Telegram

# Parámetros del modelo
VIDA_MEDIA_DIAS = float(os.getenv("VIDA_MEDIA_DIAS", "75"))   # un partido de hace 75 días pesa la mitad
PRIOR_GOLES = float(os.getenv("PRIOR_GOLES", "30"))           # cuánto se "frena" a los equipos con pocos datos
MIN_PARTIDOS = int(os.getenv("MIN_PARTIDOS", "5"))
EMPATE_EXTRA = float(os.getenv("EMPATE_EXTRA", "1.06"))      # el hockey tiene algo más de empates a 60' que un Poisson puro
CANSANCIO_ATAQUE = float(os.getenv("CANSANCIO_ATAQUE", "0.96"))   # jugó el día anterior: marca un poco menos
CANSANCIO_DEFENSA = float(os.getenv("CANSANCIO_DEFENSA", "1.04"))  # ... y encaja un poco más
PRORROGA_PESO = 0.6        # en prórroga/penaltis la ventaja del mejor equipo se reduce
MAX_GOLES = 12
DIAS_HISTORIAL = 400

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (ApexQuantHockey; +https://apexquantbot.github.io/apex-quant-hockey/)",
                "Accept": "application/json"}
INICIO_CICLO = time.time()
TIEMPO_MAX_DESCARGAS = float(os.getenv("TIEMPO_MAX_DESCARGAS", "480"))
DIAG = {"espn_ok": 0, "espn_error": 0, "muestras": []}


def tiempo_usado():
    return time.time() - INICIO_CICLO


# ==============================================================================
# MERCADOS
# Cada mercado se resuelve con el resultado r = (goles_local_60, goles_visit_60, ganador_final)
# ganador_final: 1 = local, 2 = visitante (incluye prórroga y penaltis).
# En prórroga o penaltis el ganador suma 1 gol al marcador final (norma NHL).
# ==============================================================================
def _final(r):
    gl, gv, w = r
    if gl == gv:
        return (gl + 1, gv) if w == 1 else (gl, gv + 1)
    return gl, gv


MERCADOS = {
    # clave: (grupo, tipo, texto, función de acierto)
    "ML1":   ("ganador", "GANADOR", "Gana {L} (prórroga incluida)", lambda r: r[2] == 1),
    "ML2":   ("ganador", "GANADOR", "Gana {V} (prórroga incluida)", lambda r: r[2] == 2),
    "1":     ("1x2", "1X2", "1X2 a 60': gana {L}", lambda r: r[0] > r[1]),
    "X":     ("1x2", "1X2", "1X2 a 60': empate", lambda r: r[0] == r[1]),
    "2":     ("1x2", "1X2", "1X2 a 60': gana {V}", lambda r: r[1] > r[0]),
    "1X":    ("doble", "DOBLE", "Doble oportunidad 60': {L} o empate", lambda r: r[0] >= r[1]),
    "X2":    ("doble", "DOBLE", "Doble oportunidad 60': empate o {V}", lambda r: r[1] >= r[0]),
    "O45":   ("goles60", "GOLES", "Más de 4.5 goles (60')", lambda r: r[0] + r[1] > 4.5),
    "U45":   ("goles60", "GOLES", "Menos de 4.5 goles (60')", lambda r: r[0] + r[1] < 4.5),
    "O55":   ("goles60", "GOLES", "Más de 5.5 goles (60')", lambda r: r[0] + r[1] > 5.5),
    "U55":   ("goles60", "GOLES", "Menos de 5.5 goles (60')", lambda r: r[0] + r[1] < 5.5),
    "O65":   ("goles60", "GOLES", "Más de 6.5 goles (60')", lambda r: r[0] + r[1] > 6.5),
    "U65":   ("goles60", "GOLES", "Menos de 6.5 goles (60')", lambda r: r[0] + r[1] < 6.5),
    # La línea 5.5 da lo mismo con o sin prórroga (un empate a 60' es par: 2-2 -> 5, 3-3 -> 7); la 4.5 y la 6.5 no
    "OT45":  ("golesOT", "GOLES", "Más de 4.5 goles (prórroga incluida)", lambda r: sum(_final(r)) > 4.5),
    "UT45":  ("golesOT", "GOLES", "Menos de 4.5 goles (prórroga incluida)", lambda r: sum(_final(r)) < 4.5),
    "OT65":  ("golesOT", "GOLES", "Más de 6.5 goles (prórroga incluida)", lambda r: sum(_final(r)) > 6.5),
    "UT65":  ("golesOT", "GOLES", "Menos de 6.5 goles (prórroga incluida)", lambda r: sum(_final(r)) < 6.5),
    "PL1-":  ("handicap", "HANDICAP", "Hándicap {L} -1.5", lambda r: _final(r)[0] - _final(r)[1] > 1.5),
    "PL2+":  ("handicap", "HANDICAP", "Hándicap {V} +1.5", lambda r: _final(r)[1] - _final(r)[0] > -1.5),
    "PL1+":  ("handicap", "HANDICAP", "Hándicap {L} +1.5", lambda r: _final(r)[0] - _final(r)[1] > -1.5),
    "PL2-":  ("handicap", "HANDICAP", "Hándicap {V} -1.5", lambda r: _final(r)[1] - _final(r)[0] > 1.5),
    "BTTS":  ("ambos", "AMBOS", "Ambos marcan (60'): sí", lambda r: r[0] >= 1 and r[1] >= 1),
    "BTTSN": ("ambos", "AMBOS", "Ambos marcan (60'): no", lambda r: not (r[0] >= 1 and r[1] >= 1)),
    "L25O":  ("equipo", "EQUIPO", "{L}: más de 2.5 goles (60')", lambda r: r[0] > 2.5),
    "L25U":  ("equipo", "EQUIPO", "{L}: menos de 2.5 goles (60')", lambda r: r[0] < 2.5),
    "V25O":  ("equipo", "EQUIPO", "{V}: más de 2.5 goles (60')", lambda r: r[1] > 2.5),
    "V25U":  ("equipo", "EQUIPO", "{V}: menos de 2.5 goles (60')", lambda r: r[1] < 2.5),
}
# Mercados que puede elegir el bot como «pronóstico» (el empate solo nunca llega al mínimo)
CLAVES_PICK = [k for k in MERCADOS if k != "X"]
# Mercados que se guardan para medir si las probabilidades del modelo aciertan (calibración)
CLAVES_CALIBRACION = list(MERCADOS)


def texto_mercado(clave, local, visit):
    return MERCADOS[clave][2].replace("{L}", local).replace("{V}", visit)


def cuota_justa(p):
    return round(1.0 / p, 2) if p and p > 0 else None


def cuota_minima(p):
    return round(MARGEN_VALOR / p, 2) if p and p > 0 else None


# ==============================================================================
# ESPN
# Formato interno de un partido:
# m = {"id", "utcDate", "status", "detalle", "tipo_final" ("REG"|"OT"|"SO"|None), "neutral",
#      "local": {"id","name","short","abbr","logo","record"}, "visit": {...},
#      "goles": [gl, gv] (marcador final), "reg": [gl60, gv60], "ganador": 1|2|None}
# status: TIMED | IN_PLAY | FINISHED | POSTPONED | CANCELLED
# ==============================================================================
def http_json(url, params=None, intentos=2):
    for intento in range(intentos):
        try:
            res = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=(5, 20))
            if res.status_code == 200:
                try:
                    return res.json(), 200
                except Exception:
                    return None, "200-no-json"
            if res.status_code == 429 and intento + 1 < intentos:
                time.sleep(3)
                continue
            return None, res.status_code
        except Exception as e:
            print(f"   Aviso red ({url.split('?')[0]}): {e}")
            if intento + 1 < intentos:
                time.sleep(1)
    return None, "sin-respuesta"


def _int_o_none(v):
    try:
        if v is None or str(v).strip() == "":
            return None
        return int(float(v))
    except Exception:
        return None


def _estado(st_obj):
    st = (st_obj or {}).get("type") or {}
    nombre = (st.get("name") or "").upper()
    state = st.get("state")
    detalle = st.get("shortDetail") or st.get("detail") or st.get("description") or ""
    if "POSTPONED" in nombre or "SUSPENDED" in nombre or "DELAYED" in nombre:
        return "POSTPONED", detalle
    if "CANCEL" in nombre or "FORFEIT" in nombre:
        return "CANCELLED", detalle
    if state == "post" or st.get("completed") or "FINAL" in nombre:
        return "FINISHED", detalle
    if state == "in" or "PROGRESS" in nombre or "INTERMISSION" in nombre or "END_PERIOD" in nombre:
        return "IN_PLAY", detalle
    return "TIMED", detalle


def _equipo(c):
    t = c.get("team") or {}
    logo = t.get("logo") or ""
    if not logo and t.get("logos"):
        logo = (t["logos"][0] or {}).get("href") or ""
    rec = ""
    for r in c.get("records") or []:
        if r.get("summary"):
            rec = r["summary"]
            break
    nombre = t.get("displayName") or t.get("name") or "Equipo"
    return {"id": str(t.get("id") or t.get("abbreviation") or nombre), "name": nombre,
            "short": t.get("shortDisplayName") or t.get("name") or nombre,
            "abbr": t.get("abbreviation") or "", "logo": logo, "record": rec}


def parsear_scoreboard(data):
    salida = []
    for ev in (data or {}).get("events", []) or []:
        try:
            tipo_temp = _int_o_none((ev.get("season") or {}).get("type"))
            if tipo_temp not in (None, 2, 3):
                continue   # pretemporada (1) y partido de las estrellas fuera
            comp = (ev.get("competitions") or [{}])[0]
            cs = comp.get("competitors") or []
            if len(cs) != 2:
                continue
            casa = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
            fuera = next((c for c in cs if c is not casa), cs[1])
            st_obj = comp.get("status") or ev.get("status") or {}
            status, detalle = _estado(st_obj)
            periodo = _int_o_none(st_obj.get("period")) or 0
            gl, gv = _int_o_none(casa.get("score")), _int_o_none(fuera.get("score"))
            tipo_final, reg, ganador = None, None, None
            if status == "FINISHED" and gl is not None and gv is not None and gl != gv:
                det = detalle.upper()
                if "SO" in det.replace("/", " ").split() or "SHOOTOUT" in det or periodo >= 5:
                    tipo_final = "SO"
                elif "OT" in det or periodo == 4:
                    tipo_final = "OT"
                else:
                    tipo_final = "REG"
                if tipo_final == "REG":
                    reg = [gl, gv]
                else:
                    # en prórroga o penaltis el ganador suma 1: a los 60' iban empatados
                    reg = [min(gl, gv), min(gl, gv)]
                ganador = 1 if gl > gv else 2
            elif status == "FINISHED":
                status = "PENDING_RESULT"   # final sin marcador fiable: se espera al siguiente ciclo
            salida.append({
                "id": f"nhl{comp.get('id') or ev.get('id')}",
                "utcDate": comp.get("date") or ev.get("date") or "",
                "status": status, "detalle": detalle, "periodo": periodo, "tipo_final": tipo_final,
                "neutral": bool(comp.get("neutralSite")),
                "local": _equipo(casa), "visit": _equipo(fuera),
                "goles": [gl, gv] if gl is not None and gv is not None and status != "TIMED" else None,
                "reg": reg, "ganador": ganador,
            })
        except Exception as e:
            print(f"   Aviso partido ESPN: {e}")
    return salida


def espn_rango(desde, hasta):
    """Partidos entre dos fechas (de EE. UU.). ESPN admite rangos: dates=AAAAMMDD-AAAAMMDD."""
    if tiempo_usado() > TIEMPO_MAX_DESCARGAS:
        return None
    if desde == hasta:
        params = {"dates": desde.strftime("%Y%m%d"), "limit": 200}
    else:
        params = {"dates": f"{desde.strftime('%Y%m%d')}-{hasta.strftime('%Y%m%d')}", "limit": 1000}
    data, st = http_json(ESPN_SCOREBOARD, params)
    if len(DIAG["muestras"]) < 8:
        DIAG["muestras"].append({"d": params["dates"], "st": st,
                                 "ev": len((data or {}).get("events") or []) if data else None})
    if data is None:
        DIAG["espn_error"] += 1
        print(f"   ⚠️ ESPN {params['dates']}: HTTP {st}")
        return None
    DIAG["espn_ok"] += 1
    return parsear_scoreboard(data)


def _fecha_utc(s):
    try:
        t = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t
    except Exception:
        return None


def jornada_de(utc):
    """La «noche» a la que pertenece un partido (hora de Madrid menos 12 h).
    Un partido a la 01:00 del miércoles es de la noche del martes."""
    return (utc.astimezone(TZ_MADRID) - timedelta(hours=12)).date()


def obtener_partidos(desde, hasta):
    partidos = {}
    lote = espn_rango(desde, hasta)
    if lote is None:   # si el rango falla, día a día
        lote = []
        d = desde
        while d <= hasta:
            x = espn_rango(d, d)
            lote.extend(x or [])
            d += timedelta(days=1)
    for m in lote:
        partidos[m["id"]] = m
    print(f"📡 [ESPN] {len(partidos)} partidos ({desde} a {hasta})")
    return list(partidos.values())


# ==============================================================================
# HISTORIAL DE RESULTADOS (equipos.json) Y FUERZA DE CADA EQUIPO
# ==============================================================================
ARCHIVO_EQUIPOS = "equipos.json"


def cargar_equipos():
    if os.path.exists(ARCHIVO_EQUIPOS):
        try:
            with open(ARCHIVO_EQUIPOS, "r", encoding="utf-8") as f:
                d = json.load(f)
                for k, v in (("partidos", {}), ("equipos", {}), ("estado", {})):
                    d.setdefault(k, v)
                return d
        except Exception as e:
            print(f"Aviso leyendo {ARCHIVO_EQUIPOS}: {e}")
    return {"version": 1, "partidos": {}, "equipos": {}, "estado": {}}


def guardar_equipos(eq, hoy):
    limite = (hoy - timedelta(days=DIAS_HISTORIAL + 30)).isoformat()
    eq["partidos"] = {k: v for k, v in eq["partidos"].items() if v[0] >= limite}
    try:
        with open(ARCHIVO_EQUIPOS, "w", encoding="utf-8") as f:
            json.dump(eq, f, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        print(f"Aviso guardando {ARCHIVO_EQUIPOS}: {e}")


def registrar_partido(eq, m):
    """Guarda un partido terminado: [fecha, id_local, id_visit, goles_local_60, goles_visit_60, ganador, neutral]."""
    for lado in ("local", "visit"):
        t = m[lado]
        eq["equipos"][t["id"]] = {"n": t["name"], "s": t["short"], "a": t["abbr"], "l": t["logo"]}
    if m.get("status") != "FINISHED" or not m.get("reg") or not m.get("ganador"):
        return False
    if m["id"] in eq["partidos"]:
        return False
    utc = _fecha_utc(m.get("utcDate"))
    fecha = jornada_de(utc).isoformat() if utc else (m.get("utcDate") or "")[:10]
    eq["partidos"][m["id"]] = [fecha, m["local"]["id"], m["visit"]["id"], m["reg"][0], m["reg"][1],
                               m["ganador"], 1 if m.get("neutral") else 0]
    return True


def rellenar_historial(eq, hoy):
    """La primera vez descarga los últimos ~400 días (temporada actual y anterior) en tramos de 20 días."""
    est = eq["estado"]
    cursor = datetime.fromisoformat(est.get("cursor") or (hoy - timedelta(days=DIAS_HISTORIAL)).isoformat()).date()
    nuevos, tramos = 0, 0
    while cursor < hoy and tiempo_usado() < TIEMPO_MAX_DESCARGAS * 0.7:
        fin = min(cursor + timedelta(days=19), hoy - timedelta(days=1))
        lote = espn_rango(cursor, fin)
        if lote is None:
            break   # se reintenta en el siguiente ciclo desde el mismo día
        nuevos += sum(1 for m in lote if registrar_partido(eq, m))
        cursor = fin + timedelta(days=1)
        tramos += 1
    est["cursor"] = cursor.isoformat()
    if tramos:
        print(f"   📚 Historial ESPN: {tramos} tramos (+{nuevos} partidos) · al día hasta {cursor}")
    return nuevos


def calcular_fuerzas(eq, hoy):
    """Ataque y defensa de cada equipo (1.00 = media de la liga) con goles a 60', ponderados por antigüedad."""
    filas = []
    for v in eq["partidos"].values():
        try:
            dias = (hoy - date.fromisoformat(v[0])).days
        except Exception:
            continue
        if dias < 0 or dias > DIAS_HISTORIAL:
            continue
        filas.append((0.5 ** (dias / VIDA_MEDIA_DIAS), v[1], v[2], v[3], v[4], v[6] if len(v) > 6 else 0))
    if len(filas) < 30:
        return None
    sw = sum(f[0] for f in filas)
    mu_h = sum(f[0] * f[3] for f in filas if not f[5]) / max(1e-9, sum(f[0] for f in filas if not f[5]))
    mu_a = sum(f[0] * f[4] for f in filas if not f[5]) / max(1e-9, sum(f[0] for f in filas if not f[5]))
    mu_n = (mu_h + mu_a) / 2
    equipos = {f[1] for f in filas} | {f[2] for f in filas}
    att = {t: 1.0 for t in equipos}
    de = {t: 1.0 for t in equipos}
    n_part = {t: 0 for t in equipos}
    for f in filas:
        n_part[f[1]] += 1
        n_part[f[2]] += 1
    for _ in range(25):
        marc, esp_m, enc, esp_e = ({t: 0.0 for t in equipos} for _ in range(4))
        for w, h, a, gh, ga, neu in filas:
            bh, ba = (mu_n, mu_n) if neu else (mu_h, mu_a)
            marc[h] += w * gh
            esp_m[h] += w * bh * de[a]
            marc[a] += w * ga
            esp_m[a] += w * ba * de[h]
            enc[h] += w * ga
            esp_e[h] += w * ba * att[a]
            enc[a] += w * gh
            esp_e[a] += w * bh * att[h]
        att = {t: (marc[t] + PRIOR_GOLES) / (esp_m[t] + PRIOR_GOLES) for t in equipos}
        de = {t: (enc[t] + PRIOR_GOLES) / (esp_e[t] + PRIOR_GOLES) for t in equipos}
        ma = sum(att.values()) / len(att)
        md = sum(de.values()) / len(de)
        att = {t: v / ma for t, v in att.items()}
        de = {t: v / md for t, v in de.items()}
    return {"mu_h": mu_h, "mu_a": mu_a, "mu_n": mu_n, "att": att, "def": de, "n": n_part, "peso": sw}


def jugo_ayer(eq, equipo_id, jornada, calendario):
    """True si el equipo jugó la noche anterior (back-to-back)."""
    ayer = (jornada - timedelta(days=1)).isoformat()
    for v in eq["partidos"].values():
        if v[0] == ayer and equipo_id in (v[1], v[2]):
            return True
    for m in calendario:
        utc = _fecha_utc(m.get("utcDate"))
        if utc and jornada_de(utc).isoformat() == ayer and equipo_id in (m["local"]["id"], m["visit"]["id"]):
            return True
    return False


def racha(eq, equipo_id, n=5):
    juegos = sorted((v for v in eq["partidos"].values() if equipo_id in (v[1], v[2])), key=lambda v: v[0])[-n:]
    return "".join("W" if (v[5] == 1) == (v[1] == equipo_id) else "L" for v in juegos)


# ==============================================================================
# MODELO DEL PARTIDO
# ==============================================================================
def _poisson(lam, k):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def calcular_modelo(m, fuerzas, eq, calendario):
    if not fuerzas:
        return None
    h, a = m["local"]["id"], m["visit"]["id"]
    if fuerzas["n"].get(h, 0) < MIN_PARTIDOS or fuerzas["n"].get(a, 0) < MIN_PARTIDOS:
        return None
    utc = _fecha_utc(m.get("utcDate"))
    jornada = jornada_de(utc) if utc else datetime.now(TZ_MADRID).date()
    bh, ba = (fuerzas["mu_n"], fuerzas["mu_n"]) if m.get("neutral") else (fuerzas["mu_h"], fuerzas["mu_a"])
    att, de = fuerzas["att"], fuerzas["def"]
    b2b_h, b2b_a = jugo_ayer(eq, h, jornada, calendario), jugo_ayer(eq, a, jornada, calendario)
    lam_h = bh * att.get(h, 1.0) * de.get(a, 1.0)
    lam_a = ba * att.get(a, 1.0) * de.get(h, 1.0)
    if b2b_h:
        lam_h *= CANSANCIO_ATAQUE
        lam_a *= CANSANCIO_DEFENSA
    if b2b_a:
        lam_a *= CANSANCIO_ATAQUE
        lam_h *= CANSANCIO_DEFENSA
    P = [[_poisson(lam_h, i) * _poisson(lam_a, j) for j in range(MAX_GOLES + 1)] for i in range(MAX_GOLES + 1)]
    for i in range(MAX_GOLES + 1):
        P[i][i] *= EMPATE_EXTRA
    s = sum(map(sum, P))
    P = [[x / s for x in fila] for fila in P]
    q = 0.5 + (lam_h / (lam_h + lam_a) - 0.5) * PRORROGA_PESO   # prob. de que el local gane prórroga/penaltis
    probs = {}
    for clave, (_, _, _, fn) in MERCADOS.items():
        p = 0.0
        for i in range(MAX_GOLES + 1):
            for j in range(MAX_GOLES + 1):
                if i == j:
                    p += P[i][j] * (q * fn((i, j, 1)) + (1 - q) * fn((i, j, 2)))
                elif fn((i, j, 1 if i > j else 2)):
                    p += P[i][j]
        probs[clave] = min(0.995, max(0.005, p))
    return {"lam_h": round(lam_h, 2), "lam_a": round(lam_a, 2), "probs": probs,
            "b2b_local": b2b_h, "b2b_visit": b2b_a,
            "att_local": round(att.get(h, 1.0), 2), "def_local": round(de.get(h, 1.0), 2),
            "att_visit": round(att.get(a, 1.0), 2), "def_visit": round(de.get(a, 1.0), 2)}


def tabla_cuotas(modelo, local, visit):
    """Lista para la web: cada mercado con su probabilidad, cuota justa y cuota mínima."""
    filas = []
    for clave, (grupo, tipo, _, _) in MERCADOS.items():
        p = modelo["probs"][clave]
        filas.append({"k": clave, "g": grupo, "t": texto_mercado(clave, local, visit), "p": round(p * 100, 1),
                      "j": cuota_justa(p), "m": cuota_minima(p)})
    return filas


def elegir_pick(modelo, local, visit):
    """Pronóstico del modelo: entre los mercados con probabilidad 60-85 % y cuota mínima >= 1,30,
    el de cuota más alta (más premio por el mismo margen exigido)."""
    validos = []
    for clave in CLAVES_PICK:
        p = modelo["probs"][clave]
        if not (PICK_PROB_MIN <= p <= PICK_PROB_MAX):
            continue
        cmin = cuota_minima(p)
        if cmin < PICK_CUOTA_MIN:
            continue
        validos.append((cmin, clave, p))
    if not validos:
        return None
    cmin, clave, p = max(validos)
    grupo, tipo, _, _ = MERCADOS[clave]
    return {"clave": clave, "mercado": texto_mercado(clave, local, visit), "tipo_mercado": tipo, "categoria_visual": grupo,
            "prob": p, "prob_modelo": f"{round(p * 100, 1)}%", "cuota_justa": cuota_justa(p), "cuota": cmin,
            "ev": round((MARGEN_VALOR - 1) * 100, 1), "stake": 1}


def resolver(clave, info):
    """True / False / 'PUSH' / None (aún no se sabe)."""
    if info.get("estado") in ("CANCELLED", "POSTPONED"):
        return "PUSH"
    if info.get("estado") != "FINISHED" or not info.get("reg") or not info.get("ganador"):
        return None
    r = (info["reg"][0], info["reg"][1], info["ganador"])
    return bool(MERCADOS[clave][3](r))


# ==============================================================================
# TELEGRAM
# ==============================================================================
AVISO_JUEGO = "🔞 <i>+18 • Juega con responsabilidad • Información estadística, no es asesoramiento • jugarbien.es</i>"


def enviar_telegram(mensaje, url_accion=None, texto_boton="📱 Abrir en Web App"):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"{mensaje}\n\n{AVISO_JUEGO}", "parse_mode": "HTML",
               "disable_web_page_preview": True}
    destino = url_accion or APP_URL
    if destino:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": texto_boton, "url": destino}]]}
    for intento in range(3):
        try:
            res = requests.post(url, json=payload, timeout=12)
            if res.status_code == 200:
                return True
            print(f"⚠️ Telegram rechazó el mensaje (HTTP {res.status_code}): {res.text[:200]}")
            if res.status_code in (400, 401, 403, 404):
                return False
            time.sleep(1.5)
        except Exception as e:
            print(f"Error Telegram intento {intento + 1}: {e}")
            time.sleep(2)
    return False


def _c(p):
    return f"@{cuota_justa(p):.2f}"


def bloque_previa(p, pick_enviado):
    """Texto de un partido en la previa: cuotas justas de los mercados principales."""
    pr = p["_probs"]
    L, V = p["local_corto"], p["visitante_corto"]
    cansados = [x for x, b in ((L, p["modelo"]["b2b_local"]), (V, p["modelo"]["b2b_visit"])) if b]
    lineas = [f"🏒 <b>{L} - {V}</b> · {p['hora']}" + (
        f" · 💤 {' y '.join(cansados)} {'jugaron' if len(cansados) > 1 else 'jugó'} ayer" if cansados else ""),
        f"🏆 Ganador c/prórroga: {L} {_c(pr['ML1'])} · {V} {_c(pr['ML2'])}",
        f"⏱️ 1X2 60': 1 {_c(pr['1'])} · X {_c(pr['X'])} · 2 {_c(pr['2'])}",
        f"🎯 Goles 5.5 (igual con o sin prórroga): más {_c(pr['O55'])} · menos {_c(pr['U55'])}",
        f"🎯 Goles 6.5: 60' más {_c(pr['O65'])} · menos {_c(pr['U65'])} | c/prórr. más {_c(pr['OT65'])} · menos {_c(pr['UT65'])}",
        f"⚖️ Hándicap: {L} -1.5 {_c(pr['PL1-'])} · {V} +1.5 {_c(pr['PL2+'])} | {V} -1.5 {_c(pr['PL2-'])} · {L} +1.5 {_c(pr['PL1+'])}",
        f"📐 Goles esperados: {p['modelo']['lam_h']:.2f} - {p['modelo']['lam_a']:.2f}"]
    if pick_enviado:
        lineas.append(f"📌 <b>Pronóstico:</b> {p['mercado']} · {p['prob_modelo']} · <b>cuota mínima @{p['cuota']:.2f}</b>")
    return "\n".join(lineas)


def enviar_previa(jornada, partidos):
    """Una previa por noche. Telegram admite ~4.000 caracteres por mensaje: se parte si hace falta."""
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    cab = (f"🏒 <b>APEX QUANT HOCKEY — NHL · noche del {dias[jornada.weekday()]} {jornada.day:02d}/{jornada.month:02d}</b>\n"
           f"Cuotas justas del modelo (horas de España). <b>Hay valor si Winamax paga al menos un "
           f"{round((MARGEN_VALOR - 1) * 100)} % más</b> (justa @2.00 → necesitas @{2 * MARGEN_VALOR:.2f} o más).\n"
           f"«60'» = tiempo reglamentario · «c/prórroga» = prórroga y penaltis incluidos (mira cómo lo pone Winamax).")
    bloques = [bloque_previa(p, p.get("telegram_enviado")) for p in partidos]
    picks = [p for p in partidos if p.get("telegram_enviado")]
    pie = (f"📌 <b>{len(picks)} pronóstico{'s' if len(picks) != 1 else ''} del modelo</b> esta noche. "
           f"Juégalos solo si tu casa paga la cuota mínima o más.\n⚡ <i>Apex Quant Engine • Modelo Poisson NHL</i>"
           if picks else "⚡ <i>Apex Quant Engine • Modelo Poisson NHL</i>")
    mensajes, actual = [], cab
    for b in bloques:
        if len(actual) + len(b) + 2 > 3500:
            mensajes.append(actual)
            actual = "🏒 <b>(continuación)</b>"
        actual += "\n\n" + b
    actual += "\n\n" + pie
    mensajes.append(actual)
    ok = True
    for i, txt in enumerate(mensajes):
        ok = enviar_telegram(txt, url_accion=APP_URL, texto_boton="📱 Ver todas las cuotas justas") and ok
        if i + 1 < len(mensajes):
            time.sleep(1)
    return ok


# ==============================================================================
# SEMANAS, HISTÓRICOS Y RESÚMENES (igual que fútbol, basket y tenis)
# ==============================================================================
def cargar_json(nombre, defecto):
    if os.path.exists(nombre):
        try:
            with open(nombre, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Aviso leyendo {nombre}: {e}")
    return defecto


def guardar_json(nombre, datos, compacto=False):
    try:
        with open(nombre, "w", encoding="utf-8") as f:
            if compacto:
                json.dump(datos, f, ensure_ascii=False, separators=(",", ":"))
            else:
                json.dump(datos, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Aviso guardando {nombre}: {e}")


def obtener_identificador_semana(fecha_madrid):
    anio, semana, _ = fecha_madrid.isocalendar()
    return f"{anio}-W{semana:02d}"


def obtener_fechas_limite_semana(fecha_madrid):
    lunes_dt = fecha_madrid - timedelta(days=fecha_madrid.weekday())
    return lunes_dt.strftime("%Y-%m-%d"), (lunes_dt + timedelta(days=6)).strftime("%Y-%m-%d")


def archivar_historico_mensual(alertas_viejas, mes_id):
    historico = cargar_json("historico_mensual.json", {})
    historico.setdefault(mes_id, [])
    ids = {a.get("id_alerta") for a in historico[mes_id]}
    for a in alertas_viejas:
        if a.get("id_alerta") not in ids:
            historico[mes_id].append(a)
    guardar_json("historico_mensual.json", historico)


def _a_float(txt, defecto):
    try:
        return float(str(txt).split("/")[0].replace("@", "").replace("U", "").replace("+", "").replace("%", "").strip())
    except Exception:
        return defecto


def calcular_resumen_financiero(alertas_lista):
    apostadas, neto, acertadas, finalizadas = 0.0, 0.0, 0, 0
    for a in alertas_lista:
        if a.get("finalizado") and not a.get("push"):
            finalizadas += 1
            stk = _a_float(a.get("stake", "1"), 1.0)
            cuota = _a_float(a.get("cuota", "@2.00"), 2.0)
            apostadas += stk
            if a.get("acertado") is True:
                acertadas += 1
                neto += stk * (cuota - 1.0)
            else:
                neto -= stk
    roi = round((neto / apostadas) * 100, 1) if apostadas > 0 else 0.0
    return {"finalizadas": finalizadas, "acertadas": acertadas, "falladas": finalizadas - acertadas,
            "unidades_apostadas": round(apostadas, 1), "unidades_netas": round(neto, 2), "roi_pct": roi}


def formatear_fecha_corta(fecha_str):
    p = (fecha_str or "").split("-")
    return f"{p[2]}/{p[1]}" if len(p) == 3 else (fecha_str or "")


ARCHIVO_LEDGER = "historico_picks.json"
ARCHIVO_CALIBRACION = "calibracion.json"


def actualizar_ledger(alertas, partidos):
    ledger = cargar_json(ARCHIVO_LEDGER, {"version": 1, "picks": []})
    picks = ledger.get("picks", [])
    ids = {p.get("id") for p in picks}
    nuevas = 0
    for a in alertas:
        if not (a.get("finalizado") and a.get("acertado") is not None and not a.get("push")):
            continue
        pid = f"TG_{a.get('id_alerta')}"
        if pid in ids:
            continue
        picks.append({"id": pid, "version": VERSION, "grupo": "TG", "fecha": a.get("fecha"), "mes": (a.get("fecha") or "")[:7],
                      "semana_id": a.get("semana_id"), "partido": a.get("partido"), "mercado": a.get("mercado"),
                      "casa": None, "cuota": _a_float(a.get("cuota"), 2.0), "stake": _a_float(a.get("stake"), 1.0),
                      "ev": _a_float(a.get("ev"), 0.0), "confirmado_stats": True, "acertado": bool(a.get("acertado")),
                      "solo_modelo": True, "tipo": a.get("tipo_mercado"), "marcador": a.get("marcador_final")})
        ids.add(pid)
        nuevas += 1
    for p in partidos:
        if not (p.get("estado") == "FINISHED" and p.get("acertado") is not None and p.get("tiene_pronostico")
                and not p.get("telegram_enviado") and not p.get("push")):
            continue
        pid = f"CTRL_{p.get('id')}_{p.get('clave_pick')}"
        if pid in ids:
            continue
        picks.append({"id": pid, "version": VERSION, "grupo": "CONTROL", "fecha": p.get("fecha"), "mes": (p.get("fecha") or "")[:7],
                      "semana_id": p.get("semana_id"), "partido": f"{p.get('local')} vs {p.get('visitante')}",
                      "mercado": p.get("mercado"), "casa": None, "cuota": _a_float(p.get("cuota"), 2.0),
                      "stake": _a_float(p.get("stake"), 1.0), "ev": _a_float(p.get("ev_numero"), 0.0),
                      "confirmado_stats": True, "acertado": bool(p.get("acertado")), "solo_modelo": True,
                      "tipo": p.get("tipo_mercado"), "marcador": p.get("marcador")})
        ids.add(pid)
        nuevas += 1
    if nuevas:
        ledger["picks"] = picks
        guardar_json(ARCHIVO_LEDGER, ledger, compacto=True)
        print(f"📒 Libro mayor: +{nuevas} picks (total {len(picks)})")


def actualizar_calibracion(partidos):
    """Guarda, de cada partido terminado, la probabilidad que dio el modelo a cada mercado y si salió.
    Sirve para comprobar si las cuotas justas son de fiar (si dice 60 %, ¿sale ~60 % de las veces?)."""
    cal = cargar_json(ARCHIVO_CALIBRACION, {"version": 1, "claves": CLAVES_CALIBRACION, "partidos": []})
    ids = {x.get("id") for x in cal.get("partidos", [])}
    nuevas = 0
    for p in partidos:
        if p.get("estado") != "FINISHED" or not p.get("probs_previa") or p.get("id") in ids:
            continue
        info = {"estado": "FINISHED", "reg": p.get("reg"), "ganador": p.get("ganador")}
        sal = {}
        for k in CLAVES_CALIBRACION:
            r = resolver(k, info)
            if isinstance(r, bool):
                sal[k] = 1 if r else 0
        if not sal:
            continue
        cal["partidos"].append({"id": p["id"], "f": p.get("fecha"), "p": p["probs_previa"], "o": sal})
        ids.add(p["id"])
        nuevas += 1
    if nuevas:
        cal["claves"] = CLAVES_CALIBRACION
        guardar_json(ARCHIVO_CALIBRACION, cal, compacto=True)
        print(f"🎯 Calibración: +{nuevas} partidos (total {len(cal['partidos'])})")


# ==============================================================================
# CICLO PRINCIPAL
# ==============================================================================
def ejecutar_ciclo():
    ahora_utc = datetime.now(timezone.utc)
    ahora_madrid = ahora_utc.astimezone(TZ_MADRID)
    hora_madrid_str = ahora_madrid.strftime("%H:%M")
    hoy_madrid = ahora_madrid.date()
    hoy_madrid_str = hoy_madrid.isoformat()
    semana_actual_id = obtener_identificador_semana(ahora_madrid)
    mes_actual_id = ahora_madrid.strftime("%Y-%m")
    dia_semana, hora_int = ahora_madrid.weekday(), ahora_madrid.hour
    lunes_semana_str, domingo_semana_str = obtener_fechas_limite_semana(ahora_madrid)
    jornada_actual = (ahora_madrid - timedelta(hours=12)).date()
    print(f"[{hora_madrid_str}] Iniciando ciclo Apex Quant Hockey v{VERSION} (Semana {semana_actual_id})...")

    datos_previos = cargar_json("datos.json", {})
    alertas = datos_previos.get("alertas", [])
    partidos_previos = datos_previos.get("partidos", [])
    semana_guardada = datos_previos.get("semana_activa", semana_actual_id)
    previas_enviadas = list(datos_previos.get("previas_enviadas", []))[-20:]
    resumen_semanal_enviado = datos_previos.get("resumen_semanal_enviado_para", "")
    resumen_mensual_enviado = datos_previos.get("resumen_mensual_enviado_para", "")

    telegram_ok = bool(datos_previos.get("telegram_ok"))
    if not telegram_ok and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        telegram_ok = enviar_telegram(
            "🏒 <b>APEX QUANT HOCKEY</b> ya está en marcha.\n\n"
            f"Cada tarde, hacia las {PREVIA_HORA}:00, recibirás la <b>previa de la noche de NHL</b> con la "
            "<b>cuota justa</b> de cada mercado según el modelo (ganador, 1X2, goles, hándicap…), para compararla "
            "con Winamax, y unos pocos pronósticos con su cuota mínima. Por la mañana, los resultados.",
            None, "📱 Abrir Apex Quant Hockey")
        print(f"📨 Bienvenida a Telegram: {'enviada ✅' if telegram_ok else 'FALLO ❌'}")

    if semana_actual_id != semana_guardada:
        print(f"🔄 Nueva semana ({semana_guardada} -> {semana_actual_id})")
        archivar_historico_mensual([a for a in alertas if not a.get("es_semana_actual", True)], mes_actual_id)
        for a in alertas:
            a["es_semana_actual"] = False
        semana_guardada = semana_actual_id

    # HISTORIAL + FUERZAS
    eq = cargar_equipos()
    rellenar_historial(eq, jornada_actual)
    lunes = date.fromisoformat(lunes_semana_str)
    desde = (lunes - timedelta(days=1)) if not partidos_previos else max(lunes - timedelta(days=1), jornada_actual - timedelta(days=2))
    calendario = obtener_partidos(desde, jornada_actual + timedelta(days=2))
    for m in sorted(calendario, key=lambda x: x.get("utcDate") or ""):
        registrar_partido(eq, m)
    fuerzas = calcular_fuerzas(eq, jornada_actual)
    guardar_equipos(eq, jornada_actual)
    print(f"📚 Historial: {len(eq['partidos'])} partidos · {len(eq['equipos'])} equipos · "
          f"{'modelo listo' if fuerzas else 'faltan datos para el modelo'}")

    previos_map = {str(x.get("id")): x for x in partidos_previos}
    partidos_web, mapa_por_id = [], {}

    for m in calendario:
        utc = _fecha_utc(m.get("utcDate"))
        if not utc:
            continue
        mad = utc.astimezone(TZ_MADRID)
        jornada = jornada_de(utc)
        hora_p, fecha_p = mad.strftime("%H:%M"), mad.strftime("%Y-%m-%d")
        ts_ms = int(utc.timestamp() * 1000)
        minutos_restantes = round((utc - ahora_utc).total_seconds() / 60.0)
        local, visit = m["local"]["name"], m["visit"]["name"]
        estado = m["status"]
        if estado == "TIMED" and minutos_restantes < -240:
            estado = "PENDING_RESULT"
        goles = m.get("goles")
        marcador = f"{goles[0]}-{goles[1]}" if goles else None
        if marcador and m.get("tipo_final") in ("OT", "SO"):
            marcador += " (" + ("prórroga" if m["tipo_final"] == "OT" else "penaltis") + ")"
        info = {"id": m["id"], "estado": estado, "reg": m.get("reg"), "ganador": m.get("ganador"), "marcador": marcador}
        mapa_por_id[m["id"]] = info
        if jornada.isoformat() < lunes_semana_str and fecha_p < lunes_semana_str:
            continue

        prev = previos_map.get(m["id"]) or {}
        modelo = prev.get("modelo") if prev.get("analizado") else None
        tabla, probs = prev.get("cuotas_justas"), prev.get("probs_previa")
        if estado == "TIMED" and not prev.get("analizado"):
            try:
                mod = calcular_modelo(m, fuerzas, eq, calendario)
                if mod:
                    modelo = {k: v for k, v in mod.items() if k != "probs"}
                    tabla = tabla_cuotas(mod, m["local"]["short"], m["visit"]["short"])
                    probs = {k: round(v, 4) for k, v in mod["probs"].items()}
            except Exception as e:
                print(f"   Aviso modelo {local} - {visit}: {e}")

        item = {
            "id": m["id"], "local": local, "visitante": visit,
            "local_corto": m["local"]["short"], "visitante_corto": m["visit"]["short"],
            "abbr_local": m["local"]["abbr"], "abbr_visitante": m["visit"]["abbr"],
            "escudo_local": m["local"]["logo"], "escudo_visitante": m["visit"]["logo"],
            "record_local": m["local"].get("record"), "record_visitante": m["visit"].get("record"),
            "liga": "NHL", "liga_key": "nhl", "neutral": m.get("neutral"),
            "fecha": fecha_p, "jornada": jornada.isoformat(), "hora": hora_p,
            "fecha_hora_display": f"{formatear_fecha_corta(fecha_p)} • {hora_p}",
            "utcDate": m.get("utcDate"), "timestamp_ms": ts_ms, "minutos_restantes": minutos_restantes,
            "estado": estado, "minuto_juego": m.get("detalle") if estado == "IN_PLAY" else None,
            "marcador": marcador, "goles_local": goles[0] if goles else None, "goles_visitante": goles[1] if goles else None,
            "reg": m.get("reg"), "ganador": m.get("ganador"), "tipo_final": m.get("tipo_final"),
            "modelo": modelo, "cuotas_justas": tabla, "probs_previa": probs,
            "form_info": {"racha_local": racha(eq, m["local"]["id"]), "racha_visitante": racha(eq, m["visit"]["id"])},
            "semana_id": semana_actual_id, "analizado": bool(prev.get("analizado")),
            "tiene_pronostico": bool(prev.get("tiene_pronostico")), "telegram_enviado": bool(prev.get("telegram_enviado")),
            "acertado": None, "push": False,
        }
        for k in ("mercado", "tipo_mercado", "categoria_visual", "clave_pick", "cuota", "cuota_justa", "prob_modelo",
                  "ev", "ev_numero", "stake", "nota_cuotas"):
            item[k] = prev.get(k)
        if item["tiene_pronostico"] and item["clave_pick"]:
            res = resolver(item["clave_pick"], info)
            if res == "PUSH":
                item["push"] = True
            elif isinstance(res, bool):
                item["acertado"] = res
        partidos_web.append(item)

    # Un partido terminado no "retrocede" por un dato atrasado de la fuente
    for i, it in enumerate(partidos_web):
        pv = previos_map.get(str(it.get("id")))
        if pv and pv.get("estado") == "FINISHED" and it.get("estado") != "FINISHED" and pv.get("reg"):
            r = dict(pv)
            r["minutos_restantes"] = it.get("minutos_restantes")
            partidos_web[i] = r
            mapa_por_id[r["id"]] = {"id": r["id"], "estado": "FINISHED", "reg": r.get("reg"),
                                    "ganador": r.get("ganador"), "marcador": r.get("marcador")}

    # PREVIA DE LA NOCHE: a la hora fijada, o antes si el primer partido empieza en menos de 2 h
    for j in sorted({p["jornada"] for p in partidos_web}):
        if j in previas_enviadas or j < jornada_actual.isoformat():
            continue
        de_la_noche = [p for p in partidos_web if p["jornada"] == j and p["estado"] == "TIMED" and p.get("minutos_restantes", 0) > 5]
        if not de_la_noche:
            continue
        primero = min(p["minutos_restantes"] for p in de_la_noche)
        es_hora = j == jornada_actual.isoformat() and hora_int >= PREVIA_HORA
        if not (es_hora or primero <= 120):
            continue
        if not fuerzas:
            print(f"⏳ Previa {j}: el modelo aún no tiene datos suficientes")
            continue
        candidatos = []
        for p in de_la_noche:
            p["analizado"] = True
            if not p.get("probs_previa"):
                p["nota_cuotas"] = "Sin datos suficientes de algún equipo para el modelo"
                continue
            pk = elegir_pick({"probs": p["probs_previa"]}, p["local_corto"], p["visitante_corto"])
            if not pk:
                p["nota_cuotas"] = "El modelo no ve un pronóstico claro (ningún mercado entre 60 % y 85 %)"
                continue
            p.update({"tiene_pronostico": True, "mercado": pk["mercado"], "tipo_mercado": pk["tipo_mercado"],
                      "categoria_visual": pk["categoria_visual"], "clave_pick": pk["clave"], "cuota": pk["cuota"],
                      "cuota_justa": pk["cuota_justa"], "prob_modelo": pk["prob_modelo"], "ev": f"+{pk['ev']}%",
                      "ev_numero": pk["ev"], "stake": pk["stake"],
                      "nota_cuotas": f"Pronóstico del modelo: {pk['prob_modelo']} · cuota justa @{pk['cuota_justa']:.2f} · mínima @{pk['cuota']:.2f}"})
            candidatos.append((pk["prob"], p))
        # A Telegram, los más seguros según el modelo (tope por noche); el resto queda como Control en la app
        for _, p in sorted(candidatos, key=lambda x: -x[0])[:PICKS_MAX_DIA]:
            p["telegram_enviado"] = True
            alertas.append({
                "id_partido": p["id"], "id_alerta": f"{p['id']}_{p['clave_pick']}", "partido": f"{p['local']} vs {p['visitante']}",
                "local": p["local"], "visitante": p["visitante"], "escudo_local": p["escudo_local"], "escudo_visitante": p["escudo_visitante"],
                "mercado": p["mercado"], "tipo_mercado": p["tipo_mercado"], "categoria_visual": p["categoria_visual"],
                "clave_pick": p["clave_pick"], "cuota_justa": p["cuota_justa"], "cuota_minima": True, "solo_modelo": True,
                "cuotas_reales": False, "prob_modelo": p["prob_modelo"], "liga": "NHL", "liga_code": "NHL",
                "confirmado_stats": True, "cuota": f"@{p['cuota']:.2f}", "ev": p["ev"], "stake": f"{p['stake']}/10",
                "hora": p["hora"], "fecha": p["fecha"], "fecha_hora_display": p["fecha_hora_display"], "semana_id": semana_actual_id,
                "es_semana_actual": True, "estado_partido": "TIMED", "marcador": None, "marcador_final": None,
                "finalizado": False, "acertado": None, "liquidada_telegram": False})
        orden = sorted(de_la_noche, key=lambda p: p["timestamp_ms"])
        con_modelo = [p for p in orden if p.get("probs_previa")]
        for p in con_modelo:
            p["_probs"] = p["probs_previa"]
        if con_modelo:
            enviada = enviar_previa(date.fromisoformat(j), con_modelo)
            print(f"📨 Previa {j}: {len(con_modelo)} partidos, {sum(1 for p in con_modelo if p.get('telegram_enviado'))} pronósticos "
                  f"({'enviada ✅' if enviada else 'sin Telegram'})")
        for p in con_modelo:
            p.pop("_probs", None)
        previas_enviadas.append(j)

    # LIQUIDACIÓN (por la mañana, al terminar los partidos)
    for a in alertas:
        if a.get("finalizado"):
            continue
        info = mapa_por_id.get(str(a.get("id_partido", "")))
        if not info:
            continue
        a["estado_partido"] = info["estado"]
        if info.get("marcador"):
            a["marcador"] = info["marcador"]
        res = resolver(a.get("clave_pick"), info)
        if res is None:
            continue
        es_push = res == "PUSH"
        a.update({"finalizado": True, "marcador_final": info.get("marcador") or ("Aplazado" if es_push else None),
                  "acertado": None if es_push else bool(res), "push": es_push})
        if not a.get("liquidada_telegram"):
            cab = f"🏒 <b>{a['partido']}</b>\n📌 <b>Pronóstico:</b> {a['mercado']}\n"
            if es_push:
                cuerpo = "↩️ <b>Resultado:</b> ANULADA (partido aplazado o cancelado)\n\n⚡ <i>Apex Quant Engine</i>"
            elif res:
                cuerpo = (f"✅ <b>Resultado:</b> ACERTADO\n🏁 <b>Marcador final:</b> {a['marcador_final'] or 'Final'}\n"
                          f"💰 <b>Cuota mínima:</b> {a['cuota']} (justa @{float(a.get('cuota_justa') or 0):.2f})\n💡 <b>Stake:</b> {a['stake']}\n\n"
                          f"🎉 <i>¡Verificado y sumado a la efectividad oficial en la App!</i>\n⚡ <i>Apex Quant Engine</i>")
            else:
                cuerpo = (f"❌ <b>Resultado:</b> PERDIDO\n🏁 <b>Marcador final:</b> {a['marcador_final'] or 'Final'}\n"
                          f"💡 <b>Stake:</b> {a['stake']} (control estricto de banca)\n\n⚡ <i>Apex Quant Engine • Disciplina a largo plazo</i>")
            enviar_telegram(cab + cuerpo, url_accion=APP_URL, texto_boton="📱 Ver en la Web App")
            a["liquidada_telegram"] = True

    # RESUMEN DOMINICAL (el lunes por la mañana ya han terminado los partidos de la noche del domingo)
    semana_pasada_id = obtener_identificador_semana(ahora_madrid - timedelta(days=1))
    if dia_semana == 0 and 9 <= hora_int < 14 and resumen_semanal_enviado != semana_pasada_id:
        sem = [a for a in alertas if a.get("semana_id") == semana_pasada_id and a.get("finalizado")]
        r = calcular_resumen_financiero(sem)
        if r["finalizadas"] > 0:
            pct = round(r["acertadas"] / r["finalizadas"] * 100, 1)
            s = "+" if r["unidades_netas"] >= 0 else ""
            enviar_telegram(
                f"📊 <b>CIERRE SEMANAL OFICIAL — APEX QUANT HOCKEY</b> 🏆\n\n"
                f"📅 <b>Semana:</b> {semana_pasada_id} (finalizada)\n"
                f"📈 <b>Pronósticos oficiales:</b> <b>{r['finalizadas']}</b>\n\n"
                f"✅ <b>Acertados:</b> <b>{r['acertadas']}</b> ({pct}%)\n❌ <b>Fallados:</b> <b>{r['falladas']}</b>\n\n"
                f"💰 <b>Rendimiento a cuota mínima:</b> <b>{s}{r['unidades_netas']} unidades</b> · ROI {s}{r['roi_pct']}%\n"
                f"<i>(si cobraste cuotas mejores que la mínima, tu beneficio real es mayor)</i>\n\n⚡ <i>Apex Quant Engine</i>",
                url_accion=APP_URL, texto_boton="📱 Abrir balance completo en la App")
        resumen_semanal_enviado = semana_pasada_id

    # RESUMEN MENSUAL (el día 1 por la mañana, del mes anterior)
    mes_pasado = (ahora_madrid.replace(day=1) - timedelta(days=1))
    mes_pasado_id = mes_pasado.strftime("%Y-%m")
    if ahora_madrid.day == 1 and 9 <= hora_int < 14 and resumen_mensual_enviado != mes_pasado_id:
        todas = list(cargar_json("historico_mensual.json", {}).get(mes_pasado_id, []))
        vistos = {a.get("id_alerta") for a in todas}
        todas += [a for a in alertas if (a.get("fecha") or "").startswith(mes_pasado_id) and a.get("finalizado") and a.get("id_alerta") not in vistos]
        r = calcular_resumen_financiero(todas)
        if r["finalizadas"] > 0:
            meses = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
            pct = round(r["acertadas"] / r["finalizadas"] * 100, 1)
            s = "+" if r["unidades_netas"] >= 0 else ""
            enviar_telegram(
                f"🏆 <b>CIERRE MENSUAL OFICIAL — APEX QUANT HOCKEY</b> 📊\n\n"
                f"📅 <b>Mes:</b> <b>{meses[mes_pasado.month - 1]} {mes_pasado.year}</b>\n"
                f"📈 <b>Pronósticos:</b> <b>{r['finalizadas']}</b>\n"
                f"✅ <b>Acertados:</b> <b>{r['acertadas']}</b> ({pct}%) · ❌ <b>{r['falladas']}</b>\n\n"
                f"💰 <b>Rendimiento a cuota mínima:</b> <b>{s}{r['unidades_netas']} U.</b> · ROI {s}{r['roi_pct']}%\n\n⚡ <i>Apex Quant Engine</i>",
                url_accion=APP_URL, texto_boton="📱 Abrir balance completo en la App")
        resumen_mensual_enviado = mes_pasado_id

    # FUSIÓN Y GUARDADO: se guardan los partidos de esta semana (y la noche del domingo anterior)
    limite = (lunes - timedelta(days=1)).isoformat()
    guardar = [p for p in partidos_web if p.get("jornada", p.get("fecha", "")) >= limite]
    ids = {p["id"] for p in guardar}
    for pp in partidos_previos:
        if pp.get("id") not in ids and pp.get("jornada", pp.get("fecha", "")) >= limite:
            guardar.append(pp)
            ids.add(pp.get("id"))
    actualizar_ledger(alertas, guardar)
    actualizar_calibracion(guardar)
    datos = {
        "version_bot": VERSION, "ultima_actualizacion": hora_madrid_str,
        "ultima_actualizacion_utc": ahora_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "huso_horario": "Europe/Madrid",
        "semana_activa": semana_guardada, "lunes_semana": lunes_semana_str, "domingo_semana": domingo_semana_str,
        "resumen_semanal_enviado_para": resumen_semanal_enviado, "resumen_mensual_enviado_para": resumen_mensual_enviado,
        "previas_enviadas": previas_enviadas, "previa_hora": PREVIA_HORA, "margen_valor": MARGEN_VALOR,
        "metricas_semana": calcular_resumen_financiero([a for a in alertas if a.get("semana_id") == semana_actual_id]),
        "total_partidos_hoy": len(guardar), "partidos": guardar, "api_cuotas_estado": "modelo",
        "telegram_ok": telegram_ok,
        "diagnostico": {"fuentes": DIAG, "historial_partidos": len(eq["partidos"]), "historial_estado": eq["estado"],
                        "media_goles_local_60": round(fuerzas["mu_h"], 2) if fuerzas else None,
                        "media_goles_visit_60": round(fuerzas["mu_a"], 2) if fuerzas else None},
        "alertas": alertas[-70:],
    }
    guardar_json("datos.json", datos, compacto=True)
    print(f"📁 datos.json guardado. Partidos: {len(guardar)} | Alertas: {len(alertas)}")


if __name__ == "__main__":
    ejecutar_ciclo()
