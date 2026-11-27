# ==============================================================================
# GEX MONITOR v16.4
# Lógica de microestructura, cálculo de Gamma y envío a Telegram.
# Refactorizado para buenas prácticas (Clean Code).
# ==============================================================================
import os
import re
import sys
import time
from datetime import datetime, date, timedelta
from statistics import NormalDist

import numpy as np
import pandas as pd
import requests
from dateutil.easter import easter
from curl_cffi import requests as cr

# ==============================================================================
# CONFIGURACIÓN GENERAL
# ==============================================================================
TICKERS = ["SPX", "SPY", "QQQ", "GLD", "USO", "IBIT"]
TOKEN = "8624586813:AAE3Vvh-ic3WIddMJI-b4X2QF6Ii8AP2VD4"
CHAT = "@gex_monitor"
TZ = "America/New_York"

OCC_REGEX = re.compile(r"^([A-Z]{1,6}\d?)(\d{6})([CP])(\d{8})$")
ROOTS = {"SPX": {"SPX", "SPXW"}}
SESSION = cr.Session(impersonate="chrome")
INV_NORMAL = NormalDist().inv_cdf

# ==============================================================================
# UTILIDADES Y CALENDARIO
# ==============================================================================
def log(msg):
    print(f"[{pd.Timestamp.now(tz=TZ):%H:%M:%S} NY] {msg}", flush=True)

def obtener_nth_dia_semana(year, month, weekday, n):
    """Retorna el enésimo día de la semana de un mes (ej. 3er lunes de enero)."""
    if n > 0:
        primer_dia = date(year, month, 1)
        return primer_dia + timedelta(days=(weekday - primer_dia.weekday()) % 7 + 7 * (n - 1))
    ultimo_dia = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return ultimo_dia - timedelta(days=(ultimo_dia.weekday() - weekday) % 7)

def obtener_dia_observado(d):
    """Ajusta feriados que caen en fin de semana al viernes o lunes."""
    if d.weekday() == 5: return d - timedelta(days=1)
    if d.weekday() == 6: return d + timedelta(days=1)
    return d

def es_dia_habil(d):
    """Verifica si una fecha es día hábil en la NYSE."""
    y = d.year
    feriados = {
        obtener_nth_dia_semana(y, 1, 0, 3),   # MLK
        obtener_nth_dia_semana(y, 2, 0, 3),   # Washington
        easter(y) - timedelta(days=2),        # Good Friday
        obtener_nth_dia_semana(y, 5, 0, -1),  # Memorial
        obtener_dia_observado(date(y, 7, 4)), # Independence
        obtener_nth_dia_semana(y, 9, 0, 1),   # Labor
        obtener_nth_dia_semana(y, 11, 3, 4),  # Thanksgiving
        obtener_dia_observado(date(y, 12, 25)),# Christmas
        obtener_dia_observado(date(y, 6, 19)) # Juneteenth
    }
    año_nuevo = date(y, 1, 1)
    if año_nuevo.weekday() == 6: feriados.add(date(y, 1, 2))
    elif año_nuevo.weekday() != 5: feriados.add(año_nuevo)
    
    return d.weekday() < 5 and d not in feriados

def obtener_sesion(now):
    """Retorna la hora de apertura y cierre de la sesión para la fecha dada."""
    d = now.date()
    if not es_dia_habil(d): return None
    
    # Cierres tempranos (13:00 NY)
    es_cierre_temprano = d in (
        obtener_nth_dia_semana(d.year, 11, 3, 4) + timedelta(days=1), 
        date(d.year, 7, 3), 
        date(d.year, 12, 24)
    )
    hora_cierre = 13 if es_cierre_temprano else 16
    
    apertura = pd.Timestamp(datetime(d.year, d.month, d.day, 9, 30), tz=TZ)
    cierre = pd.Timestamp(datetime(d.year, d.month, d.day, hora_cierre), tz=TZ)
    return apertura, cierre

def calcular_dte(hoy, exp):
    """Calcula los días hábiles restantes hasta el vencimiento."""
    return sum(es_dia_habil(hoy + timedelta(days=i)) for i in range(1, (exp - hoy).days + 1))

# ==============================================================================
# MATEMÁTICAS Y FORMATEO
# ==============================================================================
def phi(x): 
    """Función de densidad de probabilidad normal estándar."""
    return np.exp(-0.5 * x * x) / np.sqrt(2 * np.pi)

def formatear_cifra(x):
    """Formatea números grandes en Billones (B), Millones (M) o Miles (K)."""
    a = abs(x)
    if a >= 1e9: return f"{x / 1e9:+.2f}B"
    if a >= 1e6: return f"{x / 1e6:+.1f}M"
    if a >= 1e3: return f"{x / 1e3:+.0f}K"
    return f"{x:+.0f}"

def parsear_float(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None

# ==============================================================================
# DESCARGA Y CALIBRACIÓN
# ==============================================================================
def descargar_cadena(ticker):
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{'_SPX' if ticker == 'SPX' else ticker}.json"
    for i in range(3):
        try:
            r = SESSION.get(url, headers={"Origin": "https://www.cboe.com", "Referer": "https://www.cboe.com/"}, timeout=30)
            r.raise_for_status()
            payload = r.json()
            payload["data"]["current_price"]
            payload["data"]["options"]
            return payload
        except Exception as e:
            error = e
            time.sleep(2 * (i + 1))
    log(f"Error descargando {ticker}: {error}")
    return None

def calibrar_tiempo(s, iv, g, d, c):
    """Calibra el tiempo (T) usando la relación entre Gamma, Delta e IV de Cboe."""
    ok = np.isfinite(g) & np.isfinite(d) & (g > 0) & np.where(c, (d > 0) & (d < 1), (d < 0) & (d > -1))
    if ok.sum() < 3: return None
    iv, g, d, c = iv[ok], g[ok], d[ok], c[ok]
    top = g >= 0.5 * g.max()
    d1 = np.array([INV_NORMAL(x if k else x + 1.0) for x, k in zip(d[top], c[top])])
    t = (phi(d1) / (s * iv[top] * g[top])) ** 2
    t = t[np.isfinite(t) & (t > 0)]
    return float(np.median(t)) if len(t) else None

def calcular_iv_otm(K, G, SG, IV, s):
    """Selecciona la IV del lado Out-The-Money para cada strike."""
    ps = {}
    for k, g, sg, iv in zip(K, G, SG, IV): 
        ps.setdefault((k, g), {})[sg > 0] = iv
    return np.array([
        ps[(k, g)][k >= s] if len(ps[(k, g)]) == 2 else ps[(k, g)][sg > 0] 
        for k, g, sg in zip(K, G, SG)
    ])

# ==============================================================================
# LÓGICA PRINCIPAL DE CÁLCULO
# ==============================================================================
def calcular_gex(ticker, now, payload=None):
    payload = descargar_cadena(ticker) if payload is None else payload
    if payload is None: return None
    
    spot = parsear_float(payload["data"]["current_price"])
    opciones = payload["data"]["options"]
    if not spot or spot <= 0 or not opciones: return None
    
    hoy = now.date()
    sesion = obtener_sesion(now)
    min_al_cierre = (sesion[1] - now).total_seconds() / 60 if sesion and now < sesion[1] else None
    fraccion_dia = 1.0 if min_al_cierre is None else min(max(min_al_cierre, 2.0) / 390, 1.0)
    
    contratos = []
    for opt in opciones:
        m = OCC_REGEX.match(str(opt.get("option", "")))
        if not m or m[1] not in ROOTS.get(ticker, {ticker}): continue
        
        exp = datetime.strptime(m[2], "%y%m%d").date()
        strike = int(m[4]) / 1000
        if exp < hoy or (exp == hoy and min_al_cierre is None) or abs(strike / spot - 1) > 0.1: continue
        
        iv = parsear_float(opt.get("iv"))
        oi = parsear_float(opt.get("open_interest"))
        g = parsear_float(opt.get("gamma"))
        d = parsear_float(opt.get("delta"))
        
        if iv is None or oi is None or not 0 < iv <= 3 or oi <= 0: continue
        
        contratos.append((
            exp, m[1], calcular_dte(hoy, exp), m[3] == "C", strike, iv, oi, 
            np.nan if g is None else g, np.nan if d is None else d, opt.get("last_trade_time")
        ))
        
    if not contratos: return None
    
    dias_vencimiento = sorted({c[2] for c in contratos})
    elegidos = [x for x in dias_vencimiento if x <= 1] or [min(dias_vencimiento)]
    
    aviso_0dte = False
    if 0 in elegidos and len(elegidos) > 1 and min_al_cierre is not None and min_al_cierre < 45:
        elegidos.remove(0)
        aviso_0dte = True
        
    seleccionados = [c for c in contratos if c[2] in elegidos]
    expiraciones, raices, dtes, es_call, strikes, ivs, ois, gammas, deltas, tiempos_trade = zip(*seleccionados)
    
    if not any(es_call) or all(es_call): return None
    
    K = np.array(strikes)
    IV = np.array(ivs)
    OI = np.array(ois)
    G = np.array(gammas)
    D = np.array(deltas)
    SG = np.where(es_call, 1.0, -1.0)
    
    # Agrupar por (fecha vencimiento, raíz)
    grupos_dict = {g: i for i, g in enumerate(sorted({(e.toordinal(), r) for e, r in zip(expiraciones, raices)}))}
    GR = np.array([grupos_dict[(e.toordinal(), r)] for e, r in zip(expiraciones, raices)])
    
    IVU = calcular_iv_otm(K, GR, SG, IV, spot)
    
    tiempos = {}
    for gi in np.unique(GR):
        mask = GR == gi
        t = calibrar_tiempo(spot, IVU[mask], G[mask], D[mask], SG[mask] > 0)
        tiempos[gi] = t if t is not None and 1e-6 < t < 0.1 else (fraccion_dia + int(dtes[mask][0])) / 252
        
    S_ = IVU * np.sqrt(np.array([tiempos[g] for g in GR]))
    if not (np.isfinite(S_).all() and (S_ > 0).all()): return None
    
    def modelo_gamma(x):
        x_arr = np.asarray(x, float)
        d1 = np.log(x_arr[:, None] / K[None, :]) / S_[None, :] + 0.5 * S_[None, :]
        return phi(d1) / (x_arr[:, None] * S_[None, :])
        
    def curva_gex(x):
        x_arr = np.asarray(x, float)
        return (modelo_gamma(x) * (OI * SG * 100)[None, :]).sum(1) * x_arr ** 2 * 0.01

    gex_individual = modelo_gamma([spot])[0] * OI * SG * (100 * spot ** 2 * 0.01)
    net_gex = float(gex_individual.sum())
    gex_bruto = float(np.abs(gex_individual).sum())
    
    strikes_unicos = np.unique(K)
    indices_k = np.searchsorted(strikes_unicos, K)
    gex_por_strike = np.zeros(len(strikes_unicos))
    np.add.at(gex_por_strike, indices_k, gex_individual)
    
    umbral = 2.0 * gex_bruto / len(strikes_unicos)
    cw, pw, ncw, npw = None, None, None, None
    
    m_strikes_arriba = strikes_unicos >= spot
    m_strikes_abajo = strikes_unicos <= spot
    
    if m_strikes_arriba.any():
        i = int(np.flatnonzero(m_strikes_arriba)[np.argmax(gex_por_strike[m_strikes_arriba])])
        if gex_por_strike[i] > umbral: 
            cw, ncw = float(strikes_unicos[i]), float(gex_por_strike[i])
            
    if m_strikes_abajo.any():
        j = int(np.flatnonzero(m_strikes_abajo)[np.argmin(gex_por_strike[m_strikes_abajo])])
        if gex_por_strike[j] < -umbral: 
            pw, npw = float(strikes_unicos[j]), float(gex_por_strike[j])
            
    flip = None
    if len(strikes_unicos) > 1:
        v = curva_gex(strikes_unicos)
        cruces = []
        for i in np.where(v[:-1] * v[1:] <= 0)[0]:
            lo, hi, f_lo = strikes_unicos[i], strikes_unicos[i + 1], v[i]
            for _ in range(50):
                mid = (lo + hi) / 2
                f_mid = curva_gex([mid])[0]
                if f_mid * f_lo > 0: 
                    lo, f_lo = mid, f_mid
                else: 
                    hi = mid
            cruces.append((lo + hi) / 2)
        if cruces: 
            flip = float(min(cruces, key=lambda x: abs(x - spot)))
            
    trades_validos = [t for t in tiempos_trade if isinstance(t, str) and t.strip()]
    ts_dato = pd.Timestamp(max(trades_validos)).tz_localize(TZ) if trades_validos else None
    retraso = (now - ts_dato).total_seconds() / 60 if ts_dato is not None else None
    fresco = retraso is not None and -2 <= retraso <= 30 and (sesion is None or ts_dato >= sesion[0])
    
    rango_dte = f"{min(elegidos)}" if min(elegidos) == max(elegidos) else f"{min(elegidos)}-{max(elegidos)}"
    
    return {
        "ticker": ticker, "dte": rango_dte, "spot": spot, 
        "cw": cw, "pw": pw, "ncw": ncw, "npw": npw,
        "flip": flip, "net": net_gex, 
        "sesgo": net_gex / gex_bruto if gex_bruto else 0.0, 
        "ts": ts_dato, "ret": retraso, "fresco": fresco, "aviso": aviso_0dte
    }

# ==============================================================================
# FORMATEO DEL MENSAJE Y TELEGRAM
# ==============================================================================
def formatear_bloque(r):
    s0, s = r["spot"], r["sesgo"]
    
    def fmt_nivel(x, n=None):
        if x is None: return "N/A"
        base = f"${x:.2f} ({(x / s0 - 1) * 100:+.2f}%)"
        return base + (f" · {formatear_cifra(n)}" if n is not None else "")
        
    niveles = [
        (r["cw"], 3, f"🟩 Call wall: {fmt_nivel(r['cw'], r['ncw'])}"), 
        (s0, 2, f"🟧 Spot: ${s0:.2f}"),
        (r["flip"], 1, f"🟦 Flip: {fmt_nivel(r['flip'])}"), 
        (r["pw"], 0, f"🟥 Put wall: {fmt_nivel(r['pw'], r['npw'])}")
    ]
    niveles.sort(key=lambda x: (x[0] is not None, x[0] or 0, x[1]), reverse=True)
    
    gamma_txt = "⚪ Gamma neutro" if abs(s) < 0.05 else ("🟢 Gamma positivo" if s > 0 else "🔴 Gamma negativo")
    
    ts_txt = f" · dato {r['ts']:%H:%M}" if r["ts"] is not None else ""
    lineas = [
        f"<b>Ticker: {r['ticker']}</b> · DTE {r['dte']}{ts_txt}",
        ""
    ]
    lineas += [x[2] for x in niveles] 
    lineas += [
        f"Net GEX ($/1%): {formatear_cifra(r['net'])} (sesgo {s * 100:+.0f}%)",
        gamma_txt
    ]
    
    if r["aviso"]: lineas.append("⏳ 0DTE excluido (cierre cercano)")
    if not r["fresco"]: lineas.append("⚠️ Dato no actualizado")
    
    return "\n".join(lineas) + "\n"

def enviar_telegram(txt):
    for i in range(3):
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
            res = requests.post(url, json={"chat_id": CHAT, "text": txt[:4000], "parse_mode": "HTML"}, timeout=20)
            res.raise_for_status()
            return True
        except requests.RequestException as e:
            log(f"Error Telegram: {e}")
            time.sleep(2 * (i + 1))
    return False

# ==============================================================================
# EJECUCIÓN PRINCIPAL
# ==============================================================================
def main():
    forzar = "--forzar" in sys.argv
    now = pd.Timestamp.now(tz=TZ)
    sesion = obtener_sesion(now)
    
    if not forzar and not (sesion and sesion[0] <= now < sesion[1]):
        log("Fuera de sesión NY. Se omite envío.")
        return
        
    resultados, sin_datos = [], []
    
    for ticker in TICKERS:
        try: 
            r = calcular_gex(ticker, now)
        except Exception as e: 
            log(f"Fallo {ticker}: {e!r}")
            r = None
            
        if r: resultados.append(r)
        else: sin_datos.append(ticker)
        time.sleep(1.5)
        
    if not forzar:
        sin_datos += [r["ticker"] for r in resultados if not r["fresco"]]
        resultados = [r for r in resultados if r["fresco"]]
        
    if not resultados: 
        log("Sin datos válidos.")
        sys.exit(1 if forzar else 0)
        
    txt = f"<b>GEX Monitor v16.4</b>\nDía: {now:%Y-%m-%d} · Envío: {now:%H:%M} NY\n\n"
    txt += "\n".join(formatear_bloque(r) for r in resultados)
    
    if sin_datos: 
        txt += f"\n⚠️ Sin datos: {', '.join(sin_datos)}"
        
    if not enviar_telegram(txt): 
        sys.exit(1)

if __name__ == "__main__":
    main()
