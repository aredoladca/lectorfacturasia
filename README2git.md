# 📄 Lector de Facturas IA

Sistema inteligente de lectura, auditoría y aprendizaje automático de facturas utilizando IA local.

---

## 🚀 Descripción

Lector de Facturas IA es una aplicación que permite procesar facturas en formato PDF o imagen, extraer automáticamente los datos clave y mejorar su precisión con el uso gracias a un sistema de aprendizaje basado en correcciones del usuario.

La aplicación utiliza modelos de inteligencia artificial ejecutados en local (Ollama), evitando dependencias de servicios externos y garantizando mayor control sobre los datos.

---

## ⚙️ Características principales

- 📥 Procesamiento de facturas desde URL (PDF o imagen)
- 🧠 Extracción automática de:
  - Fecha
  - Emisor
  - CIF/NIF
  - Base imponible
  - IVA
  - Total
- 🔄 Sistema de aprendizaje automático basado en feedback del usuario
- 📊 Corrección automática de importes usando histórico de IVA por proveedor
- 🖥️ Interfaz web simple para validación y edición de datos
- ⚡ Ejecución local sin necesidad de APIs externas

---

## 🧠 Cómo funciona

1. El usuario introduce la URL de una factura
2. El sistema descarga y convierte el documento en imagen
3. La IA analiza la imagen y extrae los datos
4. Los resultados se muestran en una interfaz editable
5. El usuario puede corregir los datos si es necesario
6. El sistema aprende de la corrección y mejora futuras predicciones

---

## 🏗️ Arquitectura

```text
Frontend (HTML/JS)
        ↓
Flask API (/procesar, /feedback)
        ↓
Procesamiento (PDF → imagen)
        ↓
Ollama (IA local)
        ↓
Post-procesado + memoria
        ↓
Respuesta estructurada (JSON)
