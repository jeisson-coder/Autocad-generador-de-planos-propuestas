"""
FastAPI server para RepagasConcept — genera propuestas de cocina industrial a partir del
formulario de cliente, ejecutando el pipeline LLM -> DB -> DXF -> PDFs y devolviendo un ZIP.

Arranque:
    cd server && uvicorn webhook_server:app --reload --port 8000

Endpoints:
    POST /generar            — JSON body con FormularioCliente
    POST /generar-con-plano  — multipart/form-data: JSON + archivo DWG/DXF del cliente
    POST /feedback           — aplica cambios del usuario sobre la ultima propuesta generada
    GET  /catalogo           — devuelve equipos activos de la BD agrupados por zona
"""

import io
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from generador_cocinas import (
    FormularioCliente,
    generar_propuesta_llm,
    resolver_equipos,
    generar_plano,
    generar_plano_integrado,
    imprimir_resumen,
    get_db_connection,
)
from generar_pdf_propuesta import generar_pdf_propuesta, generar_pdf_formulario, generar_pdf_presupuesto
from convertir_dwg import dwg_a_dxf

TIPO_TO_ZONA = {
    "cocina_gas": "coccion", "cocina_electrica": "coccion", "cocina_induccion": "coccion",
    "fry_top_gas": "coccion", "fry_top_electrico": "coccion",
    "freidora_gas": "coccion", "freidora_electrica": "coccion",
    "plancha": "coccion", "barbacoa": "coccion", "marmita": "coccion",
    "bano_maria": "coccion", "cuece_pastas": "coccion", "neutro": "coccion",
    "mantenedor_fritos": "coccion", "soporte": "coccion",
    "mesa_refrig_conservacion": "refrigeracion", "mesa_refrig_congelacion": "refrigeracion",
    "mesa_refrig_conservacion_gn": "refrigeracion", "mesa_refrig_congelacion_gn": "refrigeracion",
    "armario_conservacion": "refrigeracion", "armario_congelacion": "refrigeracion",
    "armario_snack": "refrigeracion", "frente_mostrador": "refrigeracion",
    "mesa_pizza": "refrigeracion", "mesa_ensalada": "refrigeracion",
    "mesa_trabajo": "refrigeracion", "mueble_cafetera": "refrigeracion",
    "lavavajillas": "lavado", "lavautensilios": "lavado",
    "horno_combinado": "horno", "horno_conveccion": "horno",
}

app = FastAPI(
    title="Repagas - Generador de Cocinas Industriales",
    description="Webhook para recibir formulario de cliente y generar propuesta de cocina industrial.",
    version="1.0.0",
)

# allow_origins=["*"] es aceptable porque el servidor solo es accesible localmente
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

LOGIN_USER = os.getenv("REPAGAS_LOGIN_USER", "repagas")
LOGIN_PASS = os.getenv("REPAGAS_LOGIN_PASS", "concept2025")

UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "repagas_uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Contexto en memoria de la ultima generacion; permite que /feedback modifique la propuesta activa
_ultimo_contexto: dict = {}


# ─── Helpers ─────────────────────────────────────────────

def _guardar_upload(archivo: UploadFile) -> str:
    """Guarda un archivo subido en directorio temporal y devuelve la ruta."""
    ext = Path(archivo.filename).suffix.lower()
    if ext not in (".dwg", ".dxf"):
        raise HTTPException(400, f"Formato no soportado: {ext}. Usa .dwg o .dxf")

    dest = os.path.join(UPLOAD_DIR, archivo.filename)
    with open(dest, "wb") as f:
        shutil.copyfileobj(archivo.file, f)
    return dest


def _convertir_si_dwg(ruta: str) -> str:
    """Si el archivo es .dwg, lo convierte a .dxf. Si ya es .dxf, lo retorna."""
    if ruta.lower().endswith(".dxf"):
        return ruta
    return dwg_a_dxf(ruta, output_dir=UPLOAD_DIR)


def _ejecutar_pipeline(formulario: FormularioCliente, plano_dxf: str | None = None) -> StreamingResponse:
    """Ejecuta el pipeline completo. Devuelve un ZIP con resultado.json + propuesta.dxf + propuesta.png."""

    tmp_dir = os.path.join(UPLOAD_DIR, uuid.uuid4().hex)
    os.makedirs(tmp_dir, exist_ok=True)

    try:
        print("\n[WEBHOOK] Generando propuesta con LLM (incluye RAG)...")
        propuesta = generar_propuesta_llm(formulario)

        # Serie se detecta desde preferencias del cliente; comensales como fallback
        prefs = (formulario.necesidades_equipamiento.preferencias_colocacion or "").lower()
        if "serie 900" in prefs or "s900" in prefs or "fondo 900" in prefs:
            serie = "900"
        elif "serie 750" in prefs or "s750" in prefs or "fondo 750" in prefs:
            serie = "750"
        else:
            serie = "900" if formulario.proyecto.comensales > 100 else "750"
        print(f"[WEBHOOK] Resolviendo equipos contra base de datos (Serie {serie})...")
        equipos_resueltos = resolver_equipos(propuesta, serie_pref=serie)

        layout_tipo = getattr(propuesta, "layout", "L")
        dxf_path = os.path.join(tmp_dir, "propuesta.dxf")
        if plano_dxf:
            print(f"[WEBHOOK] Generando plano integrado con plano del cliente (layout={layout_tipo})...")
            dxf_path = generar_plano_integrado(equipos_resueltos, plano_dxf, filepath=dxf_path, layout_tipo=layout_tipo)
        else:
            print(f"[WEBHOOK] Generando plano DXF standalone (layout={layout_tipo})...")
            dxf_path = generar_plano(equipos_resueltos, filepath=dxf_path, layout_tipo=layout_tipo)
        png_path = dxf_path.replace(".dxf", ".png")

        total_pvp = sum((e.pvp_eur or 0) * e.cantidad for e in equipos_resueltos)
        resultado = {
            "proyecto": propuesta.nombre_proyecto,
            "layout": propuesta.layout,
            "equipos": [
                {
                    "modelo": e.modelo,
                    "tipo": e.tipo,
                    "ancho_mm": e.ancho_mm,
                    "fondo_mm": e.fondo_mm,
                    "pvp_eur": e.pvp_eur,
                    "cantidad": e.cantidad,
                    "zona": e.zona,
                    "serie": e.serie,
                }
                for e in equipos_resueltos
            ],
            "total_equipos": len(equipos_resueltos),
            "total_pvp_eur": round(total_pvp, 2),
            "notas_llm": propuesta.notas,
        }

        imprimir_resumen(formulario, equipos_resueltos, dxf_path)

        _ultimo_contexto.clear()
        _ultimo_contexto["formulario"] = formulario
        _ultimo_contexto["propuesta"] = propuesta
        _ultimo_contexto["equipos_resueltos"] = equipos_resueltos
        _ultimo_contexto["plano_dxf"] = plano_dxf

        nombre_proy = propuesta.nombre_proyecto or ""
        formulario_dict = formulario.model_dump()

        pdf_propuesta = os.path.join(tmp_dir, "propuesta_equipamiento.pdf")
        try:
            from posicionar_equipos import EquipoPosicionado
            # EquipoPosicionado requiere campos de posicion; irrelevantes para los PDFs
            equipos_pos = [
                EquipoPosicionado(
                    modelo=e.modelo, tipo=e.tipo, zona=e.zona,
                    ancho_mm=e.ancho_mm, fondo_mm=e.fondo_mm, alto_mm=e.alto_mm,
                    pvp_eur=e.pvp_eur, serie=e.serie, cantidad=1,
                    x=0, y=0, rotation=0, corners=None, wall_side="north",
                )
                for e in equipos_resueltos
                for _ in range(e.cantidad)
            ]
            generar_pdf_propuesta(equipos_pos, nombre_proy, pdf_propuesta)
            print(f"[WEBHOOK] PDF propuesta generado")
        except Exception as e:
            print(f"[WEBHOOK] WARN: PDF propuesta fallo: {e}")
            pdf_propuesta = None

        pdf_formulario = os.path.join(tmp_dir, "formulario_cliente.pdf")
        try:
            generar_pdf_formulario(formulario_dict, pdf_formulario)
            print(f"[WEBHOOK] PDF formulario generado")
        except Exception as e:
            print(f"[WEBHOOK] WARN: PDF formulario fallo: {e}")
            pdf_formulario = None

        pdf_presupuesto = os.path.join(tmp_dir, "presupuesto.pdf")
        try:
            generar_pdf_presupuesto(equipos_pos, nombre_proy, formulario_dict, pdf_presupuesto)
            print(f"[WEBHOOK] PDF presupuesto generado")
        except Exception as e:
            print(f"[WEBHOOK] WARN: PDF presupuesto fallo: {e}")
            pdf_presupuesto = None

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("resultado.json", json.dumps(resultado, ensure_ascii=False, indent=2))
            if os.path.isfile(dxf_path):
                zf.write(dxf_path, "propuesta.dxf")
            if os.path.isfile(png_path):
                zf.write(png_path, "propuesta.png")
            if pdf_propuesta and os.path.isfile(pdf_propuesta):
                zf.write(pdf_propuesta, "propuesta_equipamiento.pdf")
            if pdf_formulario and os.path.isfile(pdf_formulario):
                zf.write(pdf_formulario, "formulario_cliente.pdf")
            if pdf_presupuesto and os.path.isfile(pdf_presupuesto):
                zf.write(pdf_presupuesto, "presupuesto.pdf")

        zip_buffer.seek(0)

        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=propuesta.zip"},
        )

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ─── Endpoints ───────────────────────────────────────────

@app.post("/login")
async def login(data: dict):
    """Valida credenciales simples para acceso al formulario."""
    user = data.get("user", "")
    password = data.get("pass", "")
    if user == LOGIN_USER and password == LOGIN_PASS:
        return {"ok": True}
    raise HTTPException(status_code=401, detail="Credenciales incorrectas")


@app.post("/feedback")
async def recibir_feedback(data: dict):
    """
    Recibe feedback del usuario, lo envía a la IA para aplicar cambios,
    y regenera el ZIP completo con la propuesta modificada.
    """
    mensaje = data.get("mensaje", "").strip()
    if not mensaje:
        raise HTTPException(status_code=400, detail="Mensaje vacio")

    if not _ultimo_contexto.get("formulario"):
        raise HTTPException(status_code=400, detail="No hay propuesta previa. Genera una primero.")

    print(f"\n[FEEDBACK] Solicitud: {mensaje[:150]}")

    proyecto_nombre = ""
    try:
        proyecto_nombre = _ultimo_contexto.get("propuesta", {})
        proyecto_nombre = getattr(proyecto_nombre, "nombre_proyecto", "") or ""
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO feedback (mensaje, proyecto) VALUES (%s, %s)",
            (mensaje, proyecto_nombre),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[FEEDBACK] WARN: No se pudo guardar en BD: {e}")

    formulario = _ultimo_contexto["formulario"]
    propuesta_anterior = _ultimo_contexto["propuesta"]
    plano_dxf = _ultimo_contexto.get("plano_dxf")

    try:
        from generador_cocinas import invocar_llm_con_rotacion, PropuestaEquipos
        from langchain_core.messages import SystemMessage, HumanMessage

        equipos_prev = []
        for zona_name, zona_list in [
            ("coccion", propuesta_anterior.zona_coccion),
            ("frio", propuesta_anterior.zona_frio),
            ("lavado", propuesta_anterior.zona_lavado),
            ("horno", propuesta_anterior.zona_horno),
        ]:
            for eq in zona_list:
                equipos_prev.append(f"  - {eq.tipo} x{eq.cantidad} ({zona_name})")

        propuesta_json = propuesta_anterior.model_dump_json(indent=2)

        messages = [
            SystemMessage(content=(
                "Eres un ingeniero de cocinas industriales RepagasConcept. "
                "El usuario ya recibio una propuesta de equipamiento y quiere hacer cambios. "
                "Tu tarea: aplicar EXACTAMENTE los cambios que pide y devolver la propuesta completa modificada. "
                "Mantener todo lo que no se pida cambiar. Responde con el JSON estructurado PropuestaEquipos."
            )),
            HumanMessage(content=(
                f"PROPUESTA ACTUAL:\n{propuesta_json}\n\n"
                f"CAMBIO SOLICITADO POR EL USUARIO:\n{mensaje}\n\n"
                f"Aplica el cambio y devuelve la PropuestaEquipos completa modificada."
            )),
        ]

        print("[FEEDBACK] Enviando a la IA para aplicar cambios...")
        nueva_propuesta = invocar_llm_con_rotacion(messages, structured_cls=PropuestaEquipos)

        if not nueva_propuesta:
            raise HTTPException(500, "La IA no pudo procesar el cambio. Intenta reformular la solicitud.")

        print(f"[FEEDBACK] IA respondio. Regenerando propuesta...")

        prefs = (formulario.necesidades_equipamiento.preferencias_colocacion or "").lower()
        if "serie 900" in prefs or "fondo 900" in prefs:
            serie = "900"
        elif "serie 750" in prefs or "fondo 750" in prefs:
            serie = "750"
        else:
            serie = "900" if formulario.proyecto.comensales > 100 else "750"

        equipos_resueltos = resolver_equipos(nueva_propuesta, serie_pref=serie)

        _ultimo_contexto["propuesta"] = nueva_propuesta
        _ultimo_contexto["equipos_resueltos"] = equipos_resueltos

        tmp_dir = os.path.join(UPLOAD_DIR, uuid.uuid4().hex)
        os.makedirs(tmp_dir, exist_ok=True)

        try:
            layout_tipo = getattr(nueva_propuesta, "layout", "L")
            dxf_path = os.path.join(tmp_dir, "propuesta.dxf")
            if plano_dxf:
                dxf_path = generar_plano_integrado(equipos_resueltos, plano_dxf, filepath=dxf_path, layout_tipo=layout_tipo)
            else:
                dxf_path = generar_plano(equipos_resueltos, filepath=dxf_path, layout_tipo=layout_tipo)
            png_path = dxf_path.replace(".dxf", ".png")

            total_pvp = sum((e.pvp_eur or 0) * e.cantidad for e in equipos_resueltos)
            resultado = {
                "proyecto": nueva_propuesta.nombre_proyecto,
                "layout": nueva_propuesta.layout,
                "equipos": [
                    {"modelo": e.modelo, "tipo": e.tipo, "ancho_mm": e.ancho_mm,
                     "fondo_mm": e.fondo_mm, "pvp_eur": e.pvp_eur, "cantidad": e.cantidad,
                     "zona": e.zona, "serie": e.serie}
                    for e in equipos_resueltos
                ],
                "total_equipos": len(equipos_resueltos),
                "total_pvp_eur": round(total_pvp, 2),
                "notas_llm": nueva_propuesta.notas,
                "feedback_aplicado": mensaje,
            }

            nombre_proy = nueva_propuesta.nombre_proyecto or ""
            formulario_dict = formulario.model_dump()
            from posicionar_equipos import EquipoPosicionado
            equipos_pos = [
                EquipoPosicionado(
                    modelo=e.modelo, tipo=e.tipo, zona=e.zona,
                    ancho_mm=e.ancho_mm, fondo_mm=e.fondo_mm, alto_mm=e.alto_mm,
                    pvp_eur=e.pvp_eur, serie=e.serie, cantidad=1,
                    x=0, y=0, rotation=0, corners=None, wall_side="north",
                )
                for e in equipos_resueltos
                for _ in range(e.cantidad)
            ]

            pdf_propuesta = os.path.join(tmp_dir, "propuesta_equipamiento.pdf")
            try: generar_pdf_propuesta(equipos_pos, nombre_proy, pdf_propuesta)
            except Exception: pdf_propuesta = None

            pdf_formulario = os.path.join(tmp_dir, "formulario_cliente.pdf")
            try: generar_pdf_formulario(formulario_dict, pdf_formulario)
            except Exception: pdf_formulario = None

            pdf_presupuesto = os.path.join(tmp_dir, "presupuesto.pdf")
            try: generar_pdf_presupuesto(equipos_pos, nombre_proy, formulario_dict, pdf_presupuesto)
            except Exception: pdf_presupuesto = None

            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("resultado.json", json.dumps(resultado, ensure_ascii=False, indent=2))
                if os.path.isfile(dxf_path):
                    zf.write(dxf_path, "propuesta.dxf")
                if os.path.isfile(png_path):
                    zf.write(png_path, "propuesta.png")
                if pdf_propuesta and os.path.isfile(pdf_propuesta):
                    zf.write(pdf_propuesta, "propuesta_equipamiento.pdf")
                if pdf_formulario and os.path.isfile(pdf_formulario):
                    zf.write(pdf_formulario, "formulario_cliente.pdf")
                if pdf_presupuesto and os.path.isfile(pdf_presupuesto):
                    zf.write(pdf_presupuesto, "presupuesto.pdf")

            zip_buffer.seek(0)
            print(f"[FEEDBACK] Propuesta regenerada con cambios aplicados")

            return StreamingResponse(
                zip_buffer,
                media_type="application/zip",
                headers={"Content-Disposition": "attachment; filename=propuesta_modificada.zip"},
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    except HTTPException:
        raise
    except Exception as e:
        print(f"[FEEDBACK] Error: {e}")
        raise HTTPException(500, f"Error aplicando cambios: {str(e)[:200]}")


@app.get("/")
def root():
    return {"status": "ok", "mensaje": "Repagas Generador de Cocinas - API activa"}


@app.get("/catalogo")
def obtener_catalogo():
    """
    Devuelve el catálogo de equipos agrupado por zona (coccion, refrigeracion, lavado, horno).
    Cada equipo incluye: modelo, tipo, ancho_mm, fondo_mm, alto_mm, pvp_eur, serie.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT e.modelo, e.tipo, e.ancho_mm, e.fondo_mm, e.alto_mm,
                   e.pvp_eur, s.nombre as serie, e.alimentacion
            FROM equipos e
            LEFT JOIN series s ON e.serie_id = s.id
            WHERE e.activo = TRUE
              AND e.ancho_mm IS NOT NULL
              AND e.fondo_mm IS NOT NULL
            ORDER BY e.tipo, e.ancho_mm, e.modelo
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        raise HTTPException(500, f"Error consultando base de datos: {e}")

    catalogo = {"coccion": [], "refrigeracion": [], "lavado": [], "horno": []}
    for row in rows:
        modelo, tipo, ancho, fondo, alto, pvp, serie, alim = row
        zona = TIPO_TO_ZONA.get(tipo, "coccion")
        catalogo[zona].append({
            "modelo": modelo,
            "tipo": tipo,
            "ancho_mm": ancho,
            "fondo_mm": fondo,
            "alto_mm": alto,
            "pvp_eur": float(pvp) if pvp else None,
            "serie": serie or "",
            "alimentacion": alim or "",
        })

    return catalogo


@app.post("/generar")
def generar_cocina(formulario: FormularioCliente):
    """Recibe JSON con FormularioCliente y devuelve ZIP con resultado.json, propuesta.dxf y propuesta.png."""
    try:
        return _ejecutar_pipeline(formulario)
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@app.post("/generar-con-plano")
async def generar_con_plano(
    formulario_json: str = Form(..., description="JSON string con datos del FormularioCliente"),
    archivo: UploadFile = File(..., description="Archivo .dwg o .dxf del plano del cliente"),
):
    """
    Igual que /generar pero recibe el formulario como form-data (JSON string) junto al DWG/DXF del cliente.
    Postman: Body > form-data, claves `formulario_json` (text) y `archivo` (file).
    """
    try:
        data = json.loads(formulario_json)
        formulario = FormularioCliente(**data)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"JSON invalido en formulario_json: {e}")
    except Exception as e:
        raise HTTPException(400, f"Error en datos del formulario: {e}")

    try:
        ruta_archivo = _guardar_upload(archivo)
        plano_dxf = _convertir_si_dwg(ruta_archivo)
        print(f"[WEBHOOK] Plano del cliente: {plano_dxf}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error procesando archivo: {e}")

    try:
        return _ejecutar_pipeline(formulario, plano_dxf=plano_dxf)
    except Exception as e:
        raise HTTPException(500, detail=str(e))


# ─── Arranque directo ────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print("Iniciando servidor webhook Repagas...")
    print("Docs interactivos en: http://localhost:8000/docs")
    uvicorn.run(app, host="0.0.0.0", port=8000)
