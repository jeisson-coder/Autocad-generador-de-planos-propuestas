"""
posicionar_equipos.py -- Motor de posicionamiento de equipos en paredes reales.

Recibe equipos resueltos + espacio detectado del plano del cliente,
y calcula la posicion (x, y, rotation) de cada equipo dentro del espacio real.

Enfoque: posiciona en espacio axis-aligned (rotado), luego de-rota al espacio
original usando las coordenadas REALES de los segmentos de pared detectados.

Incluye posicionamiento asistido por IA (Gemini LLM) con fallback algoritmico.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field

from analizar_plano import EspacioCocina, SegmentoPared


# ─── Patrones profesionales (few-shot) ──────────────────

_PATRONES_CACHE: list[dict] | None = None


def _cargar_patrones() -> list[dict]:
    """Carga patrones profesionales desde JSON (cache en memoria)."""
    global _PATRONES_CACHE
    if _PATRONES_CACHE is not None:
        return _PATRONES_CACHE
    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "patrones_profesionales.json")
    if not os.path.exists(json_path):
        _PATRONES_CACHE = []
        return []
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _PATRONES_CACHE = data.get("patrones", [])
    except Exception:
        _PATRONES_CACHE = []
    return _PATRONES_CACHE


def _seleccionar_ejemplos_similares(
    width_mm: float,
    depth_mm: float,
    n_equipos: int,
    zonas_equipos: dict[str, int],
    max_ejemplos: int = 2,
) -> list[dict]:
    """Selecciona los 2 patrones profesionales mas similares al caso actual."""
    patrones = _cargar_patrones()
    if not patrones:
        return []

    area_input = width_mm * depth_mm / 1e6  # m2
    ar_input = max(width_mm, depth_mm) / max(min(width_mm, depth_mm), 1)

    scored = []
    for pat in patrones:
        if pat.get("calidad") == "baja":
            continue

        p = pat.get("cocina", {})
        area_pat = p.get("area_m2", 1)
        ar_pat = p.get("aspect_ratio", 1)
        n_pat = pat.get("equipos_total", 1)

        # Factor 1: Area similarity (log-scale)
        if area_pat > 0 and area_input > 0:
            area_sim = 1.0 - min(abs(math.log(area_input / max(area_pat, 0.1))) / math.log(10), 1.0)
        else:
            area_sim = 0.0

        # Factor 2: Aspect ratio similarity
        ar_sim = 1.0 - min(abs(ar_input - ar_pat) / 3.0, 1.0)

        # Factor 3: Equipment count similarity
        count_sim = 1.0 - min(abs(n_equipos - n_pat) / max(n_equipos, n_pat, 1), 1.0)

        # Factor 4: Zone mix similarity
        zonas_input_set = set(z for z, c in zonas_equipos.items() if c > 0)
        zonas_pat = pat.get("equipos_por_zona", {})
        zonas_pat_set = set(z for z, c in zonas_pat.items() if c > 0)
        union = zonas_input_set | zonas_pat_set
        zone_sim = len(zonas_input_set & zonas_pat_set) / len(union) if union else 0.5

        score = 0.40 * area_sim + 0.25 * ar_sim + 0.20 * count_sim + 0.15 * zone_sim

        # Bonus for high quality
        if pat.get("calidad") == "alta":
            score *= 1.05

        scored.append((score, pat))

    scored.sort(key=lambda x: -x[0])
    return [pat for _, pat in scored[:max_ejemplos]]


def _formatear_ejemplos_prompt(
    equipos: list,
    espacio: EspacioCocina,
) -> str:
    """Genera texto de ejemplos profesionales para inyectar en el prompt."""
    from collections import Counter
    zonas_count = dict(Counter(eq.zona for eq in equipos for _ in range(eq.cantidad)))
    n_equipos = sum(eq.cantidad for eq in equipos)

    ejemplos = _seleccionar_ejemplos_similares(
        espacio.width_mm, espacio.depth_mm, n_equipos, zonas_count, max_ejemplos=2,
    )
    if not ejemplos:
        return ""

    text = "\nEJEMPLOS DE LAYOUTS PROFESIONALES (cocinas de dimensiones similares):\n"
    for i, ej in enumerate(ejemplos, 1):
        cocina = ej.get("cocina", {})
        text += f"\n  EJEMPLO {i}: {ej['nombre']} ({ej.get('tipo_negocio', '')}"
        text += f", {cocina.get('width_mm', 0):.0f}x{cocina.get('depth_mm', 0):.0f}mm"
        text += f", {ej.get('equipos_total', 0)} equipos)\n"

        dist = ej.get("distribucion_paredes", {})
        for wall_side in ("north", "south", "east", "west"):
            wall_data = dist.get(wall_side)
            if not wall_data:
                continue
            eqs = wall_data.get("equipos", [])
            tipos_str = ", ".join(e.get("tipo", "?") for e in eqs[:6])
            if len(eqs) > 6:
                tipos_str += f" (+{len(eqs) - 6} mas)"
            text += f"    Pared {wall_side}: {wall_data.get('descripcion', '')} -> [{tipos_str}]\n"

        text += f"    Resumen: {ej.get('patron_resumen', '')}\n"

    text += """
  IMPORTANTE SOBRE LOS EJEMPLOS:
  - Estos son REFERENCIA, no copia exacta. Adapta al espacio y equipos del cliente.
  - PATRON CLAVE: los profesionales CONCENTRAN equipos en 1-2 paredes, NO distribuyen en 4.
  - MULTI-FILA: cuando no cabe todo en una linea, usar FILAS PARALELAS en la MISMA pared:
    * FILA 1 (contra pared, offset=0mm): coccion (cocina_gas, fry_top, barbacoa, fregadero)
    * FILA 2 (offset ~1000mm): soporte (mesa_refrigerada, armario_1_700, mesa_salida, grifo_servicio)
    * FILA 3 (offset ~2750mm): lavado pesado (botellero, lavavajillas)
    * El sistema calcula las filas automaticamente - tu solo asigna TODO a la MISMA pared.
  - ARMARIOS GRANDES de frio/conservacion van en pared PERPENDICULAR (east o west, la que tenga mejor acceso).
  - NUNCA repartir equipos equitativamente en todas las paredes. Concentrar siempre.\n"""
    return text


# ─── Reconciliacion de dimensiones equipo-bloque ────────

_BLOQUE_MAP_CACHE: dict | None = None


def _cargar_bloque_map() -> dict:
    """Carga bloque_map.json (cache en memoria)."""
    global _BLOQUE_MAP_CACHE
    if _BLOQUE_MAP_CACHE is not None:
        return _BLOQUE_MAP_CACHE
    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bloque_map.json")
    if not os.path.exists(json_path):
        _BLOQUE_MAP_CACHE = {}
        return {}
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            _BLOQUE_MAP_CACHE = json.load(f)
    except Exception:
        _BLOQUE_MAP_CACHE = {}
    return _BLOQUE_MAP_CACHE


def _buscar_bloque_nombre(modelo: str, bloque_map: dict) -> str | None:
    """Busca el nombre del bloque CAD para un modelo (misma logica que integrar_dxf)."""

    def _valid(name: str) -> bool:
        return name in bloque_map and bloque_map[name].get("width_mm", 0) > 50

    if _valid(modelo):
        return modelo

    # Normalizar: quitar /S, /M, sufijos POW, PRO, etc.
    normalizado = re.sub(r"/[A-Z0-9]+", "", modelo).strip()
    normalizado = re.sub(
        r"\s+(POW|PRO|POWER|PROFESSIONAL|BASIC|ELECTRIC).*$",
        "",
        normalizado,
        flags=re.IGNORECASE,
    ).strip()

    for suffix in ["-P", "-PLANTA", ""]:
        if _valid(normalizado + suffix):
            return normalizado + suffix

    sin_guion = normalizado.replace("-", "")
    for suffix in ["-P", "-PLANTA", ""]:
        if _valid(sin_guion + suffix):
            return sin_guion + suffix

    # Prefijo parcial
    partes = normalizado.rsplit("-", 1)
    if len(partes) > 1:
        prefix = partes[0]
        for bname in bloque_map:
            if bname.startswith(prefix) and bloque_map[bname].get("width_mm", 0) > 50:
                return bname

    return None


def _footprint_bloque(binfo: dict) -> tuple[float, float]:
    """
    Calcula el footprint de posicionamiento de un bloque.

    Usa extmax - max(0, extmin) para obtener la huella que ocupa espacio
    en la cocina (excluyendo extensiones hacia la pared como conexiones de gas).
    """
    extmin = binfo.get("extmin", [0, 0])
    extmax = binfo.get("extmax", [0, 0])
    w = extmax[0] - max(0.0, extmin[0])
    d = extmax[1] - max(0.0, extmin[1])
    # Fallback a width/depth_mm si el calculo da valores invalidos
    if w < 50:
        w = binfo.get("width_mm", 0)
    if d < 50:
        d = binfo.get("depth_mm", 0)
    return (w, d)


def _reconciliar_dimensiones_bloques(
    equipos: list,
    reporte: list[dict] | None = None,
) -> list:
    """
    Ajusta dimensiones de equipos para coincidir con bloques CAD reales.

    Cuando existe un bloque CAD para un equipo, usa el footprint real del
    bloque en vez de las dimensiones teoricas. Esto garantiza que los
    bloques se inserten a escala ~1.0 (como en los planos profesionales).

    Args:
        equipos: Lista de EquipoResuelto
        reporte: Lista donde agregar reportes de cambios (opcional)

    Returns:
        Lista de equipos (modificados in-place)
    """
    bloque_map = _cargar_bloque_map()
    if not bloque_map:
        return equipos

    if reporte is None:
        reporte = []

    for eq in equipos:
        bname = _buscar_bloque_nombre(eq.modelo, bloque_map)
        if not bname:
            continue

        binfo = bloque_map[bname]
        bw, bd = _footprint_bloque(binfo)

        if bw < 50 or bd < 50:
            continue

        # Solo ajustar si hay diferencia significativa (>5%)
        diff_w = abs(eq.ancho_mm - bw) / max(eq.ancho_mm, 1)
        diff_d = abs(eq.fondo_mm - bd) / max(eq.fondo_mm, 1)

        if diff_w > 0.05 or diff_d > 0.05:
            orig_w = eq.ancho_mm
            orig_d = eq.fondo_mm
            eq.ancho_mm = int(round(bw))
            eq.fondo_mm = int(round(bd))

            reporte.append({
                "modelo": eq.modelo,
                "bloque_cad": bname,
                "dim_original": f"{orig_w}x{orig_d}mm",
                "dim_ajustada": f"{eq.ancho_mm}x{eq.fondo_mm}mm",
                "razon": "Ajustado a footprint real del bloque CAD",
            })
            print(
                f"[RECONCILIAR] {eq.modelo}: {orig_w}x{orig_d} -> "
                f"{eq.ancho_mm}x{eq.fondo_mm}mm (bloque {bname})"
            )

    return equipos


def _buscar_alternativa_menor(
    modelo: str,
    tipo: str,
    ancho_max: int,
    fondo_max: int,
    serie_pref: str = "750",
) -> dict | None:
    """
    Busca un equipo alternativo mas pequeno cuando el original no cabe.

    Returns:
        dict con {modelo, ancho_mm, fondo_mm, alto_mm, pvp_eur, serie, razon}
        o None si no hay alternativa.
    """
    try:
        from generador_cocinas import buscar_equipo_por_tipo
        alt = buscar_equipo_por_tipo(
            tipo=tipo,
            alimentacion="gas",
            ancho_preferido=ancho_max,
            serie_preferida=serie_pref,
        )
        if alt and alt["ancho_mm"] <= ancho_max and alt["fondo_mm"] <= fondo_max:
            return {
                **alt,
                "razon": f"Sustitucion: {modelo} ({alt['ancho_mm']}x{alt['fondo_mm']}mm) "
                         f"no cabe, usando {alt['modelo']} como alternativa",
            }
    except Exception:
        pass
    return None


# ─── Modelos ─────────────────────────────────────────────

@dataclass
class EquipoPosicionado:
    """Equipo con posicion final en coordenadas del cliente (mm)."""
    modelo: str
    tipo: str
    ancho_mm: int
    fondo_mm: int
    alto_mm: int
    pvp_eur: float | None
    serie: str
    cantidad: int
    zona: str
    # Posicion calculada
    x: float
    y: float
    rotation: float          # grados
    bloque_nombre: str | None = None
    wall_side: str = ""      # "north", "south", "east", "west"
    corners: list | None = None  # 4 esquinas en espacio original [(x,y), ...]


@dataclass
class HabitacionDetectada:
    """Habitacion/sala detectada como polilinea cerrada en el DXF."""
    id: str                                     # "hab_1", "hab_2", ...
    nombre: str                                 # Etiqueta mas cercana o "Sin nombre"
    rect: tuple[float, float, float, float]     # (min_x, min_y, max_x, max_y) mm, axis-aligned
    width_mm: float
    depth_mm: float


# ─── Constantes ──────────────────────────────────────────

MIN_AISLE_MM = 1200          # Pasillo minimo operativo recomendado
MIN_AISLE_ABSOLUTE_MM = 900  # Pasillo minimo absoluto (normativa antiincendios)


def _rotar_punto(x: float, y: float, angle_rad: float, cx: float, cy: float) -> tuple[float, float]:
    """Rota un punto (x,y) alrededor de (cx,cy) por angle_rad radianes."""
    dx, dy = x - cx, y - cy
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)
    return (cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a)

# Asignacion de zonas a paredes segun layout
WALL_ASSIGNMENT = {
    "lineal": {
        "coccion": "longest",
        "frio": "longest",
        "lavado": "longest",
        "horno": "longest",
    },
    "l": {
        "coccion": "longest",
        "frio": "adjacent",
        "lavado": "perpendicular",
        "horno": "perpendicular",
    },
    "u": {
        "coccion": "longest",
        "frio": "perpendicular",
        "lavado": "opposite",
        "horno": "adjacent",
    },
    "paralelo": {
        "coccion": "longest",
        "frio": "opposite",
        "lavado": "opposite",
        "horno": "opposite",
    },
    "concentrado": {
        "coccion": "longest",
        "lavado": "longest",       # Misma pared que coccion -> multi-fila
        "frio": "perpendicular",   # east si longest=north (como FINAL profesional)
        "horno": "longest",        # Misma pared si hay hornos
    },
}


# ─── Helpers ─────────────────────────────────────────────

def _longitud_total(paredes: list[SegmentoPared]) -> float:
    """Suma la longitud de una lista de segmentos de pared."""
    return sum(p.length for p in paredes)


def _resolver_lado(
    assignment: str,
    paredes: dict[str, list[SegmentoPared]],
) -> str:
    """
    Resuelve un assignment abstracto ("longest", "perpendicular", "opposite", "adjacent")
    a un lado concreto ("north", "south", "east", "west").

    - longest: pared mas larga
    - perpendicular: idx+1 en orden circular
    - opposite: idx+2 en orden circular
    - adjacent: idx+3 en orden circular (4a pared, opuesta a perpendicular)

    Si el lado ideal no tiene segmentos, busca el siguiente disponible.
    """
    lengths = {side: _longitud_total(walls) for side, walls in paredes.items()}

    if assignment == "longest":
        # Elegir la pared con segmentos mas larga
        available = {s: l for s, l in lengths.items() if l > 0}
        if available:
            return max(available, key=available.get)
        return max(lengths, key=lengths.get)

    longest_side = max((s for s, l in lengths.items() if l > 0), key=lengths.get, default="north")

    # Orden circular: north -> east -> south -> west
    sides_order = ["north", "east", "south", "west"]
    idx = sides_order.index(longest_side)

    if assignment == "perpendicular":
        target_idx = (idx + 1) % 4
    elif assignment == "opposite":
        target_idx = (idx + 2) % 4
    elif assignment == "adjacent":
        target_idx = (idx + 3) % 4
    else:
        return longest_side

    # Si el lado ideal no tiene segmentos, buscar el siguiente disponible
    for offset in range(4):
        candidate = sides_order[(target_idx + offset) % 4]
        if candidate != longest_side and lengths.get(candidate, 0) > 0:
            return candidate

    # Fallback: devolver el lado ideal aunque no tenga segmentos
    return sides_order[target_idx]


def _obtener_coordenadas_pared(
    side: str,
    wall_segs: list[SegmentoPared],
    boundary_rect: tuple[float, float, float, float] | None = None,
) -> tuple[float, float, float]:
    """
    Obtiene coordenadas de la pared principal para un lado.

    Agrupa segmentos por franjas de coordenada, y elige la franja que sea
    un MURO REAL (cerca del borde del boundary), no mobiliario intermedio
    como barras o mostradores.
    """
    from collections import defaultdict

    if side in ("north", "south"):
        grupos: dict[float, list[SegmentoPared]] = defaultdict(list)
        for s in wall_segs:
            y_val = round((s.start[1] + s.end[1]) / 2 / 100) * 100
            grupos[y_val].append(s)

        # Elegir pared real: preferir grupo cerca del borde del boundary
        # Un muro perimetral debe tener longitud >= ancho_cocina * 0.3
        if boundary_rect:
            bx1, by1, bx2, by2 = boundary_rect
            boundary_edge = by2 if side == "north" else by1
            ancho_cocina = bx2 - bx1
            min_wall_len = ancho_cocina * 0.3  # 30% del ancho = muro real

            # Filtrar grupos que son muros reales (longitud suficiente)
            candidatos = {}
            for y_val, segs in grupos.items():
                total = sum(s.length for s in segs)
                if total >= min_wall_len:
                    candidatos[y_val] = segs

            if candidatos:
                # De los candidatos, elegir el mas cercano al borde del boundary
                mejor_y = min(candidatos.keys(), key=lambda y: abs(y - boundary_edge))
            else:
                mejor_y = max(grupos.keys(), key=lambda y: sum(s.length for s in grupos[y]))
        else:
            mejor_y = max(grupos.keys(), key=lambda y: sum(s.length for s in grupos[y]))

        segs_principales = grupos[mejor_y]
        total_len = sum(s.length for s in segs_principales)
        wall_coord = sum(((s.start[1] + s.end[1]) / 2) * s.length for s in segs_principales) / total_len if total_len > 0 else mejor_y
        start_along = min(min(s.start[0], s.end[0]) for s in segs_principales)
        end_along = max(max(s.start[0], s.end[0]) for s in segs_principales)

    else:  # east, west
        grupos: dict[float, list[SegmentoPared]] = defaultdict(list)
        for s in wall_segs:
            x_val = round((s.start[0] + s.end[0]) / 2 / 100) * 100
            grupos[x_val].append(s)

        if boundary_rect:
            bx1, by1, bx2, by2 = boundary_rect
            boundary_edge = bx2 if side == "east" else bx1
            alto_cocina = by2 - by1
            min_wall_len = alto_cocina * 0.3

            candidatos = {}
            for x_val, segs in grupos.items():
                total = sum(s.length for s in segs)
                if total >= min_wall_len:
                    candidatos[x_val] = segs

            if candidatos:
                mejor_x = min(candidatos.keys(), key=lambda x: abs(x - boundary_edge))
            else:
                mejor_x = max(grupos.keys(), key=lambda x: sum(s.length for s in grupos[x]))
        else:
            mejor_x = max(grupos.keys(), key=lambda x: sum(s.length for s in grupos[x]))

        segs_principales = grupos[mejor_x]
        total_len = sum(s.length for s in segs_principales)
        wall_coord = sum(((s.start[0] + s.end[0]) / 2) * s.length for s in segs_principales) / total_len if total_len > 0 else mejor_x
        start_along = min(min(s.start[1], s.end[1]) for s in segs_principales)
        end_along = max(max(s.start[1], s.end[1]) for s in segs_principales)

    return wall_coord, start_along, end_along


def _rotacion_para_pared(wall_side: str) -> float:
    """Rotacion del bloque CAD para que el frente mire hacia el interior de la cocina."""
    return {
        "north": 0,    # frente mira hacia sur (interior)
        "south": 180,  # frente mira hacia norte (interior)
        "east":  270,  # frente mira hacia oeste (interior)
        "west":  90,   # frente mira hacia este (interior)
    }[wall_side]


def _obb_overlap(corners1: list[tuple], corners2: list[tuple], tolerance: float = 5.0) -> bool:
    """Chequea solapamiento entre dos rectangulos orientados usando SAT (Separating Axis Theorem)."""
    def _get_axes(corners):
        axes = []
        n = len(corners)
        for i in range(min(n, 2)):  # Solo necesitamos 2 ejes para rectangulos
            p1 = corners[i]
            p2 = corners[(i + 1) % n]
            ex, ey = p2[0] - p1[0], p2[1] - p1[1]
            length = math.sqrt(ex * ex + ey * ey)
            if length > 0:
                axes.append((-ey / length, ex / length))
        return axes

    def _project(corners, axis):
        projs = [c[0] * axis[0] + c[1] * axis[1] for c in corners]
        return min(projs), max(projs)

    for axis in _get_axes(corners1) + _get_axes(corners2):
        min1, max1 = _project(corners1, axis)
        min2, max2 = _project(corners2, axis)
        if min(max1, max2) - max(min1, min2) < tolerance:
            return False  # Eje separador encontrado -> no hay overlap
    return True  # Sin eje separador -> overlap


def _de_rotar_resultados(
    resultado: list[EquipoPosicionado],
    espacio: EspacioCocina,
) -> list[EquipoPosicionado]:
    """De-rota posiciones de espacio axis-aligned al espacio original del edificio."""
    if abs(espacio.dominant_angle) > 0.5:
        angle_rad = math.radians(espacio.dominant_angle)
        rcx, rcy = espacio.rotation_center
        print(f"\n[POSICIONAR] De-rotando {espacio.dominant_angle:.1f} alrededor de ({rcx:.0f}, {rcy:.0f})")
        for ep in resultado:
            ep.x, ep.y = _rotar_punto(ep.x, ep.y, angle_rad, rcx, rcy)
            if ep.corners:
                ep.corners = [_rotar_punto(cx_, cy_, angle_rad, rcx, rcy) for cx_, cy_ in ep.corners]
            ep.rotation += espacio.dominant_angle
    return resultado


def _calcular_posicion_en_pared(
    side: str, wall_coord: float, pos_along: float,
    w: float, d: float,
) -> tuple[float, float, list[tuple[float, float]]]:
    """Calcula (x, y, corners) para un equipo en una pared dada."""
    if side == "south":
        x, y = pos_along, wall_coord
        corners = [(x, y), (x + w, y), (x + w, y + d), (x, y + d)]
    elif side == "north":
        x, y = pos_along, wall_coord - d
        corners = [(x, y), (x + w, y), (x + w, y + d), (x, y + d)]
    elif side == "west":
        x, y = wall_coord, pos_along
        corners = [(x, y), (x + d, y), (x + d, y + w), (x, y + w)]
    else:  # east
        x, y = wall_coord - d, pos_along
        corners = [(x, y), (x + d, y), (x + d, y + w), (x, y + w)]
    return x, y, corners


def _clamp_rango_pared(
    side: str, start_along: float, end_along: float,
    kitchen_limits: tuple[float, float, float, float] | None,
) -> tuple[float, float]:
    """
    Extiende el rango de colocacion al boundary completo de la cocina.

    La pared detectada puede ser mas corta que el boundary (segmentos faltantes),
    pero el equipamiento puede usar todo el ancho/alto del boundary.
    """
    if not kitchen_limits:
        return start_along, end_along
    bx1, by1, bx2, by2 = kitchen_limits
    if side in ("north", "south"):
        # Usar rango X completo del boundary
        return bx1, bx2
    else:  # east, west
        # Usar rango Y completo del boundary
        return by1, by2


def _buscar_pared_overflow(
    current_side: str,
    paredes: dict[str, list],
    cursor_por_pared: dict[str, float],
    equip_width: float,
    kitchen_limits: tuple[float, float, float, float] | None,
    start_margin: float = 150.0,
) -> str | None:
    """Busca una pared con espacio disponible para overflow cuando el equipo no cabe."""
    sides_order = ["north", "east", "south", "west"]
    idx = sides_order.index(current_side)
    # Intentar: perpendicular, opposite, adjacent
    for delta in [1, 2, 3]:
        candidate = sides_order[(idx + delta) % 4]
        segs = paredes.get(candidate, [])
        if not segs:
            continue
        _, sa, ea = _obtener_coordenadas_pared(candidate, segs, kitchen_limits)
        if kitchen_limits:
            sa, ea = _clamp_rango_pared(candidate, sa, ea, kitchen_limits)
        cursor = cursor_por_pared.get(candidate, start_margin)
        available = (ea - sa) - cursor
        if available >= equip_width + 50:
            return candidate
    return None


def _clamp_equipos_a_boundary(
    resultado: list,
    boundary_rect: tuple[float, float, float, float] | None,
    margen: float = 50.0,
) -> list:
    """Clampea posiciones de equipos para que todas las esquinas queden dentro del boundary."""
    if not boundary_rect:
        return resultado
    bx1, by1, bx2, by2 = boundary_rect
    for ep in resultado:
        if not ep.corners or len(ep.corners) < 4:
            continue
        xs = [c[0] for c in ep.corners]
        ys = [c[1] for c in ep.corners]
        dx = 0.0
        dy = 0.0
        if min(xs) < bx1 - margen:
            dx = (bx1 + margen) - min(xs)
        elif max(xs) > bx2 + margen:
            dx = (bx2 - margen) - max(xs)
        if min(ys) < by1 - margen:
            dy = (by1 + margen) - min(ys)
        elif max(ys) > by2 + margen:
            dy = (by2 - margen) - max(ys)
        if abs(dx) > 1 or abs(dy) > 1:
            print(f"[POSICIONAR] Clamp boundary: {ep.modelo} dx={dx:.0f} dy={dy:.0f}")
            ep.x += dx
            ep.y += dy
            ep.corners = [(cx + dx, cy + dy) for cx, cy in ep.corners]
    return resultado


def _resolver_overlaps(
    resultado: list,
    boundary_rect: tuple[float, float, float, float] | None,
    max_iter: int = 5,
    label: str = "",
) -> list:
    """
    Resuelve solapamientos en espacio axis-aligned desplazando equipos a lo largo de su pared.
    Tambien resuelve overlaps cross-wall (entre paredes perpendiculares en esquinas).
    """
    if not resultado or len(resultado) < 2:
        return resultado

    MIN_GAP = 10.0  # mm minimo entre equipos
    tag = f" ({label})" if label else ""

    for iteration in range(max_iter):
        found_overlap = False

        # 1. Resolver overlaps entre equipos en la MISMA pared
        by_wall: dict[str, list] = {}
        for ep in resultado:
            if ep.wall_side:
                by_wall.setdefault(ep.wall_side, []).append(ep)

        for side, eps in by_wall.items():
            if len(eps) < 2:
                continue
            if side in ("north", "south"):
                eps.sort(key=lambda e: e.x)
            else:
                eps.sort(key=lambda e: e.y)

            for i in range(len(eps) - 1):
                ep1, ep2 = eps[i], eps[i + 1]
                if not ep1.corners or not ep2.corners:
                    continue
                xs1 = [c[0] for c in ep1.corners]
                ys1 = [c[1] for c in ep1.corners]
                xs2 = [c[0] for c in ep2.corners]
                ys2 = [c[1] for c in ep2.corners]
                r1 = (min(xs1), min(ys1), max(xs1), max(ys1))
                r2 = (min(xs2), min(ys2), max(xs2), max(ys2))
                ox = max(0, min(r1[2], r2[2]) - max(r1[0], r2[0]))
                oy = max(0, min(r1[3], r2[3]) - max(r1[1], r2[1]))
                if ox > 5 and oy > 5:
                    found_overlap = True
                    if side in ("north", "south"):
                        shift = ox + MIN_GAP
                        ep2.x += shift
                        ep2.corners = [(cx + shift, cy) for cx, cy in ep2.corners]
                    else:
                        shift = oy + MIN_GAP
                        ep2.y += shift
                        ep2.corners = [(cx, cy + shift) for cx, cy in ep2.corners]
                    print(f"[POSICIONAR] Overlap fix{tag} iter {iteration}: {ep1.modelo}({ep1.wall_side}) vs {ep2.modelo}({ep2.wall_side}) -> shift +{shift:.0f}mm")

        # 2. Resolver overlaps CROSS-WALL (perpendicular walls at corners)
        # Choose which equipment to shift based on fewer same-wall neighbors
        n_same_wall = {}
        for ep in resultado:
            if ep.wall_side:
                n_same_wall[ep.wall_side] = n_same_wall.get(ep.wall_side, 0) + 1

        for i, ep1 in enumerate(resultado):
            if not ep1.corners or not ep1.wall_side:
                continue
            for j in range(i + 1, len(resultado)):
                ep2 = resultado[j]
                if not ep2.corners or not ep2.wall_side:
                    continue
                if ep1.wall_side == ep2.wall_side:
                    continue
                xs1 = [c[0] for c in ep1.corners]
                ys1 = [c[1] for c in ep1.corners]
                xs2 = [c[0] for c in ep2.corners]
                ys2 = [c[1] for c in ep2.corners]
                r1 = (min(xs1), min(ys1), max(xs1), max(ys1))
                r2 = (min(xs2), min(ys2), max(xs2), max(ys2))
                ox = max(0, min(r1[2], r2[2]) - max(r1[0], r2[0]))
                oy = max(0, min(r1[3], r2[3]) - max(r1[1], r2[1]))
                if ox > 5 and oy > 5:
                    found_overlap = True
                    # Shift the equipment on the wall with FEWER items (less cascade)
                    n1 = n_same_wall.get(ep1.wall_side, 1)
                    n2 = n_same_wall.get(ep2.wall_side, 1)
                    target = ep2 if n2 <= n1 else ep1
                    if target.wall_side in ("north", "south"):
                        shift = ox + MIN_GAP
                        target.x += shift
                        target.corners = [(cx + shift, cy) for cx, cy in target.corners]
                    else:
                        shift = oy + MIN_GAP
                        target.y += shift
                        target.corners = [(cx, cy + shift) for cx, cy in target.corners]
                    print(f"[POSICIONAR] Cross-wall fix{tag} iter {iteration}: {ep1.modelo}({ep1.wall_side}) vs {ep2.modelo}({ep2.wall_side}) -> shift {target.modelo} +{shift:.0f}mm")

        if not found_overlap:
            break

    if boundary_rect:
        resultado = _clamp_equipos_a_boundary(resultado, boundary_rect)

    return resultado



# ─── Extraccion de habitaciones del DXF ─────────────────

def _area_poligono(pts: list[tuple[float, float]]) -> float:
    """Area de un poligono usando la formula del shoelace."""
    n = len(pts)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += pts[i][0] * pts[j][1]
        area -= pts[j][0] * pts[i][1]
    return abs(area) / 2.0


def _limpiar_mtext(text: str) -> str:
    """Limpia codigos de formato MTEXT (\\pxqc;, \\P, \\fArial|..., etc.)."""
    import re
    # Remove MTEXT formatting codes
    text = re.sub(r'\\[pPfFHWAaLlOoCcQq][^;]*;', '', text)
    text = re.sub(r'\\[PSs]', ' ', text)
    text = re.sub(r'\{|\}', '', text)
    text = re.sub(r'%%[uUoOdD]', '', text)
    return text.strip()


def _extraer_habitaciones_dxf(
    source_path: str,
    scale: float,
    dominant_angle: float,
    rotation_center: tuple[float, float],
) -> list[HabitacionDetectada]:
    """
    Extrae habitaciones del DXF como polilineas cerradas rectangulares.

    Busca en todas las capas polilineas cerradas con 4+ vertices que forman
    rectangulos de tamano razonable (1.2m a 20m de lado). Asocia cada
    habitacion con la etiqueta de texto mas corta dentro de ella.

    Returns:
        Lista de HabitacionDetectada ordenadas por area (mayor primero),
        deduplicadas (sin solapamientos >90%).
    """
    import ezdxf

    try:
        doc = ezdxf.readfile(source_path)
    except Exception as e:
        print(f"[HABITACIONES] Error leyendo DXF: {e}")
        return []

    msp = doc.modelspace()
    angle_rad = math.radians(-dominant_angle) if abs(dominant_angle) > 0.5 else 0
    rcx, rcy = rotation_center

    # 1. Recopilar textos con posiciones (en espacio axis-aligned)
    #    Preferir TEXT sobre MTEXT (TEXT suelen ser nombres de sala cortos)
    texts: list[tuple[str, float, float, bool]] = []  # (text, x, y, is_text_entity)
    for entity in msp:
        etype = entity.dxftype()
        if etype not in ("TEXT", "MTEXT"):
            continue
        try:
            raw = entity.text if etype == "MTEXT" else entity.dxf.text
            x = entity.dxf.insert.x * scale
            y = entity.dxf.insert.y * scale
            if abs(dominant_angle) > 0.5:
                x, y = _rotar_punto(x, y, angle_rad, rcx, rcy)
            text_clean = _limpiar_mtext(raw) if etype == "MTEXT" else raw.strip()
            if text_clean and 2 <= len(text_clean) <= 60:
                texts.append((text_clean, x, y, etype == "TEXT"))
        except Exception:
            continue

    # 2. Buscar polilineas cerradas que formen habitaciones
    MIN_ROOM = 1200   # 1.2m minimo
    MAX_ROOM = 20000  # 20m maximo (evitar envolvente del edificio)
    MIN_RECTANGULARITY = 0.70

    raw_rooms: list[HabitacionDetectada] = []

    for entity in msp:
        if entity.dxftype() != "LWPOLYLINE" or not entity.is_closed:
            continue
        pts_raw = list(entity.get_points(format="xy"))
        if len(pts_raw) < 4:
            continue

        pts = [(p[0] * scale, p[1] * scale) for p in pts_raw]
        if abs(dominant_angle) > 0.5:
            pts = [_rotar_punto(x, y, angle_rad, rcx, rcy) for x, y in pts]

        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        w = max_x - min_x
        h = max_y - min_y

        if not (MIN_ROOM <= w <= MAX_ROOM and MIN_ROOM <= h <= MAX_ROOM):
            continue

        poly_area = _area_poligono(pts)
        bbox_area = w * h
        if bbox_area > 0 and poly_area / bbox_area < MIN_RECTANGULARITY:
            continue

        # Buscar etiqueta: preferir TEXT cortos sobre MTEXT largos
        best_label = "Sin nombre"
        best_score = float("inf")  # lower = better
        for text, tx, ty, is_text in texts:
            if min_x <= tx <= max_x and min_y <= ty <= max_y:
                # Score: prefer short labels, TEXT entities, close to center
                cx, cy = (min_x + max_x) / 2, (min_y + max_y) / 2
                dist = math.sqrt((tx - cx) ** 2 + (ty - cy) ** 2)
                len_penalty = len(text) * 100  # shorter is better
                type_bonus = 0 if is_text else 5000  # TEXT > MTEXT
                score = dist + len_penalty + type_bonus
                if score < best_score:
                    best_score = score
                    best_label = text

        raw_rooms.append(HabitacionDetectada(
            id="",
            nombre=best_label,
            rect=(min_x, min_y, max_x, max_y),
            width_mm=w,
            depth_mm=h,
        ))

    # 3. Deduplicar: eliminar rooms con bbox casi identico (misma area ±20% Y overlap >80%)
    #    NO eliminar rooms pequenos contenidos en rooms mayores (son sub-habitaciones reales)
    raw_rooms.sort(key=lambda r: r.width_mm * r.depth_mm, reverse=True)
    rooms: list[HabitacionDetectada] = []
    for r in raw_rooms:
        is_dup = False
        r_area = r.width_mm * r.depth_mm
        for existing in rooms:
            e_area = existing.width_mm * existing.depth_mm
            # Solo deduplicar si areas son similares (±20%)
            if e_area > 0 and 0.80 <= r_area / e_area <= 1.20:
                ox = max(0, min(r.rect[2], existing.rect[2]) - max(r.rect[0], existing.rect[0]))
                oy = max(0, min(r.rect[3], existing.rect[3]) - max(r.rect[1], existing.rect[1]))
                overlap = ox * oy
                if r_area > 0 and overlap / r_area > 0.80:
                    is_dup = True
                    break
        if not is_dup:
            rooms.append(r)

    # 4. Filtrar rooms muy pequenos (< 3m2) que no pueden contener equipos
    MIN_AREA_MM2 = 3_000_000  # 3m2
    rooms = [r for r in rooms if r.width_mm * r.depth_mm >= MIN_AREA_MM2]

    # 5. Si demasiadas rooms sin nombre, la deteccion no es fiable -> devolver vacio
    named = sum(1 for r in rooms if r.nombre != "Sin nombre")
    if len(rooms) > 8 and named == 0:
        print(f"[HABITACIONES] {len(rooms)} rooms sin nombre -> deteccion no fiable, omitiendo")
        return []

    # 6. Limitar a max 10 rooms (las mas grandes)
    MAX_ROOMS = 10
    if len(rooms) > MAX_ROOMS:
        rooms = rooms[:MAX_ROOMS]

    # 7. Asignar IDs
    for i, r in enumerate(rooms):
        r.id = f"hab_{i + 1}"

    if rooms:
        print(f"[HABITACIONES] {len(rooms)} habitaciones detectadas:")
        for r in rooms:
            print(f"[HABITACIONES]   {r.id}: '{r.nombre}' {r.width_mm:.0f}x{r.depth_mm:.0f}mm")

    return rooms


# ─── Modelos Pydantic para posicionamiento IA ──────────

class EquipoPosicionLLM(BaseModel):
    """Decision del LLM para la posicion de un equipo."""
    modelo: str = Field(description="Modelo del equipo (debe coincidir EXACTAMENTE con la lista de entrada)")
    habitacion_id: str = Field(description="ID de la habitacion donde va el equipo (e.g., 'hab_1')")
    wall_side: str = Field(description="Pared de ESA habitacion: 'north', 'south', 'east' o 'west'")
    orden: int = Field(description="Orden a lo largo de la pared (1=primero desde el inicio)")
    gap_before_mm: int = Field(default=0, description="Espacio en mm ANTES de este equipo (para pasillo, puerta, etc.)")
    razon: str = Field(description="Justificacion breve de esta posicion")


class LayoutLLM(BaseModel):
    """Plan completo de posicionamiento generado por el LLM."""
    estrategia: str = Field(description="Descripcion de la estrategia de layout elegida")
    layout_tipo: str = Field(description="Tipo de layout: lineal, L, U o paralelo")
    posiciones: list[EquipoPosicionLLM] = Field(description="Posicion para CADA equipo individual")
    notas: str = Field(default="", description="Notas adicionales sobre la distribucion")


# ─── IA: Prompt, Validacion, Conversion ─────────────────

def _calcular_adyacencias(
    room: HabitacionDetectada,
    all_rooms: list[HabitacionDetectada],
    tol: float = 300,
) -> dict[str, str]:
    """
    Calcula que habitaciones son adyacentes a cada pared de una habitacion.

    Para cada pared (N/S/E/W), busca otras habitaciones cuya pared opuesta
    este alineada (dentro de tolerancia) y con solapamiento en el eje paralelo.

    Returns:
        {"north": "hab_3 (ALMACEN)", "south": "hab_1 (COMEDOR)", ...}
    """
    result: dict[str, str] = {}
    rx1, ry1, rx2, ry2 = room.rect

    for other in all_rooms:
        if other.id == room.id:
            continue
        ox1, oy1, ox2, oy2 = other.rect

        # Solapamiento horizontal (para paredes N/S)
        x_overlap = max(0, min(rx2, ox2) - max(rx1, ox1))
        # Solapamiento vertical (para paredes E/W)
        y_overlap = max(0, min(ry2, oy2) - max(ry1, oy1))

        label = f"{other.id} ({other.nombre[:30]})"

        # North wall de room (y=ry2) <-> South wall de other (y=oy1)
        if abs(ry2 - oy1) < tol and x_overlap > 500:
            result.setdefault("north", label)

        # South wall de room (y=ry1) <-> North wall de other (y=oy2)
        if abs(ry1 - oy2) < tol and x_overlap > 500:
            result.setdefault("south", label)

        # East wall de room (x=rx2) <-> West wall de other (x=ox1)
        if abs(rx2 - ox1) < tol and y_overlap > 500:
            result.setdefault("east", label)

        # West wall de room (x=rx1) <-> East wall de other (x=ox2)
        if abs(rx1 - ox2) < tol and y_overlap > 500:
            result.setdefault("west", label)

    return result


def _construir_prompt_posicionamiento(
    equipos: list,
    espacio: EspacioCocina,
    layout_tipo: str,
    habitaciones: list[HabitacionDetectada] | None = None,
) -> list:
    """Construye mensajes para que el LLM posicione los equipos."""
    from langchain_core.messages import SystemMessage, HumanMessage

    # Info de habitaciones detectadas
    rooms_text = ""
    if habitaciones:
        # Filtrar: solo habitaciones que intersectan con boundary_rect
        if espacio.boundary_rect:
            bx1, by1, bx2, by2 = espacio.boundary_rect
            habitaciones_filtradas = []
            for r in habitaciones:
                rx1, ry1, rx2, ry2 = r.rect
                # Interseccion de rectangulos
                if rx1 < bx2 and rx2 > bx1 and ry1 < by2 and ry2 > by1:
                    habitaciones_filtradas.append(r)
            if habitaciones_filtradas:
                habitaciones = habitaciones_filtradas

        rooms_text = "\nHABITACIONES DETECTADAS EN EL PLANO (polilineas cerradas):\n"
        for r in habitaciones:
            # Calcular adyacencias de cada pared
            adj = _calcular_adyacencias(r, habitaciones)
            adj_str = ""
            for side in ["north", "south", "east", "west"]:
                if adj.get(side):
                    adj_str += f"      pared {side} -> adyacente a: {adj[side]}\n"
                else:
                    adj_str += f"      pared {side} -> exterior/pasillo\n"

            rooms_text += (
                f"  - {r.id}: \"{r.nombre}\" - {r.width_mm:.0f}mm x {r.depth_mm:.0f}mm\n"
                f"    Paredes: north/south={r.width_mm:.0f}mm, east/west={r.depth_mm:.0f}mm\n"
                f"{adj_str}"
            )
        rooms_text += """
INSTRUCCIONES SOBRE HABITACIONES:
- Cada equipo DEBE ir en una habitacion (habitacion_id).
- Habitaciones con nombre "ASEO" son banos - NO colocar equipos ahi.
- La habitacion de cocina/servicio (ZONA SERVICIO, COCINA, o la mas grande que no sea comedor/bar) es donde va la MAYORIA de equipos.
- NUNCA pongas equipos en el comedor/restaurante (habitaciones >15m de largo suelen ser comedor).

REGLAS CRITICAS DE DISTRIBUCION (sigue estas EXACTAMENTE):

1. PARED PRINCIPAL (la MAS LARGA de la cocina - frecuentemente adyacente a ASEO/ALMACEN):
   - Aqui va TODO: coccion + lavado + servicio en FILAS PARALELAS automaticas.
   - FILA 1 (contra pared): cocina_gas, freidora, fry_top, barbacoa, fregadero/pila (J-6).
   - FILA 2 (1000mm detras): SMPG, ARMARIO_1_700, mesa_salida, GS-83.
   - FILA 3 (2750mm detras): B-2000 (botellero), lavavajillas.
   - TU asigna TODOS a la MISMA pared con el MISMO wall_side. El sistema crea las filas automaticamente.
   - CRITICO: Si eliges wall_side="north" para coccion, TODOS los lavado van tambien en wall_side="north".
     NUNCA pongas lavado en wall_side="south" si coccion esta en wall_side="north". Son FILAS, no paredes distintas.
   - Los equipos empiezan desde el INICIO de la pared (izquierda/west), NO centrados.
   - NOTA: que un aseo este al otro lado de la pared NO impide colocar coccion. La pared separa.

2. PARED PERPENDICULAR EAST (preferente):
   - Armarios GRANDES de conservacion y congelacion (ARMARIO 1, ARMARIO 2) van en la pared EAST.
   - EAST es preferente porque deja la pared WEST libre (donde suele estar el acceso/paso).
   - Solo usa WEST si EAST no tiene espacio suficiente o no existe.

3. ZONA DE BARRA (si existe habitacion de barra/bar):
   - Lavavajillas de barra va en la zona de barra, NO en la cocina.
   - Si no hay zona de barra, el botellero va en la pared PRINCIPAL (fila 3).

4. IMPORTANTE:
   - El fregadero/pila (J-6, grifo) es parte de la linea de coccion (fila 1), NO separado.
   - GS-83, SALIDA van en la MISMA pared principal (fila 2 de soporte).
   - B-2000 y LAVAVAJILLAS van en la MISMA pared principal (fila 3 de pesado).
   - NO distribuyas equipos en paredes opuestas. CONCENTRA en 1 pared + 1 perpendicular.
"""
    else:
        # Sin habitaciones: usar las paredes del espacio global
        wall_lines = []
        for side in ["north", "south", "east", "west"]:
            segs = espacio.paredes.get(side, [])
            total_len = _longitud_total(segs)
            if total_len > 0:
                wall_lines.append(f"  - Pared {side}: {total_len:.0f}mm de longitud util")
        rooms_text = f"\nPAREDES DEL ESPACIO (sin habitaciones individuales):\n{chr(10).join(wall_lines)}\n"

    # Info de zonas detectadas
    zone_text = ""
    if espacio.zonas:
        zone_text = "\nZONAS FUNCIONALES (etiquetas de texto en el plano):\n"
        for z in espacio.zonas:
            zone_text += f"  - {z.nombre}: '{z.etiqueta}' en ({z.centro[0]:.0f}, {z.centro[1]:.0f})\n"

        # Recomendaciones basadas en zonas adyacentes
        zone_recommendations = []
        for z in espacio.zonas:
            if z.nombre == "aseo":
                zone_recommendations.append(
                    f"  - '{z.etiqueta}' detectado en ({z.centro[0]:.0f}, {z.centro[1]:.0f}). "
                    f"La pared compartida con el aseo es valida para equipos (la pared provee separacion sanitaria). "
                    f"Los profesionales frecuentemente usan esta pared para coccion."
                )
            elif z.nombre == "comedor":
                zone_recommendations.append(
                    f"  - '{z.etiqueta}' es zona de clientes. La pared que da al comedor es ideal para "
                    f"pase de platos / mesa de salida. NO colocar equipos de coccion ahi."
                )
            elif z.nombre == "acceso":
                zone_recommendations.append(
                    f"  - '{z.etiqueta}' es punto de acceso. Mantener despejada la pared adyacente "
                    f"(minimo 1200mm de paso libre)."
                )
            elif z.nombre == "residuos":
                zone_recommendations.append(
                    f"  - '{z.etiqueta}' es zona de residuos. Colocar lavado/prelavado cerca de esta zona."
                )
        if zone_recommendations:
            zone_text += "\nRECOMENDACIONES POR ZONAS ADYACENTES:\n" + "\n".join(zone_recommendations) + "\n"

    # Lista de equipos (pre-expandida por cantidad)
    equip_lines = []
    idx = 0
    for eq in equipos:
        for u in range(eq.cantidad):
            idx += 1
            suffix = f" #{u+1}" if eq.cantidad > 1 else ""
            equip_lines.append(
                f"  {idx}. modelo=\"{eq.modelo}{suffix}\" tipo={eq.tipo} zona={eq.zona} "
                f"ancho={eq.ancho_mm}mm fondo={eq.fondo_mm}mm"
            )

    hab_note = 'habitacion_id debe ser uno de los IDs listados arriba (hab_1, hab_2, etc.)' if habitaciones else 'habitacion_id = "global" (unica zona disponible)'

    # Calcular capacidad por pared (clampeadas al boundary)
    wall_capacity_lines = []
    for side in ["north", "south", "east", "west"]:
        segs = espacio.paredes.get(side, [])
        if not segs:
            continue
        _, sa, ea = _obtener_coordenadas_pared(side, segs, espacio.boundary_rect)
        if espacio.boundary_rect:
            sa, ea = _clamp_rango_pared(side, sa, ea, espacio.boundary_rect)
        effective_len = ea - sa
        if effective_len > 0:
            wall_capacity_lines.append(f"  - Pared {side}: {effective_len:.0f}mm disponibles")
    capacity_text = "\n".join(wall_capacity_lines)

    # Boundary restriction text
    boundary_text = ""
    if espacio.boundary_rect:
        bx1, by1, bx2, by2 = espacio.boundary_rect
        boundary_text = f"""
RESTRICCION CRITICA DE BOUNDARY:
  - El boundary de la cocina es: ({bx1:.0f}, {by1:.0f}) a ({bx2:.0f}, {by2:.0f})
  - NUNCA coloques equipos fuera de este rectangulo.
  - SOLO usa habitaciones que esten DENTRO de este boundary.
  - Si una habitacion se extiende fuera del boundary, solo usa las paredes que estan dentro.
"""

    # Total ancho por zona
    from collections import defaultdict as _ddict
    zone_widths = _ddict(int)
    for eq in equipos:
        for _ in range(eq.cantidad):
            zone_widths[eq.zona] += eq.ancho_mm + 30
    zone_summary = ", ".join(f"{z}={w}mm" for z, w in zone_widths.items())

    # Few-shot: ejemplos de layouts profesionales similares
    examples_text = _formatear_ejemplos_prompt(equipos, espacio)

    system_prompt = f"""Eres un ingeniero experto en diseno de cocinas industriales Repagas.
Tu tarea es decidir la POSICION EXACTA de cada equipo dentro del local del cliente.

ESPACIO DEL CLIENTE:
  Dimensiones totales: {espacio.width_mm:.0f}mm x {espacio.depth_mm:.0f}mm
  Layout sugerido: {layout_tipo}

SISTEMA DE COORDENADAS DXF:
  - Eje X = horizontal (izquierda a derecha = west a east)
  - Eje Y = vertical (abajo a arriba = south a north)
  - Pared NORTH (Y grande): equipos de izq a der (X creciente), longitud = ancho cocina
  - Pared SOUTH (Y pequena): equipos de izq a der (X creciente), longitud = ancho cocina
  - Pared WEST (X pequena): equipos de abajo a arriba (Y creciente), longitud = fondo cocina
  - Pared EAST (X grande): equipos de abajo a arriba (Y creciente), longitud = fondo cocina
  - "orden=1" siempre empieza desde el inicio de la pared (izquierda o abajo).

CAPACIDAD DE PAREDES:
{capacity_text}
  Total ancho equipos por zona: {zone_summary}
  VERIFICA que el total de anchos + gaps quepa en la pared asignada ANTES de asignar.
{examples_text}{rooms_text}{boundary_text}{zone_text}
REGLA FUNDAMENTAL - CONCENTRACION (los profesionales hacen esto):
  Los disenadores profesionales CONCENTRAN la MAYORIA de equipos (70-90%) en 1-2 paredes.
  NUNCA distribuyen uniformemente en 4 paredes. Si caben en 1-2 paredes, USA SOLO 1-2 PAREDES.
  La pared principal (la mas larga) lleva coccion + lavado + servicio TODO JUNTO en FILAS PARALELAS.
  Solo usa paredes adicionales para armarios grandes de frio (conservacion/congelacion).

PATRON PROFESIONAL - MULTI-FILA EN PARED PRINCIPAL:
  El sistema coloca automaticamente los equipos en FILAS PARALELAS segun su zona:
  - FILA 1 (contra la pared): equipos de COCCION (cocina_gas, fry_top, barbacoa, fregadero/pila)
  - FILA 2 (1000mm detras): equipos de SOPORTE/LAVADO (mesa_refrigerada SMPG, armario_1_700, mesa_salida, grifo GS-83)
  - FILA 3 (2750mm detras): equipos PESADOS de lavado (botellero B-2000, lavavajillas)
  TU SOLO asigna todos estos equipos a la MISMA pared (la principal). El sistema calcula las filas.

REGLAS DE POSICIONAMIENTO OBLIGATORIAS:
1. PARED PRINCIPAL (la mas larga, normalmente north o south):
   Coloca coccion Y lavado Y botellero en ESTA MISMA PARED con el MISMO wall_side y habitacion_id.
   [coccion] fregadero/pila(J-6) -> cocina_gas -> fry_top -> barbacoa  (wall_side=X)
   [soporte] SMPG -> ARMARIO_1_700 -> SALIDA_700 -> GS-83               (wall_side=X, MISMO X)
   [pesado] B-2000 -> LAVAVAJILLAS                                      (wall_side=X, MISMO X)
   CRITICO: Si coccion va en wall_side="north", entonces SMPG, LAVAVAJILLAS, B-2000, GS-83,
   SALIDA_700, ARMARIO_1_700 van TAMBIEN en wall_side="north" de LA MISMA habitacion.
   El sistema crea las filas paralelas automaticamente. TU solo asigna a la MISMA pared.
   PROHIBIDO: poner lavado en la pared OPUESTA (si coccion=north, NO lavado=south).
2. PARED PERPENDICULAR EAST (preferente):
   Armarios GRANDES (ARMARIO 1, ARMARIO 2, armario_conservacion, armario_congelacion) van en pared EAST.
   EAST es preferente: deja la pared WEST libre para acceso/circulacion.
   Solo usa WEST si EAST no tiene espacio o no existe.
3. PARED OPUESTA: solo si sobran equipos que NO caben en la principal.
4. PASILLOS (CRITICO - el layout se RECHAZA si no se cumple):
   - Minimo ABSOLUTO entre equipos enfrentados: 900mm (normativa antiincendios).
   - Minimo RECOMENDADO: 1200mm (ergonomia operativa).
   - Si el local es muy estrecho (< 3000mm), NO coloques equipos en paredes opuestas.
5. Equipos empiezan desde el INICIO de la pared (izquierda/abajo), NO centrados.
6. Gap entre equipos: 30mm (`gap_before_mm=30`).
7. Cada equipo DEBE aparecer exactamente una vez. "modelo" debe coincidir EXACTAMENTE.
8. SOLAPAMIENTO (CRITICO - CAUSA RECHAZO):
   - El total de anchos + gaps en una pared NO puede exceder la longitud de la pared.
   - Si una pared se queda sin espacio, MUEVE sobrantes a la pared perpendicular.
9. ORIENTACION: El frente del equipo mira al INTERIOR de la cocina (automatico por wall_side).

REGLAS DE FLUJO:
10. SECUENCIA OPERATIVA: frio -> lavado -> coccion -> servicio (marcha adelante).
11. ASEOS ADYACENTES: La pared que comparte con un aseo ES VALIDA para coccion (la pared es separacion suficiente). Los profesionales usan esta pared frecuentemente.
12. El fregadero/pila (J-6, grifo) va al INICIO de la linea de coccion, NO separado en otra pared.
13. GS-83, SALIDA, PRELAVADO van en la MISMA pared principal (fila de soporte).

IMPORTANTE:
- {hab_note}
- wall_side solo puede ser: "north", "south", "east" o "west" (de la habitacion asignada)
- orden empieza en 1 para cada combinacion habitacion+pared
- gap_before_mm = 30 por defecto (separacion sutil)"""

    user_prompt = f"""Posiciona estos {idx} equipos en el local:

EQUIPOS:
{chr(10).join(equip_lines)}

Asigna cada equipo a una habitacion (habitacion_id) y una pared (wall_side) de esa habitacion.
Responde con el JSON estructurado segun el schema LayoutLLM."""

    return [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]


def _validar_layout_llm(
    layout: LayoutLLM,
    equipos_expandidos: list[tuple[str, object]],
    espacio: EspacioCocina,
    habitaciones: list[HabitacionDetectada] | None = None,
) -> tuple[bool, str]:
    """
    Valida la respuesta del LLM.
    equipos_expandidos: lista de (modelo_con_sufijo, EquipoResuelto)
    """
    modelos_input = sorted(m for m, _ in equipos_expandidos)
    modelos_llm = sorted(p.modelo for p in layout.posiciones)

    if modelos_input != modelos_llm:
        missing = set(modelos_input) - set(modelos_llm)
        extra = set(modelos_llm) - set(modelos_input)
        return False, f"Modelos no coinciden. Faltan: {missing}, Sobran: {extra}"

    valid_sides = {"north", "south", "east", "west"}
    valid_rooms = {r.id for r in habitaciones} if habitaciones else {"global"}

    for p in layout.posiciones:
        if p.wall_side not in valid_sides:
            return False, f"wall_side invalido '{p.wall_side}' para {p.modelo}"
        if p.habitacion_id not in valid_rooms:
            return False, f"habitacion_id invalido '{p.habitacion_id}' para {p.modelo}"

    # Verificar ancho total por habitacion+pared
    eq_map = {m: eq for m, eq in equipos_expandidos}
    room_map = {r.id: r for r in habitaciones} if habitaciones else {}

    width_per_room_wall: dict[tuple[str, str], float] = {}
    for p in layout.posiciones:
        eq = eq_map.get(p.modelo)
        if eq:
            key = (p.habitacion_id, p.wall_side)
            width_per_room_wall[key] = width_per_room_wall.get(key, 0) + eq.ancho_mm + p.gap_before_mm

    for (room_id, side), total_w in width_per_room_wall.items():
        room = room_map.get(room_id)
        if room:
            wall_len = room.width_mm if side in ("north", "south") else room.depth_mm
            if total_w > wall_len * 1.3:
                return False, f"{room_id} pared {side}: equipos {total_w:.0f}mm > pared {wall_len:.0f}mm"

    # Verificar que no haya pasillos muy estrechos
    if habitaciones:
        for room in habitaciones:
            norte_depths = [eq_map[p.modelo].fondo_mm for p in layout.posiciones if p.habitacion_id == room.id and p.wall_side == "north"]
            sur_depths = [eq_map[p.modelo].fondo_mm for p in layout.posiciones if p.habitacion_id == room.id and p.wall_side == "south"]
            if norte_depths and sur_depths:
                pasillo_y = room.depth_mm - max(norte_depths) - max(sur_depths)
                if pasillo_y < MIN_AISLE_ABSOLUTE_MM:
                    return False, f"Pasillo inviable ({pasillo_y:.0f}mm < {MIN_AISLE_ABSOLUTE_MM}mm) entre pared norte y sur en habitacion {room.id}"
                if pasillo_y < MIN_AISLE_MM:
                    print(f"[VALIDAR] AVISO: pasillo estrecho N/S ({pasillo_y:.0f}mm < {MIN_AISLE_MM}mm recomendado) en {room.id}")

            este_depths = [eq_map[p.modelo].fondo_mm for p in layout.posiciones if p.habitacion_id == room.id and p.wall_side == "east"]
            oeste_depths = [eq_map[p.modelo].fondo_mm for p in layout.posiciones if p.habitacion_id == room.id and p.wall_side == "west"]
            if este_depths and oeste_depths:
                pasillo_x = room.width_mm - max(este_depths) - max(oeste_depths)
                if pasillo_x < MIN_AISLE_ABSOLUTE_MM:
                    return False, f"Pasillo inviable ({pasillo_x:.0f}mm < {MIN_AISLE_ABSOLUTE_MM}mm) entre pared este y oeste en habitacion {room.id}"
                if pasillo_x < MIN_AISLE_MM:
                    print(f"[VALIDAR] AVISO: pasillo estrecho E/W ({pasillo_x:.0f}mm < {MIN_AISLE_MM}mm recomendado) en {room.id}")

    return True, "OK"


# ─── Validacion post-posicionamiento ─────────────────────

def _validar_layout_final(
    resultado: list[EquipoPosicionado],
    espacio: EspacioCocina,
    habitaciones: list[HabitacionDetectada] | None = None,
    strict: bool = False,
) -> list[str] | tuple[list[str], list[str]]:
    """
    Validacion post-posicionamiento (ambos paths: IA y algoritmico).

    Si strict=False (default): devuelve lista de advertencias (backward compatible).
    Si strict=True: devuelve (advertencias, errores) para validacion estricta.

    Checks:
    1. Pasillos entre paredes opuestas
    2. Densidad de equipamiento (max 45% superficie)
    3. Solapamiento entre equipos
    4. Equipos dentro del boundary
    5. Orientacion de equipos (frente mira al interior)
    """
    advertencias: list[str] = []
    errores: list[str] = []

    if not resultado:
        return (advertencias, errores) if strict else advertencias

    # 1. Pasillos entre paredes opuestas (espacio global)
    north_depths = [ep.fondo_mm for ep in resultado if ep.wall_side == "north"]
    south_depths = [ep.fondo_mm for ep in resultado if ep.wall_side == "south"]
    if north_depths and south_depths:
        pasillo_ns = espacio.depth_mm - max(north_depths) - max(south_depths)
        if pasillo_ns < MIN_AISLE_ABSOLUTE_MM:
            msg = f"Pasillo N/S critico: {pasillo_ns:.0f}mm (minimo {MIN_AISLE_ABSOLUTE_MM}mm)"
            (errores if strict else advertencias).append(msg)
        elif pasillo_ns < MIN_AISLE_MM:
            advertencias.append(f"Pasillo N/S estrecho: {pasillo_ns:.0f}mm (recomendado {MIN_AISLE_MM}mm)")

    east_depths = [ep.fondo_mm for ep in resultado if ep.wall_side == "east"]
    west_depths = [ep.fondo_mm for ep in resultado if ep.wall_side == "west"]
    if east_depths and west_depths:
        pasillo_ew = espacio.width_mm - max(east_depths) - max(west_depths)
        if pasillo_ew < MIN_AISLE_ABSOLUTE_MM:
            msg = f"Pasillo E/W critico: {pasillo_ew:.0f}mm (minimo {MIN_AISLE_ABSOLUTE_MM}mm)"
            (errores if strict else advertencias).append(msg)
        elif pasillo_ew < MIN_AISLE_MM:
            advertencias.append(f"Pasillo E/W estrecho: {pasillo_ew:.0f}mm (recomendado {MIN_AISLE_MM}mm)")

    # 2. Densidad de equipamiento
    total_huella = sum(ep.ancho_mm * ep.fondo_mm for ep in resultado)
    area_cocina = espacio.width_mm * espacio.depth_mm
    if area_cocina > 0:
        densidad = total_huella / area_cocina
        if densidad > 0.45:
            advertencias.append(f"Densidad excesiva: {densidad:.0%} del suelo cubierto (max recomendado 45%)")
        elif densidad > 0.35:
            advertencias.append(f"Densidad alta: {densidad:.0%} del suelo cubierto")

    # 3. Solapamiento entre equipos (OBB con SAT para planos rotados)
    tol_overlap = 5 if strict else 50  # 5mm en estricto, 50mm en normal
    is_rotated = abs(espacio.dominant_angle) > 0.5
    for i, ep1 in enumerate(resultado):
        if not ep1.corners or len(ep1.corners) < 4:
            continue
        for j in range(i + 1, len(resultado)):
            ep2 = resultado[j]
            if not ep2.corners or len(ep2.corners) < 4:
                continue
            if is_rotated:
                # Usar OBB (SAT) para planos rotados - evita falsos positivos AABB
                if _obb_overlap(ep1.corners, ep2.corners, tol_overlap):
                    msg = f"Solapamiento: {ep1.modelo} y {ep2.modelo} (OBB)"
                    (errores if strict else advertencias).append(msg)
            else:
                # AABB para planos no rotados (mas rapido y da dimensiones)
                xs1 = [c[0] for c in ep1.corners]
                ys1 = [c[1] for c in ep1.corners]
                xs2 = [c[0] for c in ep2.corners]
                ys2 = [c[1] for c in ep2.corners]
                r1 = (min(xs1), min(ys1), max(xs1), max(ys1))
                r2 = (min(xs2), min(ys2), max(xs2), max(ys2))
                ox = max(0, min(r1[2], r2[2]) - max(r1[0], r2[0]))
                oy = max(0, min(r1[3], r2[3]) - max(r1[1], r2[1]))
                if ox > tol_overlap and oy > tol_overlap:
                    msg = f"Solapamiento: {ep1.modelo} y {ep2.modelo} ({ox:.0f}x{oy:.0f}mm)"
                    (errores if strict else advertencias).append(msg)

    # 4. Equipos dentro del boundary (re-rotar a espacio axis-aligned si necesario)
    bx1, by1, bx2, by2 = espacio.boundary_rect
    margen = 200 if strict else 500
    for ep in resultado:
        if not ep.corners or len(ep.corners) < 4:
            continue
        if is_rotated:
            # Re-rotar corners al espacio axis-aligned para comparar con boundary
            angle_rad = math.radians(-espacio.dominant_angle)
            rcx, rcy = espacio.rotation_center
            aligned_corners = [_rotar_punto(cx, cy, angle_rad, rcx, rcy) for cx, cy in ep.corners]
            xs = [c[0] for c in aligned_corners]
            ys = [c[1] for c in aligned_corners]
        else:
            xs = [c[0] for c in ep.corners]
            ys = [c[1] for c in ep.corners]
        if min(xs) < bx1 - margen or max(xs) > bx2 + margen or min(ys) < by1 - margen or max(ys) > by2 + margen:
            msg = f"Fuera de boundary: {ep.modelo} en ({ep.x:.0f}, {ep.y:.0f})"
            (errores if strict else advertencias).append(msg)

    # 5. Orientacion de equipos
    for ep in resultado:
        if not ep.wall_side:
            continue
        expected_rot = (_rotacion_para_pared(ep.wall_side) + espacio.dominant_angle) % 360
        actual_rot = ep.rotation % 360
        diff = min(abs(actual_rot - expected_rot), 360 - abs(actual_rot - expected_rot))
        if diff > 10:
            advertencias.append(
                f"Orientacion: {ep.modelo} rot={actual_rot:.0f} esperado={expected_rot:.0f} pared={ep.wall_side}"
            )

    return (advertencias, errores) if strict else advertencias


def calcular_score_flujo(resultado: list[EquipoPosicionado]) -> float:
    """
    Calcula score 0-1 del flujo de trabajo (marcha adelante).
    Mide la concordancia entre el orden posicional de equipos en cada pared
    y el orden ideal de zonas: almacen -> frio -> lavado -> coccion -> servicio.
    """
    ZONE_ORDER = {
        "almacen": 0, "frio": 1, "lavado": 2,
        "coccion": 3, "horno": 3, "servicio": 4, "barra": 4,
    }
    by_wall: dict[str, list[EquipoPosicionado]] = {}
    for ep in resultado:
        if ep.wall_side:
            by_wall.setdefault(ep.wall_side, []).append(ep)

    total_concordant = 0
    total_pairs = 0

    for wall_side, eqs in by_wall.items():
        if len(eqs) < 2:
            continue
        if wall_side in ("north", "south"):
            eqs_sorted = sorted(eqs, key=lambda e: e.x)
        else:
            eqs_sorted = sorted(eqs, key=lambda e: e.y)

        zone_indices = [ZONE_ORDER.get(ep.zona, 2) for ep in eqs_sorted]
        for i in range(len(zone_indices)):
            for j in range(i + 1, len(zone_indices)):
                total_pairs += 1
                if zone_indices[j] >= zone_indices[i]:
                    total_concordant += 1

    return total_concordant / total_pairs if total_pairs > 0 else 1.0


def _verificar_completitud_zonas(equipos: list) -> list[str]:
    """
    Verifica que el equipamiento cubra todas las zonas esenciales.
    Returns lista de advertencias por zonas faltantes.
    """
    advertencias: list[str] = []

    zonas_presentes = set()
    tipos_presentes = set()
    for eq in equipos:
        zona = eq.zona if hasattr(eq, "zona") else "coccion"
        zonas_presentes.add(zona)
        tipo = eq.tipo if hasattr(eq, "tipo") else ""
        tipos_presentes.add(tipo)

    zonas_requeridas = {"coccion", "frio", "lavado"}
    faltantes = zonas_requeridas - zonas_presentes
    if faltantes:
        advertencias.append(f"Zonas sin equipos: {', '.join(faltantes)}")

    if not any(t in tipos_presentes for t in ("lavavajillas", "lavautensilios")):
        advertencias.append("Sin sistema de lavado de vajilla")

    has_frio = any(t.startswith("armario_") or t.startswith("mesa_refrig") for t in tipos_presentes)
    if not has_frio:
        advertencias.append("Sin refrigeracion (armarios o mesas refrigeradas)")

    return advertencias


def _verificar_y_ajustar_pasillos(
    resultado: list[EquipoPosicionado],
    espacio: EspacioCocina,
) -> None:
    """
    Verifica pasillos entre paredes opuestas en path algoritmico.
    Emite warnings. Modifica resultado IN-PLACE solo si pasillo < MIN_AISLE_ABSOLUTE_MM.
    """
    # Check N/S
    north_eqs = [ep for ep in resultado if ep.wall_side == "north"]
    south_eqs = [ep for ep in resultado if ep.wall_side == "south"]
    if north_eqs and south_eqs:
        max_d_n = max(ep.fondo_mm for ep in north_eqs)
        max_d_s = max(ep.fondo_mm for ep in south_eqs)
        pasillo = espacio.depth_mm - max_d_n - max_d_s
        if pasillo < MIN_AISLE_ABSOLUTE_MM:
            print(f"[POSICIONAR] ERROR: Pasillo N/S = {pasillo:.0f}mm < {MIN_AISLE_ABSOLUTE_MM}mm - layout inviable")
            print(f"[POSICIONAR]   Considere mover equipos a paredes perpendiculares (E/W)")
        elif pasillo < MIN_AISLE_MM:
            print(f"[POSICIONAR] AVISO: Pasillo N/S = {pasillo:.0f}mm < {MIN_AISLE_MM}mm recomendado")

    # Check E/W
    east_eqs = [ep for ep in resultado if ep.wall_side == "east"]
    west_eqs = [ep for ep in resultado if ep.wall_side == "west"]
    if east_eqs and west_eqs:
        max_d_e = max(ep.fondo_mm for ep in east_eqs)
        max_d_w = max(ep.fondo_mm for ep in west_eqs)
        pasillo = espacio.width_mm - max_d_e - max_d_w
        if pasillo < MIN_AISLE_ABSOLUTE_MM:
            print(f"[POSICIONAR] ERROR: Pasillo E/W = {pasillo:.0f}mm < {MIN_AISLE_ABSOLUTE_MM}mm - layout inviable")
            print(f"[POSICIONAR]   Considere mover equipos a paredes perpendiculares (N/S)")
        elif pasillo < MIN_AISLE_MM:
            print(f"[POSICIONAR] AVISO: Pasillo E/W = {pasillo:.0f}mm < {MIN_AISLE_MM}mm recomendado")


# ─── AI Self-Correction Loop ─────────────────────────────

def _ejecutar_validaciones_rapidas(
    equipos_pos: list[EquipoPosicionado],
    equipos_input: list,
    espacio: EspacioCocina,
    layout_tipo: str = "L",
    final_path: str | None = None,
) -> tuple[list, float]:
    """
    Ejecuta 8+1 validaciones en memoria (sin necesitar DXF generado).
    Si final_path existe, agrega comparacion vs FINAL profesional.
    Retorna (lista_validaciones, score_normalizado_0_1).
    """
    try:
        from test_validacion_exhaustiva import (
            validar_boundary_estricto, validar_zero_overlaps,
            validar_orientacion_equipos, validar_pasillos,
            validar_flujo_trabajo, validar_asignacion_paredes,
            validar_completitud_zonas, validar_densidad,
            comparar_vs_final,
        )
    except ImportError:
        # Modulo de test no disponible en produccion - retornar score neutro
        print("[VALIDAR] test_validacion_exhaustiva no disponible, saltando validaciones")
        return [], 0.90

    validaciones = [
        validar_boundary_estricto(equipos_pos, espacio),
        validar_zero_overlaps(equipos_pos),
        validar_orientacion_equipos(equipos_pos, espacio),
        validar_pasillos(equipos_pos, espacio),
        validar_flujo_trabajo(equipos_pos),
        validar_asignacion_paredes(equipos_pos, espacio, layout_tipo),
        validar_completitud_zonas(equipos_pos, equipos_input),
        validar_densidad(equipos_pos, espacio),
    ]
    pesos = [0.15, 0.15, 0.10, 0.15, 0.05, 0.10, 0.10, 0.05]

    # Comparacion vs FINAL profesional si esta disponible
    if final_path and os.path.exists(final_path):
        v_final = comparar_vs_final(equipos_pos, final_path, espacio)
        validaciones.append(v_final)
        pesos.append(0.15)

    score = sum(v.score * p for v, p in zip(validaciones, pesos))
    total_peso = sum(pesos)
    return validaciones, score / total_peso if total_peso > 0 else 0.0


def _formatear_feedback_correccion(
    layout_previo,
    validaciones: list,
    equipos_pos: list[EquipoPosicionado],
    espacio: EspacioCocina,
    iteracion: int,
    score: float,
) -> str:
    """
    Convierte fallos de validacion en un prompt de correccion accionable para el LLM.
    """
    # Mapeo de validacion -> tipo de accion sugerida
    acciones = {
        "boundary": "Mueve el equipo a una pared/habitacion que este DENTRO del boundary de la cocina.",
        "overlaps": "Aumenta gap_before_mm del segundo equipo o cambia su wall_side/orden para eliminar el solapamiento.",
        "orientacion": "Verifica que wall_side sea correcto para que el frente mire al interior.",
        "pasillos": "Mueve equipos de la pared opuesta a una pared perpendicular para ampliar el pasillo.",
        "flujo": "Reordena los equipos para seguir la marcha adelante: frio -> lavado -> coccion -> servicio.",
        "paredes": "Mueve el equipo a la pared correcta segun las reglas de layout (coccion en mas larga, frio en perpendicular).",
        "completitud": "Asegurate de incluir TODOS los equipos de la lista original. Falta alguno.",
        "densidad": "Hay demasiados equipos juntos. Redistribuye a paredes menos cargadas.",
    }

    lineas = []
    lineas.append(f"REVISION DE TU LAYOUT (iteracion {iteracion}, score={score:.2f}/1.00):")
    lineas.append("")

    # Errores criticos
    criticos = [v for v in validaciones if not v.passed and v.severidad == "error"]
    if criticos:
        lineas.append("ERRORES CRITICOS (DEBEN resolverse):")
        n = 0
        for v in criticos:
            for detalle in v.detalles[:3]:
                n += 1
                accion = acciones.get(v.nombre, "Corrige este problema.")
                lineas.append(f"  [{n}] {v.nombre}: {detalle}")
                lineas.append(f"      -> ACCION: {accion}")
        lineas.append("")

    # Advertencias
    warnings = [v for v in validaciones if not v.passed and v.severidad != "error"]
    if warnings:
        lineas.append("ADVERTENCIAS (deseables de resolver):")
        n = 0
        for v in warnings:
            for detalle in v.detalles[:2]:
                n += 1
                sugerencia = acciones.get(v.nombre, "Intenta mejorar esto.")
                lineas.append(f"  [{n}] {v.nombre}: {detalle}")
                lineas.append(f"      -> SUGERENCIA: {sugerencia}")
        lineas.append("")

    # Feedback de comparacion vs FINAL profesional
    v_final = next((v for v in validaciones if v.nombre == "vs_final"), None)
    if v_final and v_final.detalles:
        lineas.append("COMPARACION VS DISEÑO PROFESIONAL:")
        for d in v_final.detalles:
            lineas.append(f"  {d}")
        if v_final.score < 0.7:
            lineas.append("  -> Los profesionales CONCENTRAN equipos en 1-2 paredes, NO distribuyen en 4.")
            lineas.append("  -> Mueve mas equipos a la pared principal de coccion.")
        lineas.append("")

    # Distribucion actual por paredes
    from collections import defaultdict as _dd
    dist = _dd(list)
    for ep in equipos_pos:
        dist[ep.wall_side].append(ep.modelo)
    lineas.append("TU DISTRIBUCION ACTUAL POR PAREDES:")
    for side in ("north", "south", "east", "west"):
        eqs = dist.get(side, [])
        if eqs:
            lineas.append(f"  {side}: {len(eqs)} equipos -> {', '.join(eqs)}")
    lineas.append("")

    # Lo que funciona
    ok_names = [v.nombre for v in validaciones if v.passed]
    if ok_names:
        lineas.append(f"LO QUE FUNCIONA (NO cambiar): {', '.join(ok_names)}")
        lineas.append("")

    lineas.append("REGLAS RECORDATORIO:")
    lineas.append("- Mantiene TODOS los modelos exactos (no quitar, no anadir, no renombrar).")
    lineas.append("- Total anchos + gaps en una pared NO puede exceder la longitud de esa pared.")
    lineas.append("- Pasillos minimos: 900mm absoluto, 1200mm recomendado.")
    lineas.append("- Prioriza corregir: boundary > overlaps > pasillos > orientacion > flujo.")
    lineas.append("- Los profesionales CONCENTRAN en 1-2 paredes. NO distribuyas en 4 paredes.")
    lineas.append("")
    lineas.append("Genera un LayoutLLM CORREGIDO completo con todos los equipos.")

    return "\n".join(lineas)


def posicionar_con_ia_iterativo(
    equipos: list,
    espacio: EspacioCocina,
    layout_tipo: str = "L",
    max_iteraciones: int = 5,
    score_objetivo: float = 0.92,
    final_path: str | None = None,
) -> list[EquipoPosicionado]:
    """
    Posicionamiento IA con loop de auto-correccion.

    1. Genera layout inicial con el LLM
    2. Valida con 8+1 metricas (incluyendo comparacion vs FINAL si disponible)
    3. Si score < objetivo: formatea feedback de errores y re-prompta al LLM
    4. Repite hasta score >= objetivo, max iteraciones, o sin mejora
    5. Retorna el mejor resultado; si todo falla, fallback algoritmico
    """
    from generador_cocinas import invocar_llm_con_rotacion
    from langchain_core.messages import AIMessage, HumanMessage

    print(f"\n[IA-LOOP] Iniciando loop iterativo (max={max_iteraciones}, objetivo={score_objetivo})")

    # Reconciliar dimensiones con bloques CAD reales
    reporte_bloques: list[dict] = []
    _reconciliar_dimensiones_bloques(equipos, reporte_bloques)
    if reporte_bloques:
        print(f"[IA-LOOP] {len(reporte_bloques)} equipos ajustados a dimensiones de bloque CAD")

    equipos_exp = _expandir_equipos(equipos)

    # Extraer habitaciones del DXF
    habitaciones: list[HabitacionDetectada] = []
    if espacio.source_path:
        try:
            habitaciones = _extraer_habitaciones_dxf(
                espacio.source_path, espacio.unit_scale,
                espacio.dominant_angle, espacio.rotation_center,
            )
        except Exception as e:
            print(f"[IA-LOOP] Error extrayendo habitaciones: {e}")

    # Construir prompt inicial
    try:
        messages = _construir_prompt_posicionamiento(
            equipos, espacio, layout_tipo,
            habitaciones=habitaciones if habitaciones else None,
        )
    except Exception as e:
        print(f"[IA-LOOP] Error construyendo prompt: {e}")
        return posicionar_en_espacio(equipos, espacio, layout_tipo)

    conversation = list(messages)  # Copia mutable

    best_score = -1.0
    best_result = None
    best_iter = 0
    sin_mejora = 0

    for iteracion in range(1, max_iteraciones + 1):
        print(f"\n[IA-LOOP] === Iteracion {iteracion}/{max_iteraciones} ===")

        # 1. Llamar al LLM
        try:
            layout = invocar_llm_con_rotacion(conversation, structured_cls=LayoutLLM)
        except Exception as e:
            print(f"[IA-LOOP] Error LLM iter {iteracion}: {e}")
            layout = None

        if layout is None:
            print(f"[IA-LOOP] LLM no respondio en iteracion {iteracion}")
            break

        print(f"[IA-LOOP] Estrategia: {layout.estrategia}")
        for p in layout.posiciones:
            print(f"[IA-LOOP]   {p.modelo} -> {p.habitacion_id}/{p.wall_side} orden={p.orden}")

        # 2. Validar estructura (modelo names, wall_side, room IDs)
        valid, msg = _validar_layout_llm(
            layout, equipos_exp, espacio,
            habitaciones=habitaciones if habitaciones else None,
        )
        if not valid:
            print(f"[IA-LOOP] Error estructural: {msg}")
            # Feedback inmediato
            try:
                conversation.append(AIMessage(content=layout.model_dump_json(indent=2)))
            except Exception:
                conversation.append(AIMessage(content=str(layout)))
            conversation.append(HumanMessage(
                content=f"ERROR ESTRUCTURAL en tu respuesta: {msg}\n"
                        f"Corrige y responde con el LayoutLLM completo."
            ))
            continue

        # 3. Convertir a coordenadas
        equipos_pos = _convertir_layout_llm_a_posiciones(
            layout, equipos_exp, espacio,
            habitaciones=habitaciones if habitaciones else None,
        )
        if not equipos_pos:
            print(f"[IA-LOOP] Conversion produjo 0 equipos en iter {iteracion}")
            break

        equipos_pos = _de_rotar_resultados(equipos_pos, espacio)
        equipos_pos = _clamp_equipos_a_boundary(equipos_pos, espacio.boundary_rect)

        # 4. Validar geometria (8+1 metricas, incluyendo vs FINAL si disponible)
        try:
            validaciones, score = _ejecutar_validaciones_rapidas(
                equipos_pos, equipos, espacio, layout_tipo,
                final_path=final_path,
            )
        except Exception as e:
            print(f"[IA-LOOP] Error validando iter {iteracion}: {e}")
            score = 0.0
            validaciones = []

        print(f"[IA-LOOP] Score: {score:.3f} (objetivo: {score_objetivo})")

        # Mostrar fallos
        for v in validaciones:
            if not v.passed:
                for d in v.detalles[:2]:
                    print(f"[IA-LOOP]   [{v.severidad}] {v.nombre}: {d}")

        # 5. Tracking best-of-N
        if score > best_score:
            best_score = score
            best_result = equipos_pos
            best_iter = iteracion
            sin_mejora = 0
        else:
            sin_mejora += 1

        # 6. Criterios de parada
        criticos_ok = all(v.passed for v in validaciones if v.severidad == "error")
        if score >= score_objetivo and criticos_ok:
            print(f"[IA-LOOP] Objetivo {score_objetivo} + criticos OK en iteracion {iteracion}!")
            break

        # Score alto pero criticos fallan -> seguir iterando
        if score >= score_objetivo and not criticos_ok:
            criticos_fallidos = [v.nombre for v in validaciones if v.severidad == "error" and not v.passed]
            print(f"[IA-LOOP] Score {score:.3f} >= {score_objetivo} PERO criticos fallan: {criticos_fallidos}")
            print(f"[IA-LOOP] Continuando iteracion para corregir criticos...")

        # Criticos OK + score razonable = suficiente
        if criticos_ok and score >= 0.85:
            print(f"[IA-LOOP] Criticos OK + score {score:.2f} >= 0.85, suficiente")
            break

        if sin_mejora >= 2:
            print(f"[IA-LOOP] Sin mejora en 2 iteraciones consecutivas, deteniendo")
            break

        # 7. Preparar feedback para la siguiente iteracion
        if iteracion < max_iteraciones:
            feedback = _formatear_feedback_correccion(
                layout, validaciones, equipos_pos, espacio, iteracion, score,
            )
            try:
                conversation.append(AIMessage(content=layout.model_dump_json(indent=2)))
            except Exception:
                conversation.append(AIMessage(content=str(layout)))
            conversation.append(HumanMessage(content=feedback))
            print(f"[IA-LOOP] Feedback enviado para iteracion {iteracion + 1}")

    # Resultado final
    if best_result:
        print(f"\n[IA-LOOP] Mejor score: {best_score:.3f} (iteracion {best_iter})")
        # Validacion final
        adv_layout = _validar_layout_final(best_result, espacio, habitaciones)
        for adv in adv_layout:
            print(f"[IA-LOOP] AVISO: {adv}")

        print(f"\n[IA-LOOP] {len(best_result)} equipos posicionados por IA (iterativo):")
        for ep in best_result:
            print(f"[IA-LOOP]   {ep.modelo} ({ep.zona}) -> ({ep.x:.0f}, {ep.y:.0f}) rot={ep.rotation:.1f} pared={ep.wall_side}")

        return best_result

    print("[IA-LOOP] Todas las iteraciones fallaron, usando fallback algoritmico")
    return posicionar_en_espacio(equipos, espacio, layout_tipo)


def _wall_from_room_rect(
    side: str,
    rect: tuple[float, float, float, float],
) -> tuple[float, float, float]:
    """
    Crea coordenadas de pared sinteticas a partir del rectangulo de una habitacion.

    Returns:
        (wall_coord, start_along, end_along)
    """
    min_x, min_y, max_x, max_y = rect
    if side == "north":
        return max_y, min_x, max_x
    elif side == "south":
        return min_y, min_x, max_x
    elif side == "east":
        return max_x, min_y, max_y
    else:  # west
        return min_x, min_y, max_y


def _normalizar_paredes_layout(
    layout: LayoutLLM,
    equipos_expandidos: list[tuple[str, object]],
) -> LayoutLLM:
    """
    Corrige asignaciones de pared del LLM:
    - Mueve lavado a la misma pared/habitacion que coccion (multi-fila automatica).
    - Si lavado esta en pared OPUESTA a coccion en la misma habitacion, lo reasigna.
    """
    eq_map = {m: eq for m, eq in equipos_expandidos}

    # Encontrar pared principal de coccion (la que tiene mas items de coccion)
    from collections import Counter
    coccion_walls: Counter = Counter()
    for p in layout.posiciones:
        eq = eq_map.get(p.modelo)
        if eq and hasattr(eq, "zona") and eq.zona in ("coccion", "horno"):
            coccion_walls[(p.habitacion_id, p.wall_side)] += 1

    if not coccion_walls:
        return layout  # Sin coccion -> no hay nada que normalizar

    # Pared principal = (habitacion, side) con mas coccion
    main_hab, main_side = coccion_walls.most_common(1)[0][0]
    _OPUESTO = {"north": "south", "south": "north", "east": "west", "west": "east"}
    opposite_side = _OPUESTO[main_side]

    # Detectar lavado en pared opuesta dentro de la misma habitacion
    movidos = 0
    for p in layout.posiciones:
        eq = eq_map.get(p.modelo)
        if not eq:
            continue
        zona = getattr(eq, "zona", "") or ""
        if zona not in ("lavado", "lavado_pesado"):
            continue
        # Si el lavado esta en la pared OPUESTA de la misma habitacion -> mover
        if p.habitacion_id == main_hab and p.wall_side == opposite_side:
            print(f"[NORMALIZAR] {p.modelo} (lavado) movido de {p.habitacion_id}/{opposite_side} -> {main_side}")
            p.habitacion_id = main_hab
            p.wall_side = main_side
            movidos += 1

    if movidos:
        # Reordenar los items en main_wall para que coccion tenga ordenes bajos
        main_wall_items = [p for p in layout.posiciones
                          if p.habitacion_id == main_hab and p.wall_side == main_side]
        # Separar coccion y lavado, mantener ordenados por zona y luego por orden original
        _ZONA_ORDER = {"coccion": 0, "horno": 1, "frio": 2, "lavado": 3, "lavado_pesado": 4}
        def _item_sort(p_item):
            eq = eq_map.get(p_item.modelo)
            z = getattr(eq, "zona", "lavado") if eq else "lavado"
            return (_ZONA_ORDER.get(z, 50), p_item.orden)
        main_wall_items.sort(key=_item_sort)
        for idx, p_item in enumerate(main_wall_items, start=1):
            p_item.orden = idx
        print(f"[NORMALIZAR] {movidos} equipos de lavado movidos a {main_hab}/{main_side}. Total en esa pared: {len(main_wall_items)}")

    return layout


def _convertir_layout_llm_a_posiciones(
    layout: LayoutLLM,
    equipos_expandidos: list[tuple[str, object]],
    espacio: EspacioCocina,
    habitaciones: list[HabitacionDetectada] | None = None,
) -> list[EquipoPosicionado]:
    """Convierte decisiones del LLM a coordenadas exactas EquipoPosicionado."""
    # Normalizar: asegurar que lavado va en la misma pared que coccion
    layout = _normalizar_paredes_layout(layout, equipos_expandidos)

    eq_map = {m: eq for m, eq in equipos_expandidos}
    room_map = {r.id: r for r in habitaciones} if habitaciones else {}

    # Agrupar por (habitacion, pared) y ordenar
    by_room_wall: dict[tuple[str, str], list[EquipoPosicionLLM]] = {}
    for p in layout.posiciones:
        key = (p.habitacion_id, p.wall_side)
        by_room_wall.setdefault(key, []).append(p)
    for items in by_room_wall.values():
        items.sort(key=lambda p: p.orden)

    resultado: list[EquipoPosicionado] = []

    for (room_id, side), items in by_room_wall.items():
        room = room_map.get(room_id)

        if room:
            # Usar paredes sinteticas de la habitacion
            wall_coord, start_along, end_along = _wall_from_room_rect(side, room.rect)
            # Clamp a boundary_rect de la cocina
            if espacio.boundary_rect:
                start_along, end_along = _clamp_rango_pared(side, start_along, end_along, espacio.boundary_rect)
                bx1, by1, bx2, by2 = espacio.boundary_rect
                if side == "north":
                    wall_coord = min(wall_coord, by2)
                elif side == "south":
                    wall_coord = max(wall_coord, by1)
                elif side == "east":
                    wall_coord = min(wall_coord, bx2)
                elif side == "west":
                    wall_coord = max(wall_coord, bx1)
            wall_length = end_along - start_along
        else:
            # Fallback: usar paredes del espacio global
            wall_segs = espacio.paredes.get(side, [])
            if not wall_segs:
                print(f"[POSICIONAR-IA] WARN: {room_id}/{side} sin segmentos, saltando {len(items)} equipos")
                continue
            wall_coord, start_along, end_along = _obtener_coordenadas_pared(side, wall_segs, espacio.boundary_rect)
            if espacio.boundary_rect:
                start_along, end_along = _clamp_rango_pared(side, start_along, end_along, espacio.boundary_rect)
            wall_length = end_along - start_along

        rotation = _rotacion_para_pared(side)

        # Empezar desde el inicio de la pared (margen minimo 100mm)
        offset = 100
        # Track cursor for overflow detection
        cursor_ia = {}

        # Multi-fila: tracking de filas por zona
        # Usar gaps profesionales: coccion(0) -> soporte(1000mm) -> pesado(2750mm)
        _ZONE_PRIORITY = {"coccion": 0, "horno": 1, "frio": 2, "lavado": 3, "lavado_pesado": 4}
        _LAVADO_PESADO = {"lavavajillas", "botellero", "fregadero_industrial"}
        ROW_GAP_NORMAL = 1000.0   # gap coccion -> soporte
        ROW_GAP_PESADO = 1750.0   # gap soporte -> pesado (total 2750mm from wall)

        # Sort items by zone priority to ensure coccion is ALWAYS row 0 (against wall)
        def _zone_sort_key(p_item):
            eq_tmp = eq_map.get(p_item.modelo)
            if eq_tmp:
                tipo = (eq_tmp.tipo if hasattr(eq_tmp, 'tipo') else '').lower()
                zona = (eq_tmp.zona if hasattr(eq_tmp, 'zona') else '').lower()
                if tipo in _LAVADO_PESADO:
                    return (_ZONE_PRIORITY.get('lavado_pesado', 99), p_item.orden)
                return (_ZONE_PRIORITY.get(zona, 50), p_item.orden)
            return (50, p_item.orden)

        items = sorted(items, key=_zone_sort_key)

        current_row_offset = 0.0  # offset perpendicular acumulado
        max_depth_current_row = 0.0  # max fondo en fila actual
        current_zone = None  # zona actual para detectar cambios

        for p in items:
            eq = eq_map.get(p.modelo)
            if not eq:
                continue

            w = eq.ancho_mm
            d = eq.fondo_mm

            # Multi-fila: detectar cambio de zona en la misma pared
            eq_tipo = (eq.tipo if hasattr(eq, "tipo") else "").lower()
            eq_zone = eq.zona if hasattr(eq, "zona") else None
            # Classify: lavado_pesado types get their own zone label
            effective_zone = "lavado_pesado" if eq_tipo in _LAVADO_PESADO else eq_zone
            if effective_zone and current_zone and effective_zone != current_zone and max_depth_current_row > 0:
                # Nueva zona -> nueva fila con gap profesional
                row_gap = ROW_GAP_PESADO if effective_zone == "lavado_pesado" else ROW_GAP_NORMAL
                current_row_offset += max_depth_current_row + row_gap
                max_depth_current_row = 0.0
                offset = 100  # Reset cursor para nueva fila
                print(f"[POSICIONAR-IA] Nueva fila en {side}: row_offset={current_row_offset:.0f}mm (zona={effective_zone})")
            current_zone = effective_zone

            offset += p.gap_before_mm

            if offset + w > wall_length + 100:
                # Intentar overflow a otra pared
                overflow_side = _buscar_pared_overflow(
                    side, espacio.paredes, cursor_ia, w,
                    espacio.boundary_rect, 100.0,
                )
                if overflow_side and overflow_side != side:
                    print(f"[POSICIONAR-IA] Overflow: {eq.modelo} {side} -> {overflow_side}")
                    wall_segs_of = espacio.paredes.get(overflow_side, [])
                    if wall_segs_of:
                        wc_of, sa_of, ea_of = _obtener_coordenadas_pared(overflow_side, wall_segs_of, espacio.boundary_rect)
                        if espacio.boundary_rect:
                            sa_of, ea_of = _clamp_rango_pared(overflow_side, sa_of, ea_of, espacio.boundary_rect)
                        of_offset = cursor_ia.get(overflow_side, 100.0)
                        rot_of = _rotacion_para_pared(overflow_side)
                        pos_along_of = sa_of + of_offset
                        x, y, corners = _calcular_posicion_en_pared(overflow_side, wc_of, pos_along_of, w, d)
                        resultado.append(EquipoPosicionado(
                            modelo=eq.modelo, tipo=eq.tipo,
                            ancho_mm=eq.ancho_mm, fondo_mm=eq.fondo_mm,
                            alto_mm=eq.alto_mm, pvp_eur=eq.pvp_eur,
                            serie=eq.serie, cantidad=1, zona=eq.zona,
                            x=x, y=y, rotation=rot_of, corners=corners,
                            wall_side=overflow_side,
                        ))
                        cursor_ia[overflow_side] = of_offset + w
                        continue
                offset = max(0, wall_length - w)

            pos_along = start_along + offset

            # Calcular wall_coord efectivo con row_offset
            sign = -1.0 if side in ("north", "east") else 1.0
            effective_wall_coord = wall_coord + sign * current_row_offset

            x, y, corners = _calcular_posicion_en_pared(side, effective_wall_coord, pos_along, w, d)
            max_depth_current_row = max(max_depth_current_row, d)

            resultado.append(EquipoPosicionado(
                modelo=eq.modelo,
                tipo=eq.tipo,
                ancho_mm=eq.ancho_mm,
                fondo_mm=eq.fondo_mm,
                alto_mm=eq.alto_mm,
                pvp_eur=eq.pvp_eur,
                serie=eq.serie,
                cantidad=1,
                zona=eq.zona,
                x=x, y=y,
                rotation=rotation,
                corners=corners,
                wall_side=side,
            ))
            offset += w

    # Post-process: redistribute for professional layout
    resultado = _redistribuir_por_zona_ia(resultado, espacio)

    return resultado


def _redistribuir_por_zona_ia(
    resultado: list[EquipoPosicionado],
    espacio: EspacioCocina,
) -> list[EquipoPosicionado]:
    """Post-process IA positions: spread lavado, center pesado, top-align armarios."""
    if not resultado or not espacio.boundary_rect:
        return resultado

    bx1, by1, bx2, by2 = espacio.boundary_rect

    # === 1. Perpendicular walls: place armarios near TOP (cooking area) ===
    for side in ("east", "west"):
        side_items = [ep for ep in resultado if ep.wall_side == side]
        if not side_items:
            continue
        wall_segs = espacio.paredes.get(side, [])
        if not wall_segs:
            continue
        wc, sa, ea = _obtener_coordenadas_pared(side, wall_segs, espacio.boundary_rect)
        sa, ea = _clamp_rango_pared(side, sa, ea, espacio.boundary_rect)
        # sa=by1, ea=by2 for east/west (Y range)

        total_h = sum(ep.ancho_mm for ep in side_items) + 30 * max(0, len(side_items) - 1)
        # Start near TOP (high Y = near north/cooking)
        new_start = ea - total_h - 200
        new_start = max(sa + 100, new_start)

        pos = new_start
        for ep in sorted(side_items, key=lambda e: e.y):
            x, y, corners = _calcular_posicion_en_pared(side, wc, pos, ep.ancho_mm, ep.fondo_mm)
            ep.x = x
            ep.y = y
            ep.corners = corners
            pos += ep.ancho_mm + 30
        print(f"[REDIST] {side}: {len(side_items)} armarios movidos al TOP (Y~{new_start:.0f}-{pos:.0f})")

    # === 2. North/south walls: spread lavado row, center pesado row ===
    for side in ("north", "south"):
        side_items = [ep for ep in resultado if ep.wall_side == side]
        if len(side_items) < 4:
            continue
        wall_segs = espacio.paredes.get(side, [])
        if not wall_segs:
            continue
        wc, sa, ea = _obtener_coordenadas_pared(side, wall_segs, espacio.boundary_rect)
        sa, ea = _clamp_rango_pared(side, sa, ea, espacio.boundary_rect)
        wall_len = ea - sa

        # Cluster into rows by Y coordinate
        sort_desc = (side == "north")  # north: higher Y = closer to wall
        side_items_sorted = sorted(side_items, key=lambda ep: -ep.y if sort_desc else ep.y)
        rows: list[list[EquipoPosicionado]] = []
        current_row = [side_items_sorted[0]]
        for ep in side_items_sorted[1:]:
            if abs(ep.y - current_row[-1].y) < 600:
                current_row.append(ep)
            else:
                rows.append(current_row)
                current_row = [ep]
        rows.append(current_row)

        # Row 0 = coccion - leave as is
        # Row 1 = lavado support - SPREAD (left group stays, right group to end)
        if len(rows) >= 2:
            lavado_row = rows[1]
            _RIGHT_KW = {"SALIDA", "GS-83", "GS_83"}
            left_group = []
            right_group = []
            for ep in lavado_row:
                modelo_up = (ep.modelo or "").upper().replace("-", "_").replace(" ", "_")
                if any(k in modelo_up for k in _RIGHT_KW):
                    right_group.append(ep)
                else:
                    left_group.append(ep)

            if right_group:
                # Get wall_coord for this row
                row_wc = max(ep.y + ep.fondo_mm for ep in lavado_row) if side == "north" else min(ep.y for ep in lavado_row)
                # Right group: place at END of wall
                right_total = sum(ep.ancho_mm for ep in right_group) + 30 * max(0, len(right_group) - 1)
                right_start = ea - right_total - 200
                pos = right_start
                for ep in sorted(right_group, key=lambda e: e.x):
                    x, y, corners = _calcular_posicion_en_pared(side, row_wc, pos, ep.ancho_mm, ep.fondo_mm)
                    ep.x = x
                    ep.y = y
                    ep.corners = corners
                    pos += ep.ancho_mm + 30
                print(f"[REDIST] {side} lavado: {len(right_group)} items moved to right (X~{right_start:.0f})")

        # Row 2 = pesado - CENTER on wall
        if len(rows) >= 3:
            pesado_row = rows[2]
            total_w = sum(ep.ancho_mm for ep in pesado_row) + 30 * max(0, len(pesado_row) - 1)
            center_start = sa + (wall_len - total_w) / 2
            center_start = max(sa + 100, center_start)
            row_wc = max(ep.y + ep.fondo_mm for ep in pesado_row) if side == "north" else min(ep.y for ep in pesado_row)
            pos = center_start
            for ep in sorted(pesado_row, key=lambda e: e.x):
                x, y, corners = _calcular_posicion_en_pared(side, row_wc, pos, ep.ancho_mm, ep.fondo_mm)
                ep.x = x
                ep.y = y
                ep.corners = corners
                pos += ep.ancho_mm + 30
            print(f"[REDIST] {side} pesado: {len(pesado_row)} items centered (X~{center_start:.0f})")

    return resultado


def _expandir_equipos(equipos: list) -> list[tuple[str, object]]:
    """Expande equipos por cantidad, anadiendo sufijo #N si cantidad > 1."""
    expandidos = []
    for eq in equipos:
        if eq.cantidad <= 1:
            expandidos.append((eq.modelo, eq))
        else:
            for i in range(eq.cantidad):
                expandidos.append((f"{eq.modelo} #{i+1}", eq))
    return expandidos


def posicionar_con_ia(
    equipos: list,
    espacio: EspacioCocina,
    layout_tipo: str = "L",
    iterativo: bool = True,
    max_iteraciones: int = 5,
    score_objetivo: float = 0.92,
    final_path: str | None = None,
) -> list[EquipoPosicionado]:
    """
    Posicionamiento asistido por IA usando Gemini LLM.

    Si iterativo=True (por defecto), usa el loop de auto-correccion que
    valida el resultado y re-prompta al LLM hasta alcanzar el score objetivo.
    Si final_path se proporciona, usa el FINAL profesional como referencia
    en las validaciones de cada iteracion.

    Si iterativo=False, usa el path single-pass original.
    """
    if iterativo:
        return posicionar_con_ia_iterativo(
            equipos, espacio, layout_tipo, max_iteraciones, score_objetivo,
            final_path=final_path,
        )

    from generador_cocinas import invocar_llm_con_rotacion

    print("\n[POSICIONAR-IA] Solicitando layout a Gemini LLM...")

    equipos_exp = _expandir_equipos(equipos)

    # 0. Extraer habitaciones del DXF
    habitaciones: list[HabitacionDetectada] = []
    if espacio.source_path:
        try:
            habitaciones = _extraer_habitaciones_dxf(
                espacio.source_path,
                espacio.unit_scale,
                espacio.dominant_angle,
                espacio.rotation_center,
            )
        except Exception as e:
            print(f"[POSICIONAR-IA] Error extrayendo habitaciones: {e}")

    # 1. Construir prompt (con habitaciones si las hay)
    try:
        messages = _construir_prompt_posicionamiento(
            equipos, espacio, layout_tipo,
            habitaciones=habitaciones if habitaciones else None,
        )
    except Exception as e:
        print(f"[POSICIONAR-IA] Error construyendo prompt: {e}")
        return posicionar_en_espacio(equipos, espacio, layout_tipo)

    # 2. Llamar al LLM
    try:
        layout = invocar_llm_con_rotacion(messages, structured_cls=LayoutLLM)
    except Exception as e:
        print(f"[POSICIONAR-IA] Error LLM: {e}")
        layout = None

    if layout is None:
        print("[POSICIONAR-IA] LLM no respondio, usando fallback algoritmico")
        return posicionar_en_espacio(equipos, espacio, layout_tipo)

    print(f"[POSICIONAR-IA] Estrategia: {layout.estrategia}")
    print(f"[POSICIONAR-IA] Layout: {layout.layout_tipo}")
    for p in layout.posiciones:
        print(f"[POSICIONAR-IA]   {p.modelo} -> {p.habitacion_id}/{p.wall_side} orden={p.orden} gap={p.gap_before_mm}mm | {p.razon}")

    # 3. Validar
    valid, msg = _validar_layout_llm(
        layout, equipos_exp, espacio,
        habitaciones=habitaciones if habitaciones else None,
    )
    if not valid:
        print(f"[POSICIONAR-IA] Validacion fallo: {msg}")
        print("[POSICIONAR-IA] Usando fallback algoritmico")
        return posicionar_en_espacio(equipos, espacio, layout_tipo)

    # 4. Convertir a coordenadas (usando paredes de habitaciones)
    resultado = _convertir_layout_llm_a_posiciones(
        layout, equipos_exp, espacio,
        habitaciones=habitaciones if habitaciones else None,
    )

    if not resultado:
        print("[POSICIONAR-IA] Conversion produjo 0 equipos, usando fallback")
        return posicionar_en_espacio(equipos, espacio, layout_tipo)

    # 5. Clamp + resolver overlaps en espacio axis-aligned (ANTES de de-rotar)
    resultado = _clamp_equipos_a_boundary(resultado, espacio.boundary_rect)
    resultado = _resolver_overlaps(resultado, espacio.boundary_rect, label="IA-pre-derot")

    # 5b. De-rotar al espacio original
    resultado = _de_rotar_resultados(resultado, espacio)

    # 6. Validacion post-posicionamiento
    adv_zonas = _verificar_completitud_zonas(equipos)
    for adv in adv_zonas:
        print(f"[POSICIONAR-IA] AVISO ZONAS: {adv}")
    adv_layout = _validar_layout_final(resultado, espacio, habitaciones)
    for adv in adv_layout:
        print(f"[POSICIONAR-IA] AVISO LAYOUT: {adv}")

    print(f"\n[POSICIONAR-IA] {len(resultado)} equipos posicionados por IA:")
    for ep in resultado:
        print(f"[POSICIONAR-IA]   {ep.modelo} ({ep.zona}) -> ({ep.x:.0f}, {ep.y:.0f}) rot={ep.rotation:.1f} pared={ep.wall_side}")

    return resultado


# ─── Funcion principal (algoritmico) ───────────────────

def posicionar_en_espacio(
    equipos: list,
    espacio: EspacioCocina,
    layout_tipo: str = "L",
) -> list[EquipoPosicionado]:
    """
    Posiciona equipos dentro del espacio detectado del plano del cliente.

    Usa las coordenadas REALES de los segmentos de pared clasificados
    (en espacio axis-aligned), luego de-rota al espacio original.

    Args:
        equipos: Lista de EquipoResuelto (de generador_cocinas)
        espacio: EspacioCocina devuelto por analizar_plano
        layout_tipo: "lineal", "l", "u", "paralelo"

    Returns:
        Lista de EquipoPosicionado con coordenadas en mm del cliente
    """
    # Reconciliar dimensiones con bloques CAD reales
    reporte_bloques: list[dict] = []
    _reconciliar_dimensiones_bloques(equipos, reporte_bloques)
    if reporte_bloques:
        print(f"[POSICIONAR] {len(reporte_bloques)} equipos ajustados a dimensiones de bloque CAD")

    layout_key = layout_tipo.lower().strip()
    if layout_key not in WALL_ASSIGNMENT:
        layout_key = "l"

    assignments = WALL_ASSIGNMENT[layout_key]

    # Agrupar equipos por zona
    equipos_por_zona: dict[str, list] = {
        "coccion": [],
        "frio": [],
        "lavado": [],
        "horno": [],
    }
    for eq in equipos:
        zona = eq.zona if hasattr(eq, "zona") else "coccion"
        if zona in equipos_por_zona:
            equipos_por_zona[zona].append(eq)
        else:
            equipos_por_zona["coccion"].append(eq)

    # Resolver que lado le toca a cada zona
    sides_usados: dict[str, str] = {}
    for zona, assignment in assignments.items():
        sides_usados[zona] = _resolver_lado(assignment, espacio.paredes)

    # Informar si estamos usando zona cocina
    zona_cocina = next((z for z in espacio.zonas if z.nombre == "cocina"), None) if espacio.zonas else None

    # Usar boundary_rect como limites de la cocina (ya filtrado a zona cocina si aplica)
    kitchen_limits: tuple[float, float, float, float] | None = None
    if zona_cocina:
        area_str = f" ({zona_cocina.area_m2:.1f}m²)" if zona_cocina.area_m2 else ""
        print(f"\n[POSICIONAR] Zona cocina detectada: '{zona_cocina.etiqueta}'{area_str}")
        # boundary_rect ya contiene el rectangulo de paredes filtradas por zona
        kitchen_limits = espacio.boundary_rect
        print(f"[POSICIONAR] Kitchen limits (boundary_rect): X=[{kitchen_limits[0]:.0f}, {kitchen_limits[2]:.0f}], Y=[{kitchen_limits[1]:.0f}, {kitchen_limits[3]:.0f}]")
    else:
        print(f"\n[POSICIONAR] Sin etiqueta de cocina, usando edificio completo")

    print(f"[POSICIONAR] Layout: {layout_key}")
    for zona, side in sides_usados.items():
        n = len(equipos_por_zona.get(zona, []))
        if n:
            print(f"[POSICIONAR]   {zona} ({n} equipos) -> pared {side}")

    # Posicionar equipos zona por zona
    resultado: list[EquipoPosicionado] = []

    # Track de cursores por pared (para no sobreponer zonas en la misma pared)
    cursor_por_pared: dict[str, float] = {}  # side -> offset acumulado

    # Multi-fila: tracking de filas por pared (layout "concentrado")
    row_offset_por_pared: dict[str, float] = {}  # side -> offset perpendicular acumulado
    max_depth_current_row: dict[str, float] = {}  # side -> max fondo en fila actual
    # Define constants for margins and gaps
    is_concentrado = layout_key == "concentrado"
    ROW_GAP = 1000.0 if is_concentrado else 200.0  # Professional aisle (~1m) vs basic gap
    if is_concentrado:
        start_margin = 200.0  # Margin from wall edges (prevent visual overflow)
        gap_between = 0.0     # Zero gap between equipment (like FINAL)
    else:
        start_margin = 150.0  # Margen inicial desde la pared
        gap_between = 30.0    # Separacion entre equipos

    # For concentrado: split lavado into support (row 2) and heavy washing (row 3)
    _LAVADO_PESADO_TIPOS = {"lavavajillas", "botellero", "fregadero_industrial"}
    zone_order = ["coccion", "frio", "lavado", "horno"]

    if is_concentrado:
        lavado_all = equipos_por_zona.get("lavado", [])
        lavado_soporte = [eq for eq in lavado_all
                          if (eq.tipo if hasattr(eq, "tipo") else "").lower() not in _LAVADO_PESADO_TIPOS]
        lavado_pesado = [eq for eq in lavado_all
                         if (eq.tipo if hasattr(eq, "tipo") else "").lower() in _LAVADO_PESADO_TIPOS]

        if lavado_soporte and lavado_pesado:
            equipos_por_zona["lavado"] = lavado_soporte
            equipos_por_zona["lavado_pesado"] = lavado_pesado
            sides_usados["lavado_pesado"] = sides_usados["lavado"]
            zone_order = ["coccion", "frio", "lavado", "horno", "lavado_pesado"]
            print(f"[POSICIONAR]   Lavado split: {len(lavado_soporte)} soporte + {len(lavado_pesado)} pesado")

    for zona in zone_order:
        zona_real = "lavado" if zona == "lavado_pesado" else zona
        eqs = equipos_por_zona.get(zona, [])
        if not eqs:
            continue

        side = sides_usados[zona]
        wall_segs = espacio.paredes.get(side, [])
        if not wall_segs:
            print(f"[POSICIONAR] WARN: No hay segmentos para pared {side}, saltando {zona}")
            continue

        # Obtener coordenadas reales de la pared (en espacio axis-aligned)
        wall_coord, start_along, end_along = _obtener_coordenadas_pared(side, wall_segs, kitchen_limits)
        rotation = _rotacion_para_pared(side)

        # Restringir rango de colocacion a los limites de la cocina
        start_along, end_along = _clamp_rango_pared(side, start_along, end_along, kitchen_limits)

        # Multi-fila: si esta pared ya tiene equipos de otra zona, iniciar nueva fila
        if is_concentrado and side in cursor_por_pared and side in max_depth_current_row:
            prev_depth = max_depth_current_row[side]
            current_offset = row_offset_por_pared.get(side, 0.0)
            # Larger aisle before heavy washing row (matches FINAL professional)
            row_gap = 1750.0 if zona == "lavado_pesado" else ROW_GAP
            new_offset = current_offset + prev_depth + row_gap
            row_offset_por_pared[side] = new_offset
            cursor_por_pared[side] = 0.0  # Reset cursor lineal para nueva fila
            max_depth_current_row[side] = 0.0
            print(f"[POSICIONAR]   Nueva fila en pared {side}: row_offset={new_offset:.0f}mm (zona={zona})")

        # Ajustar wall_coord con row_offset (multi-fila)
        row_offset = row_offset_por_pared.get(side, 0.0)
        if row_offset > 0:
            # Mover wall_coord hacia el interior de la cocina
            # north/east: restar (interior es menor coord)
            # south/west: sumar (interior es mayor coord)
            sign = -1.0 if side in ("north", "east") else 1.0
            effective_wall_coord = wall_coord + sign * row_offset
        else:
            effective_wall_coord = wall_coord

        print(f"[POSICIONAR]   Pared {side}: coord={effective_wall_coord:.0f}, rango=[{start_along:.0f}, {end_along:.0f}]"
              + (f" (row_offset={row_offset:.0f})" if row_offset > 0 else ""))

        # Offset acumulado en esta pared (lineal a lo largo)
        offset = cursor_por_pared.get(side, 0.0)
        wall_length = end_along - start_along

        # Zone-specific distribution for concentrado layout
        zone_gap = gap_between
        zone_start = start_margin
        if is_concentrado:
            total_w = sum(eq.ancho_mm * eq.cantidad for eq in eqs)
            n_items = sum(eq.cantidad for eq in eqs)
            if zona == "lavado" and n_items > 1:
                # Spread support items evenly across wall (justify)
                zone_gap = max(30.0, (wall_length - total_w - 2 * start_margin) / (n_items - 1))
            elif zona == "lavado_pesado":
                # Center heavy items on wall
                heavy_total = total_w + 30.0 * max(0, n_items - 1)
                zone_start = max(start_margin, (wall_length - heavy_total) / 2)
                zone_gap = 30.0
            elif zona_real == "frio":
                # Push frio to END of wall (near cooking/north wall, like FINAL)
                frio_total = total_w + 30.0 * max(0, n_items - 1)
                zone_start = max(start_margin, wall_length - frio_total - start_margin)
                zone_gap = 30.0
            elif zona == "coccion":
                zone_gap = 30.0  # Small gap between cooking items

        # Apply initial margin if this is the first item on this wall/row
        if offset == 0.0:
            offset = zone_start

        for eq in eqs:
            for _ in range(eq.cantidad):
                w = eq.ancho_mm
                d = eq.fondo_mm

                # Verificar que el equipo cabe en la pared
                if offset + w > wall_length + 100:  # 100mm tolerancia
                    # Estrategia 1: overflow a otra pared
                    overflow_side = _buscar_pared_overflow(
                        side, espacio.paredes, cursor_por_pared, w,
                        kitchen_limits, start_margin,
                    )
                    if overflow_side and overflow_side != side:
                        print(f"[POSICIONAR] Overflow: {eq.modelo} no cabe en {side} (offset={offset:.0f}, len={wall_length:.0f}) -> {overflow_side}")
                        wall_segs_of = espacio.paredes.get(overflow_side, [])
                        wc_of, sa_of, ea_of = _obtener_coordenadas_pared(overflow_side, wall_segs_of, kitchen_limits)
                        sa_of, ea_of = _clamp_rango_pared(overflow_side, sa_of, ea_of, kitchen_limits)
                        of_offset = cursor_por_pared.get(overflow_side, start_margin)
                        rot_of = _rotacion_para_pared(overflow_side)
                        pos_along_of = sa_of + of_offset
                        x, y, corners = _calcular_posicion_en_pared(overflow_side, wc_of, pos_along_of, w, d)
                        resultado.append(EquipoPosicionado(
                            modelo=eq.modelo, tipo=eq.tipo,
                            ancho_mm=eq.ancho_mm, fondo_mm=eq.fondo_mm,
                            alto_mm=eq.alto_mm, pvp_eur=eq.pvp_eur,
                            serie=eq.serie, cantidad=1, zona=zona_real,
                            x=x, y=y, rotation=rot_of, corners=corners,
                            wall_side=overflow_side,
                        ))
                        cursor_por_pared[overflow_side] = of_offset + w + gap_between
                        continue

                    # Estrategia 2: nueva fila en misma pared
                    prev_depth = max_depth_current_row.get(side, d)
                    new_row_off = row_offset_por_pared.get(side, 0.0) + prev_depth + ROW_GAP
                    row_offset_por_pared[side] = new_row_off
                    max_depth_current_row[side] = 0.0
                    offset = start_margin
                    sign = -1.0 if side in ("north", "east") else 1.0
                    effective_wall_coord = wall_coord + sign * new_row_off
                    print(f"[POSICIONAR]   Auto-fila en {side}: row_offset={new_row_off:.0f}mm para {eq.modelo}")

                # Posicion a lo largo de la pared
                pos_along = start_along + offset

                # Calcular posicion y esquinas segun el lado (usando effective_wall_coord)
                x, y, corners = _calcular_posicion_en_pared(side, effective_wall_coord, pos_along, w, d)

                resultado.append(EquipoPosicionado(
                    modelo=eq.modelo,
                    tipo=eq.tipo,
                    ancho_mm=eq.ancho_mm,
                    fondo_mm=eq.fondo_mm,
                    alto_mm=eq.alto_mm,
                    pvp_eur=eq.pvp_eur,
                    serie=eq.serie,
                    cantidad=1,
                    zona=zona_real,
                    x=x,
                    y=y,
                    rotation=rotation,
                    corners=corners,
                    wall_side=side,
                ))

                offset += w + zone_gap
                # Track max depth for multi-row
                max_depth_current_row[side] = max(max_depth_current_row.get(side, 0.0), d)

        cursor_por_pared[side] = offset

    # Verificar pasillos antes de de-rotar (en espacio axis-aligned)
    _verificar_y_ajustar_pasillos(resultado, espacio)

    # Clamp + resolver overlaps en espacio axis-aligned (ANTES de de-rotar)
    resultado = _clamp_equipos_a_boundary(resultado, espacio.boundary_rect)
    resultado = _resolver_overlaps(resultado, espacio.boundary_rect, label="pre-derot")

    # De-rotar posiciones al espacio original si el edificio esta rotado
    resultado = _de_rotar_resultados(resultado, espacio)

    # Validacion post-posicionamiento
    adv_zonas = _verificar_completitud_zonas(equipos)
    for adv in adv_zonas:
        print(f"[POSICIONAR] AVISO ZONAS: {adv}")
    adv_layout = _validar_layout_final(resultado, espacio)
    for adv in adv_layout:
        print(f"[POSICIONAR] AVISO LAYOUT: {adv}")

    # Resumen
    print(f"\n[POSICIONAR] {len(resultado)} equipos posicionados:")
    for ep in resultado:
        print(f"[POSICIONAR]   {ep.modelo} ({ep.zona}) -> ({ep.x:.0f}, {ep.y:.0f}) rot={ep.rotation:.1f} pared={ep.wall_side}")

    return resultado
