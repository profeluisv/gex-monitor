# GEX MONITOR v17 - GitHub Actions + Telegram (SPX dual OI + Volumen)
import re
import sys
import time
from datetime import datetime, date, timedelta
from statistics import NormalDist

import numpy as np
import pandas as pd
import requests
from curl_cffi import requests as cffi_requests

# Configuración
TICKERS = ["SPX", "SPY", "QQQ", "GLD", "USO", "IBIT"]
TARGET_DTE = 1
TOKEN = "8624586813:AAE3Vvh-ic3WIddMJI-b4X2QF6Ii8AP2VD4"
CHAT_ID = "@gex_monitor"
ENVIAR_SOLO_EN_SESION = True

NYSE_TZ = "America/New_York"
MULT_CONTRATO = 100
PCT_MOV = 0.01
DATO_MAX_RETRASO_MIN = 45   # Cboe ya trae ~15 min; más que esto = dato no actualizado
IV_MAX = 3.0
CBOE_SYMBOL = {"SPX": "_SPX"}
CBOE_ROOTS = {"SPX": {"SPX", "SPXW"}}
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

def _fm(x):
    a = abs(x)
    if a >= 1e6:
        return f"{x / 1e6:+.1f}M"
    if a >= 1e3:
        return f"{x / 1e3:+.0f}K"
    return f"{x:+.0f}"

def _txt_retraso(m):
    if m < 120:
        return f"{m:.0f} min"
    if m < 2880:
        return f"{m / 60:.1f} h"
    return f"{m / 1440:.1f} días"

def descargar_cadena(ticker, reintentos=3):
    simbolo = CBOE_SYMBOL.get(ticker.upper(), ticker.upper())
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{simbolo}.json"
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
        except Exception as e:
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

def hora_dato(payload):
    tr = [o.get("last_trade_time") for o in payload["data"]["options"]]
    tr = [t for t in tr if isinstance(t, str) and t.strip()]
    cand = [(max(tr), NYSE_TZ)] if tr else []
    cand += [(payload.get("timestamp"), "UTC"), (payload["data"].get("last_trade_time"), "UTC")]
    for valor, zona in cand:
        try:
            t = pd.Timestamp(valor)
        except (ValueError, TypeError):
            continue
        if pd.isna(t):
            continue
        t = t.tz_localize(zona) if t.tzinfo is None else t
        return t.tz_convert(NYSE_TZ)
    return None

def subyacente_implicito(spot, K, GRP, SG, BID, ASK):
    # Paridad put-call: F = K + C - P (mid). Mediana de 5 strikes cercanos de menor spread.
    q = {}
    for k, g, sg, b, a in zip(K, GRP, SG, BID, ASK):
        if np.isfinite(b) and np.isfinite(a) and b > 0 and a >= b:
            q[(k, g, sg > 0)] = ((a + b) / 2.0, a - b)
    est = []
    for (k, g, call), (mc, sc) in q.items():
        if call and (k, g, False) in q:
            mp, sp = q[(k, g, False)]
            est.append((abs(k - spot), sc + sp, k + mc - mp))
    if len(est) < 3:
        return None
    est = sorted(est, key=lambda x: x[0])[:20]
    cerca = sorted(est, key=lambda x: x[1])[:5]
    return float(np.median([x[2] for x in cerca]))

def calibrar_t(s, iv, g, d, es_call):
    # T implícito en gamma/delta de Cboe, con los contratos de mayor gamma (ATM)
    ok = np.isfinite(g) & np.isfinite(d) & (g > 0)
    ok &= np.where(es_call, (d > 0) & (d < 1), (d < 0) & (d > -1))
    if ok.sum() < 3:
        return None
    iv, g, d, es_call = iv[ok], g[ok], d[ok], es_call[ok]
    top = g >= 0.5 * g.max()
    d1 = np.array([_INV(x if c else x + 1.0) for x, c in zip(d[top], es_call[top])])
    t = (_phi(d1) / (s * iv[top] * g[top])) ** 2
    t = t[np.isfinite(t) & (t > 0)]
    return float(np.median(t)) if len(t) else None

def iv_otm(K, GRP, SG, IV, s_ref):
    # Un solo IV por strike y grupo (fecha, raíz): el del lado OTM.
    por_strike = {}
    for k, g, sg, iv in zip(K, GRP, SG, IV):
        por_strike.setdefault((k, g), {})[sg > 0] = iv
    out = np.empty(len(K))
    for n, (k, g, sg) in enumerate(zip(K, GRP, SG)):
        lados = por_strike[(k, g)]
        if True in lados and False in lados:
            out[n] = lados[True] if k >= s_ref else lados[False]
        else:
            out[n] = lados[sg > 0]
    return out

def _calcular_muros(ks, idx_k, es_c, gex_vec):
    gc, gneg = np.zeros(len(ks)), np.zeros(len(ks))
    np.add.at(gc, idx_k[es_c], gex_vec[es_c])
    np.add.at(gneg, idx_k[~es_c], gex_vec[~es_c])
    gp = -gneg

    call_wall = put_wall = None
    if gc.max() > 0 and gp.max() > 0:
        ic, ip = int(np.argmax(gc)), int(np.argmax(gp))
        if ks[ic] > ks[ip]:
            call_wall, put_wall = float(ks[ic]), float(ks[ip])
        else:  # inversión: pareja Put < Call de mayor GEX total
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

    net_cw = net_pw = None
    if call_wall is not None:
        i = int(np.searchsorted(ks, call_wall))
        net_cw = float(gc[i] - gp[i])
    if put_wall is not None:
        i = int(np.searchsorted(ks, put_wall))
        net_pw = float(gc[i] - gp[i])
    return call_wall, put_wall, net_cw, net_pw, float(gex_vec.sum())

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
    _, abre, _ = horario_hoy(ahora)
    ts_dato = hora_dato(payload)

    raices_validas = CBOE_ROOTS.get(ticker, {ticker})
    exp_cache = {}
    parsed = []
    for opt in options:
        m = OCC_REGEX.match(str(opt.get("option", "")))
        if not m or m.group("root") not in raices_validas:
            continue
        try:
            exp = datetime.strptime(m.group("date"), "%y%m%d").date()
        except ValueError:
            continue
        if exp < hoy:
            continue
        if exp not in exp_cache:
            exp_cache[exp] = max(0, _nyse_valid_days_count(hoy, exp) - 1)

        iv = _num(opt.get("iv"))
        oi_raw = _num(opt.get("open_interest"))
        vol_raw = _num(opt.get("volume"))
        oi = 0.0 if (oi_raw is None or oi_raw < 0) else oi_raw
        vol = 0.0 if (vol_raw is None or vol_raw < 0) else vol_raw
        if iv is None or not (0 < iv <= IV_MAX) or (oi <= 0 and vol <= 0):
            continue
        g, d = _num(opt.get("gamma")), _num(opt.get("delta"))
        b, a = _num(opt.get("bid")), _num(opt.get("ask"))
        parsed.append({
            "exp": exp, "root": m.group("root"), "dte": exp_cache[exp], "call": m.group("cp") == "C",
            "strike": int(m.group("strike")) / 1000.0, "iv": iv, "oi": oi, "vol": vol,
            "g": np.nan if g is None else g, "d": np.nan if d is None else d,
            "bid": np.nan if b is None else b, "ask": np.nan if a is None else a,
        })

    if not parsed:
        log(f"No hay contratos válidos para {ticker}.")
        return None

    dtes = sorted({p["dte"] for p in parsed})
    elegidos = [x for x in dtes if x <= target_dte] or [min(dtes, key=lambda x: (abs(x - target_dte), x))]
    sel = [p for p in parsed if p["dte"] in elegidos]

    if not any(p["call"] for p in sel) or all(p["call"] for p in sel):
        log(f"Datos incoherentes (calls/puts) para {ticker}.")
        return None

    dte_min, dte_max = min(p["dte"] for p in sel), max(p["dte"] for p in sel)
    rango_dte = f"{dte_min}" if dte_min == dte_max else f"{dte_min}-{dte_max}"

    K = np.array([p["strike"] for p in sel])
    IV = np.array([p["iv"] for p in sel])
    OI = np.array([p["oi"] for p in sel])
    VOL = np.array([p["vol"] for p in sel])
    SG = np.array([1.0 if p["call"] else -1.0 for p in sel])
    DTE = np.array([p["dte"] for p in sel])
    GV = np.array([p["g"] for p in sel])
    DV = np.array([p["d"] for p in sel])
    BID = np.array([p["bid"] for p in sel])
    ASK = np.array([p["ask"] for p in sel])

    # Agrupación por (fecha, raíz) para no mezclar SPX AM y SPXW PM en el 3er viernes
    grps = sorted({(p["exp"].toordinal(), p["root"]) for p in sel})
    grp_id = {g: idx for idx, g in enumerate(grps)}
    GRP = np.array([grp_id[(p["exp"].toordinal(), p["root"])] for p in sel])

    # Subyacente implícito en las cotizaciones (el que usa Cboe para IV y griegas)
    s_ref = subyacente_implicito(spot, K, GRP, SG, BID, ASK)
    if s_ref is None or not np.isfinite(s_ref) or abs(s_ref / spot - 1) > 0.05:
        s_ref = spot
    desalineo = s_ref / spot - 1

    # T por grupo (calibrado en ATM) y sigma*sqrt(T) por contrato
    IVU = iv_otm(K, GRP, SG, IV, s_ref)
    T_map = {}
    for g_idx in np.unique(GRP):
        mk = GRP == g_idx
        t = calibrar_t(s_ref, IVU[mk], GV[mk], DV[mk], SG[mk] > 0)
        if t is None or not (1e-6 < t < 0.1):
            t = max(int(DTE[mk][0]), 0.5) / 252.0
        T_map[g_idx] = t
    S_ = IVU * np.sqrt(np.array([T_map[g_idx] for g_idx in GRP]))
    if not (np.isfinite(S_).all() and (S_ > 0).all()):
        log(f"Datos corruptos (IV/T) en {ticker}.")
        return None

    def gamma_modelo(precio):
        precio = np.asarray(precio, dtype=float)
        d1 = np.log(precio[:, None] / K[None, :]) / S_[None, :] + 0.5 * S_[None, :]
        return _phi(d1) / (precio[:, None] * S_[None, :])

    def curva(precios):
        precios = np.asarray(precios, dtype=float)
        return (gamma_modelo(precios) * (OI * SG * MULT_CONTRATO)[None, :]).sum(axis=1) * precios ** 2 * PCT_MOV

    # GEX en USD por cada 1% de movimiento (OI y, en SPX, también Volumen)
    k_usd = MULT_CONTRATO * s_ref ** 2 * PCT_MOV
    gex_base = gamma_modelo([s_ref])[0] * SG * k_usd
    ks = np.unique(K)
    idx_k = np.searchsorted(ks, K)
    es_c = SG > 0

    call_wall, put_wall, net_cw, net_pw, net_gex = _calcular_muros(ks, idx_k, es_c, gex_base * OI)

    vol_data = None
    if ticker == "SPX" and VOL.sum() > 0:
        vcw, vpw, vnet_cw, vnet_pw, vnet_gex = _calcular_muros(ks, idx_k, es_c, gex_base * VOL)
        vol_data = {
            "call_wall": vcw, "put_wall": vpw,
            "net_cw": vnet_cw, "net_pw": vnet_pw, "net_gex": vnet_gex,
        }

    # Gamma Flip (cruce por cero de la curva OI)
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

    # Frescura del dato
    retraso = None
    fresco = False
    if ts_dato is not None:
        retraso = (pd.Timestamp(ahora).tz_convert(NYSE_TZ) - ts_dato).total_seconds() / 60
        fresco = retraso <= DATO_MAX_RETRASO_MIN and (abre is None or ts_dato >= abre)

    return {
        "ticker": ticker, "dte": rango_dte, "spot": spot, "s_ref": s_ref, "desalineo": desalineo,
        "call_wall": call_wall, "put_wall": put_wall, "net_cw": net_cw, "net_pw": net_pw,
        "flip": flip, "net_gex": net_gex, "vol_data": vol_data,
        "ts_dato": ts_dato, "retraso": retraso, "fresco": fresco,
    }

def _fmt_muro(precio, net_muro, es_call):
    if precio is None:
        return "N/A", False
    txt = f"${precio:.2f}"
    disputa = False
    if net_muro is not None:
        txt += f" ({_fm(net_muro)})"
        if (es_call and net_muro < 0) or ((not es_call) and net_muro > 0):
            txt += " ⚠️"
            disputa = True
    return txt, disputa

def _fmt_regimen(net_gex):
    if net_gex > 0:
        return "🟢 Gamma positivo"
    if net_gex < 0:
        return "🔴 Gamma negativo"
    return "⚪ Gamma neutro"

def _ico_regimen(net_gex):
    if net_gex > 0:
        return "🟢"
    if net_gex < 0:
        return "🔴"
    return "⚪"

def formatear_mensaje(resultados, ahora_ny):
    if not resultados:
        return None
    lineas = [
        "<b>GEX Monitor v17</b>",
        f"Día: {ahora_ny:%Y-%m-%d}",
        f"Hora: {ahora_ny:%H:%M} NY",
        "",
    ]
    hay_disputa = False
    for r in resultados:
        cw, disp_cw = _fmt_muro(r["call_wall"], r["net_cw"], True)
        pw, disp_pw = _fmt_muro(r["put_wall"], r["net_pw"], False)
        fl = f"${r['flip']:.2f}" if r["flip"] is not None else "N/A"
        hay_disputa = hay_disputa or disp_cw or disp_pw

        if r["ticker"] == "SPX":
            vd = r.get("vol_data")
            if vd is not None:
                vcw, d_vcw = _fmt_muro(vd["call_wall"], vd["net_cw"], True)
                vpw, d_vpw = _fmt_muro(vd["put_wall"], vd["net_pw"], False)
                vnet_txt = f"{_fm(vd['net_gex'])} {_ico_regimen(vd['net_gex'])}"
                hay_disputa = hay_disputa or d_vcw or d_vpw
            else:
                vcw = vpw = vnet_txt = "N/A"

            bloque = (
                f"<b>Ticker: {r['ticker']}</b>\n"
                f"DTE: {r['dte']}\n"
                f"Spot: ${r['spot']:.2f}\n"
                f"🟩 Call wall:\n"
                f"OI: {cw}\n"
                f"Volumen: {vcw}\n"
                f"🟥 Put wall:\n"
                f"OI: {pw}\n"
                f"Volumen: {vpw}\n"
                f"Flip: {fl}\n"
                f"Net GEX:\n"
                f"OI: {_fm(r['net_gex'])} {_ico_regimen(r['net_gex'])}\n"
                f"Volumen: {vnet_txt}\n"
            )
        else:
            bloque = (
                f"<b>Ticker: {r['ticker']}</b>\n"
                f"DTE: {r['dte']}\n"
                f"Spot: ${r['spot']:.2f}\n"
                f"Call wall: {cw}\n"
                f"Put wall: {pw}\n"
                f"Flip: {fl}\n"
                f"Net GEX: {_fm(r['net_gex'])}\n"
                f"{_fmt_regimen(r['net_gex'])}\n"
            )

        if not r["fresco"]:
            ret = _txt_retraso(r["retraso"]) if r["retraso"] is not None else "?"
            bloque += f"⚠️ Dato no actualizado (retraso {ret})\n"
        lineas.append(bloque)
    if hay_disputa:
        lineas.append("⚠️ Muro disputado: en ese strike domina el lado contrario.")
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
            resultados.ap
