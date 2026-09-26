#!/usr/bin/env python3
"""
RADAR GLOBAL — screener cross-asset semanal.

Mide cada instrumento contra su propia normalidad (z-score de 3 años), detecta
extremos, revisa qué pasó históricamente tras extremos similares, cruza con el
calendario electoral y publica:
  - docs/index.html          (página web, GitHub Pages)
  - docs/archivo/FECHA.html  (histórico de reportes)
  - data/historial.csv       (z-scores de cada corrida)
  - correo HTML con el resumen (si hay credenciales)

Uso:
  python screener.py            # datos reales (Yahoo Finance + FRED)
  python screener.py --demo     # datos simulados, para probar sin internet
  python screener.py --no-email
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import io
import json
import math
import os
import smtplib
import ssl
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
ARCHIVO = DOCS / "archivo"
DATA = ROOT / "data"
TZ = ZoneInfo("America/Bogota")
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]

NIVEL_ORDEN = {"extremo": 3, "fuerte": 2, "aviso": 1, None: 0}
NIVEL_TXT = {"extremo": "Extremo", "fuerte": "Fuerte", "aviso": "Aviso"}


def log(msg: str) -> None:
    print(f"[radar] {msg}", flush=True)


def fecha_es(d) -> str:
    d = pd.Timestamp(d)
    return f"{d.day}-{MESES[d.month - 1]}-{d.year}"


# =============================================================================
# 1. Configuración
# =============================================================================
def cargar_config():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    elec_path = ROOT / "elecciones.yaml"
    elec = yaml.safe_load(elec_path.read_text(encoding="utf-8")) if elec_path.exists() else {}
    instrumentos = []
    for b in cfg["bloques"]:
        for ins in b["instrumentos"]:
            ins = dict(ins)
            ins["bloque_id"] = b["id"]
            ins["bloque"] = b["nombre"]
            instrumentos.append(ins)
    return cfg, instrumentos, (elec or {}).get("elecciones", []) or []


def fuentes_de(serie) -> list[str]:
    if isinstance(serie, dict):
        return [serie["a"], serie["b"]]
    return [serie]


# =============================================================================
# 2. Descarga de datos
# =============================================================================
def descargar_yahoo(tickers: list[str], anos: int = 11) -> dict[str, pd.Series]:
    import yfinance as yf

    out: dict[str, pd.Series] = {}
    if not tickers:
        return out

    def extraer(df, t):
        if df is None or df.empty:
            return None
        try:
            if isinstance(df.columns, pd.MultiIndex):
                lvl0 = df.columns.get_level_values(0)
                sub = df[t] if t in lvl0 else df.xs(t, axis=1, level=1)
                s = sub["Close"]
            else:
                s = df["Close"]
            s = s.dropna()
            return s if len(s) > 50 else None
        except Exception:
            return None

    for intento in range(2):
        faltan = [t for t in tickers if t not in out]
        if not faltan:
            break
        try:
            df = yf.download(faltan, period=f"{anos}y", interval="1d", auto_adjust=True,
                             progress=False, group_by="ticker", threads=True)
        except Exception as e:  # noqa: BLE001
            log(f"Yahoo lote falló: {e}")
            df = None
        for t in faltan:
            s = extraer(df, t)
            if s is not None:
                out[t] = s
        time.sleep(2)

    # reintento uno a uno para los que siguen faltando
    for t in [t for t in tickers if t not in out]:
        try:
            df = yf.Ticker(t).history(period=f"{anos}y", interval="1d", auto_adjust=True)
            s = df["Close"].dropna() if df is not None and not df.empty else None
            if s is not None and len(s) > 50:
                out[t] = s
        except Exception as e:  # noqa: BLE001
            log(f"Yahoo {t} falló: {e}")
        time.sleep(0.5)

    for t, s in out.items():
        idx = pd.to_datetime(s.index)
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        out[t] = pd.Series(s.values, index=idx.normalize(), name=t).astype(float)
    return out


def descargar_fred(ids: list[str], anos: int = 11) -> dict[str, pd.Series]:
    import requests

    out: dict[str, pd.Series] = {}
    inicio = (dt.date.today() - dt.timedelta(days=365 * anos)).isoformat()
    for sid in ids:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={inicio}"
        for intento in range(3):
            try:
                r = requests.get(url, timeout=30, headers={"User-Agent": "radar-global/1.0"})
                r.raise_for_status()
                df = pd.read_csv(io.StringIO(r.text))
                col_fecha = df.columns[0]
                s = pd.to_numeric(df[sid], errors="coerce")
                s.index = pd.to_datetime(df[col_fecha])
                s = s.dropna()
                if len(s):
                    out[sid] = s.astype(float)
                break
            except Exception as e:  # noqa: BLE001
                log(f"FRED {sid} intento {intento + 1} falló: {e}")
                time.sleep(3)
    return out


def datos_demo(fuentes: list[str], semanas: int = 560, seed: int = 7) -> dict[str, pd.Series]:
    """Series simuladas con algunos extremos inyectados, para probar la página."""
    rng = np.random.default_rng(seed)
    fin = pd.Timestamp.today().normalize()
    idx = pd.date_range(end=fin, periods=semanas * 5, freq="B")
    out = {}
    for i, f in enumerate(sorted(fuentes)):
        n = len(idx)
        if f.startswith("fred:"):
            base = {"BAMLH0A0HYM2": 3.5, "BAMLC0A0CM": 1.1, "T10Y2Y": 0.3, "DFII10": 1.5,
                    "T10YIE": 2.3}.get(f[5:], 3.0)
            paso = rng.normal(0, 0.035, n)
            s = base + np.cumsum(paso) * 0.6
            s = s - (s.mean() - base)
        else:
            vol = 0.012 if not f.endswith("=X") else 0.005
            ret = rng.normal(0.0002, vol, n)
            s = 100 * np.exp(np.cumsum(ret))
        # choques en las últimas semanas para algunas series
        shock_n = 30 + (i % 4) * 10
        if i % 9 == 0:
            s[-shock_n:] = s[-shock_n:] * np.linspace(1, 1.22, shock_n) if not f.startswith("fred:") \
                else s[-shock_n:] + np.linspace(0, 1.4, shock_n)
        elif i % 11 == 3:
            s[-shock_n:] = s[-shock_n:] * np.linspace(1, 0.80, shock_n) if not f.startswith("fred:") \
                else s[-shock_n:] - np.linspace(0, 1.2, shock_n)
        out[f] = pd.Series(s, index=idx)
    # series mensuales (OCDE) simuladas como mensuales
    for f in list(out):
        if f.startswith("fred:IRLTLT"):
            out[f] = out[f].resample("MS").first()
    return out


def cargar_fuentes(instrumentos, demo: bool):
    fuentes = sorted({f for ins in instrumentos for f in fuentes_de(ins["serie"])})
    if demo:
        crudo = datos_demo(fuentes)
    else:
        yf_t = [f[3:] for f in fuentes if f.startswith("yf:")]
        fr_t = [f[5:] for f in fuentes if f.startswith("fred:")]
        log(f"Descargando {len(yf_t)} series de Yahoo y {len(fr_t)} de FRED…")
        y = descargar_yahoo(yf_t)
        r = descargar_fred(fr_t)
        crudo = {**{f"yf:{k}": v for k, v in y.items()}, **{f"fred:{k}": v for k, v in r.items()}}
    ultimo_dato = {k: v.dropna().index.max() for k, v in crudo.items() if len(v.dropna())}
    semanal = {}
    for k, s in crudo.items():
        s = s.dropna().sort_index()
        s = s[~s.index.duplicated(keep="last")]
        w = s.resample("W-FRI").last()
        mensual = k.startswith("fred:IRLTLT") or (len(s) > 3 and (s.index.to_series().diff().median() > pd.Timedelta(days=20)))
        w = w.ffill(limit=6 if mensual else 2)
        semanal[k] = w
    faltantes = [f for f in fuentes if f not in semanal]
    return semanal, ultimo_dato, faltantes


def construir_serie(serie, semanal):
    if isinstance(serie, dict):
        a, b = semanal.get(serie["a"]), semanal.get(serie["b"])
        if a is None or b is None:
            return None
        df = pd.concat([a, b], axis=1, join="inner").dropna()
        if df.empty:
            return None
        return df.iloc[:, 0] / df.iloc[:, 1] if serie["op"] == "/" else df.iloc[:, 0] - df.iloc[:, 1]
    return semanal.get(serie)


# =============================================================================
# 3. Métricas
# =============================================================================
def es_tipo_precio(tipo):
    return tipo in ("precio", "fx")


def es_tipo_tasa(tipo):
    return tipo in ("tasa", "spread")


def calcular(ins, s: pd.Series, p: dict, lecturas: dict) -> dict | None:
    s = s.dropna()
    L, T = p["lookback_semanas"], p["media_tendencia_semanas"]
    if len(s) < L // 2 + (T if es_tipo_precio(ins["tipo"]) else 0):
        return None
    if es_tipo_precio(ins["tipo"]):
        s = s[s > 0]
        base = np.log(s) - np.log(s.rolling(T, min_periods=int(T * 0.8)).mean())
    else:
        base = s.copy()
    mu = base.rolling(L, min_periods=int(L * 0.5)).mean()
    sd = base.rolling(L, min_periods=int(L * 0.5)).std()
    z = ((base - mu) / sd).replace([np.inf, -np.inf], np.nan).dropna()
    if len(z) < 3:
        return None

    u = p["umbrales"]
    z_now, z_prev = float(z.iloc[-1]), float(z.iloc[-2])

    def nivel(v):
        a = abs(v)
        return "extremo" if a >= u["extremo"] else "fuerte" if a >= u["fuerte"] else "aviso" if a >= u["aviso"] else None

    nv, nv_prev = nivel(z_now), nivel(z_prev)

    # semanas consecutivas en zona de aviso, mismo signo
    semanas = 0
    if nv:
        signo = np.sign(z_now)
        for v in z.iloc[::-1]:
            if abs(v) >= u["aviso"] and np.sign(v) == signo:
                semanas += 1
            else:
                break

    # cambio 4 semanas del nivel crudo
    s_al = s.reindex(z.index).ffill()
    nivel_actual = float(s_al.iloc[-1])
    cambio4 = None
    if len(s_al) > 4:
        prev = float(s_al.iloc[-5])
        if es_tipo_tasa(ins["tipo"]):
            cambio4 = (nivel_actual - prev) * 100  # pb
        elif prev:
            cambio4 = (nivel_actual / prev - 1) * 100

    # percentil 10 años del nivel "base"
    hist = base.dropna().iloc[-p["historia_larga_semanas"]:]
    pct10 = float((hist < base.iloc[-1]).mean() * 100) if len(hist) > 20 else None

    # confiabilidad histórica: ¿qué pasó H semanas después de entrar en extremos similares?
    H = p["horizonte_confiabilidad_semanas"]
    confi = None
    if nv:
        signo = np.sign(z_now)
        zz = z.iloc[:-1]
        entradas, ultima = [], None
        for i in range(1, len(zz) - H):
            v, vp = zz.iloc[i], zz.iloc[i - 1]
            if abs(v) >= u["aviso"] and np.sign(v) == signo and abs(vp) < u["aviso"]:
                if ultima is None or i - ultima >= H:
                    entradas.append(i)
                    ultima = i
        movs = []
        for i in entradas:
            t0, t1 = zz.index[i], zz.index[i + H]
            a0, a1 = s_al.get(t0), s_al.get(t1)
            if a0 is None or a1 is None or pd.isna(a0) or pd.isna(a1):
                continue
            movs.append((a1 - a0) * 100 if es_tipo_tasa(ins["tipo"]) else (a1 / a0 - 1) * 100)
        if movs:
            movs = np.array(movs)
            revirtio = float(np.mean(np.sign(movs) == -signo) * 100)
            confi = {"n": int(len(movs)), "revirtio": revirtio, "mediana": float(np.median(movs)),
                     "unidad": "pb" if es_tipo_tasa(ins["tipo"]) else "%"}

    lect = lecturas.get(ins["tipo"], {})
    lectura = (ins.get("lectura_alta") or lect.get("alta", "")) if z_now >= 0 else (ins.get("lectura_baja") or lect.get("baja", ""))

    return {
        "id": ins["id"], "nombre": ins["nombre"], "bloque": ins["bloque"], "bloque_id": ins["bloque_id"],
        "tipo": ins["tipo"], "que_cuenta": ins.get("que_cuenta", ""),
        "z": z_now, "z_prev": z_prev, "dz": z_now - z_prev,
        "nivel": nv, "nuevo": bool(nv and NIVEL_ORDEN[nv] > NIVEL_ORDEN[nv_prev]),
        "semanas": semanas, "valor": nivel_actual, "cambio4": cambio4, "pct10": pct10,
        "confi": confi, "lectura": lectura if nv else "",
        "lectura_si_extremo": lectura,
        "spark": [round(float(v), 2) for v in z.iloc[-52:]],
        "fecha": z.index[-1],
    }


# =============================================================================
# 4. Elecciones
# =============================================================================
def procesar_elecciones(elecciones, res_por_id, p, hoy):
    out = []
    for e in elecciones:
        f = pd.Timestamp(e["fecha"]).date()
        dias = (f - hoy).days
        if dias < -14 or dias > p["elecciones_horizonte_dias"]:
            continue
        activos = [res_por_id[a] for a in e.get("activos", []) if a in res_por_id]
        out.append({**e, "fecha": f, "dias": dias, "en_ventana": -14 <= dias <= p["elecciones_aviso_dias"],
                    "activos_res": activos})
    return sorted(out, key=lambda x: x["dias"])


# =============================================================================
# 5. Render HTML
# =============================================================================
def esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def fmt_valor(r):
    v = r["valor"]
    if es_tipo_tasa(r["tipo"]):
        return f"{v:.2f}%"
    if r["tipo"] == "ratio":
        return f"{v:.4g}"
    if abs(v) >= 1000:
        return f"{v:,.0f}".replace(",", ".")
    return f"{v:,.2f}" if abs(v) >= 10 else f"{v:.4g}"


def fmt_cambio(r):
    c = r["cambio4"]
    if c is None:
        return "—"
    return f"{c:+.0f} pb" if es_tipo_tasa(r["tipo"]) else f"{c:+.1f}%"


def gauge_svg(z, nivel, u, w=120, h=14):
    lim = 3.5
    zc = max(-lim, min(lim, z))
    pad = 6
    def px(v):
        return pad + (v + lim) / (2 * lim) * (w - 2 * pad)
    x, a1, a0 = px(zc), px(u["aviso"]), px(-u["aviso"])
    col = {"extremo": "var(--crit)", "fuerte": "var(--serious)", "aviso": "var(--warn)"}.get(nivel, "var(--ink-2)")
    return (f'<svg class="gauge" viewBox="0 0 {w} {h}" width="{w}" height="{h}" aria-hidden="true">'
            f'<rect x="0" y="{h/2-2}" width="{w}" height="4" rx="2" fill="var(--track)"/>'
            f'<rect x="{a0:.1f}" y="{h/2-2}" width="{a1-a0:.1f}" height="4" fill="var(--track-mid)"/>'
            f'<line x1="{w/2}" y1="2" x2="{w/2}" y2="{h-2}" stroke="var(--ink-3)" stroke-width="1"/>'
            f'<circle cx="{x:.1f}" cy="{h/2}" r="5" fill="{col}" stroke="var(--surface)" stroke-width="2"/></svg>')


def spark_svg(vals, u, w=220, h=48):
    if not vals or len(vals) < 2:
        return ""
    lim = max(3.2, max(abs(v) for v in vals) + 0.2)
    def yy(v):
        return h / 2 - v / lim * (h / 2 - 3)
    n = len(vals)
    pts = " ".join(f"{i/(n-1)*w:.1f},{yy(v):.1f}" for i, v in enumerate(vals))
    band_y, band_h = yy(u["aviso"]), yy(-u["aviso"]) - yy(u["aviso"])
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="100%" height="{h}" preserveAspectRatio="none" '
            f'role="img" aria-label="z-score de las últimas 52 semanas">'
            f'<rect x="0" y="{band_y:.1f}" width="{w}" height="{band_h:.1f}" fill="var(--track)"/>'
            f'<line x1="0" y1="{h/2}" x2="{w}" y2="{h/2}" stroke="var(--ink-3)" stroke-width="0.6" stroke-dasharray="2 3"/>'
            f'<polyline points="{pts}" fill="none" stroke="var(--line)" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>'
            f'<circle cx="{w:.1f}" cy="{yy(vals[-1]):.1f}" r="3" fill="var(--line)"/></svg>')


def badge(nivel):
    if not nivel:
        return ""
    ico = {"extremo": "▲▲▲", "fuerte": "▲▲", "aviso": "▲"}[nivel]
    return f'<span class="badge b-{nivel}"><span aria-hidden="true">{ico}</span> {NIVEL_TXT[nivel]}</span>'


def texto_confi(c, horizonte):
    if not c:
        return "Sin extremos comparables en la historia disponible: señal sin respaldo estadístico."
    if c["n"] < 3:
        return f"Solo {c['n']} caso(s) comparable(s) en ~10 años: muestra insuficiente para confiar en la reversión."
    med = f"{c['mediana']:+.0f} pb" if c["unidad"] == "pb" else f"{c['mediana']:+.1f}%"
    return (f"En {c['n']} extremos similares, {horizonte} semanas después el activo revirtió "
            f"{c['revirtio']:.0f}% de las veces (movimiento mediano {med}).")


def direccion(r):
    return "por encima" if r["z"] >= 0 else "por debajo"


CSS = """
:root{--surface:#fcfcfb;--bg:#f4f3f0;--card:#ffffff;--ink:#0b0b0b;--ink-2:#52514e;--ink-3:#8b8a85;
--border:#e4e2dc;--track:#ebe9e4;--track-mid:#dedcd5;--line:#2a78d6;--accent:#2a78d6;
--warn:#fab219;--serious:#ec835a;--crit:#d03b3b;--good:#0ca30c}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--surface:#1a1a19;--bg:#121211;--card:#1f1f1e;
--ink:#ffffff;--ink-2:#c3c2b7;--ink-3:#8b8a80;--border:#33332f;--track:#2b2b28;--track-mid:#3a3a36;--line:#3987e5;--accent:#6da7ec}}
:root[data-theme="dark"]{--surface:#1a1a19;--bg:#121211;--card:#1f1f1e;--ink:#ffffff;--ink-2:#c3c2b7;--ink-3:#8b8a80;
--border:#33332f;--track:#2b2b28;--track-mid:#3a3a36;--line:#3987e5;--accent:#6da7ec}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:20px 16px 60px}
header h1{font-size:26px;margin:0 0 2px;letter-spacing:-.02em}
.sub{color:var(--ink-2);font-size:13px}
h2{font-size:18px;margin:32px 0 10px;letter-spacing:-.01em}
h2 small{font-weight:400;color:var(--ink-2);font-size:13px;margin-left:6px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px 14px;margin:10px 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-top:16px}
.kpi{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px}
.kpi .n{font-size:26px;font-weight:650;line-height:1.1}.kpi .l{font-size:12px;color:var(--ink-2)}
.pulse{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.pulse .gauge{width:70px}
.pulse .card{margin:0}
.row{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.name{font-weight:600}.blk{font-size:12px;color:var(--ink-2)}
.z{font-variant-numeric:tabular-nums;font-weight:650}
.meta{display:flex;gap:14px;flex-wrap:wrap;font-size:12.5px;color:var(--ink-2);margin:8px 0 6px}
.meta b{color:var(--ink);font-weight:600}
.lect{margin:6px 0}.confi{font-size:13px;color:var(--ink-2);border-left:3px solid var(--track-mid);padding-left:9px;margin:8px 0}
.badge{display:inline-flex;gap:4px;align-items:center;font-size:11.5px;font-weight:600;border-radius:999px;padding:2px 9px;border:1px solid var(--border);color:var(--ink)}
.badge span{font-size:9px}.b-aviso span{color:#c98500}.b-fuerte span{color:var(--serious)}.b-extremo span{color:var(--crit)}
.new{font-size:11px;font-weight:700;color:var(--accent);border:1px solid var(--accent);border-radius:999px;padding:1px 7px;margin-left:6px}
details{margin:0}summary{cursor:pointer;list-style:none}summary::-webkit-details-marker{display:none}
.more{font-size:13px;color:var(--accent);margin-top:4px;display:inline-block}
.info{font-size:13px;color:var(--ink-2);padding:8px 0 2px}
.blkcard summary{display:flex;justify-content:space-between;align-items:center;gap:8px}
.blkcard summary h3{margin:0;font-size:16px}.chev{color:var(--ink-3);transition:transform .15s}
details[open]>summary .chev{transform:rotate(90deg)}
.why{background:var(--bg);border-radius:8px;padding:10px 12px;font-size:13.5px;color:var(--ink-2);margin:10px 0}
.why b{color:var(--ink)}
.item{border-top:1px solid var(--border);padding:9px 0}
.item summary{display:grid;grid-template-columns:1fr auto;gap:4px 10px;align-items:center}
.item .r{display:flex;align-items:center;gap:8px;justify-content:flex-end}
.item .v{font-size:12.5px;color:var(--ink-2);font-variant-numeric:tabular-nums}
.gauge{display:block}.spark{display:block;margin:6px 0 2px}
.hdr{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}.hdr>div:first-child{min-width:0}
.elec .d{font-size:28px;font-weight:700;line-height:1}.elec .dl{font-size:12px;color:var(--ink-2)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.chip{font-size:12.5px;border:1px solid var(--border);border-radius:8px;padding:4px 8px;background:var(--bg)}
.chip b{font-variant-numeric:tabular-nums}
.muted{color:var(--ink-2);font-size:13px}.warnbox{border-left:3px solid var(--warn)}
table.gl{width:100%;border-collapse:collapse;font-size:13.5px}table.gl td{padding:7px 0;border-top:1px solid var(--border);vertical-align:top}
table.gl td:first-child{font-weight:600;width:34%;padding-right:10px}
a{color:var(--accent)}footer{margin-top:40px;font-size:12px;color:var(--ink-3)}
.tgl{float:right;background:var(--card);border:1px solid var(--border);color:var(--ink-2);border-radius:8px;padding:4px 10px;font-size:12px;cursor:pointer}
"""


def render_alerta(r, p):
    u, H = p["umbrales"], p["horizonte_confiabilidad_semanas"]
    nuevo = '<span class="new">NUEVO</span>' if r["nuevo"] else ""
    sem = "primera semana" if r["semanas"] <= 1 else f"{r['semanas']} semanas seguidas"
    pct = f"{r['pct10']:.0f}" if r["pct10"] is not None else "—"
    return f"""
<div class="card">
  <div class="row"><div><div class="name">{esc(r['nombre'])}{nuevo}</div><div class="blk">{esc(r['bloque'])}</div></div>
  <div>{badge(r['nivel'])}</div></div>
  <div class="row" style="margin-top:8px"><span class="z">z = {r['z']:+.2f}</span>{gauge_svg(r['z'], r['nivel'], u)}</div>
  <div class="meta"><span>Nivel <b>{fmt_valor(r)}</b></span><span>4 sem <b>{fmt_cambio(r)}</b></span>
  <span>Percentil 10a <b>{pct}</b></span><span>{sem} {direccion(r)}</span></div>
  <p class="lect">{esc(r['lectura'])}</p>
  <div class="confi">{esc(texto_confi(r['confi'], H))}</div>
  {spark_svg(r['spark'], u)}
  <details><summary class="more">¿Qué te cuenta este activo?</summary><div class="info">{esc(r['que_cuenta'])}</div></details>
</div>"""


def render_eleccion(e, p):
    d = e["dias"]
    if d > 0:
        cuenta, dl = str(d), "días"
    elif d == 0:
        cuenta, dl = "HOY", ""
    else:
        cuenta, dl = f"hace {-d}", "días"
    chips = "".join(
        f'<span class="chip">{esc(a["nombre"])} <b>z {a["z"]:+.1f}</b>{" · " + NIVEL_TXT[a["nivel"]] if a["nivel"] else ""}</span>'
        for a in e["activos_res"]) or '<span class="muted">Sin datos de los activos asociados.</span>'
    tent = "" if e.get("confirmada", True) else ' <span class="muted">(fecha tentativa)</span>'
    esc_txt = (e.get("escenarios") or "").strip()
    escen = f'<div class="why"><b>Escenarios:</b> {esc(esc_txt)}</div>' if esc_txt else ""
    extendidos = [a for a in e["activos_res"] if a["nivel"]]
    if extendidos:
        desc = "El mercado ya se movió: " + ", ".join(f"{a['nombre']} {direccion(a)} de su normalidad" for a in extendidos) + \
               ". Parte del evento podría estar descontado."
    elif e["activos_res"]:
        desc = "Los activos asociados están en rango normal: el mercado no está descontando un resultado extremo."
    else:
        desc = ""
    return f"""
<div class="card elec">
  <div class="hdr"><div><div class="name">{esc(e['pais'])} — {esc(e['evento'])}</div>
  <div class="blk">{fecha_es(e['fecha'])}{tent}</div></div>
  <div style="text-align:right"><div class="d">{cuenta}</div><div class="dl">{dl}</div></div></div>
  <div class="chips">{chips}</div>
  <p class="lect" style="font-size:14px">{esc(desc)}</p>
  <details><summary class="more">Contexto</summary><div class="info">{esc((e.get('contexto') or '').strip())}</div></details>
  {escen}
</div>"""


def render_item(r, p):
    u = p["umbrales"]
    pct = f"{r['pct10']:.0f}" if r["pct10"] is not None else "—"
    lect = r["lectura_si_extremo"]
    return f"""
<details class="item"><summary>
  <div><div class="name" style="font-weight:550">{esc(r['nombre'])}</div><div class="v">{fmt_valor(r)} · 4 sem {fmt_cambio(r)} · pct 10a {pct}</div></div>
  <div class="r"><span class="z" style="font-size:13px">{r['z']:+.1f}</span>{gauge_svg(r['z'], r['nivel'], u, w=84)}</div>
</summary>
<div class="info"><b>Qué te cuenta:</b> {esc(r['que_cuenta'])}<br><b>Si llega a extremo {'alto' if r['z']>=0 else 'bajo'}:</b> {esc(lect)}</div>
{spark_svg(r['spark'], u)}
</details>"""


def render_html(ctx) -> str:
    p, u = ctx["p"], ctx["p"]["umbrales"]
    res, alertas, elec, movs = ctx["res"], ctx["alertas"], ctx["elecciones"], ctx["movs"]
    by = {r["id"]: r for r in res}

    def pulso(iid, titulo, texto_alto, texto_bajo, texto_normal):
        r = by.get(iid)
        if not r:
            return ""
        t = texto_alto if r["z"] >= u["aviso"] else texto_bajo if r["z"] <= -u["aviso"] else texto_normal
        return (f'<div class="card"><div class="blk">{titulo}</div><div class="row"><span class="z">{fmt_valor(r)}</span>'
                f'{gauge_svg(r["z"], r["nivel"], u, w=84)}</div><div class="muted">{t}</div></div>')

    pulso_html = "".join([
        pulso("VIX", "Miedo en acciones (VIX)", "Miedo extremo", "Complacencia", "Normal"),
        pulso("HYSPR", "Estrés de crédito (HY)", "Crédito estresado", "Crédito complaciente", "Normal"),
        pulso("DXY", "Dólar global (DXY)", "Dólar muy fuerte", "Dólar muy débil", "Normal"),
        pulso("USDJPY", "Carry trade (USD/JPY)", "Yen muy débil: carry cargado", "Yen fuerte: desarme", "Normal"),
        pulso("USDCOP", "Tu tasa (USD/COP)", "Dólar caro vs. tendencia", "Dólar barato: ventana para dolarizar", "Normal"),
        pulso("CURVA", "Curva EE. UU. 10a−2a", "Curva muy empinada", "Curva muy plana/invertida", "Normal"),
    ])

    n_nuevas = sum(1 for a in alertas if a["nuevo"])
    en_ventana = [e for e in elec if e["en_ventana"]]
    alert_html = "".join(render_alerta(a, p) for a in alertas) or \
        '<div class="card muted">Ningún activo del universo está en zona de extremo esta semana. Mercados dentro de su rango normal.</div>'
    elec_html = "".join(render_eleccion(e, p) for e in en_ventana) or \
        f'<div class="card muted">Ninguna elección en los próximos {p["elecciones_aviso_dias"]} días.</div>'
    prox = [e for e in elec if not e["en_ventana"] and e["dias"] > 0]
    prox_html = "".join(f'<div class="item" style="display:flex;justify-content:space-between;gap:8px"><span>{esc(e["pais"])} — {esc(e["evento"])}</span>'
                        f'<span class="v">{fecha_es(e["fecha"])} · {e["dias"]} d</span></div>' for e in prox)

    mov_html = "".join(
        f'<div class="item" style="display:flex;justify-content:space-between;gap:8px;align-items:center">'
        f'<span><span class="name" style="font-weight:550">{esc(m["nombre"])}</span><br><span class="v">{esc(m["bloque"])}</span></span>'
        f'<span class="z" style="font-size:13px;white-space:nowrap">{m["z_prev"]:+.1f} → {m["z"]:+.1f}</span></div>' for m in movs)

    bloques_html = ""
    for b in ctx["cfg"]["bloques"]:
        items = [by[i["id"]] for i in b["instrumentos"] if i["id"] in by]
        n_al = sum(1 for r in items if r["nivel"])
        tag = f'<span class="badge b-aviso"><span aria-hidden="true">▲</span> {n_al}</span>' if n_al else ""
        bloques_html += f"""
<div class="card blkcard"><details><summary><h3>{esc(b['nombre'])}</h3><span>{tag} <span class="chev">›</span></span></summary>
<div class="why"><b>Por qué está aquí:</b> {esc(b['por_que'].strip())}</div>
{''.join(render_item(r, p) for r in items)}
</details></div>"""

    problemas = ""
    if ctx["problemas"]:
        problemas = ('<h2>Calidad de datos</h2><div class="card warnbox"><div class="muted">'
                     + "<br>".join(esc(x) for x in ctx["problemas"]) + "</div></div>")

    archivo = "".join(f'<a href="{esc(ctx["prefijo_archivo"])}{esc(f)}.html">{fecha_es(f)}</a> · ' for f in ctx["archivo"][:26])
    demo = '<div class="card warnbox"><b>MODO DEMO:</b> datos simulados, no reales.</div>' if ctx["demo"] else ""

    glos = [
        ("z-score", f"Cuántas desviaciones estándar está el activo lejos de su normalidad de {p['lookback_semanas']//52} años. "
                    f"Para precios y divisas se mide la distancia a su tendencia de {p['media_tendencia_semanas']} semanas; "
                    "para tasas, spreads y ratios, el nivel. |z| ≥ 2 ocurre ~5% del tiempo en una distribución normal."),
        ("Niveles", f"Aviso |z| ≥ {u['aviso']} · Fuerte ≥ {u['fuerte']} · Extremo ≥ {u['extremo']}. Se ajustan en config.yaml."),
        ("Barra", "El punto muestra dónde está hoy entre −3.5 y +3.5; la franja central es la zona normal (±aviso)."),
        ("Gráfica", "El z-score de las últimas 52 semanas. La franja gris es la zona normal."),
        ("Percentil 10a", "Qué porcentaje de las últimas ~10 años estuvo por debajo del nivel actual. 95 = más alto que el 95% del tiempo."),
        ("Confiabilidad", f"Busca las veces anteriores que el activo entró en extremo en la misma dirección y mide qué pasó "
                          f"{p['horizonte_confiabilidad_semanas']} semanas después. Si revirtió menos de la mitad de las veces, "
                          "el extremo ha sido más tendencia que oportunidad."),
        ("Elecciones", f"Aparecen cuando faltan ≤ {p['elecciones_aviso_dias']} días. Si los activos asociados ya están en extremo, "
                       "el mercado probablemente descontó parte del resultado."),
    ]
    glos_html = "".join(f"<tr><td>{esc(a)}</td><td>{esc(b)}</td></tr>" for a, b in glos)

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Radar Global</title><meta name="description" content="Screener cross-asset semanal">
<style>{CSS}</style></head>
<body><div class="wrap">
<header><button class="tgl" id="tgl" type="button">Tema</button><h1>Radar Global</h1>
<div class="sub">Semana cerrada al {fecha_es(ctx['corte'])} · generado {ctx['generado']} (hora Colombia)</div></header>
{demo}
<div class="kpis">
 <div class="kpi"><div class="n">{len(alertas)}</div><div class="l">activos en extremo</div></div>
 <div class="kpi"><div class="n">{n_nuevas}</div><div class="l">nuevos esta semana</div></div>
 <div class="kpi"><div class="n">{len(en_ventana)}</div><div class="l">elecciones a ≤{p['elecciones_aviso_dias']} días</div></div>
 <div class="kpi"><div class="n">{len(res)}</div><div class="l">instrumentos monitoreados</div></div>
</div>

<h2>Pulso de riesgo</h2><div class="pulse">{pulso_html}</div>

<h2>Alertas de la semana <small>ordenadas por intensidad</small></h2>
{alert_html}

<h2>Elecciones en ventana</h2>
{elec_html}
{'<div class="card"><div class="blk" style="margin-bottom:4px">Más adelante</div>' + prox_html + '</div>' if prox_html else ''}

<h2>Lo que más se movió <small>cambio de z en la semana</small></h2>
<div class="card">{mov_html}</div>

<h2>Universo completo <small>toca un bloque para abrirlo</small></h2>
{bloques_html}

{problemas}

<h2>Cómo leer esto</h2>
<div class="card"><table class="gl">{glos_html}</table></div>
<div class="card muted"><b>Esto no es una recomendación de inversión.</b> Es un radar de desajustes estadísticos: un extremo
puede extenderse mucho más de lo que parece razonable. Úsalo para decidir qué investigar, no qué comprar.</div>

{'<h2>Reportes anteriores</h2><div class="card muted">' + archivo.rstrip(' · ') + '</div>' if archivo else ''}
<footer>Fuentes: Yahoo Finance, FRED (Reserva Federal de St. Louis). Radar Global v1.0</footer>
</div>
<script>
(function(){{var b=document.getElementById('tgl'),r=document.documentElement;
try{{var t=localStorage.getItem('radar-theme');if(t)r.setAttribute('data-theme',t);}}catch(e){{}}
b.onclick=function(){{var dark=r.getAttribute('data-theme')?r.getAttribute('data-theme')==='dark':matchMedia('(prefers-color-scheme: dark)').matches;
var n=dark?'light':'dark';r.setAttribute('data-theme',n);try{{localStorage.setItem('radar-theme',n);}}catch(e){{}}}};}})();
</script>
</body></html>"""


# =============================================================================
# 6. Correo
# =============================================================================
def render_email(ctx, url_pagina):
    p = ctx["p"]
    alertas = ctx["alertas"][: p["max_alertas_correo"]]
    col = {"extremo": "#d03b3b", "fuerte": "#ec835a", "aviso": "#fab219"}
    filas = ""
    for a in alertas:
        nuevo = ' <span style="color:#2a78d6;font-weight:700;font-size:11px">NUEVO</span>' if a["nuevo"] else ""
        filas += (f'<tr><td style="padding:10px 0;border-top:1px solid #e4e2dc;vertical-align:top">'
                  f'<div style="font-weight:600">{esc(a["nombre"])}{nuevo}</div>'
                  f'<div style="font-size:12px;color:#52514e">{esc(a["bloque"])} · {fmt_valor(a)} · 4 sem {fmt_cambio(a)}</div>'
                  f'<div style="font-size:13px;margin-top:4px">{esc(a["lectura"])}</div>'
                  f'<div style="font-size:12px;color:#52514e;margin-top:4px">{esc(texto_confi(a["confi"], p["horizonte_confiabilidad_semanas"]))}</div></td>'
                  f'<td style="padding:10px 0 10px 12px;border-top:1px solid #e4e2dc;vertical-align:top;text-align:right;white-space:nowrap">'
                  f'<div style="font-weight:700">z {a["z"]:+.2f}</div>'
                  f'<div style="font-size:11px;font-weight:600;border-left:3px solid {col[a["nivel"]]};padding-left:5px">{NIVEL_TXT[a["nivel"]]}</div></td></tr>')
    if not filas:
        filas = '<tr><td style="padding:10px 0;color:#52514e">Ningún activo en extremo esta semana.</td></tr>'
    extra = len(ctx["alertas"]) - len(alertas)
    mas = f'<p style="font-size:13px;color:#52514e">+{extra} alertas más en la página.</p>' if extra > 0 else ""

    elec = ""
    for e in [e for e in ctx["elecciones"] if e["en_ventana"]]:
        act = ", ".join(f'{esc(a["nombre"])} (z {a["z"]:+.1f})' for a in e["activos_res"])
        cuando = f"en {e['dias']} días" if e["dias"] > 0 else ("hoy" if e["dias"] == 0 else f"hace {-e['dias']} días")
        elec += (f'<div style="padding:8px 0;border-top:1px solid #e4e2dc"><b>{esc(e["pais"])} — {esc(e["evento"])}</b> '
                 f'<span style="color:#52514e">{fecha_es(e["fecha"])}, {cuando}</span><br>'
                 f'<span style="font-size:13px;color:#52514e">{act}</span></div>')
    elec_sec = f'<h3 style="font-size:16px;margin:24px 0 4px">Elecciones en ventana</h3>{elec}' if elec else ""

    boton = (f'<p style="margin:24px 0"><a href="{esc(url_pagina)}" style="background:#2a78d6;color:#fff;text-decoration:none;'
             f'padding:10px 16px;border-radius:8px;font-weight:600">Ver radar completo</a></p>') if url_pagina else ""
    demo = '<p style="color:#d03b3b;font-weight:700">MODO DEMO: datos simulados.</p>' if ctx["demo"] else ""
    html_body = f"""<!doctype html><html><body style="margin:0;background:#f4f3f0">
<div style="max-width:600px;margin:0 auto;padding:20px 16px;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#0b0b0b;background:#ffffff">
<h1 style="font-size:22px;margin:0">Radar Global</h1>
<div style="font-size:13px;color:#52514e">Semana cerrada al {fecha_es(ctx['corte'])} · {len(ctx['alertas'])} activos en extremo · {sum(1 for a in ctx['alertas'] if a['nuevo'])} nuevos</div>
{demo}
<h3 style="font-size:16px;margin:24px 0 4px">Alertas</h3>
<table style="width:100%;border-collapse:collapse;font-size:14px">{filas}</table>{mas}
{elec_sec}
{boton}
<p style="font-size:11px;color:#8b8a85">Radar de desajustes estadísticos, no recomendación de inversión. Un extremo puede extenderse.</p>
</div></body></html>"""

    txt = [f"RADAR GLOBAL — semana al {fecha_es(ctx['corte'])}", ""]
    for a in alertas:
        txt.append(f"- {a['nombre']}: z {a['z']:+.2f} ({NIVEL_TXT[a['nivel']]}){' NUEVO' if a['nuevo'] else ''}. {a['lectura']}")
    if url_pagina:
        txt += ["", f"Radar completo: {url_pagina}"]
    asunto = f"Radar Global {fecha_es(ctx['corte'])}: {len(ctx['alertas'])} extremos"
    if any(a["nuevo"] for a in ctx["alertas"]):
        asunto += f", {sum(1 for a in ctx['alertas'] if a['nuevo'])} nuevos"
    if any(e["en_ventana"] and 0 <= e["dias"] <= 14 for e in ctx["elecciones"]):
        asunto += " · elección cerca"
    return asunto, html_body, "\n".join(txt)


def enviar_correo(asunto, html_body, txt):
    user, pwd = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("MAIL_TO") or user
    if not (user and pwd):
        log("Sin credenciales de correo (GMAIL_USER / GMAIL_APP_PASSWORD): no se envía.")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = asunto, f"Radar Global <{user}>", to
    msg.attach(MIMEText(txt, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, pwd.replace(" ", ""))
        s.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())
    log(f"Correo enviado a {to}")
    return True


# =============================================================================
# 7. Orquestación
# =============================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="usar datos simulados")
    ap.add_argument("--no-email", action="store_true")
    args = ap.parse_args()

    cfg, instrumentos, elecciones = cargar_config()
    p = cfg["parametros"]
    ahora = dt.datetime.now(TZ)
    hoy = ahora.date()

    semanal, ultimo_dato, faltantes = cargar_fuentes(instrumentos, args.demo)
    problemas = [f"Sin datos: {f}" for f in faltantes]
    for k, d in ultimo_dato.items():
        diario = not k.startswith("fred:IRLTLT")
        edad = (pd.Timestamp(hoy) - pd.Timestamp(d)).days
        if diario and edad > p["dias_dato_viejo"]:
            problemas.append(f"Dato viejo: {k} (último {fecha_es(d)}, hace {edad} días)")

    res = []
    for ins in instrumentos:
        s = construir_serie(ins["serie"], semanal)
        if s is None:
            problemas.append(f"No se pudo construir: {ins['nombre']}")
            continue
        try:
            r = calcular(ins, s, p, cfg.get("lecturas_por_tipo", {}))
        except Exception as e:  # noqa: BLE001
            problemas.append(f"Error calculando {ins['nombre']}: {e}")
            continue
        if r is None:
            problemas.append(f"Historia insuficiente: {ins['nombre']}")
        else:
            res.append(r)

    if not res:
        log("No hay resultados: abortando sin publicar.")
        sys.exit(1)
    if len(problemas) > len(instrumentos) * 0.5:
        log(f"Advertencia: {len(problemas)} problemas de datos.")

    alertas = sorted([r for r in res if r["nivel"]], key=lambda r: (-NIVEL_ORDEN[r["nivel"]], -int(r["nuevo"]), -abs(r["z"])))
    movs = sorted(res, key=lambda r: -abs(r["dz"]))[:6]
    res_por_id = {r["id"]: r for r in res}
    elec = procesar_elecciones(elecciones, res_por_id, p, hoy)
    corte = max(r["fecha"] for r in res)

    # historial
    DATA.mkdir(exist_ok=True)
    hist_path = DATA / "historial.csv"
    nuevo_hist = pd.DataFrame([{"fecha": pd.Timestamp(corte).date().isoformat(), "id": r["id"], "z": round(r["z"], 3),
                                "nivel": r["nivel"] or "", "valor": r["valor"]} for r in res])
    if not args.demo:
        if hist_path.exists():
            viejo = pd.read_csv(hist_path)
            viejo = viejo[viejo["fecha"] != nuevo_hist["fecha"].iloc[0]]
            nuevo_hist = pd.concat([viejo, nuevo_hist], ignore_index=True)
        nuevo_hist.to_csv(hist_path, index=False)

    # página + archivo
    ARCHIVO.mkdir(parents=True, exist_ok=True)
    tag = pd.Timestamp(corte).date().isoformat()
    archivos_prev = sorted({f.stem for f in ARCHIVO.glob("*.html")} | ({tag} if not args.demo else set()), reverse=True)
    ctx = {"cfg": cfg, "p": p, "res": res, "alertas": alertas, "elecciones": elec, "movs": movs,
           "problemas": problemas, "corte": corte, "generado": ahora.strftime("%d/%m/%Y %H:%M"),
           "demo": args.demo, "archivo": archivos_prev}
    ctx["prefijo_archivo"] = "archivo/"
    (DOCS / "index.html").write_text(render_html(ctx), encoding="utf-8")
    if not args.demo:
        ctx["prefijo_archivo"] = ""
        (ARCHIVO / f"{tag}.html").write_text(render_html(ctx), encoding="utf-8")
    (DOCS / ".nojekyll").write_text("")
    resumen = {"corte": tag, "alertas": [{k: a[k] for k in ("id", "nombre", "z", "nivel", "nuevo")} for a in alertas],
               "elecciones_en_ventana": [f"{e['pais']} {e['fecha']}" for e in elec if e["en_ventana"]],
               "problemas": problemas}
    (DATA / ("ultimo_demo.json" if args.demo else "ultimo.json")).write_text(
        json.dumps(resumen, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    log(f"Página generada: {len(res)} instrumentos, {len(alertas)} alertas, {len(problemas)} avisos de datos.")

    # correo
    url = os.environ.get("PAGES_URL")
    if not url and os.environ.get("GITHUB_REPOSITORY"):
        owner, repo = os.environ["GITHUB_REPOSITORY"].split("/")
        url = f"https://{owner.lower()}.github.io/{repo}/"
    asunto, html_body, txt = render_email(ctx, url)
    (DATA / "correo_preview.html").write_text(html_body, encoding="utf-8")
    if not args.no_email:
        try:
            enviar_correo(asunto, html_body, txt)
        except Exception as e:  # noqa: BLE001
            log(f"Error enviando correo: {e}")


if __name__ == "__main__":
    main()
