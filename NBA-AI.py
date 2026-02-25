"""
╔══════════════════════════════════════════════════════════════╗
║          NBA EDGE ALPHA BOT  v2.0                           ║
║  Detecta oportunidades de valor en Polymarket NBA           ║
║                                                              ║
║  FÓRMULA NEA (NBA Edge Alpha):                              ║
║  NEA = P_Poly - [0.45·P_Vegas + 0.40·N + 0.10·V + 0.05·R] ║
║                                                              ║
║  Variables:                                                  ║
║    P_Poly : precio actual en Polymarket (0-100)             ║
║    P_Vegas: prob. implícita casas de apuestas (0-100)       ║
║    N      : factor noticias/lesiones (-100 a 100)           ║
║    V      : localía (+5 local / -5 visitante)               ║
║    R      : racha últimos 5 partidos (0-100)                ║
║                                                              ║
║  Requiere:                                                   ║
║    pip install requests google-genai                        ║
║    GEMINI_API_KEY en variables de entorno                   ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import re

def _cargar_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key   = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

_cargar_env()

import json
import requests
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Configuración ─────────────────────────────────────────────────────────────

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GAMMA_API      = "https://gamma-api.polymarket.com"
CLOB_API       = "https://clob.polymarket.com"
NBA_SERIES_ID  = 10345
NEA_UMBRAL     = 5.0
GEMINI_MODEL   = "gemini-flash-lite-latest"

HEADERS = {"User-Agent": "Mozilla/5.0"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 1 — POLYMARKET (Gamma + CLOB)
# ══════════════════════════════════════════════════════════════════════════════

def obtener_partidos_hoy() -> list[dict]:
    hoy = date.today().strftime("%Y-%m-%d")
    resp = SESSION.get(
        f"{GAMMA_API}/events",
        params={
            "series_id": NBA_SERIES_ID, "tag_id": 100639,
            "active": "true", "closed": "false",
            "limit": 50, "order": "startTime", "ascending": "true",
        }, timeout=15
    )
    resp.raise_for_status()
    todos  = resp.json()
    return [e for e in todos if e.get("eventDate") == hoy]


def clasificar_mercado(pregunta: str) -> str | None:
    p, pl = pregunta.strip(), pregunta.lower()
    excluir = [
        "points o/u", "rebounds o/u", "assists o/u", "steals o/u",
        "blocks o/u", "turnovers o/u", "3-pointer", "field goal", "free throw",
        "first quarter", "second quarter", "third quarter", "fourth quarter",
        "first half", "second half", "halftime", "triple double", "double double",
        "will there be", "lead at any", "margin of victory", "largest lead",
    ]
    if any(ex in pl for ex in excluir): return None
    if p.startswith("Spread:"):         return "📐 Spread"
    if ": O/U" in p:                    return "🎯 Total O/U"
    if "vs." in pl and ":" not in p:    return "💰 Moneyline"
    return None


def extraer_token_ids(m: dict) -> list[str]:
    raw = m.get("clobTokenIds", "[]")
    try:   return [str(i) for i in (json.loads(raw) if isinstance(raw, str) else raw)]
    except: return []


def extraer_outcomes(m: dict) -> list[str]:
    raw = m.get("outcomes", "[]")
    try:   return json.loads(raw) if isinstance(raw, str) else raw
    except: return []


def precio_clob(token_id: str) -> tuple[str, float | None]:
    try:
        r = SESSION.get(f"{CLOB_API}/midpoint",
                        params={"token_id": token_id}, timeout=8)
        r.raise_for_status()
        mid = r.json().get("mid")
        return token_id, float(mid) if mid is not None else None
    except Exception:
        return token_id, None


def obtener_precios_paralelo(token_ids: list[str]) -> dict[str, float]:
    resultado = {}
    with ThreadPoolExecutor(max_workers=20) as pool:
        futuros = {pool.submit(precio_clob, tid): tid for tid in token_ids}
        for f in as_completed(futuros):
            tid, precio = f.result()
            if precio is not None:
                resultado[tid] = precio
    return resultado


def construir_estructura(partidos: list[dict]) -> list[dict]:
    estructura = []
    for evento in partidos:
        candidatos = []
        for m in evento.get("markets", []):
            tipo = clasificar_mercado(m.get("question", ""))
            if not tipo: continue
            token_ids = extraer_token_ids(m)
            if not token_ids: continue
            candidatos.append({
                "tipo":      tipo,
                "pregunta":  m.get("question", ""),
                "volumen":   float(m.get("volume", 0) or 0),
                "token_ids": token_ids,
                "outcomes":  extraer_outcomes(m),
            })
        seleccionados = {}
        for c in sorted(candidatos, key=lambda x: x["volumen"], reverse=True):
            if c["tipo"] not in seleccionados:
                seleccionados[c["tipo"]] = c
            if len(seleccionados) == 3: break
        estructura.append({"evento": evento, "mercados": seleccionados})
    return estructura


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 2 — GEMINI (noticias + análisis con búsqueda web)
# ══════════════════════════════════════════════════════════════════════════════

def analizar_partido_con_gemini(equipo_local: str, equipo_visitante: str,
                                 linea_ml_local: float) -> dict:
    if not GEMINI_API_KEY:
        print("  ⚠️  GEMINI_API_KEY no configurada, usando valores por defecto")
        return _valores_defecto(linea_ml_local)

    # Import lazy para evitar problemas en build
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""Eres un analista experto de apuestas deportivas NBA.
Necesito que analices el partido de HOY: {equipo_visitante} (visitante) @ {equipo_local} (local).

Usando búsqueda web, encuentra y responde EXACTAMENTE en este formato JSON (sin markdown, sin explicaciones):

{{
  "p_vegas": <número 0-100, probabilidad implícita del equipo LOCAL según las casas de apuestas hoy>,
  "n_local": <número -100 a 100, factor noticias equipo local: lesiones clave (-), alineación completa (+)>,
  "n_visitante": <número -100 a 100, factor noticias equipo visitante>,
  "r_local": <número 0-100, racha equipo local últimos 5 partidos: 5 victorias=100, 0 victorias=0>,
  "r_visitante": <número 0-100, racha equipo visitante últimos 5 partidos>,
  "resumen": "<2 oraciones: estado actual de ambos equipos, lesiones importantes y contexto del partido>"
}}

Busca específicamente:
1. Odds actuales de casas como DraftKings, FanDuel o BetMGM para {equipo_local} vs {equipo_visitante}
2. Lesiones o ausencias confirmadas para HOY
3. Resultados de los últimos 5 partidos de cada equipo

Responde SOLO el JSON."""

    try:
        respuesta_texto = ""
        for chunk in client.models.generate_content_stream(
            model=GEMINI_MODEL,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                tools=[types.Tool(googleSearch=types.GoogleSearch())],
            ),
        ):
            if chunk.text:
                respuesta_texto += chunk.text

        respuesta_texto = re.sub(r"```json|```", "", respuesta_texto).strip()
        match = re.search(r"\{.*\}", respuesta_texto, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return {
                "p_vegas":      float(data.get("p_vegas", 50)),
                "n_local":      float(data.get("n_local", 0)),
                "n_visitante":  float(data.get("n_visitante", 0)),
                "r_local":      float(data.get("r_local", 50)),
                "r_visitante":  float(data.get("r_visitante", 50)),
                "resumen":      data.get("resumen", "Sin información disponible."),
            }
    except Exception as e:
        print(f"  ⚠️  Error Gemini: {e}")

    return _valores_defecto(linea_ml_local)


def _valores_defecto(linea_ml_local: float) -> dict:
    return {
        "p_vegas":     linea_ml_local * 100,
        "n_local":     0.0,
        "n_visitante": 0.0,
        "r_local":     50.0,
        "r_visitante": 50.0,
        "resumen":     "Análisis no disponible (sin API key de Gemini).",
    }


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 3 — FÓRMULA NEA
# ══════════════════════════════════════════════════════════════════════════════

def calcular_nea(p_poly: float, p_vegas: float, n: float,
                 v: float, r: float) -> float:
    valor_real = 0.45 * p_vegas + 0.40 * n + 0.10 * v + 0.05 * r
    return p_poly - valor_real


def interpretar_nea(nea: float) -> tuple[str, str]:
    if nea <= -NEA_UMBRAL:
        return "🔥 OPORTUNIDAD", f"Polymarket está {abs(nea):.1f}pts BARATO vs valor real"
    elif nea >= NEA_UMBRAL:
        return "❌ SOBREVALUADO", f"Polymarket está {nea:.1f}pts CARO vs valor real"
    else:
        return "➖ PRECIO JUSTO", f"Precio en línea con valor real (NEA={nea:.1f})"


def extraer_equipos(titulo: str) -> tuple[str, str]:
    partes = titulo.split(" vs. ")
    if len(partes) == 2:
        return partes[0].strip(), partes[1].strip()
    return titulo, titulo


# ══════════════════════════════════════════════════════════════════════════════
# MÓDULO 4 — OUTPUT
# ══════════════════════════════════════════════════════════════════════════════

def hora_et(st: str) -> str:
    try:
        dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
        return (dt - timedelta(hours=5)).strftime("%I:%M %p ET")
    except: return st


def barra(valor: float, total: float = 100, largo: int = 20) -> str:
    ratio = max(0, min(1, valor / total))
    lleno = int(ratio * largo)
    return "█" * lleno + "░" * (largo - lleno)


def imprimir_analisis(item: dict, analisis: dict, precios: dict) -> list[dict]:
    ev      = item["evento"]
    titulo  = ev.get("title", "?")
    hora    = hora_et(ev.get("startTime", ""))
    vol     = float(ev.get("volume", 0) or 0)

    equipo_visit, equipo_local = extraer_equipos(titulo)
    oportunidades = []

    print(f"\n{'═'*68}")
    print(f"  🏀  {titulo.upper()}")
    print(f"  ⏰  {hora}   |   Vol ${vol:,.0f}")
    print(f"{'═'*68}")
    print(f"  📰  {analisis['resumen']}")
    print(f"{'─'*68}")

    ml = item["mercados"].get("💰 Moneyline")
    if not ml:
        print("  ⚠️  Sin mercado Moneyline disponible")
        return oportunidades

    for i, (outcome, token_id) in enumerate(zip(ml["outcomes"], ml["token_ids"])):
        precio_poly = precios.get(token_id)
        if precio_poly is None:
            continue

        p_poly_pct = precio_poly * 100
        es_local   = (outcome == equipo_local)
        v_factor   = 5.0 if es_local else -5.0

        if es_local:
            p_vegas = analisis["p_vegas"]
            n       = analisis["n_local"]
            r       = analisis["r_local"]
        else:
            p_vegas = 100 - analisis["p_vegas"]
            n       = analisis["n_visitante"]
            r       = analisis["r_visitante"]

        n_norm     = (n + 100) / 2
        nea        = calcular_nea(p_poly_pct, p_vegas, n_norm, v_factor, r)
        emoji, descripcion = interpretar_nea(nea)
        valor_real = 0.45 * p_vegas + 0.40 * n_norm + 0.10 * v_factor + 0.05 * r

        print(f"\n  {'🏠' if es_local else '✈️ '} {outcome.upper()} {'(LOCAL)' if es_local else '(VISITANTE)'}")
        print(f"     P_Poly  : {p_poly_pct:5.1f}  {barra(p_poly_pct)}")
        print(f"     P_Vegas : {p_vegas:5.1f}  {barra(p_vegas)}")
        print(f"     Noticias: {n:+5.1f}  (normalizado: {n_norm:.1f})")
        print(f"     Localía : {v_factor:+5.1f}")
        print(f"     Racha   : {r:5.1f}  {barra(r)}")
        print(f"     {'─'*40}")
        print(f"     Valor Real (ponderado): {valor_real:.1f}")
        print(f"     NEA = {p_poly_pct:.1f} - {valor_real:.1f} = {nea:+.1f}")
        print(f"     {emoji}: {descripcion}")

        if abs(nea) >= NEA_UMBRAL:
            direccion = "COMPRAR (precio bajo)" if nea <= -NEA_UMBRAL else "EVITAR (precio alto)"
            oportunidades.append({
                "partido":    titulo,
                "equipo":     outcome,
                "p_poly":     p_poly_pct,
                "valor_real": valor_real,
                "nea":        nea,
                "accion":     direccion,
                "hora":       hora,
            })

    spr = item["mercados"].get("📐 Spread")
    tot = item["mercados"].get("🎯 Total O/U")

    print(f"\n  {'─'*66}")
    print(f"  {'SPREAD':<32} {'TOTAL'}")

    n_rows = max(
        len(spr["outcomes"]) if spr else 0,
        len(tot["outcomes"]) if tot else 0,
    )
    for row in range(n_rows):
        spr_str = tot_str = ""
        if spr and row < len(spr["outcomes"]):
            o   = spr["outcomes"][row]
            tid = spr["token_ids"][row]
            p   = precios.get(tid)
            if p:
                try:
                    pts   = spr["pregunta"].split("(")[1].rstrip(")")
                    pts_f = float(pts)
                    fav   = spr["pregunta"].split(":")[1].split("(")[0].strip()
                    pts_label = f"{pts_f:+.1f}" if o == fav else f"{-pts_f:+.1f}"
                except: pts_label = ""
                spr_str = f"  {o} {pts_label}  →  {round(p*100)}¢"
        if tot and row < len(tot["outcomes"]):
            o   = tot["outcomes"][row]
            tid = tot["token_ids"][row]
            p   = precios.get(tid)
            if p:
                try:
                    num    = tot["pregunta"].split("O/U")[1].strip()
                    prefix = "O" if o.lower() == "over" else "U"
                    tot_str = f"  {prefix} {num}  →  {round(p*100)}¢"
                except: tot_str = f"  {o}  →  {round(p*100)}¢"
        print(f"  {spr_str:<32} {tot_str}")

    return oportunidades


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "╔" + "═"*66 + "╗")
    print("║" + "  🏀  NBA EDGE ALPHA BOT  v2.0  —  Detector de Oportunidades  ".center(66) + "║")
    print("╚" + "═"*66 + "╝")
    print(f"\n  Fecha: {date.today()}  |  Umbral NEA: ±{NEA_UMBRAL} puntos\n")

    print("📡 [1/4] Cargando partidos desde Polymarket...")
    try:
        partidos = obtener_partidos_hoy()
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return

    if not partidos:
        print("  Sin partidos para hoy.")
        return
    print(f"  ✅ {len(partidos)} partido(s) encontrado(s)")

    estructura = construir_estructura(partidos)

    print("\n💹 [2/4] Obteniendo precios CLOB en tiempo real...")
    all_tokens = list({
        tid
        for item in estructura
        for m in item["mercados"].values()
        for tid in m["token_ids"]
    })
    precios = obtener_precios_paralelo(all_tokens)
    print(f"  ✅ {len(precios)}/{len(all_tokens)} precios obtenidos")

    print(f"\n🤖 [3/4] Analizando {len(estructura)} partido(s) con Gemini + Google Search...")
    analisis_por_partido = {}

    for item in estructura:
        titulo = item["evento"].get("title", "?")
        equipo_visit, equipo_local = extraer_equipos(titulo)

        ml = item["mercados"].get("💰 Moneyline")
        p_local_clob = 0.5
        if ml:
            for outcome, tid in zip(ml["outcomes"], ml["token_ids"]):
                if outcome == equipo_local and tid in precios:
                    p_local_clob = precios[tid]
                    break

        print(f"  🔍 Analizando: {titulo}...")
        analisis = analizar_partido_con_gemini(equipo_local, equipo_visit, p_local_clob)
        analisis_por_partido[titulo] = analisis
        print(f"     P_Vegas local={analisis['p_vegas']:.1f}  N_local={analisis['n_local']:+.1f}  R_local={analisis['r_local']:.1f}")

    print(f"\n📊 [4/4] Calculando NBA Edge Alpha (NEA)...\n")

    todas_oportunidades = []
    for item in estructura:
        titulo   = item["evento"].get("title", "?")
        analisis = analisis_por_partido.get(titulo, _valores_defecto(0.5))
        ops      = imprimir_analisis(item, analisis, precios)
        todas_oportunidades.extend(ops)

    print(f"\n\n{'═'*68}")
    print(f"  🎯  RESUMEN DE OPORTUNIDADES  (NEA ≥ {NEA_UMBRAL} puntos)")
    print(f"{'═'*68}")

    if not todas_oportunidades:
        print(f"\n  ➖  No se detectaron oportunidades significativas hoy.")
        print(f"      Todos los precios están dentro del rango justo (±{NEA_UMBRAL}pts).\n")
    else:
        todas_oportunidades.sort(key=lambda x: abs(x["nea"]), reverse=True)
        for i, op in enumerate(todas_oportunidades, 1):
            emoji = "🔥" if op["nea"] <= -NEA_UMBRAL else "⚠️"
            print(f"\n  {emoji} [{i}] {op['partido']}")
            print(f"       Equipo   : {op['equipo']}")
            print(f"       Precio   : {op['p_poly']:.1f}¢  →  Valor real: {op['valor_real']:.1f}¢")
            print(f"       NEA      : {op['nea']:+.1f} pts")
            print(f"       Acción   : {op['accion']}")
            print(f"       Hora     : {op['hora']}")

    print(f"\n{'═'*68}")
    print(f"  ⚠️  Este bot es solo informativo. No constituye consejo financiero.")
    print(f"{'═'*68}\n")


if __name__ == "__main__":
    main()
