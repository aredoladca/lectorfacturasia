import json, requests, fitz, ollama, os, re
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# --- CONFIGURACIÓN ALEXANDER CORE ---
MODELO = "alexander-ia"
MEMORIA_FILE = "memoria_alexander.json"
# POTENCIA_GPU: 
# Mín: 0 | Máx: 99. 
# En la GTX 1650 (4GB), 99 funciona perfecto para modelos de 2 billones de parámetros.
POTENCIA_GPU = 99

# NUM_THREAD: 
# Mín: 1 | Máx: 4.   
# Tu i5-7400 tiene 4 núcleos/4 hilos. Si pones más de 4, el sistema se congelará durante la inferencia.
NUM_THREAD = 2

# NUM_CTX: 
# Mín: 512 | Máx: 8192 (No recomendado en tu caso). 
# 2048 es el equilibrio entre velocidad y memoria para procesar facturas con historial.
NUM_CTX =  512

def cargar_memoria():
    base = {"cifs": {}, "nombres": {}, "ivas": {}}
    if os.path.exists(MEMORIA_FILE):
        try:
            with open(MEMORIA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                print(f"📂 [MEMORIA] Emisores activos: {len(data.get('ivas', {}))}")
                return {**base, **data}
        except: pass
    return base

def guardar_memoria(cif, emisor, base_val, iva_val):
    m = cargar_memoria()
    c, e = str(cif).strip().upper(), str(emisor).strip().upper()
    m["cifs"][c] = e
    m["nombres"][e] = c
    
    if base_val > 0:
        porcentaje_iva = round((iva_val / base_val) * 100, 0)
        m["ivas"][e] = porcentaje_iva
        print(f"🧠 [APRENDIZAJE] {e} -> IVA {porcentaje_iva}%")

    with open(MEMORIA_FILE, 'w', encoding='utf-8') as f:
        json.dump(m, f, ensure_ascii=False, indent=4)

def to_f(v):
    if v is None: return 0.0
    s = re.sub(r'[^\d.,]', '', str(v))
    if not s: return 0.0
    if ',' in s and '.' in s:
        if s.rfind(',') > s.rfind('.'): s = s.replace('.', '').replace(',', '.')
        else: s = s.replace(',', '')
    elif ',' in s: s = s.replace(',', '.')
    try: return round(float(s), 2)
    except: return 0.0

def format_salida(valor):
    """Formato: 3 000,64 (Espacio para miles, coma para decimales)"""
    return "{:,.2f}".format(valor).replace(",", " ").replace(".", ",")

@app.route('/procesar', methods=['POST'])
def procesar():
    try:
        data_in = request.get_json(force=True)
        url = data_in.get("url_imagen")
        print(f"\n🚀 [AUDITANDO] {url[-30:]}")
        
        # 1. Captura y Renderizado
        r = requests.get(url, timeout=20)
        doc = fitz.open(stream=r.content, filetype="pdf" if url.lower().endswith('.pdf') else None)
        #pix = doc[0].get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
        img_bytes = pix.tobytes("png")
        doc.close()

        # 2. Inferencia con Alexander-IA
        m = cargar_memoria()
        reglas_oro = f"REGLAS DE ORO: {json.dumps(m['cifs'])}"
        
        res = ollama.generate(
            model=MODELO, 
            images=[img_bytes],
            prompt=reglas_oro,
            options={
                "num_gpu": POTENCIA_GPU, 
                "num_thread": NUM_THREAD,
                "num_ctx": NUM_CTX
            }
        )
        
        raw = res.get('response', '')
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        d = json.loads(match.group(0)) if match else {}

        # 3. Cruce e Identidad
        cif_ia = str(d.get('doc_id','')).strip().upper()
        emisor_ia = str(d.get('emisor','')).strip().upper()

                # 1. Intento por CIF
        emisor_f = m["cifs"].get(cif_ia)

        # 2. Si falla → intento por nombre
        if not emisor_f:
            cif_guardado = m["nombres"].get(emisor_ia)
            if cif_guardado:
                cif_ia = cif_guardado
                emisor_f = emisor_ia

        # 3. Si sigue fallando → usar IA
        if not emisor_f:
            emisor_f = emisor_ia
        
        # 4. Auditoría de Importes
        base = to_f(d.get('base'))
        iva = to_f(d.get('iva'))
        total = to_f(d.get('total'))
        iva_guardado = m["ivas"].get(emisor_f)

        if iva_guardado and total > 0:
            print(f"⚖️ [AJUSTE] Aplicando IVA histórico ({iva_guardado}%)")
            base = round(total / (1 + (iva_guardado / 100)), 2)
            iva = round(total - base, 2)
        elif abs((base + iva) - total) > 0.1 and total > 0:
            base = round(total - iva, 2)

        return jsonify({
            "fecha": d.get("fecha", ""),
            "emisor": emisor_f,
            "cif": cif_ia,
            "base": format_salida(base),
            "iva": format_salida(iva),
            "total": format_salida(total)
        })

    except Exception as e:
        print(f"❌ [ERROR] {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/feedback', methods=['POST'])
def feedback():
    try:
        data = request.get_json(force=True).get('correccion', {})
        guardar_memoria(
            data.get('cif'), 
            data.get('emisor'), 
            to_f(data.get('base')), 
            to_f(data.get('iva'))
        )
        return jsonify({"status": "ok"})
    except: return jsonify({"status": "error"}), 400

if __name__ == '__main__':
    print("💎 ALEXANDER-IA ENGINE ACTIVADO")
    app.run(host='0.0.0.0', port=5000, debug=False)