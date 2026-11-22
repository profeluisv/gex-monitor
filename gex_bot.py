# GEX MONITOR - GitHub Actions + Telegram (Integración V12 - Net GEX en Muros)
import re
import sys
import time
import os
from datetime import datetime, date, timedelta
from statistics import NormalDist

import numpy as np
import pandas as pd
import requests
from curl_cffi import requests as cffi_requests

# Configuración
TICKERS = ["SPY", "QQQ", "GLD", "USO", "IBIT"]
TARGET_DTE = 1
# SEGURIDAD: Usar variable de entorno en GitHub, con fallback para pruebas locales
TOKEN = os.environ.get("TELEGRAM_TOKEN", "8624586813:AAE3Vvh-ic3WIddMJI-b4X2QF6Ii8AP2VD4")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "@gex_monitor")
ENVIAR_SOLO_EN_SESION = True

NYSE_TZ = "America/New_York"
MULT_CONTRATO = 100
PCT_MOV = 0.01
OCC_REGEX = re.compile(r"^(?P<root>[A-Z]{1,6}\d?)(?P<date>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.cboe.com",
    "Referer": "https://www.cboe.com/",
}
_SESSION = cffi_requests.Session(impersonate="chrome")
_INV = NormalDist().inv_cdf
_FERIADOS_CACHE = {}

def log(msg):
    ahora = pd.Timestamp.now(tz=NYSE_TZ)
    print(f"[{ahora:%Y-%m-%d %H:%M:%S} NY] {msg}", flush=True)

def _nth_weekday(year, month, weekday, n):
    if n > 0:
        d = date(year, month, 1)
        return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))
    last = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return last - timedelta(days=(last.weekday() - weekday) % 7)

def _observed(d):
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d

def _good_friday(y):
    a = y % 19
    b, c = divmod(y, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(y, month, day) - timedelta(days=2)

def _nyse_feriados(y):
    if y in _FERIADOS_CACHE:
        return _FERIADOS_CACHE[y]
    nyd = date(y, 1, 1)
    fer = set()
    if nyd.weekday() == 6:
        fer.add(date(y, 1, 2))
    elif nyd.weekday() != 5:
        fer.add(nyd)
    fer.add(_nth_weekday(y, 1, 0, 3))
    fer.add(_nth_weekday(y, 2, 0, 3))
    fer.add(_good_friday(y))
    fer.add(_nth_weekday(y, 5, 0, -1))
    if y >= 2022:
        fer.add(_observed(date(y, 6, 19)))
    fer.add(_observed(date(y, 7, 4)))
    fer.add(_nth_weekday(y, 9, 0, 1))
    fer.add(_nth_weekday(y, 11, 3, 4))
    fer.add(_observed(date(y, 12, 25)))
    _FERIADOS_CACHE[y] = fer
    return fer

def _es_dia_nyse(d):
    return d.weekday() < 5 and d not in _nyse_feriados(d.year)

def _cierre_nyse_hora(d):
    tg = _nth_weekday(d.year, 11, 3, 4) + timedelta(days=1)
    if d == tg or d in (date(d.year, 7, 3), date(d.year, 12, 24)):
        return 13, 0
    return 16, 0

def _nyse_valid_days_count(start_date, end_date):
    cur = start_date
    cnt = 0
    while cur <= end_date:
        if _es_dia_nyse(cur):
            cnt += 1
        cur += timedelta(days=1)
    return cnt

def _phi(x):
    return np.exp(-0.5 * x * x) / np.sqrt(2.0 * np.pi)

def descargar_cadena(ticker, reintentos=3):
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{ticker}.json"
    ultimo_error = None
    for i in range(reintentos):
        try:
            r = _SESSION.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 403:
                raise ValueError(f"403 Forbidden ({r.url})")
            r.raise_for_status()
            payload = r.json()
            payload["data"]["current_price"]
            payload["data"]["options"]
            return payload
        except (cffi_requests.RequestsError, ValueError, KeyError, TypeError) as e:
            ultimo_error = e
            time.sleep(2 * (i + 1))
    log(f"Error con {ticker}: {ultimo_error}")
    return None

def _num(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None

def horario_hoy(ahora):
    ahora = pd.Timestamp(ahora)
    if ahora.tzinfo is None:
        ahora = ahora.tz_localize(NYSE_TZ)
    dia = ahora.tz_convert(NYSE_TZ).date()
    if not _es_dia_nyse(dia):
        return ahora, None, None
    ch, cm = _cierre_nyse_hora(dia)
    abre = pd.Timestamp(datetime(dia.year, dia.month, dia.day, 9, 30), tz=NYSE_TZ)
    cierra = pd.Timestamp(datetime(dia.year, dia.month, dia.day, ch, cm), tz=NYSE_TZ)
    return ahora, abre, cierra

def estado_sesion(ahora):
    ahora, abre, cierra = horario_hoy(ahora)
    if abre is None:
        return "cierre"
    if ahora < abre:
        return "pre"
    if ahora < cierra:
        return "sesion"
    return "cierre"

def calcular_gex(ticker, target_dte=TARGET_DTE, payload=None, ahora=None):
    ticker = ticker.upper()
    if payload is None:
        payload = descargar_cadena(ticker)
        if payload is None:
            return None

    spot = _num(payload["data"]["current_price"])
    options = payload["data"]["options"]
    if spot is None or spot <= 0 or not options:
        log(f"Datos de Cboe inválidos para {ticker}.")
        return None

    ahora = ahora if ahora is not None else pd.Timestamp.now(tz=NYSE_TZ)
    hoy = ahora.date()

    exp_cache = {}
    parsed = []
    for opt in options:
        m = OCC_REGEX.match(str(opt.get("option", "")))
        if not m or m.group("root") != ticker:
            continue
        try:
            exp = datetime.strptime(m.group("date"), "%y%m%d").date()
        except ValueError:
            continue
        if exp < hoy:
            continue
        if exp not in exp_cache:
            exp_cache[exp] = max(0, _nyse_valid_days_count(hoy, exp) - 1)
        dte = exp_cache[exp]

        g = _num(opt.get("gamma"))
        d = _num(opt.get("delta"))
        iv = _num(opt.get("iv"))
        oi = _num(opt.get("open_interest"))
        if None in (g, d, iv, oi) or g <= 0 or iv <= 0 or oi <= 0:
            continue

        es_call = m.group("cp") == "C"
        if es_call and not (0 < d < 1):
            continue
        if not es_call and not (-1 < d < 0):
            continue

        parsed.append({
            "exp": exp, "dte": dte, "call": es_call,
            "strike": int(m.group("strike")) / 1000.0,
            "iv": iv, "oi": oi, "gamma": g,
            "d1": _INV(d if es_call else d + 1.0),
        })

    if not parsed:
        log(f"No hay contratos válidos para {ticker}.")
        return None

    dtes = sorted({p["dte"] for p in parsed})
    elegidos = [x for x in dtes if x <= target_dte]
    if not elegidos:
        elegidos = [min(dtes, key=lambda x: (abs(x - target_dte), x))]
    sel = [p for p in parsed if p["dte"] in elegidos]

    if not any(p["call"] for p in sel) or all(p["call"] for p in sel):
        log(f"Datos incoherentes (calls/puts) para {ticker}.")
        return None

    dte_min = min(p["dte"] for p in sel)
    dte_max = max(p["dte"] for p in sel)
    rango_dte = f"{dte_min}" if dte_min == dte_max else f"{dte_min}-{dte_max}"

    K = np.array([p["strike"] for p in sel])
    IV = np.array([p["iv"] for p in sel])
    OI = np.array([p["oi"] for p in sel])
    G = np.array([p["gamma"] for p in sel])
    D1 = np.array([p["d1"] for p in sel])
    SG = np.array([1.0 if p["call"] else -1.0 for p in sel])
    EXP = np.array([p["exp"].toordinal() for p in sel])

    if np.isnan(IV).any() or np.isinf(IV).any() or np.isnan(G).any() or np.isinf(G).any():
        log(f"Datos corruptos (NaN/Inf) en {ticker}.")
        return None

    ti_chk = (_phi(D1) / (spot * IV * G)) ** 2
    ok = np.isfinite(D1) & np.isfinite(ti_chk) & (ti_chk > 0)
    if not ok.all():
        K, IV, OI, G, D1, SG, EXP = (a[ok] for a in (K, IV, OI, G, D1, SG, EXP))
        if len(K) == 0 or not (SG > 0).any() or not (SG < 0).any():
            log(f"Datos insuficientes tras depuración para {ticker}.")
            return None

    k_usd = MULT_CONTRATO * spot ** 2 * PCT_MOV
    net_gex = float((G * OI * SG * k_usd).sum())

    ks = np.unique(K)
    gex_i = G * OI * SG * k_usd
    idx_k = np.searchsorted(ks, K)
    es_c = SG > 0
    gex_k_call = np.zeros(len(ks))
    gex_k_put = np.zeros(len(ks))
    np.add.at(gex_k_call, idx_k[es_c], gex_i[es_c])
    np.add.at(gex_k_put, idx_k[~es_c], gex_i[~es_c])

    gc = gex_k_call
    gp = -gex_k_put
    call_wall = None
    put_wall = None

    if gc.max() > 0 and gp.max() > 0:
        ic = int(np.argmax(gc))
        ip = int(np.argmax(gp))
        if ks[ic] > ks[ip]:
            call_wall, put_wall = float(ks[ic]), float(ks[ip])
        else:
            mejor = None
            m_p = ks < ks[ic]
            if m_p.any() and gp[m_p].max() > 0:
                j = int(np.flatnonzero(m_p)[np.argmax(gp[m_p])])
                mejor = (gc[ic] + gp[j], float(ks[ic]), float(ks[j]))
            m_c = ks > ks[ip]
            if m_c.any() and gc[m_c].max() > 0:
                j = int(np.flatnonzero(m_c)[np.argmax(gc[m_c])])
                cand = (gc[j] + gp[ip], float(ks[j]), float(ks[ip]))
                if mejor is None or cand[0] > mejor[0]:
                    mejor = cand
            if mejor is not None:
                call_wall, put_wall = mejor[1], mejor[2]
    else:
        call_wall = float(ks[np.argmax(gc)]) if gc.max() > 0 else None
        put_wall = float(ks[np.argmax(gp)]) if gp.max() > 0 else None

    # ==========================================
    # NUEVO: CÁLCULO DEL NET GEX EN LOS MUROS
    # ==========================================
    net_gex_cw = None
    net_gex_pw = None
    
    if call_wall is not None:
        idx_cw = np.where(ks == call_wall)[0][0]
        net_gex_cw = gc[idx_cw] - gp[idx_cw]
        
    if put_wall is not None:
        idx_pw = np.where(ks == put_wall)[0][0]
        net_gex_pw = gc[idx_pw] - gp[idx_pw]

    t_i = (_phi(D1) / (spot * IV * G)) ** 2
    S_ = np.zeros(len(K))
    escala = np.ones(len(K))
    for e in np.unique(EXP):
        mk = EXP == e
        S_[mk] = IV[mk] * np.sqrt(np.median(t_i[mk]))
        g_mod = _phi(D1[mk]) / (spot * S_[mk])
        den = (g_mod * OI[mk]).sum()
        escala[mk] = (G[mk] * OI[mk]).sum() / den if den > 0 else 1.0

    def curva(precios):
        precios = np.asarray(precios, dtype=float)
        d1n = D1[None, :] + np.log(precios[:, None] / spot) / S_[None, :]
        g = _phi(d1n) / (precios[:, None] * S_[None, :])
        return (g * (OI * SG * escala * MULT_CONTRATO)[None, :]).sum(axis=1) * precios ** 2 * PCT_MOV

    flip = None
    if len(ks) > 1:
        v = curva(ks)
        a, b = v[:-1], v[1:]
        idx = np.where(((a < 0) & (b >= 0)) | ((a > 0) & (b <= 0)))[0]
        raices = []
        for i in idx:
            lo, hi, flo = ks[i], ks[i + 1], v[i]
            for _ in range(60):
                mid = 0.5 * (lo + hi)
                fm = curva([mid])[0]
                if fm == 0:
                    lo = hi = mid
                    break
                if fm * flo > 0:
                    lo, flo = mid, fm
                else:
                    hi = mid
            raices.append(0.5 * (lo + hi))
        if raices:
            flip = float(min(raices, key=lambda x: abs(x - spot)))

    return {
        "ticker": ticker, "dte": rango_dte, "spot": spot,
        "call_wall": call_wall, "put_wall": put_wall, "flip": flip,
        "net_gex": net_gex, "net_gex_cw": net_gex_cw, "net_gex_pw": net_gex_pw
    }

def formatear_mensaje(resultados, ahora_ny):
    if not resultados:
        return None
    lineas = [
        "<b>GEX Monitor</b>",
        f"Día: {ahora_ny:%Y-%m-%d}",
        f"Hora: {ahora_ny:%H:%M} NY",
        "",
    ]
    for r in resultados:
        # Formateo del Call Wall con Net GEX
        cw = f"${r['call_wall']:.2f}" if r["call_wall"] is not None else "N/A"
        if r.get("net_gex_cw") is not None:
            cw += f" (Net GEX: {r['net_gex_cw']/1_000_000:+.1f}M)"
            
        # Formateo del Put Wall con Net GEX
        pw = f"${r['put_wall']:.2f}" if r["put_wall"] is not None else "N/A"
        if r.get("net_gex_pw") is not None:
            pw += f" (Net GEX: {r['net_gex_pw']/1_000_000:+.1f}M)"
            
        fl = f"${r['flip']:.2f}" if r["flip"] is not None else "N/A"
        
        # Cambiado a 2 decimales (+.2f) según tu ejemplo
        gex_m = r["net_gex"] / 1_000_000
        
        if r["net_gex"] > 0:
            gamma_txt = "🟢 Gamma positivo"
        elif r["net_gex"] < 0:
            gamma_txt = "🔴 Gamma negativo"
        else:
            gamma_txt = "⚪ Gamma neutro"

        lineas.append(
            f"<b>Ticker: {r['ticker']}</b>\n"
            f"DTE: {r['dte']}\n"
            f"Spot: ${r['spot']:.2f}\n"
            f"Call wall: {cw}\n"
            f"Put wall: {pw}\n"
            f"Flip: {fl}\n"
            f"Net GEX: {gex_m:+.2f}M\n"
            f"{gamma_txt}\n"
        )
    return "\n".join(lineas).strip()

def enviar_telegram(mensaje, reintentos=3):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    for parte in [mensaje[i:i + 4000] for i in range(0, len(mensaje), 4000)]:
        enviado = False
        for i in range(reintentos):
            try:
                r = requests.post(
                    url,
                    json={"chat_id": CHAT_ID, "text": parte, "parse_mode": "HTML", "disable_web_page_preview": True},
                    timeout=20,
                )
                r.raise_for_status()
                enviado = True
                break
            except requests.RequestException as e:
                log(f"Error Telegram (intento {i + 1}): {e}")
                time.sleep(2 * (i + 1))
        if not enviado:
            return False
    return True

def main():
    forzar = "--forzar" in sys.argv
    ahora_ny = pd.Timestamp.now(tz=NYSE_TZ)
    estado = estado_sesion(ahora_ny)

    if ENVIAR_SOLO_EN_SESION and estado != "sesion" and not forzar:
        log(f"Sesión NY en estado '{estado}'. Se omite envío.")
        return

    resultados = []
    for idx, t in enumerate(TICKERS):
        res = calcular_gex(t, TARGET_DTE, ahora=ahora_ny)
        if res is not None:
            resultados.append(res)
        if idx < len(TICKERS) - 1:
            time.sleep(1.5)

    if not resultados:
        log("Sin resultados válidos de Cboe.")
        sys.exit(1)

    mensaje = formatear_mensaje(resultados, ahora_ny)
    if mensaje and enviar_telegram(mensaje):
        log(f"Reporte enviado a {CHAT_ID} ({len(resultados)} tickers).")
    else:
        log("No se pudo enviar el reporte a Telegram.")
        sys.exit(1)

if __name__ == "__main__":
    main()
