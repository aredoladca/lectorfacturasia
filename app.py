import os, re, json, time, base64, hashlib, requests, fitz, ollama
from flask import Flask, request, jsonify
from flask_cors import CORS
from difflib import SequenceMatcher

app = Flask(__name__)
CORS(app)

# ==================================================
# CONFIG
# ==================================================

MODELO = "alexander-ia"
MEMORIA_FILE = "memoria_alexander.json"

TIMEOUT_URL = 25
MIN_TEXTO_PDF = 200
MAX_TEXTO = 7000
ZOOM_PDF_ESCANEADO = 2.0

# Si quieres que /procesar devuelva una muestra del texto leído para depurar/aprender, pon True.
# En producción déjalo en False.
DEVOLVER_TEXTO_DEBUG = False

CATEGORIAS = [
    "TELECOMUNICACIONES",
    "SUMINISTROS",
    "ALQUILER",
    "SOFTWARE",
    "MARKETING Y PUBLICIDAD",
    "SERVICIOS PROFESIONALES",
    "TRANSPORTE",
    "BANCO / FINANCIERO",
    "SEGUROS",
    "FORMACIÓN",
    "LIMPIEZA Y MANTENIMIENTO",
    "COMPRA DE MERCANCÍA",
    "OTROS"
]

# Etiquetas base.
# OJO: esto NO se guarda entero en memoria por cada proveedor.
# Solo se usa para proponer pistas si el usuario corrige un campo.
ETIQUETAS_BASE = {
    "id_emisor": ["CIF", "NIF", "VAT", "VAT ID", "TAX ID", "Company ID", "C.I.F.", "ID Fiscal"],
    "fecha": ["Fecha emisión", "Fecha factura", "Invoice date", "Date", "Fecha"],
    "numero_factura": ["Nº Factura", "Número de factura", "Factura Nº", "Serie - Nº. Factura", "Invoice number", "Invoice No.", "Referencia", "Ref."],
    "concepto": ["Concepto", "Descripción", "Detalle", "Producto", "Servicio", "Description"],
    "base": ["Base imponible", "Base", "Subtotal", "Net amount"],
    "iva": ["Cuota IVA", "IVA", "VAT amount", "Tax"],
    "total": ["TOTAL A PAGAR", "Total factura", "Total", "Importe total", "Amount due", "Total due"]
}

EVITAR_BASE = {
    "id_emisor": ["Datos del cliente", "Cliente", "Receptor", "Destinatario", "Billing to", "Customer", "Titular"],
    "fecha": ["Fecha vencimiento", "Periodo de consumo", "Periodo facturado", "Rango de servicio"],
    "numero_factura": ["CIF", "NIF", "VAT", "IBAN", "Contrato", "Cliente", "Teléfono", "Fecha"],
    "base": ["Consumo medio", "€/día", "Histórico", "Bonificación", "Descuento informativo"],
    "iva": ["Consumo medio", "Histórico", "Bonificación", "Descuento informativo"],
    "total": ["Consumo medio", "€/día", "Histórico", "Bonificación", "Descuento informativo", "Saldo informativo"]
}

ZONA_BASE = {
    "id_emisor": "cabecera_izquierda_o_datos_fiscales_emisor",
    "nombre_emisor": "cabecera_izquierda_o_logo",
    "fecha": "cabecera_derecha_o_bloque_datos_factura",
    "numero_factura": "cabecera_derecha_o_bloque_datos_factura",
    "concepto": "tabla_lineas_o_detalle_servicio",
    "base": "bloque_inferior_totales",
    "iva": "bloque_inferior_totales",
    "total": "bloque_inferior_totales"
}

# ==================================================
# LOGS / UTILS
# ==================================================

def log(sec, msg, ico="🔹"):
    print(f"[{time.strftime('%H:%M:%S')}] {ico} {sec:<16} | {msg}", flush=True)

def sep(t):
    print("\n" + "═" * 100, flush=True)
    log("SISTEMA", t, "🚀")

def norm(v):
    v = str(v or "").upper()
    v = re.sub(r"\s+", " ", v)
    return re.sub(r"[^A-ZÁÉÍÓÚÜÑ0-9.,€/%\-:() ]", " ", v).strip()

def key(v):
    return re.sub(r"[^A-Z0-9]", "", norm(v))

def ratio(a, b):
    return SequenceMatcher(None, norm(a), norm(b)).ratio()

def url_hash(url):
    return hashlib.sha1(str(url).encode("utf-8")).hexdigest()[:16]

def limpiar_id(v):
    k = key(v)
    return "N/A" if not k or k in ["NA", "NONE", "NULL"] else k

def id_base(v):
    k = limpiar_id(v)
    return k[2:] if k.startswith("ES") and len(k) > 4 else k

def ids_eq(v):
    k = limpiar_id(v)
    if k == "N/A":
        return []
    return list(dict.fromkeys([k, k[2:] if k.startswith("ES") else "ES" + k]))

def num(v):
    try:
        s = re.sub(r"[^\d,.-]", "", str(v or ""))

        if not s:
            return 0.0

        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
        elif "," in s:
            s = s.replace(",", ".")

        return round(float(s), 2)

    except:
        return 0.0

def dinero(v):
    try:
        return "{:,.2f}".format(float(v)).replace(",", "X").replace(".", ",").replace("X", " ")
    except:
        return "0,00"

def importe_presente(v):
    s = str(v or "").strip().upper()
    return s not in ["", "N/A", "NA", "NONE", "NULL"]

def factura_ok(v):
    s, k = norm(v), key(v)

    if not s or s == "N/A":
        return False

    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", s):
        return False

    if re.fullmatch(r"[A-Z]?\d{7,9}[A-Z]?", k):
        return False

    if num(s) > 0 and len(k) < 8:
        return False

    return True

def fecha_ok(v):
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(v or "").strip())

    if not m:
        return False

    d, mo, y = map(int, m.groups())
    return 1 <= d <= 31 and 1 <= mo <= 12 and 1900 <= y <= 2100

def id_ok(v):
    k = limpiar_id(v)

    pats = [
        r"^ES[A-Z]\d{8}$",
        r"^[A-Z]\d{8}$",
        r"^\d{8}[A-Z]$",
        r"^[XYZ]\d{7}[A-Z]$",
        r"^[A-Z]{2}[A-Z0-9]{8,14}$"
    ]

    return k != "N/A" and any(re.fullmatch(p, k) for p in pats)

def categoria_ok(c):
    c = norm(c)
    return c if c in CATEGORIAS else "OTROS"

def keywords(t, n=20):
    stop = {
        "FACTURA", "FECHA", "TOTAL", "BASE", "IVA", "CLIENTE",
        "IMPORTE", "NUMERO", "NÚMERO", "DATOS", "PAGO",
        "OTROS", "NONE", "NULL"
    }

    out = []

    for p in re.findall(r"[A-ZÁÉÍÓÚÜÑ]{4,}", norm(t)):
        if p not in stop and p not in out:
            out.append(p)

    return out[:n]

def add_unique(lista, valores, limite=8):
    lista = lista if isinstance(lista, list) else []
    valores = valores if isinstance(valores, list) else [valores]

    out = list(lista)

    for v in valores:
        v = str(v or "").strip()

        if v and v not in out:
            out.append(v)

    return out[-limite:]

def regex_factura(v):
    s = re.sub(r"\s+", "", str(v or "").upper())

    if not factura_ok(s):
        return ""

    # DD2026-16814 -> \bDD2026\-\d+\b
    m = re.match(r"^([A-Z]+)(\d{4})([-/]?)(\d+)$", s)

    if m:
        pre, year, sep, _ = m.groups()
        return rf"\b{re.escape(pre)}{year}{re.escape(sep) if sep else ''}\d+\b"

    # MC260002423035 -> \bMC\d+\b
    m = re.match(r"^([A-Z]+)(\d+)$", s)

    if m:
        return rf"\b{re.escape(m.group(1))}\d+\b"

    # 20260671720
    if re.fullmatch(r"\d{8,}", s):
        return r"\b\d{8,}\b"

    r = re.escape(s)
    r = re.sub(r"(?:\\\d)+", r"\\d+", r)

    return rf"\b{r}\b"

def regex_id(v):
    k = limpiar_id(v)

    if k == "N/A":
        return ""

    if k.startswith("ES"):
        return rf"\b(?:ES)?{re.escape(k[2:])}\b"

    return rf"\b(?:ES)?{re.escape(k)}\b"

def math_ok(base, iva, total):
    if base > 0 and iva > 0 and total > 0:
        return abs((base + iva) - total) <= 0.05

    return True

def contexto_alrededor(texto, valor, ancho=90):
    """
    Busca el valor corregido dentro del texto y guarda un contexto corto.
    Esto sí es personalizado porque captura frases reales de la factura.
    """
    if not texto or not valor:
        return ""

    t = str(texto)
    v = str(valor).strip()

    if not v or v.upper() in ["N/A", "NA", "NONE", "NULL"]:
        return ""

    # Busca literal flexible con espacios.
    patron = re.escape(v)
    patron = patron.replace(r"\ ", r"\s+")

    m = re.search(patron, t, re.I)

    if not m:
        # Para importes, prueba con punto/coma.
        nv = num(v)
        if nv > 0:
            posibles = [
                dinero(nv),
                f"{nv:.2f}".replace(".", ","),
                f"{nv:.2f}"
            ]

            for p in posibles:
                mm = re.search(re.escape(p), t, re.I)
                if mm:
                    m = mm
                    break

    if not m:
        return ""

    ini = max(0, m.start() - ancho)
    fin = min(len(t), m.end() + ancho)
    ctx = re.sub(r"\s+", " ", t[ini:fin]).strip()

    return ctx[:220]

def detectar_etiquetas_en_contexto(ctx, campo):
    """
    De un contexto real, intenta detectar qué etiqueta aparece antes/cerca del valor.
    """
    if not ctx:
        return []

    etiquetas = ETIQUETAS_BASE.get(campo, [])
    encontradas = []

    for e in etiquetas:
        if norm(e) in norm(ctx):
            encontradas.append(e)

    return encontradas[:4]

# ==================================================
# CIF / ID PRE-SCAN
# ==================================================

def extraer_cifs_posibles(texto):
    t = str(texto or "").upper()

    pats = [
        r"\b[ABCDEFGHJKLMNPQRSUVW]\d{7}[0-9A-Z]\b",
        r"\b\d{8}[TRWAGMYFPDXBNJZSQVHLCKE]\b",
        r"\b[XYZ]\d{7}[TRWAGMYFPDXBNJZSQVHLCKE]\b",
        r"\bES[A-Z0-9]{9}\b"
    ]

    encontrados = []

    for p in pats:
        encontrados.extend(re.findall(p, t))

    return list(dict.fromkeys(encontrados))

# ==================================================
# MEMORIA / PLANTILLAS
# ==================================================

def memoria_base():
    return {
        "version": "memoria-alexander-plantillas-5.0",
        "proveedores": {},
        "urls": {}
    }

def plantilla_base():
    """
    Plantilla compacta por proveedor.
    Guarda pistas, no frases largas.
    """
    return {
        "id_emisor": {
            "buscar": [],
            "evitar": [],
            "regex": "",
            "zona": "",
            "contexto_limpio": []
        },
        "nombre_emisor": {
            "buscar": [],
            "evitar": [],
            "zona": "",
            "contexto_limpio": []
        },
        "fecha": {
            "buscar": [],
            "evitar": [],
            "regex": "",
            "zona": "",
            "contexto_limpio": []
        },
        "numero_factura": {
            "buscar": [],
            "evitar": [],
            "regex": "",
            "zona": "",
            "contexto_limpio": []
        },
        "concepto": {
            "buscar": [],
            "evitar": [],
            "zona": "",
            "contexto_limpio": []
        },
        "base": {
            "buscar": [],
            "evitar": [],
            "zona": "",
            "contexto_limpio": []
        },
        "iva": {
            "buscar": [],
            "evitar": [],
            "zona": "",
            "contexto_limpio": []
        },
        "total": {
            "buscar": [],
            "evitar": [],
            "zona": "",
            "prioridad": "",
            "contexto_limpio": []
        }
    }

def stats_base():
    return {
        "correcciones": 0,
        "fallos": {},
        "actualizado": ""
    }

def proveedor_base(id_e):
    return {
        "id": id_e,
        "ids": ids_eq(id_e),
        "nombre": "N/A",
        "alias": [],
        "categoria": "OTROS",
        "keywords": [],
        "regex_factura": "",
        "plantilla": plantilla_base(),
        "stats": stats_base()
    }

def migrar_plantilla(p):
    base = plantilla_base()
    tpl = p.get("plantilla", {})

    # Compatibilidad con la versión anterior de prompt_personalizado.
    # Si existía, no copiamos las reglas largas: solo dejamos plantilla limpia.
    if not isinstance(tpl, dict):
        tpl = {}

    for campo, datos in base.items():
        viejo = tpl.get(campo, {})

        if not isinstance(viejo, dict):
            viejo = {}

        for k, v in datos.items():
            viejo.setdefault(k, v)

        # Normalizar tipos
        for lk in ["buscar", "evitar", "contexto_limpio"]:
            if not isinstance(viejo.get(lk), list):
                viejo[lk] = []

        base[campo] = viejo

    return base

def migrar_stats(p):
    st = p.get("stats", {})

    if not isinstance(st, dict):
        st = {}

    st.setdefault("correcciones", p.get("correcciones", 0))
    st.setdefault("fallos", {})
    st.setdefault("actualizado", p.get("actualizado", p.get("ultima_actualizacion", "")))

    if not isinstance(st["fallos"], dict):
        st["fallos"] = {}

    return st

def migrar_p(p, pid="N/A"):
    id_fijo = p.get("id") or p.get("id_fijo") or pid

    return {
        "id": id_fijo,
        "ids": p.get("ids", p.get("ids_alternativos", ids_eq(id_fijo))),
        "nombre": p.get("nombre", "N/A"),
        "alias": p.get("alias", p.get("alias_visuales", [])),
        "categoria": p.get("categoria", p.get("categoria_habitual", "OTROS")),
        "keywords": p.get("keywords", []),
        "regex_factura": p.get("regex_factura", ""),
        "plantilla": migrar_plantilla(p),
        "stats": migrar_stats(p)
    }

def cargar_memoria():
    if not os.path.exists(MEMORIA_FILE):
        log("MEMORIA", "No existe. Memoria limpia.", "📂")
        return memoria_base()

    try:
        with open(MEMORIA_FILE, "r", encoding="utf-8") as f:
            m = json.load(f)

        m.setdefault("version", "memoria-alexander-plantillas-5.0")
        m.setdefault("proveedores", {})
        m.setdefault("urls", {})

        for pid in list(m["proveedores"]):
            m["proveedores"][pid] = migrar_p(m["proveedores"][pid], pid)

        log("MEMORIA", f"OK proveedores={len(m['proveedores'])} urls={len(m['urls'])}", "📂")
        return m

    except Exception as e:
        log("MEMORIA", f"Error leyendo memoria: {e}", "⚠️")
        return memoria_base()

def guardar_memoria(m):
    m["version"] = "memoria-alexander-plantillas-5.0"

    with open(MEMORIA_FILE, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=4, ensure_ascii=False)

    log("MEMORIA", "Guardada.", "💾")

def buscar_pid_equiv(id_e, mem):
    cand = set(ids_eq(id_e) + [id_base(id_e)])

    for pid, p in mem.get("proveedores", {}).items():
        p = migrar_p(p, pid)

        ids = set([pid, p.get("id", "")] + p.get("ids", []))
        ids = {
            x
            for i in ids
            for x in [limpiar_id(i), id_base(i)]
            if x != "N/A"
        }

        if cand & ids:
            return pid

    return None

def buscar_url(url, mem):
    h = url_hash(url)

    if h in mem.get("urls", {}):
        log("FAST URL", "URL encontrada. Última corrección aplicada.", "⚡")
        return mem["urls"][h].get("resultado")

    return None

def memoria_prompt(mem, limite=6):
    """
    Memoria general muy compacta.
    No incluye plantillas completas de todos.
    """
    proveedores = []

    ordenados = sorted(
        mem.get("proveedores", {}).items(),
        key=lambda x: x[1].get("stats", {}).get("correcciones", 0),
        reverse=True
    )[:limite]

    for pid, p in ordenados:
        p = migrar_p(p, pid)

        proveedores.append({
            "id": p.get("id"),
            "ids": p.get("ids", [])[:3],
            "nombre": p.get("nombre"),
            "alias": p.get("alias", [])[:3],
            "categoria": p.get("categoria"),
            "keywords": p.get("keywords", [])[:6],
            "regex_factura": p.get("regex_factura"),
            "correcciones": p.get("stats", {}).get("correcciones", 0)
        })

    return {"proveedores": proveedores}

def plantilla_prompt(p, max_contextos=2):
    """
    Convierte una plantilla estructurada en texto corto para Ollama.
    Solo manda campos que tengan información útil.
    """
    p = migrar_p(p, p.get("id", "N/A"))
    tpl = p.get("plantilla", {})
    lineas = []

    for campo in ["id_emisor", "fecha", "numero_factura", "concepto", "base", "iva", "total"]:
        d = tpl.get(campo, {})

        partes = []

        if d.get("buscar"):
            partes.append("buscar=" + ", ".join([f'"{x}"' for x in d["buscar"][:5]]))

        if d.get("evitar"):
            partes.append("evitar=" + ", ".join([f'"{x}"' for x in d["evitar"][:4]]))

        if d.get("regex"):
            partes.append(f"regex={d.get('regex')}")

        if d.get("zona"):
            partes.append(f"zona={d.get('zona')}")

        if d.get("prioridad"):
            partes.append(f"prioridad={d.get('prioridad')}")

        ctxs = d.get("contexto_limpio", [])[-max_contextos:]

        if ctxs:
            partes.append("contexto=" + " | ".join([f'"{x}"' for x in ctxs]))

        if partes:
            lineas.append(f"- {campo}: " + "; ".join(partes))

    if not lineas:
        return ""

    return "PLANTILLA COMPACTA DEL PROVEEDOR DETECTADO:\n" + "\n".join(lineas)

def pre_buscar_memoria(texto, mem):
    tnorm = norm(texto)
    tkey = key(texto)

    for cand in extraer_cifs_posibles(texto):
        pid = buscar_pid_equiv(cand, mem)

        if pid:
            log("ANCLA", f"CIF detectado: {cand} -> {pid}", "⚓")
            return pid, mem["proveedores"].get(pid)

    mejor_pid = None
    mejor_score = 0

    for pid, p in mem.get("proveedores", {}).items():
        p = migrar_p(p, pid)
        score = 0

        for i in p.get("ids", []):
            ik = key(i)

            if ik and ik in tkey:
                score += 0.80

        nombre = norm(p.get("nombre", "N/A"))

        if nombre != "N/A" and nombre in tnorm:
            score += 0.50

        for alias in p.get("alias", []):
            a = norm(alias)

            if a and a in tnorm:
                score += 0.45

        for kw in p.get("keywords", []):
            k = norm(kw)

            if k and k in tnorm:
                score += 0.08

        score = min(score, 1)

        if score > mejor_score:
            mejor_score = score
            mejor_pid = pid

    if mejor_pid and mejor_score >= 0.45:
        log("ANCLA", f"Proveedor anclado por memoria: {mejor_pid} score={mejor_score}", "⚓")
        return mejor_pid, mem["proveedores"].get(mejor_pid)

    log("ANCLA", "Sin proveedor anclado.", "⚪")
    return None, None

def buscar_proveedor_local(texto, raw, mem):
    tn = norm(texto + " " + json.dumps(raw, ensure_ascii=False))
    tk = key(tn)

    rid = limpiar_id(raw.get("id_emisor", ""))
    rnom = norm(raw.get("nombre_emisor", ""))
    rfac = str(raw.get("numero_factura", "") or "")

    mejor, score_m = None, 0

    for pid, p in mem.get("proveedores", {}).items():
        p = migrar_p(p, pid)
        score = 0

        ids = set([pid, p.get("id", "")] + p.get("ids", []))
        ids = {
            x
            for i in ids
            for x in [limpiar_id(i), id_base(i)]
            if x != "N/A"
        }

        if rid != "N/A" and (rid in ids or id_base(rid) in ids):
            score += 0.85

        if any(x and x in tk for x in ids):
            score += 0.70

        nombre = norm(p.get("nombre"))

        if nombre != "N/A" and nombre in tn:
            score += 0.25

        for a in p.get("alias", []):
            an = norm(a)

            if an and an in tn:
                score += 0.35

            if an and rnom and ratio(an, rnom) > 0.65:
                score += 0.35

        rx = p.get("regex_factura", "")

        try:
            if rx and rfac and re.search(rx, rfac, re.I):
                score += 0.35
        except:
            pass

        for kw in p.get("keywords", []):
            if norm(kw) and norm(kw) in tn:
                score += 0.05

        if min(score, 1) > score_m:
            mejor, score_m = pid, min(score, 1)

    if score_m >= 0.45:
        log("MEMORIA", f"Proveedor reconocido: {mejor} score={score_m}", "🧠")
        return mejor, score_m

    log("MEMORIA", "Proveedor no reconocido.", "⚪")
    return None, 0

# ==================================================
# LECTOR PDF / IMAGEN
# ==================================================

def leer_archivo(url):
    log("RED", f"Descargando {url[:90]}", "🌐")

    r = requests.get(url, timeout=TIMEOUT_URL)
    r.raise_for_status()

    data = r.content

    if data[:4] == b"%PDF":
        log("PDF", "PDF detectado.", "📄")

        doc = fitz.open(stream=data, filetype="pdf")
        n = len(doc)

        if n == 1:
            pags = [doc[0]]
            log("PDF", "Leo página 1.", "📄")

        elif n == 2:
            pags = [doc[0], doc[1]]
            log("PDF", "Leo página 1 y 2.", "📄")

        else:
            pags = [doc[0], doc[1], doc[-1]]
            log("PDF", f"PDF largo {n} páginas. Leo 1, 2 y última.", "📄")

        texto = ""

        for p in pags:
            bloques = p.get_text("blocks")
            bloques = sorted(bloques, key=lambda b: (round(b[1], 1), round(b[0], 1)))

            for b in bloques:
                texto += str(b[4]) + "\n"

        chars = len(norm(texto))

        if chars >= MIN_TEXTO_PDF:
            log("PDF", f"Texto seleccionable por bloques: {chars} chars.", "✅")
            return "pdf_texto", texto, ""

        log("PDF", "Escaneado o poco texto. Render a imagen.", "🖼️")

        pix = doc[0].get_pixmap(
            matrix=fitz.Matrix(ZOOM_PDF_ESCANEADO, ZOOM_PDF_ESCANEADO),
            alpha=False
        )

        return "imagen", "", base64.b64encode(pix.tobytes("png")).decode()

    log("IMAGEN", "Imagen directa.", "🖼️")
    return "imagen", "", base64.b64encode(data).decode()

# ==================================================
# PROMPT
# ==================================================

def prompt_app(modo, mem, texto="", ancla_pid=None, ancla_datos=None):
    bloque = (
        f"MODO: PDF_TEXTO_SELECCIONABLE\n\nTEXTO DE FACTURA:\n{texto[:MAX_TEXTO]}"
        if modo == "pdf"
        else "MODO: IMAGEN_FACTURA\n\nAnaliza la imagen completa."
    )

    instruccion_ancla = ""

    if ancla_pid:
        nombre = "N/A"
        categoria = "OTROS"
        tpl_txt = ""

        if ancla_datos:
            ancla_datos = migrar_p(ancla_datos, ancla_pid)
            nombre = ancla_datos.get("nombre", "N/A")
            categoria = ancla_datos.get("categoria", "OTROS")
            tpl_txt = plantilla_prompt(ancla_datos)

            if tpl_txt:
                log("PLANTILLA", "Plantilla compacta inyectada al prompt.", "🧩")

        instruccion_ancla = f"""
PROVEEDOR ANCLADO POR MEMORIA:
- id_emisor probable: {ancla_pid}
- nombre conocido: {nombre}
- categoría habitual: {categoria}

Usa esta información como referencia fuerte, pero la factura actual manda.
Nunca copies importes, fechas ni números de factura desde memoria.

{tpl_txt}
"""

    return f"""
MEMORIA COMPACTA:
{json.dumps(memoria_prompt(mem), ensure_ascii=False)}

{instruccion_ancla}

{bloque}

CATEGORÍAS PERMITIDAS:
{", ".join(CATEGORIAS)}

Devuelve exclusivamente el JSON definido en tu configuración.
""".strip()

# ==================================================
# IA
# ==================================================

def normalizar_raw_ia(raw):
    if not isinstance(raw, dict):
        return {}

    if "result" in raw and isinstance(raw["result"], dict):
        raw = raw["result"]

    mapa = {
        "invoice_number": "numero_factura",
        "invoice_no": "numero_factura",
        "invoice": "numero_factura",
        "factura": "numero_factura",
        "n_factura": "numero_factura",
        "num_factura": "numero_factura",
        "numero": "numero_factura",

        "supplier_tax_id": "id_emisor",
        "tax_id": "id_emisor",
        "vat": "id_emisor",
        "cif": "id_emisor",
        "nif": "id_emisor",
        "id": "id_emisor",

        "supplier": "nombre_emisor",
        "proveedor": "nombre_emisor",
        "emisor": "nombre_emisor",
        "empresa": "nombre_emisor",

        "date": "fecha",
        "invoice_date": "fecha",

        "description": "concepto_breve",
        "concepto": "concepto_breve",
        "descripcion": "concepto_breve",

        "category": "categoria_sugerida",
        "categoria": "categoria_sugerida",

        "base": "base_imponible",
        "subtotal": "base_imponible",
        "net_amount": "base_imponible",

        "iva": "cuota_iva",
        "vat_amount": "cuota_iva",
        "tax": "cuota_iva",

        "total": "total_factura",
        "amount_due": "total_factura",
        "total_due": "total_factura"
    }

    limpio = dict(raw)

    for origen, destino in mapa.items():
        if destino not in limpio and origen in raw:
            limpio[destino] = raw.get(origen)

    limpio.setdefault("id_emisor", "N/A")
    limpio.setdefault("nombre_emisor", "N/A")
    limpio.setdefault("fecha", "N/A")
    limpio.setdefault("numero_factura", "N/A")
    limpio.setdefault("concepto_breve", "N/A")
    limpio.setdefault("categoria_sugerida", "OTROS")
    limpio.setdefault("razon_categoria", "N/A")
    limpio.setdefault("base_imponible", "N/A")
    limpio.setdefault("cuota_iva", "N/A")
    limpio.setdefault("total_factura", "N/A")
    limpio.setdefault("candidatos", [])
    limpio.setdefault("confianza", {})
    limpio.setdefault("notas", "N/A")

    return limpio

def ia(prompt, img=""):
    log("IA", f"Llamando {MODELO}", "🤖")
    t = time.time()

    try:
        r = ollama.generate(
            model=MODELO,
            prompt=prompt,
            format="json",
            images=[img] if img else []
        )

        raw = json.loads(r.get("response", "{}"))
        raw = normalizar_raw_ia(raw)

        log("IA", f"OK {time.time() - t:.2f}s keys={list(raw.keys())}", "✅")
        return raw

    except Exception as e:
        log("IA", f"Error: {e}", "❌")
        return normalizar_raw_ia({})

# ==================================================
# POSTPROCESADO
# ==================================================

def total_claro_en_raw(raw):
    return importe_presente(raw.get("total_factura", "N/A"))

def importes_pdf_math(texto, raw):
    if total_claro_en_raw(raw):
        log("IMPORTES", f"Total claro respetado: {raw.get('total_factura')}", "🛡️")
        return raw

    vals = []

    for v in re.findall(r"\d{1,3}(?:\.\d{3})*(?:,\d{2})|\d+\.\d{2}|\d+,\d{2}", texto):
        n = num(v)

        if n > 0 and n not in vals:
            vals.append(n)

    vals = sorted(vals)

    for total in sorted(vals, reverse=True):
        menores = [x for x in vals if x < total]

        for base in sorted(menores, reverse=True):
            for iva in sorted(menores):
                if base >= iva and abs((base + iva) - total) <= 0.05:
                    raw["base_imponible"] = dinero(base)
                    raw["cuota_iva"] = dinero(iva)
                    raw["total_factura"] = dinero(total)
                    raw["notas"] = "importes reforzados por matemática en PDF"

                    log("IMPORTES", f"PDF math base={base} iva={iva} total={total}", "🧮")
                    return raw

    return raw

def normalizar(raw, pid="N/A", score=0, metodo="ia", ancla_ide=None, ancla_datos=None):
    if ancla_datos:
        ancla_datos = migrar_p(ancla_datos, ancla_ide or pid)

        if limpiar_id(raw.get("id_emisor")) == "N/A" and ancla_ide:
            raw["id_emisor"] = ancla_ide
            raw["notas"] = "ID recuperado vía ancla en pre-escaneo."

        if norm(raw.get("nombre_emisor", "N/A")) == "N/A" and ancla_datos.get("nombre", "N/A") != "N/A":
            raw["nombre_emisor"] = ancla_datos.get("nombre")

        if categoria_ok(raw.get("categoria_sugerida", "OTROS")) == "OTROS" and ancla_datos.get("categoria", "OTROS") != "OTROS":
            raw["categoria_sugerida"] = ancla_datos.get("categoria")

    elif limpiar_id(raw.get("id_emisor")) == "N/A" and ancla_ide:
        raw["id_emisor"] = ancla_ide
        raw["notas"] = "ID recuperado vía ancla en pre-escaneo."

    b = num(raw.get("base_imponible"))
    i = num(raw.get("cuota_iva"))
    t = num(raw.get("total_factura"))

    total_presente = importe_presente(raw.get("total_factura"))

    if t == 0 and not total_presente:
        cand = [num(x) for x in raw.get("candidatos", []) if num(x) > 0]
        t = max(cand) if cand else 0

    rid = limpiar_id(raw.get("id_emisor"))
    factura = str(raw.get("numero_factura", "N/A")).strip()
    fecha = str(raw.get("fecha", "N/A")).strip()
    ok_math = math_ok(b, i, t)

    out = {
        "id": rid,
        "nombre_emisor": norm(raw.get("nombre_emisor", "N/A")),
        "numero_factura": factura,
        "fecha": fecha,
        "concepto": norm(raw.get("concepto_breve", "N/A")),
        "categoria": categoria_ok(raw.get("categoria_sugerida", "OTROS")),
        "razon_categoria": raw.get("razon_categoria", "N/A"),
        "base": dinero(b),
        "iva": dinero(i),
        "total": dinero(t),
        "confianza": raw.get("confianza", {}),
        "notas": raw.get("notas", "N/A"),
        "metodo": metodo,
        "proveedor_memoria": pid,
        "proveedor_score": round(score, 2),
        "math_ok": ok_math,
        "needs_review": (
            rid == "N/A"
            or not factura_ok(factura)
            or fecha == "N/A"
            or not total_presente
            or not ok_math
        )
    }

    log("RESULTADO", f"{metodo} id={out['id']} fact={factura} total={out['total']} review={out['needs_review']}", "📊")
    return out

# ==================================================
# VALIDACIÓN Y APRENDIZAJE DE PLANTILLA
# ==================================================

def validar_correccion(c):
    errores, avisos = [], []

    b = num(c.get("base"))
    i = num(c.get("iva"))
    t = num(c.get("total"))

    total_presente = importe_presente(c.get("total"))

    if not id_ok(c.get("id")):
        errores.append("ID fiscal sospechoso")

    if not factura_ok(c.get("numero_factura")):
        errores.append("Número de factura sospechoso")

    if not fecha_ok(c.get("fecha")):
        errores.append("Fecha inválida")

    if not total_presente:
        errores.append("Total vacío o inválido")

    if total_presente and t > 0:
        if b > 0 and t < b:
            errores.append("Total menor que base")

        if i > 0 and t < i:
            errores.append("Total menor que IVA")

    if b > 0 and i > 0 and t > 0 and not math_ok(b, i, t):
        avisos.append("Base + IVA no coincide con total")

    if b > 0 and i > 0 and abs(b - i) <= 0.01 and not math_ok(b, i, t):
        avisos.append("IVA parece copiado de la base")

    return {
        "ok": not errores and not avisos,
        "errores": errores,
        "avisos": avisos
    }

def registrar_fallo(stats, campo):
    fallos = stats.setdefault("fallos", {})
    fallos[campo] = fallos.get(campo, 0) + 1
    return stats

def actualizar_campo_plantilla(tpl, campo, valor_correcto, texto_factura="", original_val="N/A"):
    """
    Guarda pistas compactas y personalizadas:
    - buscar: etiquetas realmente encontradas en contexto, o etiquetas base mínimas si no hay texto.
    - evitar: etiquetas de riesgo.
    - zona: zona habitual del campo.
    - regex: para ID y factura.
    - contexto_limpio: contexto real alrededor del valor corregido si hay texto.
    """
    if campo not in tpl:
        return tpl

    d = tpl[campo]

    ctx = contexto_alrededor(texto_factura, valor_correcto)
    etiquetas_ctx = detectar_etiquetas_en_contexto(ctx, campo)

    # Si hay contexto y contiene etiquetas reales, usamos esas.
    if etiquetas_ctx:
        d["buscar"] = add_unique(d.get("buscar", []), etiquetas_ctx, limite=6)

    # Si no hay contexto, guardamos pocas etiquetas base, no una lista enorme.
    elif campo in ETIQUETAS_BASE:
        d["buscar"] = add_unique(d.get("buscar", []), ETIQUETAS_BASE[campo][:2], limite=6)

    if campo in EVITAR_BASE:
        d["evitar"] = add_unique(d.get("evitar", []), EVITAR_BASE[campo][:3], limite=6)

    if campo in ZONA_BASE and not d.get("zona"):
        d["zona"] = ZONA_BASE[campo]

    if ctx:
        d["contexto_limpio"] = add_unique(d.get("contexto_limpio", []), ctx, limite=4)

    if campo == "id_emisor":
        d["regex"] = regex_id(valor_correcto)

    if campo == "numero_factura":
        d["regex"] = regex_factura(valor_correcto)

    if campo == "total":
        nctx = norm(ctx)
        if "TOTAL A PAGAR" in nctx:
            d["prioridad"] = "usar_total_a_pagar_si_existe"
            d["buscar"] = add_unique(d.get("buscar", []), "TOTAL A PAGAR", limite=6)

    tpl[campo] = d
    return tpl

def aprender_plantilla(p, original, corregido, texto_factura=""):
    """
    Compara original vs corrección.
    Solo aprende si hubo diferencia real.
    Guarda estructura compacta, no prompts largos.
    """
    if not isinstance(original, dict):
        original = {}

    tpl = p.get("plantilla", plantilla_base())
    stats = p.get("stats", stats_base())

    mapa_campos = {
        "id": "id_emisor",
        "nombre_emisor": "nombre_emisor",
        "numero_factura": "numero_factura",
        "fecha": "fecha",
        "concepto": "concepto",
        "categoria": "categoria",
        "base": "base",
        "iva": "iva",
        "total": "total"
    }

    aprendidos = 0

    for campo_original, campo_tpl in mapa_campos.items():
        ok_val = str(corregido.get(campo_original, "N/A")).strip()
        ia_val = str(original.get(campo_original, "N/A")).strip()

        if not ok_val or ok_val.upper() in ["N/A", "NA", "NONE", "NULL"]:
            continue

        if norm(ok_val) != norm(ia_val):
            stats = registrar_fallo(stats, campo_original)

            # La categoría no tiene plantilla de extracción visual, se guarda en proveedor.
            if campo_original == "categoria":
                aprendidos += 1
                continue

            tpl = actualizar_campo_plantilla(
                tpl,
                campo_tpl,
                ok_val,
                texto_factura=texto_factura,
                original_val=ia_val
            )

            aprendidos += 1

    p["plantilla"] = tpl
    p["stats"] = stats

    if aprendidos:
        log("PLANTILLA", f"{aprendidos} pistas compactas aprendidas.", "🧩")
    else:
        log("PLANTILLA", "Sin cambios útiles para plantilla.", "⚪")

    return p

def aprender(mem, c, url, validacion, original=None, texto_factura=""):
    id_e = limpiar_id(c.get("id"))
    pid = buscar_pid_equiv(id_e, mem) or id_e

    p = migrar_p(mem["proveedores"].get(pid, proveedor_base(id_e)), pid)

    nombre = norm(c.get("nombre_emisor", c.get("nombre", "N/A")))
    concepto = norm(c.get("concepto", "N/A"))
    categoria = categoria_ok(c.get("categoria", "OTROS"))
    factura = str(c.get("numero_factura", "N/A")).strip()
    ahora = time.strftime("%Y-%m-%d %H:%M:%S")

    resultado = {
        "id": pid,
        "nombre_emisor": nombre if nombre != "N/A" else p.get("nombre", "N/A"),
        "numero_factura": factura,
        "fecha": c.get("fecha", "N/A"),
        "concepto": concepto,
        "categoria": categoria,
        "base": c.get("base", "0,00"),
        "iva": c.get("iva", "0,00"),
        "total": c.get("total", "0,00"),
        "validacion": validacion,
        "needs_review": not validacion["ok"],
        "actualizado": ahora
    }

    errores_graves = len(validacion.get("errores", [])) > 0

    if not errores_graves:
        rx = regex_factura(factura)

        p["id"] = id_e
        p["ids"] = list(dict.fromkeys(p.get("ids", []) + ids_eq(id_e)))

        if categoria != "OTROS":
            p["categoria"] = categoria

        p["stats"]["correcciones"] = p["stats"].get("correcciones", 0) + 1
        p["stats"]["actualizado"] = ahora

        if nombre != "N/A":
            if p.get("nombre", "N/A") == "N/A":
                p["nombre"] = nombre

            p["alias"] = list(dict.fromkeys(p.get("alias", []) + [nombre]))[:8]

        if rx:
            p["regex_factura"] = rx

        fuente_kw = " ".join([
            "" if nombre == "N/A" else nombre,
            "" if concepto == "N/A" else concepto,
            "" if categoria == "OTROS" else categoria
        ])

        nuevas_kw = keywords(fuente_kw)

        if nuevas_kw:
            p["keywords"] = list(dict.fromkeys(p.get("keywords", []) + nuevas_kw))[:16]

        p = aprender_plantilla(
            p,
            original or {},
            resultado,
            texto_factura=texto_factura
        )

        mem["proveedores"][pid] = p

        if validacion["ok"]:
            log("MEMORIA", "Proveedor actualizado con plantilla válida.", "🧠")
        else:
            log("MEMORIA", "Proveedor actualizado, pero URL queda con avisos.", "⚠️")

    else:
        log("MEMORIA", "Proveedor NO actualizado por errores graves.", "🛡️")

    if url:
        h = url_hash(url)

        mem["urls"][h] = {
            "proveedor": pid,
            "factura": factura,
            "resultado": resultado,
            "actualizado": ahora
        }

        log("MEMORIA", "URL actualizada con última corrección.", "🔗")

    return pid, resultado

# ==================================================
# ENDPOINTS
# ==================================================

@app.route("/procesar", methods=["POST"])
def procesar():
    t0 = time.time()
    sep("Nueva factura")

    try:
        body = request.get_json() or {}
        url = body.get("url_imagen", "")

        if not url:
            return jsonify({"error": "Falta url_imagen"}), 400

        mem = cargar_memoria()

        if res := buscar_url(url, mem):
            out = dict(res)
            out["metodo"] = "memoria_url_ultima"

            log("FIN", f"{time.time() - t0:.2f}s por memoria URL.", "✅")
            return jsonify(out)

        tipo, texto, img = leer_archivo(url)

        ancla_pid, ancla_datos = pre_buscar_memoria(texto if tipo == "pdf_texto" else "", mem)

        if tipo == "pdf_texto":
            raw = ia(prompt_app("pdf", mem, texto, ancla_pid, ancla_datos))
            raw = importes_pdf_math(texto, raw)
            metodo = "ollama_pdf_texto"
            texto_match = texto

        else:
            raw = ia(prompt_app("imagen", mem, "", ancla_pid, ancla_datos), img)
            metodo = "ollama_imagen"
            texto_match = ""

        pid, score = buscar_proveedor_local(texto_match, raw, mem)

        res = normalizar(
            raw,
            pid or ancla_pid or "N/A",
            score,
            metodo,
            ancla_ide=ancla_pid,
            ancla_datos=ancla_datos
        )

        # Opcional para depurar y para enviar luego a /corregir como texto_factura.
        if DEVOLVER_TEXTO_DEBUG and texto_match:
            res["_texto_factura_debug"] = texto_match[:MAX_TEXTO]

        log("FIN", f"{time.time() - t0:.2f}s", "✅")
        return jsonify(res)

    except Exception as e:
        log("ERROR", str(e), "❌")
        return jsonify({"error": str(e)}), 500

@app.route("/corregir", methods=["POST"])
def corregir():
    """
    Espera:
    {
      "url": "...",
      "original": {...resultado de /procesar...},
      "correccion": {...datos corregidos por usuario...},
      "texto_factura": "opcional, texto leído del PDF para aprender contextos reales"
    }

    texto_factura es opcional, pero si lo mandas, la plantilla aprende mucho mejor:
    guarda contextos reales alrededor del valor corregido.
    """
    sep("Corrección recibida")

    try:
        body = request.get_json() or {}

        c = body.get("correccion", {})
        original = body.get("original", {})
        url = body.get("url", "")
        texto_factura = body.get("texto_factura", "")

        mem = cargar_memoria()
        validacion = validar_correccion(c)

        log("VALIDAR", f"ok={validacion['ok']} errores={len(validacion['errores'])} avisos={len(validacion['avisos'])}", "🛡️")

        if limpiar_id(c.get("id")) == "N/A":
            return jsonify({"error": "No se puede guardar sin ID fiscal"}), 400

        pid, resultado = aprender(
            mem,
            c,
            url,
            validacion,
            original=original,
            texto_factura=texto_factura
        )

        guardar_memoria(mem)

        return jsonify({
            "status": "ok" if validacion["ok"] else "guardado_con_avisos",
            "proveedor": pid,
            "resultado": resultado,
            "validacion": validacion,
            "memoria_proveedor": mem.get("proveedores", {}).get(pid, {})
        })

    except Exception as e:
        log("ERROR", str(e), "❌")
        return jsonify({"error": str(e)}), 500

@app.route("/estado", methods=["GET"])
def estado():
    m = cargar_memoria()

    return jsonify({
        "estado": "ok",
        "modelo": MODELO,
        "version": m.get("version"),
        "proveedores": len(m.get("proveedores", {})),
        "urls": len(m.get("urls", {})),
        "modo": "pdf bloques + pre-ancla + ollama + memoria de plantillas compactas"
    })

@app.route("/memoria", methods=["GET"])
def ver_memoria():
    return jsonify(cargar_memoria())

@app.route("/memoria/proveedor/<pid>", methods=["GET"])
def ver_memoria_proveedor(pid):
    m = cargar_memoria()
    pid_real = buscar_pid_equiv(pid, m) or pid

    return jsonify({
        "pid": pid_real,
        "proveedor": m.get("proveedores", {}).get(pid_real, {})
    })

if __name__ == "__main__":
    log("SISTEMA", f"Alexander-IA activo | modelo={MODELO} | memoria de plantillas compactas", "🔥")
    app.run(host="0.0.0.0", port=5000, debug=True)
