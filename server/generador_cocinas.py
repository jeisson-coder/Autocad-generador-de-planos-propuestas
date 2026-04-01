"""
generador_cocinas.py — Motor de Diseño v1.0
=============================================
Primer prototipo funcional del Sistema IA para Diseño de Cocinas Industriales.

Flujo completo:
  1. Recibe formulario del cliente (comensales, tipo negocio, energía)
  2. Consulta reglas de diseño vía RAG (Mundo Semántico)
  3. LLM selecciona equipos necesarios (Structured Output con Pydantic)
  4. Consulta tabla `equipos` en Supabase (Mundo Racional)
  5. Genera plano DXF con layout de línea mural

Uso:
  python generador_cocinas.py
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Optional

import ezdxf
from ezdxf import xref as ezdxf_xref
import psycopg2
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# --───────────────────────────────────────────
# 0.  CONFIGURACIÓN Y VARIABLES DE ENTORNO
# --───────────────────────────────────────────

load_dotenv()

# Conexión directa a PostgreSQL (Supabase)
DB_CONFIG = {
    "host":     os.getenv("SUPABASE_DB_HOST"),
    "port":     os.getenv("SUPABASE_DB_PORT", "6543"),
    "dbname":   os.getenv("SUPABASE_DB_NAME", "postgres"),
    "user":     os.getenv("SUPABASE_DB_USER"),
    "password": os.getenv("SUPABASE_DB_PASSWORD"),
    "sslmode":  "require",
}

# Key principal para LLM (más tokens), luego las 6 de embeddings como fallback
GEMINI_KEYS = [v for v in [
    os.getenv("GEMINI_API_KEY_LLM"),  # Key dedicada para LLM
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
    os.getenv("GEMINI_API_KEY_4"),
    os.getenv("GEMINI_API_KEY_5"),
    os.getenv("GEMINI_API_KEY_6"),
] if v]

GEMINI_MODEL = "gemini-2.5-pro"

# OpenRouter fallback (API compatible OpenAI)
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-pro-preview")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Libreria de bloques CAD (generada por extraer_bloques.py)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LIBRERIA_DXF    = os.path.join(_BASE_DIR, "data", "libreria_bloques.dxf")
BLOQUE_MAP_JSON = os.path.join(_BASE_DIR, "data", "bloque_map.json")

# Carga el mapa de bloques si existe (block_name -> {width_mm, depth_mm, extmin, extmax})
_bloque_map: dict = {}
if os.path.exists(BLOQUE_MAP_JSON):
    with open(BLOQUE_MAP_JSON, encoding="utf-8") as _f:
        _bloque_map = json.load(_f)
    print(f"[INFO] Libreria CAD cargada: {len(_bloque_map)} bloques disponibles")


# --───────────────────────────────────────────
# 1.  MODELOS PYDANTIC — Structured Output
# --───────────────────────────────────────────

class EquipoSeleccionado(BaseModel):
    """Un equipo que el LLM ha decidido incluir en el diseño."""
    tipo: str = Field(description="Tipo de equipo: cocina_gas, freidora_gas, fry_top_gas, plancha, neutro, horno_combinado, lavavajillas, mesa_refrig_conservacion, etc.")
    cantidad: int = Field(default=1, description="Número de unidades necesarias")
    alimentacion: str = Field(default="gas", description="gas o electrico")
    ancho_mm_preferido: Optional[int] = Field(default=None, description="Ancho preferido en mm (400, 800, 1200). Null = el LLM no tiene preferencia")
    razon: str = Field(description="Justificación breve de por qué se incluye este equipo")


class PropuestaEquipos(BaseModel):
    """Salida estructurada del LLM: lista completa de equipos para el proyecto."""
    nombre_proyecto: str = Field(description="Nombre descriptivo del proyecto")
    layout: str = Field(default="L", description="Tipo de distribución: lineal, L, U, paralelo")
    zona_coccion: list[EquipoSeleccionado] = Field(description="Equipos para la línea de cocción mural")
    zona_frio: list[EquipoSeleccionado] = Field(default_factory=list, description="Equipos de refrigeración")
    zona_lavado: list[EquipoSeleccionado] = Field(default_factory=list, description="Equipos de lavado")
    zona_horno: list[EquipoSeleccionado] = Field(default_factory=list, description="Hornos combinados")
    notas: str = Field(default="", description="Notas adicionales sobre el diseño")


class InfoProyecto(BaseModel):
    """Datos generales del proyecto."""
    nombre: Optional[str] = None
    tipo_negocio: str = Field(description="restaurante, taperia, fast_food, pizzeria, hotel, hospital, catering")
    concepto: Optional[str] = None               # "parrilla argentina", "cocina mediterránea"
    comensales: int = Field(description="Número de comensales por servicio")
    superficie_m2: Optional[float] = None
    presupuesto_max: Optional[float] = None

class DesnivelesSuelo(BaseModel):
    """Información sobre desniveles en el suelo."""
    existe: bool = False
    detalle: Optional[str] = None                # "pendiente 3.95%"

class InfoTecnica(BaseModel):
    """Datos técnicos de la cocina / local."""
    tipo_proyecto: str = "nuevo"                  # "nuevo" | "renovacion"
    retirar_cocina_antigua: bool = False
    existe_plano_tecnico: bool = False
    altura_suelo_techo_m: Optional[float] = None
    material_paredes: list[str] = Field(default_factory=list)
    material_suelo: Optional[str] = None
    desniveles_suelo: DesnivelesSuelo = Field(default_factory=DesnivelesSuelo)
    dimensiones_accesos: Optional[dict[str, float]] = None  # {"puerta_principal_m": 1.4, ...}

class InfoEnergia(BaseModel):
    """Energía e instalaciones disponibles."""
    tipo_energia: str = "gas"                     # "gas" | "electrico" | "mixto"
    tipo_gas: Optional[str] = None                # "gas_natural" | "propano"
    caudal_gas_disponible: Optional[str] = None
    tipo_electrico: Optional[str] = None          # "trifasico" | "monofasico"
    potencia_contratada_kw: Optional[float] = None

class InfoEquipamiento(BaseModel):
    """Necesidades de equipamiento por zona."""
    coccion: list[str] = Field(default_factory=list)
    refrigeracion: list[str] = Field(default_factory=list)
    lavado: list[str] = Field(default_factory=list)
    otros: list[str] = Field(default_factory=list)
    preferencias_colocacion: Optional[str] = None
    marcas_preferidas: list[str] = Field(default_factory=list)

class InfoGastronomica(BaseModel):
    """Identidad gastronómica del negocio."""
    identidad: Optional[str] = None               # "tradicional", "creativa", "alta_cocina", "fusion"
    tipo_cocina: Optional[str] = None             # "parrilla", "japonesa", etc.
    estructura_menu: list[str] = Field(default_factory=list)  # ["carta", "menu_mediodia"]
    cantidad_platos: Optional[int] = None
    ingredientes_frescos: list[str] = Field(default_factory=list)
    ingredientes_congelados: list[str] = Field(default_factory=list)
    cuarta_gama: list[str] = Field(default_factory=list)
    quinta_gama: list[str] = Field(default_factory=list)

class InfoLavado(BaseModel):
    """Vajilla y utensilios a lavar."""
    platos: Optional[int] = None
    vasos: Optional[int] = None
    copas: Optional[int] = None
    cubiertos: Optional[int] = None
    tazas: Optional[int] = None
    otros_utensilios: list[str] = Field(default_factory=list)
    consideraciones: list[str] = Field(default_factory=list)  # ["desagues", "trampas_grasas"]

class GamaProducto(BaseModel):
    """Productos de una gama con kg aproximados."""
    productos: list[str] = Field(default_factory=list)
    kg_aproximados: Optional[float] = None

class SegundaGama(BaseModel):
    """Segunda gama (conservas) con estanterías."""
    productos: list[str] = Field(default_factory=list)
    necesita_estanterias: bool = False

class InfoRefrigeracion(BaseModel):
    """Producto que almacena, organizado por gamas."""
    primera_gama: GamaProducto = Field(default_factory=GamaProducto)
    segunda_gama: SegundaGama = Field(default_factory=SegundaGama)
    tercera_gama: GamaProducto = Field(default_factory=GamaProducto)
    cuarta_gama: GamaProducto = Field(default_factory=GamaProducto)
    quinta_gama: GamaProducto = Field(default_factory=GamaProducto)

class InfoPersonal(BaseModel):
    """Personal de cocina."""
    personas_en_cocina: Optional[int] = None
    roles: list[str] = Field(default_factory=list)

class InfoEscalabilidad(BaseModel):
    """Escalabilidad y futuro."""
    puede_ampliar_carta: Optional[bool] = None
    espacio_mas_equipamiento: Optional[bool] = None
    instalacion_permite_mas_potencia: Optional[bool] = None

class InfoFormacion(BaseModel):
    """Formación sobre equipamiento."""
    requiere_formacion: bool = False
    equipos_formacion: list[str] = Field(default_factory=list)

class FormularioCliente(BaseModel):
    """Datos completos del cliente — cuestionario real Repagas."""
    proyecto: InfoProyecto
    parte_tecnica: InfoTecnica = Field(default_factory=InfoTecnica)
    energia: InfoEnergia = Field(default_factory=InfoEnergia)
    necesidades_equipamiento: InfoEquipamiento = Field(default_factory=InfoEquipamiento)
    identidad_gastronomica: InfoGastronomica = Field(default_factory=InfoGastronomica)
    lavado: InfoLavado = Field(default_factory=InfoLavado)
    refrigeracion: InfoRefrigeracion = Field(default_factory=InfoRefrigeracion)
    personal: InfoPersonal = Field(default_factory=InfoPersonal)
    escalabilidad: InfoEscalabilidad = Field(default_factory=InfoEscalabilidad)
    formacion: InfoFormacion = Field(default_factory=InfoFormacion)
    visita_fabrica: bool = False

    @property
    def comensales(self) -> int:
        return self.proyecto.comensales

    @property
    def tipo_negocio(self) -> str:
        return self.proyecto.tipo_negocio

    @property
    def energia_principal(self) -> str:
        return self.energia.tipo_energia


class EquipoResuelto(BaseModel):
    """Un equipo ya resuelto contra la base de datos con medidas reales."""
    modelo: str
    tipo: str
    ancho_mm: int
    fondo_mm: int
    alto_mm: int
    pvp_eur: Optional[float] = None
    serie: str = ""
    cantidad: int = 1
    zona: str = ""  # coccion, frio, lavado, horno


# --───────────────────────────────────────────
# 2.  CONEXIÓN A SUPABASE
# --───────────────────────────────────────────

def get_db_connection() -> psycopg2.extensions.connection:
    """Crea una conexión fresca a Supabase PostgreSQL."""
    return psycopg2.connect(**DB_CONFIG)


def buscar_equipo_por_tipo(
    tipo: str,
    alimentacion: str = "gas",
    ancho_preferido: Optional[int] = None,
    serie_preferida: str = "750",
) -> Optional[dict]:
    """
    Busca en la tabla `equipos` el mejor match para un tipo dado.

    Estrategia de matching:
      1. Filtra por tipo exacto y alimentación (case-insensitive)
      2. Prefiere la serie indicada (750 por defecto = fondo estándar)
      3. Si se pide un ancho específico, prioriza ese
      4. Desempata por precio (más barato primero, disponibilidad comercial)
    """
    conn = get_db_connection()
    cur = conn.cursor()

    # Query que hace JOIN con series para obtener el nombre de serie
    query = """
        SELECT e.modelo, e.tipo, e.ancho_mm, e.fondo_mm, e.alto_mm,
               e.pvp_eur, s.nombre as serie, e.alimentacion
        FROM equipos e
        LEFT JOIN series s ON e.serie_id = s.id
        WHERE LOWER(e.tipo) = LOWER(%s)
          AND LOWER(e.alimentacion) = LOWER(%s)
          AND e.ancho_mm IS NOT NULL
          AND e.fondo_mm IS NOT NULL
          AND e.alto_mm  IS NOT NULL
          AND e.activo = TRUE
        ORDER BY
            -- Priorizar serie preferida
            CASE WHEN s.nombre ILIKE %s THEN 0 ELSE 1 END,
            -- Priorizar ancho preferido si se especifica
            CASE WHEN %s IS NOT NULL THEN ABS(e.ancho_mm - %s) ELSE 0 END,
            -- Desempatar por precio (más económico = más estándar)
            COALESCE(e.pvp_eur, 999999)
        LIMIT 1
    """
    params = (tipo, alimentacion, f"%{serie_preferida}%", ancho_preferido, ancho_preferido)

    try:
        cur.execute(query, params)
        row = cur.fetchone()
        if row:
            return {
                "modelo": row[0],
                "tipo": row[1],
                "ancho_mm": row[2],
                "fondo_mm": row[3],
                "alto_mm": row[4],
                "pvp_eur": float(row[5]) if row[5] else None,
                "serie": row[6] or "",
            }
        return None
    finally:
        cur.close()
        conn.close()


def resolver_equipos(propuesta: PropuestaEquipos, serie_pref: str = "750") -> list[EquipoResuelto]:
    """
    Toma la propuesta del LLM y resuelve cada equipo contra la DB real.

    Para cada EquipoSeleccionado, busca el modelo concreto en `equipos`.
    Si no hay match exacto en DB, usa datos dummy de la Serie 750.
    """
    equipos_resueltos = []

    # Juntar todas las zonas en una sola lista
    todas_las_zonas = [
        ("coccion", propuesta.zona_coccion),
        ("frio", propuesta.zona_frio),
        ("lavado", propuesta.zona_lavado),
        ("horno", propuesta.zona_horno),
    ]

    for zona_nombre, zona_equipos in todas_las_zonas:
        for eq in zona_equipos:
            for _ in range(eq.cantidad):
                # Intentar resolver contra la DB
                match = buscar_equipo_por_tipo(
                    tipo=eq.tipo,
                    alimentacion=eq.alimentacion,
                    ancho_preferido=eq.ancho_mm_preferido,
                    serie_preferida=serie_pref,
                )

                if match:
                    equipos_resueltos.append(EquipoResuelto(
                        modelo=match["modelo"],
                        tipo=match["tipo"],
                        ancho_mm=match["ancho_mm"],
                        fondo_mm=match["fondo_mm"],
                        alto_mm=match["alto_mm"],
                        pvp_eur=match["pvp_eur"],
                        serie=match["serie"],
                        zona=zona_nombre,
                    ))
                    print(f"    [DB] {match['modelo']:25s} {match['ancho_mm']}x{match['fondo_mm']}mm  €{match['pvp_eur'] or '?'}  zona={zona_nombre}")
                else:
                    # Fallback: equipo genérico con dimensiones estándar
                    print(f"    [FALLBACK] {eq.tipo} — sin match en DB, usando dimensiones genéricas")
                    equipos_resueltos.append(EquipoResuelto(
                        modelo=f"GENERICO-{eq.tipo.upper()}",
                        tipo=eq.tipo,
                        ancho_mm=eq.ancho_mm_preferido or 800,
                        fondo_mm=750,
                        alto_mm=900,
                        serie="generico",
                        zona=zona_nombre,
                    ))

    return equipos_resueltos


# --───────────────────────────────────────────
# 3.  EL CEREBRO — LangChain + Gemini LLM
# --───────────────────────────────────────────

# Reglas de diseño base por tipo de negocio.
# En producción, estas vendrán del RAG (Mundo Semántico).
REGLAS_DISENO = {
    "restaurante_tradicional": """
Reglas de diseño para Restaurante Tradicional:
- Línea mural de cocción completa: cocina a gas (4-6 fuegos), fry-top, freidora, plancha
- Elemento neutro entre cada equipo de cocción para apoyo
- Horno combinado (mínimo 6 GN 1/1 para <100 comensales, 10 GN para 100-200, 20 GN para >200)
- Mesa refrigerada de conservación para mise en place
- Lavavajillas de capota para >80 comensales, de cesto para <80
- Dimensionar por ratio: ~0.5m² de cocina por comensal (mínimo)
- Serie 750 para espacios estándar, Serie 900 para alta producción
""",
    "taperia": """
Reglas de diseño para Tapería/Cafetería:
- Línea corta: plancha o fry-top como equipo principal, freidora
- Cocina a gas de 2-4 fuegos
- Horno combinado compacto (6 GN 1/1)
- Mesa refrigerada para ingredientes de tapas
- Barra con mueble cafetería
- Serie 750 (espacios reducidos habitual en tapas)
""",
    "fast_food": """
Reglas de diseño para Fast Food:
- Múltiples freidoras (2-3 unidades) como equipo central
- Plancha o fry-top de ancho grande (800-1200mm)
- Sin cocina de fuegos abiertos típicamente
- Mantenedor de fritos
- Horno combinado compacto para panes/bakery
- Serie 750 o 550 (espacios compactos)
""",
    "hotel": """
Reglas de diseño para Hotel/Buffet:
- Línea mural extensa: múltiples cocinas, fry-tops, planchas
- Varios hornos combinados de gran capacidad (20 GN 1/1)
- Marmita para caldos y sopas
- Baño maría para servicio buffet
- Cuece-pastas si hay menú mediterráneo
- Mesa refrigerada grande para mise en place
- Serie 900 obligatoria por volumen de producción
""",
}

# Fallback por defecto si no hay match
REGLAS_DISENO["default"] = REGLAS_DISENO["restaurante_tradicional"]


def obtener_reglas_diseno(formulario) -> str:
    """
    Obtiene las reglas de diseño combinando RAG (Mundo Semántico) + reglas base.

    Acepta un FormularioCliente completo o un str (tipo_negocio) para compatibilidad.
    1. Busca chunks relevantes en el RAG via buscar_similar()
    2. Combina con reglas hardcodeadas como fallback/base
    3. Si RAG falla (keys agotadas, error DB), usa solo hardcodeado
    """
    # Compatibilidad: acepta str o FormularioCliente
    if isinstance(formulario, str):
        tipo_negocio = formulario
        identidad = None
        estructura_menu = None
        tiene_quinta_gama = False
        tiene_congelados = False
    else:
        tipo_negocio = formulario.tipo_negocio
        identidad = formulario.identidad_gastronomica.identidad
        estructura_menu = ", ".join(formulario.identidad_gastronomica.estructura_menu) or None
        tiene_quinta_gama = bool(formulario.identidad_gastronomica.quinta_gama)
        tiene_congelados = bool(formulario.identidad_gastronomica.ingredientes_congelados)

    reglas_base = REGLAS_DISENO.get(tipo_negocio, REGLAS_DISENO["default"])

    # Intentar enriquecer con RAG (con timeout de 15s para no colgar)
    try:
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
        from rag_pipeline import buscar_similar

        def _rag_search():
            queries = [
                f"diseño cocina industrial {tipo_negocio.replace('_', ' ')}",
                f"equipamiento cocina {(identidad or tipo_negocio).replace('_', ' ')} {(estructura_menu or '').replace('_', ' ')} comensales",
            ]
            if tiene_quinta_gama:
                queries.append("horno regeneración quinta gama cocina industrial")
            if tiene_congelados:
                queries.append("conservación congelados cocina industrial equipamiento")
            chunks_vistos = set()
            parts = []
            for q in queries:
                resultados = buscar_similar(q, top_k=3)
                for r in resultados:
                    contenido = r["contenido"][:200]
                    if contenido not in chunks_vistos and r["similitud"] > 0.3:
                        chunks_vistos.add(contenido)
                        parts.append(
                            f"[{r['titulo']} — sim:{r['similitud']:.2f}]\n{r['contenido']}"
                        )
            return parts

        with ThreadPoolExecutor(max_workers=1) as pool:
            reglas_rag_parts = pool.submit(_rag_search).result(timeout=15)

        if reglas_rag_parts:
            reglas_rag = "\n\n".join(reglas_rag_parts[:5])
            print(f"  RAG: {len(reglas_rag_parts)} reglas encontradas del Mundo Semántico")
            return f"{reglas_base}\n\nINFORMACIÓN ADICIONAL DEL RAG (documentación real Repagas):\n{reglas_rag}"
        else:
            print("  RAG: sin resultados relevantes, usando reglas base")
    except FutTimeout:
        print("  RAG: timeout (15s), usando reglas base")
    except Exception as e:
        print(f"  RAG: fallback a reglas base ({e})")

    return reglas_base


def obtener_tipos_disponibles() -> str:
    """Consulta la DB para saber qué tipos de equipo existen realmente."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT LOWER(e.tipo), LOWER(e.alimentacion), COUNT(*)
            FROM equipos e
            WHERE e.activo = TRUE
              AND e.ancho_mm IS NOT NULL
            GROUP BY LOWER(e.tipo), LOWER(e.alimentacion)
            ORDER BY COUNT(*) DESC
        """)
        lineas = []
        for row in cur.fetchall():
            lineas.append(f"  - {row[0]} ({row[1]}): {row[2]} modelos")
        return "\n".join(lineas)
    finally:
        cur.close()
        conn.close()


def _es_error_rate_limit(err: str) -> bool:
    """Detecta si un error es de rate limit (429)."""
    return "429" in err or "RESOURCE_EXHAUSTED" in err


def _es_limite_diario(err: str) -> bool:
    """Detecta si el rate limit es diario (no vale esperar)."""
    err_lower = err.lower()
    return (
        ("PerDay" in err and "limit: 0" in err)
        or "exceeded your current quota" in err_lower
        or "quota exceeded" in err_lower
        or "daily" in err_lower
    )


def invocar_llm_con_rotacion(messages, structured_cls=None, max_reintentos: int = 2, espera_s: int = 15):
    """
    Invoca el LLM rotando API keys al fallar por rate limit.

    Args:
        messages: Lista de mensajes para el LLM
        structured_cls: Clase Pydantic para structured output (opcional)
        max_reintentos: Rondas de reintentos si todas fallan por limite por minuto
        espera_s: Segundos de espera entre rondas

    Returns:
        Respuesta del LLM o None si todas las keys fallan
    """
    # 1. Primero: OpenRouter (prioridad)
    if OPENROUTER_KEY:
        print(f"  Probando OpenRouter ({OPENROUTER_MODEL})...")
        try:
            from langchain_openai import ChatOpenAI
            llm = ChatOpenAI(
                model=OPENROUTER_MODEL,
                base_url=OPENROUTER_BASE_URL,
                api_key=OPENROUTER_KEY,
                temperature=0.3,
                timeout=180,
                max_tokens=16384,
            )
            if structured_cls:
                result = llm.with_structured_output(structured_cls).invoke(messages)
            else:
                result = llm.invoke(messages)
            print(f"  LLM respondio via OpenRouter ({OPENROUTER_MODEL})")
            return result
        except Exception as e:
            print(f"  OpenRouter error: {str(e)[:150]}")
            print(f"  Intentando con API keys de Gemini como fallback...")

    # 2. Fallback: API keys gratuitas de Gemini
    from langchain_google_genai import ChatGoogleGenerativeAI

    os.environ.pop("GOOGLE_API_KEY", None)

    for intento in range(max_reintentos + 1):
        hubo_limite_minuto = False

        for key_idx, key in enumerate(GEMINI_KEYS):
            try:
                llm = ChatGoogleGenerativeAI(
                    model=GEMINI_MODEL,
                    google_api_key=key,
                    temperature=0.3,
                    timeout=120,
                )
                if structured_cls:
                    result = llm.with_structured_output(structured_cls).invoke(messages)
                else:
                    result = llm.invoke(messages)
                print(f"  LLM respondio con API key {key_idx + 1}/{len(GEMINI_KEYS)}")
                return result
            except Exception as e:
                err = str(e)
                if _es_error_rate_limit(err):
                    if _es_limite_diario(err):
                        print(f"  Key {key_idx + 1}/{len(GEMINI_KEYS)} -- limite DIARIO")
                        continue
                    print(f"  Key {key_idx + 1}/{len(GEMINI_KEYS)} -- rate limit, probando siguiente...")
                    hubo_limite_minuto = True
                    continue
                if "404" in err or "NOT_FOUND" in err:
                    print(f"  ERROR: Modelo '{GEMINI_MODEL}' no encontrado.")
                    return None
                print(f"  Key {key_idx + 1}/{len(GEMINI_KEYS)} -- error: {err[:100]}")
                continue

        if not hubo_limite_minuto:
            print(f"  Todas las keys con limite DIARIO.")
            break
        if intento < max_reintentos:
            print(f"  Esperando {espera_s}s antes de reintentar ({intento + 1}/{max_reintentos})...")
            time.sleep(espera_s)
        else:
            print(f"  Keys agotadas tras {max_reintentos + 1} rondas.")

    return None


def generar_propuesta_llm(formulario: FormularioCliente) -> PropuestaEquipos:
    """
    EL CEREBRO: Usa LangChain + Gemini para generar la propuesta de equipos.

    1. Obtiene reglas de diseño (hardcoded ahora, RAG en producción)
    2. Obtiene tipos de equipo disponibles en DB
    3. Pide al LLM una propuesta estructurada (Pydantic)
    4. Si el LLM falla (rate limit), usa fallback inteligente
    """
    print("\n--Consultando reglas de diseño --")
    reglas = obtener_reglas_diseno(formulario)
    print(f"  Reglas cargadas para: {formulario.tipo_negocio}")

    print("\n--Consultando equipos disponibles en DB --")
    try:
        tipos_db = obtener_tipos_disponibles()
        print(f"  {tipos_db.count(chr(10)) + 1} tipos de equipo encontrados")
    except Exception:
        tipos_db = "  (No se pudo consultar la DB)"
        print("  WARN: No se pudo conectar a Supabase")

    # Intentar usar el LLM real
    print("\n--Conectando con Gemini LLM --")

    # Prompt del sistema con toda la información
    system_prompt = f"""Eres un ingeniero experto en diseño de cocinas industriales para la empresa Repagas.
Tu trabajo es seleccionar los equipos necesarios para una cocina industrial.

REGLAS DE DISEÑO PARA ESTE TIPO DE NEGOCIO:
{reglas}

TIPOS DE EQUIPO DISPONIBLES EN BASE DE DATOS:
{tipos_db}

REGLAS DE SELECCIÓN:
- Solo usa tipos de equipo que existan en la lista anterior
- El campo 'tipo' debe coincidir EXACTAMENTE con los nombres de la lista
- Cada equipo de cocción debe tener un elemento neutro adyacente para apoyo
- Serie 750 (fondo 750mm) para espacios estándar; Serie 900 (fondo 900mm) para alta producción o alta cocina
- Para alimentación, usa exactamente "gas" o "electrico" (sin tilde)

REGLAS DE LAYOUT — elige el tipo de distribución más adecuado:
  * "lineal" — todo en una pared (espacios <20m²)
  * "L" — cocción en una pared + frío/lavado perpendicular (espacios 20-40m²)
  * "U" — tres paredes, máximo aprovechamiento (espacios 25-50m²)
  * "paralelo" — dos líneas enfrentadas con pasillo (espacios >35m²)

REGLAS SEGÚN PERFIL DEL CLIENTE:
- Si trabaja con quinta gama (platos listos para calentar), priorizar horno de regeneración
- Si trabaja con muchos ingredientes frescos, necesita más mesas refrigeradas de conservación
- Si trabaja con congelados, necesita armario o arcón de congelación
- Si la identidad es "alta_cocina" o "creativa", considerar Serie 900 y equipos premium
- Si es "buffet", considerar mesas calientes y baños maría
- Si la potencia eléctrica contratada es baja (<15kW), priorizar equipos a gas
- Si el acceso al local es estrecho (<90cm), evitar equipos de más de 800mm de ancho
- Si la altura del techo es <2.5m, no apilar hornos
- Dimensionar lavavajillas según cantidad de vajilla declarada (>200 piezas/servicio → capota)
- Dimensionar refrigeración según kg de producto declarados
- Más personas en cocina = más espacio de trabajo = más neutros y mesas de apoyo
"""

    # Construir secciones del user prompt solo con datos disponibles
    proy = formulario.proyecto
    _sec = []
    _sec.append(f"""DATOS BÁSICOS:
- Proyecto: {proy.nombre or proy.tipo_negocio}
- Tipo de negocio: {proy.tipo_negocio}{f' ({proy.concepto})' if proy.concepto else ''}
- Comensales: {proy.comensales}
- Superficie: {proy.superficie_m2 or 'no especificada'}m²
- Presupuesto: {'€{:,.0f}'.format(proy.presupuesto_max) if proy.presupuesto_max else 'sin límite'}
- Personas en cocina: {formulario.personal.personas_en_cocina or 'no especificado'}
- Roles: {', '.join(formulario.personal.roles) if formulario.personal.roles else 'no especificado'}""")

    ei = formulario.energia
    _sec.append(f"""ENERGÍA:
- Tipo principal: {ei.tipo_energia}
- Gas: {ei.tipo_gas or 'no especificado'}{f' (caudal: {ei.caudal_gas_disponible})' if ei.caudal_gas_disponible else ''}
- Eléctrico: {ei.tipo_electrico or 'no especificado'}
- Potencia contratada: {str(ei.potencia_contratada_kw) + 'kW' if ei.potencia_contratada_kw else 'no especificada'}""")

    ig = formulario.identidad_gastronomica
    _sec.append(f"""IDENTIDAD GASTRONÓMICA:
- Identidad: {ig.identidad or 'no especificada'}{f' ({ig.tipo_cocina})' if ig.tipo_cocina else ''}
- Menú: {', '.join(ig.estructura_menu) or 'no especificado'}
- Nº platos en carta: {ig.cantidad_platos or 'no especificado'}
- Ingredientes frescos: {', '.join(ig.ingredientes_frescos) or 'no especificado'}
- Ingredientes congelados: {', '.join(ig.ingredientes_congelados) or 'no especificado'}
- Cuarta gama: {', '.join(ig.cuarta_gama) or 'no'}
- Quinta gama: {', '.join(ig.quinta_gama) or 'no'}""")

    ti = formulario.parte_tecnica
    accesos_str = 'no especificado'
    if ti.dimensiones_accesos:
        accesos_str = ', '.join(f"{k}: {v}m" for k, v in ti.dimensiones_accesos.items())
    _sec.append(f"""INFRAESTRUCTURA:
- {'Renovación' + (' (retirar antigua)' if ti.retirar_cocina_antigua else '') if ti.tipo_proyecto == 'renovacion' else 'Instalación nueva'}
- Plano técnico: {'sí' if ti.existe_plano_tecnico else 'no'}
- Altura techo: {str(ti.altura_suelo_techo_m) + 'm' if ti.altura_suelo_techo_m else 'no especificada'}
- Paredes: {', '.join(ti.material_paredes) or 'no especificado'}
- Suelo: {ti.material_suelo or 'no especificado'}{' (con desniveles: ' + ti.desniveles_suelo.detalle + ')' if ti.desniveles_suelo.existe and ti.desniveles_suelo.detalle else ''}
- Accesos: {accesos_str}""")

    li = formulario.lavado
    if any([li.platos, li.vasos, li.copas, li.cubiertos]):
        _sec.append(f"""VAJILLA POR SERVICIO:
- Platos: {li.platos or '?'}, Vasos: {li.vasos or '?'}, Copas: {li.copas or '?'}
- Cubiertos: {li.cubiertos or '?'}, Tazas: {li.tazas or '?'}
- Otros: {', '.join(li.otros_utensilios) or 'no'}
- Consideraciones: {', '.join(li.consideraciones) or 'ninguna'}""")

    ri = formulario.refrigeracion
    if any([ri.primera_gama.kg_aproximados, ri.tercera_gama.kg_aproximados, ri.cuarta_gama.kg_aproximados]):
        _sec.append(f"""ALMACENAMIENTO:
- 1ª gama (frescos): {', '.join(ri.primera_gama.productos) or '?'} — {ri.primera_gama.kg_aproximados or '?'}kg
- 2ª gama (conservas): {', '.join(ri.segunda_gama.productos) or '?'} — estanterías: {'sí' if ri.segunda_gama.necesita_estanterias else 'no'}
- 3ª gama (congelados): {', '.join(ri.tercera_gama.productos) or '?'} — {ri.tercera_gama.kg_aproximados or '?'}kg
- 4ª gama: {', '.join(ri.cuarta_gama.productos) or '?'} — {ri.cuarta_gama.kg_aproximados or '?'}kg
- 5ª gama: {', '.join(ri.quinta_gama.productos) or 'no'}""")

    neq = formulario.necesidades_equipamiento
    if any([neq.coccion, neq.refrigeracion, neq.lavado, neq.otros]):
        _sec.append(f"""EQUIPAMIENTO SOLICITADO:
- Cocción: {', '.join(neq.coccion) or 'a determinar'}
- Refrigeración: {', '.join(neq.refrigeracion) or 'a determinar'}
- Lavado: {', '.join(neq.lavado) or 'a determinar'}
- Otros: {', '.join(neq.otros) or 'ninguno'}
- Marcas preferidas: {', '.join(neq.marcas_preferidas) or 'sin preferencia'}""")

    if neq.preferencias_colocacion:
        _sec.append(f"PREFERENCIAS DE COLOCACIÓN: {neq.preferencias_colocacion}")

    user_prompt = "Diseña la cocina industrial para este cliente:\n\n" + "\n\n".join(_sec)
    user_prompt += f"""

Responde SOLO con un JSON válido que siga exactamente este schema Pydantic:
{json.dumps(PropuestaEquipos.model_json_schema(), indent=2, ensure_ascii=False)}

Recuerda: el campo "tipo" de cada equipo DEBE ser uno de los tipos disponibles en la DB.
"""

    print("  Generando propuesta con IA...")
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "human", "content": user_prompt},
    ]
    propuesta = invocar_llm_con_rotacion(messages, structured_cls=PropuestaEquipos)

    if propuesta is None:
        print("  WARN: Todas las API keys agotadas -- usando fallback dummy")
        return _propuesta_fallback(formulario)

    print(f"  Propuesta generada: {propuesta.nombre_proyecto}")
    return propuesta


def _propuesta_fallback(formulario: FormularioCliente) -> PropuestaEquipos:
    """
    Fallback inteligente: genera una propuesta razonable sin LLM.

    Basado en reglas simples según comensales, tipo de negocio e identidad.
    """
    c = formulario.comensales
    es_gas = formulario.energia_principal in ("gas", "mixto")
    tipo_cocina = "cocina_gas" if es_gas else "cocina_gas"
    tipo_fry = "fry_top_gas" if es_gas else "fry_top_gas"
    tipo_freidora = "freidora_gas" if es_gas else "freidora_gas"
    identidad = formulario.identidad_gastronomica.identidad or ""

    # Zona de cocción: escalar según comensales y tipo
    zona_coccion = []

    if formulario.tipo_negocio == "fast_food":
        zona_coccion = [
            EquipoSeleccionado(tipo=tipo_freidora, cantidad=2, alimentacion="gas", ancho_mm_preferido=400, razon=f"Doble freidora para {c} comensales fast food"),
            EquipoSeleccionado(tipo="neutro", cantidad=1, alimentacion="gas", ancho_mm_preferido=400, razon="Apoyo entre freidoras y plancha"),
            EquipoSeleccionado(tipo="plancha", cantidad=1, alimentacion="gas", ancho_mm_preferido=800, razon="Plancha para hamburguesas y sandwiches"),
            EquipoSeleccionado(tipo="neutro", cantidad=1, alimentacion="gas", ancho_mm_preferido=400, razon="Apoyo lateral"),
        ]
    else:
        n_fuegos = 1 if c <= 50 else (2 if c <= 150 else 3)
        # Alta cocina / creativa: Serie 900 (ancho 800)
        ancho = 800 if identidad in ("alta_cocina", "creativa") else 800
        zona_coccion.append(EquipoSeleccionado(
            tipo=tipo_cocina, cantidad=n_fuegos, alimentacion="gas",
            ancho_mm_preferido=ancho, razon=f"Cocina gas {n_fuegos}x para {c} comensales"
        ))
        zona_coccion.append(EquipoSeleccionado(
            tipo="neutro", cantidad=1, alimentacion="gas",
            ancho_mm_preferido=400, razon="Elemento neutro de apoyo"
        ))
        zona_coccion.append(EquipoSeleccionado(
            tipo=tipo_fry, cantidad=1, alimentacion="gas",
            ancho_mm_preferido=800, razon="Fry-top para planchas y salteados"
        ))
        zona_coccion.append(EquipoSeleccionado(
            tipo="neutro", cantidad=1, alimentacion="gas",
            ancho_mm_preferido=400, razon="Elemento neutro de apoyo"
        ))
        zona_coccion.append(EquipoSeleccionado(
            tipo=tipo_freidora, cantidad=1 if c <= 100 else 2, alimentacion="gas",
            ancho_mm_preferido=400, razon=f"Freidora para {c} comensales"
        ))

    # Zona frío — más refrigeración si trabaja con frescos
    zona_frio = [
        EquipoSeleccionado(
            tipo="mesa_refrig_conservacion", cantidad=1, alimentacion="electrico",
            razon="Mesa refrigerada para mise en place"
        ),
    ]
    if formulario.identidad_gastronomica.ingredientes_congelados:
        zona_frio.append(EquipoSeleccionado(
            tipo="armario_congelacion", cantidad=1, alimentacion="electrico",
            razon=f"Armario congelación para: {', '.join(formulario.identidad_gastronomica.ingredientes_congelados)}"
        ))

    # Zona lavado
    zona_lavado = [
        EquipoSeleccionado(
            tipo="lavavajillas", cantidad=1, alimentacion="electrico",
            razon=f"Lavavajillas para {c} comensales"
        ),
    ]

    # Zona horno — añadir horno regeneración si quinta gama
    zona_horno = [
        EquipoSeleccionado(
            tipo="horno_combinado", cantidad=1, alimentacion="electrico",
            razon=f"Horno combinado para {c} comensales"
        ),
    ]

    # Serie según identidad
    serie_nota = "Serie 900" if identidad in ("alta_cocina", "creativa") else "Serie 750"

    return PropuestaEquipos(
        nombre_proyecto=f"Cocina {formulario.tipo_negocio.replace('_', ' ').title()} — {c} comensales",
        zona_coccion=zona_coccion,
        zona_frio=zona_frio,
        zona_lavado=zona_lavado,
        zona_horno=zona_horno,
        notas=f"Propuesta generada por fallback (sin LLM). {serie_nota} Repagas. Energía: {formulario.energia_principal}.",
    )


# --───────────────────────────────────────────
# 4.  EL MÚSCULO GEOMÉTRICO — ezdxf para DXF
# --───────────────────────────────────────────

# --───────────────────────────────────────────
# PLANTILLAS DE LAYOUT — basadas en planos reales analizados
# Cada zona tiene: (start_x, start_y, dirección)
#   "auto" = continúa tras la zona anterior en el mismo tramo
#   "end"  = empieza donde terminó la primera zona (esquina del L/U)
# Dirección: "X" = izq→der, "-X" = der→izq, "Y" = abajo→arriba, "-Y" = arriba→abajo
# --───────────────────────────────────────────

LAYOUTS = {
    "lineal": {
        "coccion": (0, 0, "X"),
        "frio":    ("auto", 0, "X"),
        "lavado":  ("auto", 0, "X"),
        "horno":   ("auto", 0, "X"),
    },
    "l": {
        "coccion": (0, 0, "X"),           # Tramo horizontal (pared superior)
        "frio":    ("end", 0, "-Y"),       # Gira 90° hacia abajo desde la esquina
        "lavado":  ("auto", 0, "-Y"),      # Continúa vertical
        "horno":   ("auto", 0, "-Y"),      # Al final del tramo vertical
    },
    "u": {
        "coccion": (0, 0, "X"),            # Tramo horizontal superior
        "frio":    ("end", 0, "-Y"),       # Baja por la derecha
        "lavado":  ("end_u", 0, "-X"),     # Vuelve horizontal por abajo
        "horno":   ("auto", 0, "-X"),      # Continúa en el tramo inferior
    },
    "paralelo": {
        "coccion": (0, 0, "X"),            # Línea superior
        "frio":    (0, -2500, "X"),        # Línea inferior (pasillo ~1500mm + fondo equipo)
        "lavado":  ("auto", -2500, "X"),   # Continúa línea inferior
        "horno":   ("auto", -2500, "X"),
    },
}

# Colores por zona de layout
COLORES_ZONA_LAYOUT = {
    "coccion": 1,   # Rojo
    "frio":    4,   # Cyan
    "lavado":  5,   # Azul
    "horno":   30,  # Naranja
}

# Colores AutoCAD DXF por zona
COLORES_ZONA = {
    "cocina_gas": 1,          # Rojo — fuego
    "fry_top_gas": 1,         # Rojo
    "freidora_gas": 1,        # Rojo
    "plancha": 1,             # Rojo
    "marmita": 1,             # Rojo
    "bano_maria": 1,          # Rojo
    "cuece_pastas": 1,        # Rojo
    "barbacoa": 1,            # Rojo
    "neutro": 8,              # Gris — neutro
    "soporte": 8,             # Gris
    "mesa_trabajo": 8,        # Gris
    "horno_combinado": 30,    # Naranja — hornos
    "lavavajillas": 5,        # Azul — lavado
    "lavautensilios": 5,      # Azul
    "mesa_refrig_conservacion": 4,     # Cyan — frío
    "mesa_refrig_congelacion": 4,      # Cyan
    "armario_conservacion": 4,         # Cyan
    "armario_congelacion": 4,          # Cyan
}


def _buscar_bloque(modelo: str) -> Optional[str]:
    """
    Encuentra el nombre de bloque DXF que mejor corresponde a un modelo de DB.

    Estrategia de normalización:
      "CG-740/M POW"  -> prueba "CG-740-P"  -> OK
      "FTG-72/S"      -> prueba "FTG-72-P"  -> OK
      "MN-49"         -> prueba "MN-49-P"   -> OK
      "HP-14"         -> prueba "HP-14-P" (falla) -> prueba "HP-14" -> OK
      "LAVAVAJILLAS"  -> prueba prefix match -> OK

    Solo devuelve bloques con dimensiones validas (width_mm > 50).
    """
    if not _bloque_map:
        return None

    # 1. Coincidencia exacta
    info = _bloque_map.get(modelo)
    if info and info["width_mm"] > 50:
        return modelo

    # 2. Normalizar: quitar sufijo de variante (/M, /S, /L, /2...) y de línea (POW, PRO...)
    normalizado = re.sub(r'/[A-Z0-9]+', '', modelo).strip()
    normalizado = re.sub(
        r'\s+(POW|PRO|POWER|PROFESSIONAL|BASIC|ELECTRIC).*$', '',
        normalizado, flags=re.IGNORECASE
    ).strip()

    # 3. Con sufijo -P (convencion Repagas: vista en planta)
    con_p = normalizado + "-P"
    info = _bloque_map.get(con_p)
    if info and info["width_mm"] > 50:
        return con_p

    # 4. Sin sufijo -P
    info = _bloque_map.get(normalizado)
    if info and info["width_mm"] > 50:
        return normalizado

    # 5. Prefijo: los primeros N chars antes del último guion
    # Ej: "LAVAVAJILLAS-60" busca bloques que empiecen por "LAVAVAJILLAS"
    partes = normalizado.rsplit("-", 1)
    if len(partes) > 1:
        prefix = partes[0]
        for bname, binfo in _bloque_map.items():
            if bname.startswith(prefix) and binfo["width_mm"] > 50:
                return bname

    return None


def generar_plano(
    equipos: list[EquipoResuelto],
    filepath: str = "propuesta_cocina_v1.dxf",
    layout_tipo: str = "L",
    margen_entre_equipos: float = 0,  # mm entre equipos dentro de una zona (0 = pegados)
) -> str:
    """
    Genera un archivo DXF con layout multi-zona.

    Distribuye los equipos en zonas separadas según el tipo de layout
    (lineal, L, U, paralelo), basado en análisis de planos reales.

    Args:
        equipos: Lista de equipos resueltos con medidas reales y zona asignada
        filepath: Ruta del archivo DXF de salida
        layout_tipo: Tipo de distribución ("lineal", "L", "U", "paralelo")
        margen_entre_equipos: Separación en mm entre equipos dentro de una zona

    Returns:
        Ruta absoluta del archivo generado
    """
    doc = ezdxf.new("R2010")
    ezdxf.setup_linetypes(doc)
    msp = doc.modelspace()

    # --Crear layers --
    for tipo, color in COLORES_ZONA.items():
        doc.layers.add(tipo, color=color)
    doc.layers.add("textos", color=7)
    doc.layers.add("cotas", color=3)
    doc.layers.add("contorno", color=2)
    doc.layers.add("bbox", color=8)
    doc.layers.add("zona_contorno", color=2)
    doc.layers.add("pasillo", color=9)
    doc.styles.add("EQUIPO", font="Arial")

    # --Importar bloques CAD desde la libreria --
    bloques_por_equipo: dict[int, str] = {}
    bloques_importados: set[str] = set()

    if _bloque_map and os.path.exists(LIBRERIA_DXF):
        try:
            libreria_doc = ezdxf.readfile(LIBRERIA_DXF)
            loader = ezdxf_xref.Loader(
                libreria_doc, doc,
                conflict_policy=ezdxf_xref.ConflictPolicy.KEEP,
            )
            for i, eq in enumerate(equipos):
                bname = _buscar_bloque(eq.modelo)
                if bname:
                    bloques_por_equipo[i] = bname
                    if bname not in bloques_importados:
                        block_layout = libreria_doc.blocks.get(bname)
                        if block_layout:
                            loader.load_block_layout(block_layout)
                            bloques_importados.add(bname)
            loader.execute()
            print(f"  Bloques CAD importados: {len(bloques_importados)}")
        except Exception as e:
            print(f"  WARN: No se pudo cargar libreria CAD: {e}")
            bloques_por_equipo.clear()
            bloques_importados.clear()
    else:
        print("  INFO: Sin libreria CAD -- usando rectangulos (ejecuta extraer_bloques.py)")

    # --Agrupar equipos por zona --
    zonas_equipos: dict[str, list[tuple[int, EquipoResuelto]]] = {
        "coccion": [], "frio": [], "lavado": [], "horno": [],
    }
    for i, eq in enumerate(equipos):
        zona = eq.zona if eq.zona in zonas_equipos else "coccion"
        zonas_equipos[zona].append((i, eq))

    # --Seleccionar layout --
    layout_key = layout_tipo.lower().replace("_shape", "").replace("-", "").strip()
    if layout_key not in LAYOUTS:
        print(f"  WARN: Layout '{layout_tipo}' no reconocido, usando 'L'")
        layout_key = "l"
    layout = LAYOUTS[layout_key]

    print(f"\n  Layout: {layout_key.upper()}")
    print(f"  Generando plano DXF con {len(equipos)} equipos en {sum(1 for z in zonas_equipos.values() if z)} zonas...")

    # --Posicionar equipos por zona --
    # Tracking de fin de cada zona para resolver "auto" y "end"
    zona_bounds: dict[str, dict] = {}  # zona -> {end_x, end_y, min_x, min_y, max_x, max_y}
    all_bounds = {"min_x": float("inf"), "min_y": float("inf"),
                  "max_x": float("-inf"), "max_y": float("-inf")}
    eq_counter = 0

    zona_orden = ["coccion", "frio", "lavado", "horno"]
    prev_zona_end = None  # Referencia al final de la zona anterior

    for zona_nombre in zona_orden:
        zona_eqs = zonas_equipos.get(zona_nombre, [])
        if not zona_eqs:
            continue

        zona_cfg = layout.get(zona_nombre)
        if not zona_cfg:
            continue

        start_x_cfg, start_y_cfg, direccion = zona_cfg

        # Resolver posiciones especiales
        if start_x_cfg == "auto" and prev_zona_end:
            # Continúa donde terminó la zona anterior (mismo tramo)
            start_x = prev_zona_end["cursor_x"]
            start_y = prev_zona_end["cursor_y"]
        elif start_x_cfg == "end" and "coccion" in zona_bounds:
            # Esquina: empieza donde terminó la primera zona (cocción)
            # El fondo del equipo se pega al borde derecho (alineado con cocción)
            # Se deja un pasillo de ~1200mm entre muros
            PASILLO_MM = 1200
            cb = zona_bounds["coccion"]
            if direccion in ("-Y", "Y"):
                start_x = cb["end_x"] - float(zona_eqs[0][1].fondo_mm)
                start_y = cb["min_y"] - PASILLO_MM
            else:
                start_x = cb["end_x"]
                start_y = cb["end_y"]
        elif start_x_cfg == "end_u" and "frio" in zona_bounds:
            # U-shape: tercer tramo (inferior) empieza en el borde IZQUIERDO del
            # muro vertical y va hacia la izquierda ("-X").  Así no solapa con frio.
            fb = zona_bounds["frio"]
            start_x = fb["min_x"]   # borde izquierdo del muro vertical
            start_y = fb["end_y"]   # fondo del muro vertical
        elif isinstance(start_x_cfg, (int, float)):
            start_x = float(start_x_cfg)
            start_y = float(start_y_cfg)
        else:
            start_x = 0.0
            start_y = 0.0

        cursor_x = start_x
        cursor_y = start_y
        zona_min_x = float("inf")
        zona_min_y = float("inf")
        zona_max_x = float("-inf")
        zona_max_y = float("-inf")

        for idx, (i, eq) in enumerate(zona_eqs):
            w = float(eq.ancho_mm)
            d = float(eq.fondo_mm)

            # Calcular posición según dirección
            if direccion == "X":
                x0, y0 = cursor_x, cursor_y
                x1, y1 = x0 + w, y0 + d
                cursor_x = x1 + margen_entre_equipos
                rotacion = 0.0
            elif direccion == "-X":
                x1 = cursor_x
                x0 = x1 - w
                y0 = cursor_y
                y1 = y0 + d
                cursor_x = x0 - margen_entre_equipos
                rotacion = 0.0
            elif direccion == "-Y":
                # Vertical hacia abajo: ancho en Y, fondo en X
                x0 = cursor_x
                y1 = cursor_y
                x1 = x0 + d  # fondo en X
                y0 = y1 - w  # ancho en -Y
                cursor_y = y0 - margen_entre_equipos
                rotacion = 90.0
            elif direccion == "Y":
                # Vertical hacia arriba
                x0 = cursor_x
                y0 = cursor_y
                x1 = x0 + d  # fondo en X
                y1 = y0 + w  # ancho en Y
                cursor_y = y1 + margen_entre_equipos
                rotacion = 90.0
            else:
                x0, y0 = cursor_x, cursor_y
                x1, y1 = x0 + w, y0 + d
                cursor_x = x1 + margen_entre_equipos
                rotacion = 0.0

            # Actualizar bounds
            zona_min_x = min(zona_min_x, x0)
            zona_min_y = min(zona_min_y, y0)
            zona_max_x = max(zona_max_x, x1)
            zona_max_y = max(zona_max_y, y1)

            # Layer según tipo
            layer = eq.tipo if eq.tipo in COLORES_ZONA else "neutro"

            # --Insertar bloque CAD o fallback --
            bname = bloques_por_equipo.get(i)
            binfo = _bloque_map.get(bname) if bname else None
            usa_bloque = bname and bname in bloques_importados and binfo and binfo["width_mm"] > 50

            if usa_bloque:
                bw = binfo["width_mm"]
                bd = binfo["depth_mm"]
                scale_x = w / bw if bw > 0 else 1.0
                scale_y = d / bd if bd > 0 else 1.0

                if abs(rotacion) < 1.0:
                    # Sin rotación
                    insert_x = x0 - binfo["extmin"][0] * scale_x
                    insert_y = y0 - binfo["extmin"][1] * scale_y
                    msp.add_blockref(bname, (insert_x, insert_y), dxfattribs={
                        "layer": layer, "xscale": scale_x, "yscale": scale_y,
                    })
                else:
                    # Rotado 90°: el bloque se gira, swap scales
                    insert_x = x1 + binfo["extmin"][1] * scale_y
                    insert_y = y0 - binfo["extmin"][0] * scale_x
                    msp.add_blockref(bname, (insert_x, insert_y), dxfattribs={
                        "layer": layer, "xscale": scale_x, "yscale": scale_y,
                        "rotation": 90.0,
                    })

                pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
                msp.add_lwpolyline(pts, dxfattribs={"layer": "bbox", "linetype": "DASHED"})
                eq_counter += 1
                print(f"    [{eq_counter:2d}] BLOQUE {bname:20s}  ({int(x0)},{int(y0)})  {int(w)}x{int(d)}mm  zona={zona_nombre}  rot={rotacion}")
            else:
                pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
                msp.add_lwpolyline(pts, dxfattribs={"layer": layer})
                msp.add_line((x0, y0), (x1, y1), dxfattribs={"layer": layer, "color": 9})
                eq_counter += 1
                print(f"    [{eq_counter:2d}] RECT  {eq.modelo:25s}  ({int(x0)},{int(y0)})  {int(w)}x{int(d)}mm  zona={zona_nombre}")

            # --Etiqueta --
            cx = (x0 + x1) / 2
            cy = (y0 + y1) / 2
            txt_height = max(25, min(50, min(w, d) / 12))

            if abs(rotacion) < 1.0:
                label_x, label_y = cx, y1 + 30
            else:
                label_x, label_y = x1 + 30, cy

            msp.add_text(
                eq.modelo,
                height=txt_height,
                dxfattribs={
                    "layer": "textos", "style": "EQUIPO",
                    "halign": ezdxf.const.CENTER, "valign": ezdxf.const.BOTTOM,
                    "insert": (label_x, label_y), "align_point": (label_x, label_y),
                },
            )
            dim_text = f"{int(w)}x{int(d)}mm"
            msp.add_text(
                dim_text,
                height=txt_height * 0.65,
                dxfattribs={
                    "layer": "cotas", "style": "EQUIPO",
                    "halign": ezdxf.const.CENTER, "valign": ezdxf.const.BOTTOM,
                    "insert": (label_x, label_y + txt_height + 5),
                    "align_point": (label_x, label_y + txt_height + 5),
                },
            )

        # Guardar bounds de esta zona
        zona_bounds[zona_nombre] = {
            "min_x": zona_min_x, "min_y": zona_min_y,
            "max_x": zona_max_x, "max_y": zona_max_y,
            "end_x": cursor_x if direccion in ("X", "-X") else (x1 if direccion == "-Y" else x0),
            "end_y": cursor_y if direccion in ("-Y", "Y") else (y0 if direccion == "-X" else y1),
        }
        prev_zona_end = {"cursor_x": cursor_x, "cursor_y": cursor_y}

        all_bounds["min_x"] = min(all_bounds["min_x"], zona_min_x)
        all_bounds["min_y"] = min(all_bounds["min_y"], zona_min_y)
        all_bounds["max_x"] = max(all_bounds["max_x"], zona_max_x)
        all_bounds["max_y"] = max(all_bounds["max_y"], zona_max_y)

        # --Contorno de zona + etiqueta --
        zona_color = COLORES_ZONA_LAYOUT.get(zona_nombre, 2)
        margen_z = 80  # margen alrededor de la zona
        z_pts = [
            (zona_min_x - margen_z, zona_min_y - margen_z),
            (zona_max_x + margen_z, zona_min_y - margen_z),
            (zona_max_x + margen_z, zona_max_y + margen_z),
            (zona_min_x - margen_z, zona_max_y + margen_z),
            (zona_min_x - margen_z, zona_min_y - margen_z),
        ]
        msp.add_lwpolyline(z_pts, dxfattribs={
            "layer": "zona_contorno", "color": zona_color, "linetype": "DASHED",
        })
        msp.add_text(
            f"ZONA {zona_nombre.upper()}",
            height=60,
            dxfattribs={
                "layer": "zona_contorno", "color": zona_color, "style": "EQUIPO",
                "halign": ezdxf.const.LEFT, "valign": ezdxf.const.TOP,
                "insert": (zona_min_x - margen_z, zona_max_y + margen_z + 70),
                "align_point": (zona_min_x - margen_z, zona_max_y + margen_z + 70),
            },
        )

    # --Pasillo para layout paralelo --
    if layout_key == "paralelo" and "coccion" in zona_bounds and any(
        z in zona_bounds for z in ("frio", "lavado", "horno")
    ):
        cb = zona_bounds["coccion"]
        # Encontrar la línea inferior más alta
        inf_max_y = max(
            zona_bounds[z]["max_y"]
            for z in ("frio", "lavado", "horno") if z in zona_bounds
        )
        pasillo_y_top = cb["min_y"] - 100
        pasillo_y_bot = inf_max_y + 100
        pasillo_x_min = all_bounds["min_x"] - 50
        pasillo_x_max = all_bounds["max_x"] + 50
        # Líneas punteadas del pasillo
        msp.add_line(
            (pasillo_x_min, pasillo_y_top), (pasillo_x_max, pasillo_y_top),
            dxfattribs={"layer": "pasillo", "linetype": "DASHED"},
        )
        msp.add_line(
            (pasillo_x_min, pasillo_y_bot), (pasillo_x_max, pasillo_y_bot),
            dxfattribs={"layer": "pasillo", "linetype": "DASHED"},
        )
        pasillo_cx = (pasillo_x_min + pasillo_x_max) / 2
        pasillo_cy = (pasillo_y_top + pasillo_y_bot) / 2
        msp.add_text(
            "PASILLO DE TRABAJO",
            height=50,
            dxfattribs={
                "layer": "pasillo", "style": "EQUIPO",
                "halign": ezdxf.const.CENTER, "valign": ezdxf.const.MIDDLE,
                "insert": (pasillo_cx, pasillo_cy),
                "align_point": (pasillo_cx, pasillo_cy),
            },
        )

    # --Contorno total --
    pad = 150
    contorno = [
        (all_bounds["min_x"] - pad, all_bounds["min_y"] - pad),
        (all_bounds["max_x"] + pad, all_bounds["min_y"] - pad),
        (all_bounds["max_x"] + pad, all_bounds["max_y"] + pad),
        (all_bounds["min_x"] - pad, all_bounds["max_y"] + pad),
        (all_bounds["min_x"] - pad, all_bounds["min_y"] - pad),
    ]
    msp.add_lwpolyline(contorno, dxfattribs={"layer": "contorno", "linetype": "DASHED"})

    # --Título --
    total_w = all_bounds["max_x"] - all_bounds["min_x"]
    total_h = all_bounds["max_y"] - all_bounds["min_y"]
    titulo_x = (all_bounds["min_x"] + all_bounds["max_x"]) / 2
    titulo_y = all_bounds["max_y"] + pad + 100
    layout_label = {"lineal": "LINEAL", "l": "EN L", "u": "EN U", "paralelo": "PARALELO"}
    msp.add_text(
        f"COCINA INDUSTRIAL — LAYOUT {layout_label.get(layout_key, layout_key.upper())}",
        height=80,
        dxfattribs={
            "layer": "textos", "style": "EQUIPO",
            "halign": ezdxf.const.CENTER, "valign": ezdxf.const.MIDDLE,
            "insert": (titulo_x, titulo_y), "align_point": (titulo_x, titulo_y),
        },
    )
    msp.add_text(
        f"DIMENSIONES: {int(total_w)}mm x {int(total_h)}mm ({total_w/1000:.2f}m x {total_h/1000:.2f}m)",
        height=50,
        dxfattribs={
            "layer": "cotas", "style": "EQUIPO",
            "halign": ezdxf.const.CENTER, "valign": ezdxf.const.MIDDLE,
            "insert": (titulo_x, all_bounds["min_y"] - pad - 80),
            "align_point": (titulo_x, all_bounds["min_y"] - pad - 80),
        },
    )

    # --Guardar DXF --
    abs_path = os.path.abspath(filepath)
    doc.saveas(abs_path)
    print(f"\n  Archivo DXF guardado: {abs_path}")
    print(f"  Layout: {layout_key.upper()} — {int(total_w)}mm x {int(total_h)}mm")

    # --Preview PNG --
    png_path = abs_path.replace(".dxf", ".png")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from ezdxf.addons.drawing import RenderContext, Frontend
        from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

        fig, ax = plt.subplots(1, 1, figsize=(20, 14), dpi=150)
        ax.set_aspect("equal")
        ax.set_facecolor("#1a1a2e")

        ctx = RenderContext(doc)
        out = MatplotlibBackend(ax)
        Frontend(ctx, out).draw_layout(msp)

        ax.set_title(
            f"Layout {layout_key.upper()} — {int(total_w)}mm x {int(total_h)}mm",
            color="white", fontsize=14, pad=10,
        )
        fig.patch.set_facecolor("#1a1a2e")
        fig.tight_layout()
        fig.savefig(png_path, dpi=150, facecolor=fig.get_facecolor())
        plt.close(fig)
        print(f"  Preview PNG guardado: {png_path}")
    except Exception as e:
        print(f"  WARN: No se pudo generar preview PNG: {e}")

    return abs_path


# --───────────────────────────────────────────
# 4b.  LAYOUT INTEGRADO CON PLANO DEL CLIENTE
# --───────────────────────────────────────────

def generar_plano_integrado(
    equipos: list[EquipoResuelto],
    plano_cliente_dxf: str,
    filepath: str = "propuesta_cocina_v1.dxf",
    layout_tipo: str = "L",
) -> str:
    """
    Genera un DXF con equipos posicionados DENTRO del plano del cliente.

    Si el analisis del plano falla, cae automaticamente al layout standalone.

    Args:
        equipos: Equipos resueltos contra la DB
        plano_cliente_dxf: Ruta al DXF del cliente (INICIAL)
        filepath: Ruta de salida
        layout_tipo: Tipo de layout (L, U, paralelo, lineal)

    Returns:
        Ruta absoluta del DXF generado
    """
    from analizar_plano import analizar_plano_cliente
    from posicionar_equipos import EquipoPosicionado
    from integrar_dxf import generar_dxf_catalogo

    try:
        # Analizar plano del cliente
        espacio = analizar_plano_cliente(plano_cliente_dxf)

        if espacio.confidence == "fallback":
            print("[WARN] Deteccion de paredes limitada, pero se integrara en el plano del cliente")

        # Convertir equipos resueltos a EquipoPosicionado (solo para datos del bloque)
        equipos_pos = []
        for eq in equipos:
            for i in range(eq.cantidad):
                sufijo = f" #{i+1}" if eq.cantidad > 1 else ""
                equipos_pos.append(EquipoPosicionado(
                    modelo=f"{eq.modelo}{sufijo}",
                    tipo=eq.tipo,
                    ancho_mm=eq.ancho_mm,
                    fondo_mm=eq.fondo_mm,
                    alto_mm=eq.alto_mm,
                    pvp_eur=eq.pvp_eur,
                    serie=eq.serie,
                    cantidad=1,
                    zona=eq.zona,
                    x=0, y=0, rotation=0,
                    corners=None,
                    wall_side="north",
                ))

        # Generar DXF con equipos en catalogo (organizados por zona fuera del plano)
        dxf_path = generar_dxf_catalogo(equipos_pos, espacio, filepath)

        # Generar PDF de propuesta junto al DXF
        try:
            from generar_pdf_propuesta import generar_pdf_propuesta
            pdf_path = filepath.replace(".dxf", ".pdf")
            generar_pdf_propuesta(equipos_pos, nombre_proyecto="", filepath=pdf_path)
        except Exception as e:
            print(f"[WARN] No se pudo generar PDF: {e}")

        return dxf_path

    except Exception as e:
        print(f"[WARN] Integracion de plano fallo: {e}, usando layout standalone")
        return generar_plano(equipos, filepath, layout_tipo)


# --───────────────────────────────────────────
# 5.  RESUMEN Y PRESUPUESTO
# --───────────────────────────────────────────

def imprimir_resumen(formulario: FormularioCliente, equipos: list[EquipoResuelto], dxf_path: str):
    """Imprime un resumen ejecutivo de la propuesta."""
    total_pvp = sum(eq.pvp_eur or 0 for eq in equipos)
    ancho_total = sum(eq.ancho_mm for eq in equipos)

    proy = formulario.proyecto
    print("\n" + "=" * 60)
    print("  RESUMEN DE PROPUESTA")
    print("=" * 60)
    print(f"  Proyecto: {proy.nombre or proy.tipo_negocio}")
    print(f"  Cliente: {proy.tipo_negocio.replace('_', ' ').title()}")
    ig = formulario.identidad_gastronomica
    if ig.identidad:
        print(f"  Identidad: {ig.identidad}")
    if ig.estructura_menu:
        print(f"  Menú: {', '.join(ig.estructura_menu)} ({ig.cantidad_platos or '?'} platos)")
    print(f"  Comensales: {proy.comensales}")
    print(f"  Energía: {formulario.energia_principal}" + (f" ({formulario.energia.tipo_gas})" if formulario.energia.tipo_gas else ""))
    print(f"  Superficie: {proy.superficie_m2 or '?'}m²")
    print(f"  Total equipos: {len(equipos)}")
    print(f"  Ancho línea mural: {ancho_total}mm ({ancho_total/1000:.2f}m)")
    print(f"  PVP estimado: €{total_pvp:,.2f}")
    if proy.presupuesto_max:
        restante = proy.presupuesto_max - total_pvp
        print(f"  Presupuesto: €{proy.presupuesto_max:,.0f} (margen: €{restante:,.2f})")
    print(f"  Archivo DXF: {dxf_path}")
    print("=" * 60)

    print("\n  Detalle de equipos:")
    print(f"  {'#':>3} {'Modelo':25s} {'Tipo':25s} {'Ancho':>6} {'Fondo':>6} {'PVP':>10} {'Serie':15s}")
    print("  " + "-" * 95)
    for i, eq in enumerate(equipos, 1):
        pvp_str = f"€{eq.pvp_eur:,.2f}" if eq.pvp_eur else "—"
        print(f"  {i:3d} {eq.modelo:25s} {eq.tipo:25s} {eq.ancho_mm:5d}mm {eq.fondo_mm:5d}mm {pvp_str:>10} {eq.serie:15s}")


# --───────────────────────────────────────────
# 6.  ORQUESTACIÓN — main()
# --───────────────────────────────────────────

def main():
    """
    Flujo completo del Motor de Diseño v1.0:

      Input cliente → Reglas diseño → LLM → Match DB → DXF
    """
    print("=" * 60)
    print("  MOTOR DE DISEÑO DE COCINAS INDUSTRIALES v1.0")
    print("  Repagas — Sistema IA")
    print("=" * 60)

    # --PASO 1: Formulario del cliente (ejemplo completo) --
    formulario = FormularioCliente(
        proyecto=InfoProyecto(
            nombre="Restaurante Demo",
            tipo_negocio="restaurante_tradicional",
            comensales=50,
            superficie_m2=45.0,
            presupuesto_max=30000,
        ),
        energia=InfoEnergia(
            tipo_energia="gas",
            tipo_gas="gas_natural",
            tipo_electrico="trifasico",
            potencia_contratada_kw=25,
        ),
        parte_tecnica=InfoTecnica(
            tipo_proyecto="nuevo",
            altura_suelo_techo_m=3.0,
            dimensiones_accesos={"puerta_principal_m": 1.2},
        ),
        identidad_gastronomica=InfoGastronomica(
            identidad="tradicional",
            estructura_menu=["carta"],
            cantidad_platos=25,
            ingredientes_frescos=["carnes", "pescados", "verduras"],
            ingredientes_congelados=["patatas", "rebozados"],
        ),
        lavado=InfoLavado(
            platos=150, vasos=100, copas=80, cubiertos=200,
        ),
        refrigeracion=InfoRefrigeracion(
            primera_gama=GamaProducto(productos=["carnes", "verduras"], kg_aproximados=50),
            tercera_gama=GamaProducto(productos=["patatas"], kg_aproximados=20),
        ),
        personal=InfoPersonal(personas_en_cocina=3),
    )
    print(f"\n--Formulario del cliente --")
    print(f"  Comensales: {formulario.comensales}")
    print(f"  Tipo negocio: {formulario.tipo_negocio}")
    print(f"  Energía: {formulario.energia_principal} ({formulario.energia.tipo_gas or ''})")
    print(f"  Superficie: {formulario.proyecto.superficie_m2}m²")
    print(f"  Presupuesto: €{formulario.proyecto.presupuesto_max:,.0f}")
    print(f"  Identidad: {formulario.identidad_gastronomica.identidad}")
    print(f"  Menú: {', '.join(formulario.identidad_gastronomica.estructura_menu)} ({formulario.identidad_gastronomica.cantidad_platos} platos)")
    print(f"  Personas en cocina: {formulario.personal.personas_en_cocina}")

    # --PASO 2: EL CEREBRO — LLM genera propuesta --
    print("\n--PASO 2: Generando propuesta con IA --")
    propuesta = generar_propuesta_llm(formulario)
    print(f"\n  Proyecto: {propuesta.nombre_proyecto}")
    print(f"  Layout: {propuesta.layout}")
    print(f"  Equipos cocción: {len(propuesta.zona_coccion)}")
    print(f"  Equipos frío: {len(propuesta.zona_frio)}")
    print(f"  Equipos lavado: {len(propuesta.zona_lavado)}")
    print(f"  Hornos: {len(propuesta.zona_horno)}")
    if propuesta.notas:
        print(f"  Notas: {propuesta.notas}")

    # --PASO 3: Resolver equipos contra DB real --
    # Usar Serie 750 para <=100 comensales, Serie 900 para >100
    serie = "900" if formulario.comensales > 100 else "750"
    print(f"\n--PASO 3: Resolviendo equipos contra Supabase (Serie {serie}) --")
    equipos_resueltos = resolver_equipos(propuesta, serie_pref=serie)

    # --PASO 4: Generar plano DXF --
    layout_tipo = getattr(propuesta, "layout", "L")
    print(f"\n--PASO 4: Generando plano DXF (layout={layout_tipo}) --")
    dxf_path = generar_plano(equipos_resueltos, layout_tipo=layout_tipo)

    # --PASO 5: Resumen --
    imprimir_resumen(formulario, equipos_resueltos, dxf_path)

    return dxf_path


if __name__ == "__main__":
    main()
