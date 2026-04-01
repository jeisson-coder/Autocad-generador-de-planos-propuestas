"""
analizar_plano.py — Análisis de planos DXF de clientes.

Lee un DXF (plano del local), detecta unidades, extrae paredes,
y devuelve el espacio disponible para posicionar equipos.
"""

import math
import re
from dataclasses import dataclass, field
from collections import defaultdict

import ezdxf


# ─── Modelos de datos ────────────────────────────────────

@dataclass
class SegmentoPared:
    """Segmento de pared en milímetros."""
    start: tuple[float, float]
    end: tuple[float, float]
    length: float
    angle: float  # grados: 0=horizontal→, 90=vertical↑, 180=←, 270=↓


@dataclass
class ZonaFuncional:
    """Zona funcional detectada en el plano (cocina, lavado, frio, etc.)."""
    nombre: str          # Tipo: "cocina", "lavado", "frio", "preparacion", "almacen"
    etiqueta: str        # Texto original del plano
    centro: tuple[float, float]  # Coordenadas en mm (espacio original, antes de rotar)
    area_m2: float | None = None


@dataclass
class EspacioCocina:
    """Resultado del análisis del plano del cliente."""
    boundary_rect: tuple[float, float, float, float]  # (min_x, min_y, max_x, max_y) mm
    paredes: dict[str, list[SegmentoPared]]  # {"north": [...], "south": [...], "east": [...], "west": [...]}
    width_mm: float
    depth_mm: float
    unit_scale: float
    source_path: str
    confidence: str  # "high", "medium", "fallback"
    dominant_angle: float = 0.0  # Rotacion del edificio en grados (0-90)
    rotation_center: tuple[float, float] = (0.0, 0.0)  # Centro para de-rotacion
    zonas: list[ZonaFuncional] = field(default_factory=list)  # Zonas funcionales detectadas


# ─── Constantes ──────────────────────────────────────────

MIN_WALL_LENGTH_MM = 500       # Segmentos más cortos no son paredes útiles
ANGLE_TOLERANCE_DEG = 15       # Tolerancia para snap a cardinal (0/90/180/270) - cubre edificios rotados
ENDPOINT_TOLERANCE_MM = 100    # Tolerancia para conectar endpoints de paredes
DOOR_GAP_MIN_MM = 600          # Ancho mínimo de puerta
DOOR_GAP_MAX_MM = 1500         # Ancho máximo de puerta

# Capas a excluir (no son paredes)
LAYERS_EXCLUIR = {
    "00_COTAS", "00_CARPINTERIA", "00_SECCION", "00_MOBILIARIO FIJO",
    "ASHADE", "Defpoints",
}


# ─── Detección de unidades ───────────────────────────────

def _extent_modelspace(doc) -> tuple[float, float]:
    """Calcula el span X e Y de TODA la geometria en modelspace (todas las entidades)."""
    xs = []
    ys = []
    msp = doc.modelspace()
    for entity in msp:
        etype = entity.dxftype()
        try:
            if etype == "LINE":
                xs.extend([entity.dxf.start.x, entity.dxf.end.x])
                ys.extend([entity.dxf.start.y, entity.dxf.end.y])
            elif etype == "LWPOLYLINE":
                for pt in entity.get_points(format="xy"):
                    xs.append(pt[0])
                    ys.append(pt[1])
            elif etype == "INSERT":
                xs.append(entity.dxf.insert.x)
                ys.append(entity.dxf.insert.y)
            elif etype in ("CIRCLE", "ARC"):
                xs.append(entity.dxf.center.x)
                ys.append(entity.dxf.center.y)
            elif etype == "POINT":
                xs.append(entity.dxf.location.x)
                ys.append(entity.dxf.location.y)
        except Exception:
            continue
    if not xs:
        return 0.0, 0.0
    # Filtrar outliers: usar percentil 5-95 para ignorar bloques lejanos (artefactos Revit)
    xs.sort()
    ys.sort()
    n = len(xs)
    trim = max(1, n // 20)  # 5%
    return xs[-1-trim] - xs[trim], ys[-1-trim] - ys[trim]


def _max_segment_length(doc) -> float:
    """Calcula la longitud del segmento mas largo (LINE/LWPOLYLINE), ignorando outliers."""
    msp = doc.modelspace()
    max_len = 0.0
    for entity in msp:
        if entity.dxf.layer in LAYERS_EXCLUIR:
            continue
        etype = entity.dxftype()
        try:
            if etype == "LINE":
                dx = entity.dxf.end.x - entity.dxf.start.x
                dy = entity.dxf.end.y - entity.dxf.start.y
                max_len = max(max_len, math.sqrt(dx*dx + dy*dy))
            elif etype == "LWPOLYLINE":
                pts = list(entity.get_points(format="xy"))
                for i in range(len(pts) - 1):
                    dx = pts[i+1][0] - pts[i][0]
                    dy = pts[i+1][1] - pts[i][1]
                    max_len = max(max_len, math.sqrt(dx*dx + dy*dy))
        except Exception:
            continue
    return max_len


def _detectar_escala(doc) -> float:
    """Detecta la escala para convertir a mm basandose en $INSUNITS y heuristica."""
    insunits = doc.header.get("$INSUNITS", 0)

    # Mapeo estandar de INSUNITS
    scale_map = {
        1: 25.4,    # pulgadas -> mm
        2: 304.8,   # pies -> mm
        4: 1000.0,  # metros -> mm
        5: 10.0,    # centimetros -> mm
        6: 1.0,     # milimetros
        13: 1e6,    # kilometros (improbable)
    }

    # Medir segmento mas largo (mejor indicador que extent, ignora outlier INSERTs)
    max_seg = _max_segment_length(doc)

    if insunits in scale_map:
        scale = scale_map[insunits]
        # Validar: si INSUNITS dice mm pero segmentos son diminutos, la geometria no esta en mm
        if scale == 1.0 and 0 < max_seg < 50:
            print(f"[ANALIZAR] INSUNITS={insunits} dice mm pero segmento max={max_seg:.2f} -> realmente metros")
            return 1000.0
        if scale == 1.0 and 50 <= max_seg < 500:
            print(f"[ANALIZAR] INSUNITS={insunits} dice mm pero segmento max={max_seg:.2f} -> realmente cm")
            return 10.0
        return scale

    # Heuristica sin INSUNITS: basada en segmento mas largo
    if max_seg < 1:
        return 1.0
    elif max_seg < 50:
        return 1000.0  # metros
    elif max_seg < 500:
        return 10.0    # centimetros
    return 1.0  # ya en mm


# ─── Rotación y ángulo dominante ─────────────────────────

def _rotar_punto(x: float, y: float, angle_rad: float, cx: float, cy: float) -> tuple[float, float]:
    """Rota un punto (x,y) alrededor de (cx,cy) por angle_rad radianes."""
    dx, dy = x - cx, y - cy
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)
    return (cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a)


def _detectar_angulo_dominante(segmentos: list[SegmentoPared], wall_segmentos: list[SegmentoPared] | None = None) -> float:
    """
    Detecta el angulo de rotacion del edificio respecto a los ejes.
    Devuelve un angulo en [0, 90) grados.
    Si el edificio esta alineado con los ejes, devuelve 0.

    Args:
        segmentos: Todos los segmentos extraidos
        wall_segmentos: Segmentos de capas de pared (A-WALL, muro, etc.) si disponibles
    """
    if not segmentos:
        return 0.0

    # Preferir segmentos de capas de pared si disponibles
    base = wall_segmentos if wall_segmentos and len(wall_segmentos) >= 4 else segmentos

    # Filtrar a segmentos largos (>2m) — paredes reales, no detalles
    MIN_LONG_SEG = 2000  # mm
    long_segs = [s for s in base if s.length >= MIN_LONG_SEG]
    if not long_segs:
        long_segs = base

    # Normalizar angulos a [0, 90)
    angles_weights = []
    for seg in long_segs:
        a = seg.angle % 180
        if a >= 90:
            a -= 90
        angles_weights.append((a, seg.length))

    if not angles_weights:
        return 0.0

    # Si tenemos segmentos de capas de pared, usar mediana ponderada (mas precisa)
    if wall_segmentos and len(wall_segmentos) >= 4:
        angles_weights.sort(key=lambda x: x[0])
        total_weight = sum(w for _, w in angles_weights)
        half_weight = total_weight / 2
        cumulative = 0.0
        best_angle = angles_weights[0][0]
        for a, w in angles_weights:
            cumulative += w
            if cumulative >= half_weight:
                best_angle = a
                break
    else:
        # Sin capas de pared: histograma + mediana dentro del cluster
        count_bins: dict[int, int] = defaultdict(int)
        for a, _ in angles_weights:
            count_bins[round(a)] += 1
        # Sumar bins adyacentes para suavizar
        smoothed: dict[int, int] = {}
        for b in count_bins:
            smoothed[b] = sum(count_bins.get(b + d, 0) for d in range(-1, 2))
        best_bin = max(smoothed, key=smoothed.get)

        # Mediana ponderada dentro del cluster (±5° del pico)
        cluster = [(a, w) for a, w in angles_weights if abs(a - best_bin) <= 5]
        if not cluster:
            cluster = angles_weights
        cluster.sort(key=lambda x: x[0])
        total_weight = sum(w for _, w in cluster)
        half_weight = total_weight / 2
        cumulative = 0.0
        best_angle = cluster[0][0]
        for a, w in cluster:
            cumulative += w
            if cumulative >= half_weight:
                best_angle = a
                break

    # Si el angulo es > 45°, es mas natural usar 90° - angulo
    # (una rotacion de 87° = una rotacion de 3° con ejes intercambiados)
    if best_angle > 45.0:
        best_angle = 90.0 - best_angle

    # Si casi alineado con ejes, snap a 0
    if best_angle < 3.0 or best_angle > 87.0:
        return 0.0

    return best_angle


def _rotar_segmentos(
    segmentos: list[SegmentoPared],
    angle_deg: float,
    cx: float,
    cy: float,
) -> list[SegmentoPared]:
    """Rota todos los segmentos por -angle_deg grados alrededor de (cx, cy)."""
    if abs(angle_deg) < 0.5:
        return segmentos

    angle_rad = math.radians(-angle_deg)
    rotados = []
    for seg in segmentos:
        s = _rotar_punto(seg.start[0], seg.start[1], angle_rad, cx, cy)
        e = _rotar_punto(seg.end[0], seg.end[1], angle_rad, cx, cy)
        dx = e[0] - s[0]
        dy = e[1] - s[1]
        new_angle = math.degrees(math.atan2(dy, dx)) % 360
        length = math.sqrt(dx * dx + dy * dy)
        rotados.append(SegmentoPared(start=s, end=e, length=length, angle=new_angle))
    return rotados


# ─── Extracción de segmentos ─────────────────────────────

# Keywords para identificar capas de paredes en DXFs arquitectonicos
_WALL_LAYER_KEYWORDS = {"WALL", "PARED", "MURO"}


def _extraer_segmentos(msp, scale: float) -> tuple[list[SegmentoPared], list[SegmentoPared]]:
    """
    Extrae segmentos de LINE y LWPOLYLINE, escalados a mm.

    Returns:
        (todos_segmentos, wall_segmentos)
        wall_segmentos: solo segmentos de capas identificadas como paredes (A-WALL, muro, etc.)
    """
    segmentos = []
    wall_segmentos = []

    for entity in msp:
        layer = entity.dxf.layer
        if layer in LAYERS_EXCLUIR:
            continue

        is_wall_layer = any(kw in layer.upper() for kw in _WALL_LAYER_KEYWORDS)
        new_segs = []

        if entity.dxftype() == "LINE":
            s = (entity.dxf.start.x * scale, entity.dxf.start.y * scale)
            e = (entity.dxf.end.x * scale, entity.dxf.end.y * scale)
            seg = _crear_segmento(s, e)
            if seg:
                new_segs.append(seg)

        elif entity.dxftype() == "LWPOLYLINE":
            points = [(pt[0] * scale, pt[1] * scale) for pt in entity.get_points(format="xy")]
            for i in range(len(points) - 1):
                seg = _crear_segmento(points[i], points[i + 1])
                if seg:
                    new_segs.append(seg)
            if entity.is_closed and len(points) > 2:
                seg = _crear_segmento(points[-1], points[0])
                if seg:
                    new_segs.append(seg)

        segmentos.extend(new_segs)
        if is_wall_layer:
            wall_segmentos.extend(new_segs)

    return segmentos, wall_segmentos


def _crear_segmento(start: tuple[float, float], end: tuple[float, float]) -> SegmentoPared | None:
    """Crea un SegmentoPared si tiene longitud suficiente."""
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.sqrt(dx * dx + dy * dy)
    if length < MIN_WALL_LENGTH_MM:
        return None
    angle = math.degrees(math.atan2(dy, dx)) % 360
    return SegmentoPared(start=start, end=end, length=length, angle=angle)


# ─── Filtrado de paredes ─────────────────────────────────

def _es_cardinal(angle: float) -> bool:
    """Verifica si un ángulo está cerca de 0, 90, 180 o 270 grados."""
    for cardinal in [0, 90, 180, 270, 360]:
        if abs(angle - cardinal) <= ANGLE_TOLERANCE_DEG:
            return True
    return False


def _snap_cardinal(angle: float) -> float:
    """Ajusta el ángulo al cardinal más cercano."""
    cardinals = [0, 90, 180, 270, 360]
    closest = min(cardinals, key=lambda c: abs(angle - c))
    return closest % 360


def _filtrar_paredes(segmentos: list[SegmentoPared]) -> list[SegmentoPared]:
    """Filtra solo segmentos que parecen paredes (largos y cardinales)."""
    paredes = []
    for seg in segmentos:
        if not _es_cardinal(seg.angle):
            continue
        paredes.append(SegmentoPared(
            start=seg.start,
            end=seg.end,
            length=seg.length,
            angle=_snap_cardinal(seg.angle),
        ))
    return paredes


# ─── Detección de contorno ───────────────────────────────

def _bounding_box(paredes: list[SegmentoPared]) -> tuple[float, float, float, float]:
    """Calcula el bounding box de todos los segmentos."""
    xs = []
    ys = []
    for p in paredes:
        xs.extend([p.start[0], p.end[0]])
        ys.extend([p.start[1], p.end[1]])
    return (min(xs), min(ys), max(xs), max(ys))


def _bounding_box_all_entities(msp, scale: float) -> tuple[float, float, float, float]:
    """Calcula el bounding box de TODA la geometria en modelspace, con filtro de outliers."""
    xs = []
    ys = []
    for entity in msp:
        etype = entity.dxftype()
        try:
            if etype == "LINE":
                xs.extend([entity.dxf.start.x * scale, entity.dxf.end.x * scale])
                ys.extend([entity.dxf.start.y * scale, entity.dxf.end.y * scale])
            elif etype == "LWPOLYLINE":
                for pt in entity.get_points(format="xy"):
                    xs.append(pt[0] * scale)
                    ys.append(pt[1] * scale)
            elif etype == "INSERT":
                xs.append(entity.dxf.insert.x * scale)
                ys.append(entity.dxf.insert.y * scale)
            elif etype in ("CIRCLE", "ARC"):
                xs.append(entity.dxf.center.x * scale)
                ys.append(entity.dxf.center.y * scale)
        except Exception:
            continue
    if not xs:
        return (0, 0, 10000, 10000)
    # Filtrar outliers con percentil 2-98%
    xs.sort()
    ys.sort()
    n = len(xs)
    trim = max(1, n // 50)  # 2%
    return (xs[trim], ys[trim], xs[-1-trim], ys[-1-trim])


def _detectar_contorno(paredes: list[SegmentoPared]) -> tuple[tuple[float, float, float, float], str]:
    """
    Detecta el contorno de la cocina.

    Tier 1: Buscar el rectángulo más grande formado por paredes que se cruzan.
    Tier 2: Cluster más denso de paredes.
    Tier 3: Bounding box total (fallback).

    Returns: (boundary_rect, confidence)
    """
    if not paredes:
        return (0, 0, 0, 0), "fallback"

    # Separar paredes horizontales y verticales
    h_walls = [p for p in paredes if p.angle in (0, 180)]
    v_walls = [p for p in paredes if p.angle in (90, 270)]

    # Tier 1: Buscar rectángulo formado por paredes
    if len(h_walls) >= 2 and len(v_walls) >= 2:
        rect, conf = _buscar_rectangulo(h_walls, v_walls)
        if conf != "fallback":
            return rect, conf

    # Tier 2: Cluster denso
    bbox = _bounding_box(paredes)
    # Verificar que hay paredes en al menos 3 de los 4 lados
    min_x, min_y, max_x, max_y = bbox
    has_top = any(abs(p.start[1] - max_y) < ENDPOINT_TOLERANCE_MM or abs(p.end[1] - max_y) < ENDPOINT_TOLERANCE_MM for p in h_walls)
    has_bottom = any(abs(p.start[1] - min_y) < ENDPOINT_TOLERANCE_MM or abs(p.end[1] - min_y) < ENDPOINT_TOLERANCE_MM for p in h_walls)
    has_left = any(abs(p.start[0] - min_x) < ENDPOINT_TOLERANCE_MM or abs(p.end[0] - min_x) < ENDPOINT_TOLERANCE_MM for p in v_walls)
    has_right = any(abs(p.start[0] - max_x) < ENDPOINT_TOLERANCE_MM or abs(p.end[0] - max_x) < ENDPOINT_TOLERANCE_MM for p in v_walls)
    sides = sum([has_top, has_bottom, has_left, has_right])

    if sides >= 3:
        return bbox, "medium"

    # Tier 3: Fallback
    return bbox, "fallback"


def _agrupar_por_coordenada(
    walls: list[SegmentoPared],
    axis: str,  # "y" o "x"
    tol: float,
) -> dict[float, list[SegmentoPared]]:
    """Agrupa paredes por coordenada (Y para H, X para V) con tolerancia."""
    groups: dict[float, list[SegmentoPared]] = defaultdict(list)
    for w in walls:
        if axis == "y":
            val = (w.start[1] + w.end[1]) / 2
        else:
            val = (w.start[0] + w.end[0]) / 2
        found = False
        for key in list(groups.keys()):
            if abs(val - key) < tol:
                groups[key].append(w)
                found = True
                break
        if not found:
            groups[val].append(w)
    return groups


def _buscar_rectangulo(
    h_walls: list[SegmentoPared],
    v_walls: list[SegmentoPared],
) -> tuple[tuple[float, float, float, float], str]:
    """
    Busca el rectángulo más grande formado por pares de paredes H y V.

    Optimizado: solo considera los N grupos con mayor longitud total
    para evitar O(n^4) con planos muy complejos.
    """
    tol = ENDPOINT_TOLERANCE_MM
    MAX_GROUPS = 20  # Limitar a los 20 grupos mas significativos

    h_by_y = _agrupar_por_coordenada(h_walls, "y", tol)
    v_by_x = _agrupar_por_coordenada(v_walls, "x", tol)

    # Ordenar por longitud total y quedarse con los mas significativos
    h_ys_sorted = sorted(h_by_y.keys(), key=lambda k: sum(w.length for w in h_by_y[k]), reverse=True)
    v_xs_sorted = sorted(v_by_x.keys(), key=lambda k: sum(w.length for w in v_by_x[k]), reverse=True)

    h_ys = sorted(h_ys_sorted[:MAX_GROUPS])
    v_xs = sorted(v_xs_sorted[:MAX_GROUPS])

    best_area = 0
    best_rect = None

    for i, y_bot in enumerate(h_ys):
        for y_top in h_ys[i + 1:]:
            height = y_top - y_bot
            if height < MIN_WALL_LENGTH_MM:
                continue

            for j, x_left in enumerate(v_xs):
                for x_right in v_xs[j + 1:]:
                    width = x_right - x_left
                    if width < MIN_WALL_LENGTH_MM:
                        continue

                    area = width * height
                    if area <= best_area:
                        continue

                    h_bot_len = sum(w.length for w in h_by_y[y_bot]
                                    if _segmento_en_rango_x(w, x_left, x_right, tol))
                    h_top_len = sum(w.length for w in h_by_y[y_top]
                                    if _segmento_en_rango_x(w, x_left, x_right, tol))
                    v_left_len = sum(w.length for w in v_by_x[x_left]
                                     if _segmento_en_rango_y(w, y_bot, y_top, tol))
                    v_right_len = sum(w.length for w in v_by_x[x_right]
                                      if _segmento_en_rango_y(w, y_bot, y_top, tol))

                    coverage = min(
                        h_bot_len / max(width, 1),
                        h_top_len / max(width, 1),
                        v_left_len / max(height, 1),
                        v_right_len / max(height, 1),
                    )

                    if coverage > 0.3:
                        best_area = area
                        best_rect = (x_left, y_bot, x_right, y_top)

    if best_rect:
        return best_rect, "high"
    return (0, 0, 0, 0), "fallback"


def _segmento_en_rango_x(seg: SegmentoPared, x_min: float, x_max: float, tol: float) -> bool:
    """Verifica si un segmento horizontal cae dentro del rango X."""
    seg_x_min = min(seg.start[0], seg.end[0])
    seg_x_max = max(seg.start[0], seg.end[0])
    return seg_x_min < x_max + tol and seg_x_max > x_min - tol


def _segmento_en_rango_y(seg: SegmentoPared, y_min: float, y_max: float, tol: float) -> bool:
    """Verifica si un segmento vertical cae dentro del rango Y."""
    seg_y_min = min(seg.start[1], seg.end[1])
    seg_y_max = max(seg.start[1], seg.end[1])
    return seg_y_min < y_max + tol and seg_y_max > y_min - tol


def _buscar_rectangulo_cerca(
    paredes: list[SegmentoPared],
    centro: tuple[float, float],
    max_dim: float = 50000,
) -> tuple[float, float, float, float] | None:
    """
    Finds the best rectangle near a target center point with size constraints.

    Unlike _buscar_rectangulo which finds the LARGEST rectangle,
    this finds the rectangle closest to the center with dimensions < max_dim.
    """
    h_walls = [p for p in paredes if p.angle in (0, 180)]
    v_walls = [p for p in paredes if p.angle in (90, 270)]
    if len(h_walls) < 2 or len(v_walls) < 2:
        return None

    tol = ENDPOINT_TOLERANCE_MM
    h_by_y = _agrupar_por_coordenada(h_walls, "y", tol)
    v_by_x = _agrupar_por_coordenada(v_walls, "x", tol)

    # Only consider wall groups near the center
    cx, cy = centro
    h_ys = sorted(k for k in h_by_y if abs(k - cy) < max_dim)
    v_xs = sorted(k for k in v_by_x if abs(k - cx) < max_dim)

    if len(h_ys) < 2 or len(v_xs) < 2:
        return None

    # Limit to top-N most populated wall groups to keep O(n^4) manageable
    MAX_GROUPS = 25
    if len(h_ys) > MAX_GROUPS:
        h_ys = sorted(h_ys, key=lambda y: -len(h_by_y[y]))[:MAX_GROUPS]
        h_ys.sort()
    if len(v_xs) > MAX_GROUPS:
        v_xs = sorted(v_xs, key=lambda x: -len(v_by_x[x]))[:MAX_GROUPS]
        v_xs.sort()

    best_score = float("inf")
    best_rect = None
    MIN_DIM = 4000  # 4m minimum kitchen dimension

    for i, y_bot in enumerate(h_ys):
        for y_top in h_ys[i + 1:]:
            height = y_top - y_bot
            if height < MIN_DIM or height > max_dim:
                continue
            for j, x_left in enumerate(v_xs):
                for x_right in v_xs[j + 1:]:
                    width = x_right - x_left
                    if width < MIN_DIM or width > max_dim:
                        continue

                    # Check wall coverage
                    h_bot_len = sum(w.length for w in h_by_y[y_bot]
                                    if _segmento_en_rango_x(w, x_left, x_right, tol))
                    h_top_len = sum(w.length for w in h_by_y[y_top]
                                    if _segmento_en_rango_x(w, x_left, x_right, tol))
                    v_left_len = sum(w.length for w in v_by_x[x_left]
                                     if _segmento_en_rango_y(w, y_bot, y_top, tol))
                    v_right_len = sum(w.length for w in v_by_x[x_right]
                                      if _segmento_en_rango_y(w, y_bot, y_top, tol))

                    coverage = min(
                        h_bot_len / max(width, 1),
                        h_top_len / max(width, 1),
                        v_left_len / max(height, 1),
                        v_right_len / max(height, 1),
                    )
                    if coverage < 0.3:
                        continue

                    # Score: prefer rectangles whose center is close to the target
                    rect_cx = (x_left + x_right) / 2
                    rect_cy = (y_bot + y_top) / 2
                    dist = math.sqrt((rect_cx - cx) ** 2 + (rect_cy - cy) ** 2)
                    # Bonus for larger area (prefer bigger kitchens)
                    area_bonus = width * height / (max_dim * max_dim)
                    score = dist - area_bonus * 5000

                    if score < best_score:
                        best_score = score
                        best_rect = (x_left, y_bot, x_right, y_top)

    return best_rect


# ─── Clasificación de paredes ────────────────────────────

def _clasificar_paredes(
    paredes: list[SegmentoPared],
    boundary: tuple[float, float, float, float],
) -> dict[str, list[SegmentoPared]]:
    """
    Clasifica paredes como north/south/east/west según su posición
    relativa al contorno detectado.
    """
    min_x, min_y, max_x, max_y = boundary
    tol = ENDPOINT_TOLERANCE_MM
    cx = (min_x + max_x) / 2
    cy = (min_y + max_y) / 2

    result: dict[str, list[SegmentoPared]] = {
        "north": [], "south": [], "east": [], "west": [],
    }

    for p in paredes:
        px = (p.start[0] + p.end[0]) / 2
        py = (p.start[1] + p.end[1]) / 2

        # Verificar que el segmento está dentro o sobre el contorno
        if not _punto_cerca_de_rect(px, py, boundary, tol * 3):
            continue

        if p.angle in (0, 180):  # Horizontal
            if abs(py - max_y) < tol:
                result["north"].append(p)
            elif abs(py - min_y) < tol:
                result["south"].append(p)
            elif py > cy:
                result["north"].append(p)
            else:
                result["south"].append(p)
        elif p.angle in (90, 270):  # Vertical
            if abs(px - max_x) < tol:
                result["east"].append(p)
            elif abs(px - min_x) < tol:
                result["west"].append(p)
            elif px > cx:
                result["east"].append(p)
            else:
                result["west"].append(p)

    # Ordenar cada grupo por posición a lo largo de la pared
    for side in ["north", "south"]:
        result[side].sort(key=lambda s: min(s.start[0], s.end[0]))
    for side in ["east", "west"]:
        result[side].sort(key=lambda s: min(s.start[1], s.end[1]))

    return result


def _punto_cerca_de_rect(
    x: float, y: float,
    rect: tuple[float, float, float, float],
    tol: float,
) -> bool:
    """Verifica si un punto está dentro o cerca de un rectángulo."""
    min_x, min_y, max_x, max_y = rect
    return (min_x - tol <= x <= max_x + tol and
            min_y - tol <= y <= max_y + tol)


# ─── Detección de zonas funcionales ──────────────────────

# Keywords para identificar zonas funcionales en etiquetas TEXT/MTEXT
_ZONA_KEYWORDS: dict[str, str] = {
    # Cocina / servicio
    "cocina": "cocina",
    "kitchen": "cocina",
    "servicio": "cocina",
    "barra": "barra",
    # Lavado
    "plonge": "lavado",
    "lavado": "lavado",
    # Frio
    "frio": "frio",
    "frío": "frio",
    "camara": "frio",
    "cámara": "frio",
    "congelad": "frio",
    # Preparacion / almacen
    "preparaci": "preparacion",
    "almacen": "almacen",
    "almacén": "almacen",
    # Sanitarios (separacion higienica)
    "aseo": "aseo",
    "bano": "aseo",
    "baño": "aseo",
    "wc": "aseo",
    "vestuario": "aseo",
    # Accesos
    "entrada": "acceso",
    "acceso": "acceso",
    "recepcion": "acceso",
    "recepción": "acceso",
    # Residuos
    "basura": "residuos",
    "residuos": "residuos",
    # Zonas de cliente (NO colocar equipos de cocina)
    "comedor": "comedor",
    "restaurante": "comedor",
    "salon": "comedor",
    "salón": "comedor",
    "terraza": "comedor",
    # Administracion
    "office": "office",
    "despacho": "office",
    "oficina": "office",
}


def _extraer_zonas(msp, scale: float) -> list[ZonaFuncional]:
    """
    Extrae zonas funcionales de etiquetas TEXT/MTEXT en el plano.

    Busca en capas como 'A-AREA-IDEN' y cualquier otra que contenga
    etiquetas de habitaciones con keywords conocidos.
    """
    # Paso 1: Recopilar todos los textos con posiciones
    all_texts: list[tuple[str, float, float]] = []
    for entity in msp:
        etype = entity.dxftype()
        if etype not in ("MTEXT", "TEXT"):
            continue
        try:
            if etype == "MTEXT":
                text = entity.text
            else:
                text = entity.dxf.text
            x = entity.dxf.insert.x * scale
            y = entity.dxf.insert.y * scale
        except Exception:
            continue
        text_clean = text.strip()
        if text_clean:
            all_texts.append((text_clean, x, y))

    # Paso 2: Buscar zonas funcionales
    zonas = []
    seen: set[tuple[str, int, int]] = set()

    for text, x, y in all_texts:
        text_lower = text.lower()

        for keyword, zona_tipo in _ZONA_KEYWORDS.items():
            if keyword in text_lower:
                # Buscar area en textos cercanos (dentro de 500mm)
                area = None
                area_match = re.search(r'(\d+[.,]\d+)\s*m', text_lower)
                if area_match:
                    area = float(area_match.group(1).replace(',', '.'))
                else:
                    # Buscar en textos vecinos cercanos
                    for t2, x2, y2 in all_texts:
                        dist = math.sqrt((x2 - x) ** 2 + (y2 - y) ** 2)
                        if 0 < dist < 500 * scale / 1000:  # 500mm en unidades DXF
                            m = re.search(r'(\d+[.,]\d+)\s*m', t2.lower())
                            if m:
                                area = float(m.group(1).replace(',', '.'))
                                break

                key = (zona_tipo, round(x / 2000), round(y / 2000))
                if key not in seen:
                    seen.add(key)
                    zonas.append(ZonaFuncional(
                        nombre=zona_tipo,
                        etiqueta=text.strip(),
                        centro=(x, y),
                        area_m2=area,
                    ))
                break

    return zonas


def _filtrar_paredes_zona(
    paredes: dict[str, list[SegmentoPared]],
    zona_centro: tuple[float, float],
    radio_mm: float,
) -> dict[str, list[SegmentoPared]]:
    """
    Filtra paredes para quedarse solo con las que estan cerca de una zona.

    Args:
        paredes: Paredes clasificadas por lado (en espacio axis-aligned)
        zona_centro: Centro de la zona (en espacio axis-aligned)
        radio_mm: Radio de busqueda en mm
    """
    resultado: dict[str, list[SegmentoPared]] = {}
    zx, zy = zona_centro

    for side, segs in paredes.items():
        cerca = []
        for s in segs:
            mx = (s.start[0] + s.end[0]) / 2
            my = (s.start[1] + s.end[1]) / 2
            dist = math.sqrt((mx - zx) ** 2 + (my - zy) ** 2)
            if dist < radio_mm:
                cerca.append(s)
        resultado[side] = cerca

    return resultado


def _filtrar_outlier_segmentos(paredes: list[SegmentoPared]) -> list[SegmentoPared]:
    """
    Removes outlier wall segments using IQR-based filtering.
    Falls back to grid-based clustering if IQR is ineffective.
    """
    if len(paredes) < 10:
        return paredes

    mids_x = sorted((p.start[0] + p.end[0]) / 2 for p in paredes)
    mids_y = sorted((p.start[1] + p.end[1]) / 2 for p in paredes)

    n = len(mids_x)
    q1_x, q3_x = mids_x[n // 4], mids_x[3 * n // 4]
    q1_y, q3_y = mids_y[n // 4], mids_y[3 * n // 4]
    iqr_x = q3_x - q1_x
    iqr_y = q3_y - q1_y

    low_x = q1_x - 1.5 * iqr_x
    high_x = q3_x + 1.5 * iqr_x
    low_y = q1_y - 1.5 * iqr_y
    high_y = q3_y + 1.5 * iqr_y

    filtered = [p for p in paredes
                if low_x <= (p.start[0] + p.end[0]) / 2 <= high_x
                and low_y <= (p.start[1] + p.end[1]) / 2 <= high_y]

    print(f"[ANALIZAR] Outlier filter: {len(paredes)} -> {len(filtered)} segments "
          f"(X=[{low_x:.0f},{high_x:.0f}] Y=[{low_y:.0f},{high_y:.0f}])")
    return filtered


def _cluster_segmentos_grid(
    paredes: list[SegmentoPared],
    cell_size: float = 15000,  # 15m grid cells
    target_center: tuple[float, float] | None = None,
) -> list[SegmentoPared] | None:
    """
    Grid-based density clustering: finds the densest connected cluster of segments.

    Divides the space into cells, finds connected clusters via flood-fill,
    and picks the best one (nearest to target_center if given, else densest).
    """
    if len(paredes) < 10:
        return None

    # Compute midpoints
    mids = [((p.start[0] + p.end[0]) / 2, (p.start[1] + p.end[1]) / 2) for p in paredes]
    min_x = min(m[0] for m in mids)
    min_y = min(m[1] for m in mids)
    span_x = max(m[0] for m in mids) - min_x
    span_y = max(m[1] for m in mids) - min_y

    # Only cluster if the span is large (>200m in any direction)
    max_span = max(span_x, span_y)
    if max_span < 200000:
        return None

    # Adaptive cell size: larger for GIS-scale files
    if max_span > 10_000_000:     # >10km: likely GIS with far-apart copies
        cell_size = 50000         # 50m cells
    elif max_span > 1_000_000:    # >1km
        cell_size = 30000         # 30m cells

    # Assign each segment to a grid cell
    cell_map: dict[tuple[int, int], list[int]] = {}
    for i, (mx, my) in enumerate(mids):
        cx = int((mx - min_x) / cell_size)
        cy = int((my - min_y) / cell_size)
        cell_map.setdefault((cx, cy), []).append(i)

    if not cell_map:
        return None

    # Find ALL connected clusters via flood-fill
    all_cells = set(cell_map.keys())
    visited: set[tuple[int, int]] = set()
    clusters: list[list[int]] = []

    for start_cell in all_cells:
        if start_cell in visited:
            continue
        # Flood-fill this cluster
        queue = [start_cell]
        cluster_indices: list[int] = []
        while queue:
            cell = queue.pop(0)
            if cell in visited:
                continue
            visited.add(cell)
            if cell not in cell_map:
                continue
            cluster_indices.extend(cell_map[cell])
            ccx, ccy = cell
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    neighbor = (ccx + dx, ccy + dy)
                    if neighbor not in visited and neighbor in cell_map:
                        queue.append(neighbor)
        if len(cluster_indices) >= 4:
            clusters.append(cluster_indices)

    if not clusters:
        return None

    # Pick best cluster
    best_cluster = None
    if target_center and len(clusters) > 1:
        # Prefer cluster whose centroid is closest to target_center
        tcx, tcy = target_center
        best_dist = float("inf")
        for cl in clusters:
            cl_cx = sum(mids[i][0] for i in cl) / len(cl)
            cl_cy = sum(mids[i][1] for i in cl) / len(cl)
            dist = math.sqrt((cl_cx - tcx) ** 2 + (cl_cy - tcy) ** 2)
            if dist < best_dist:
                best_dist = dist
                best_cluster = cl
    else:
        # Pick the largest cluster
        best_cluster = max(clusters, key=len)

    if not best_cluster:
        return None

    result = [paredes[i] for i in best_cluster]
    if len(result) < len(paredes):
        cls_mids = [mids[i] for i in best_cluster]
        cl_x1 = min(m[0] for m in cls_mids)
        cl_x2 = max(m[0] for m in cls_mids)
        cl_y1 = min(m[1] for m in cls_mids)
        cl_y2 = max(m[1] for m in cls_mids)
        print(f"[ANALIZAR] Grid cluster: {len(paredes)} -> {len(result)} segments, "
              f"{len(clusters)} clusters found "
              f"(selected: {(cl_x2-cl_x1)/1000:.0f}m x {(cl_y2-cl_y1)/1000:.0f}m)")
    return result


# ─── Función principal ───────────────────────────────────

def analizar_plano_cliente(dxf_path: str) -> EspacioCocina:
    """
    Analiza un DXF de plano de cliente y devuelve el espacio de cocina detectado.

    Args:
        dxf_path: Ruta al archivo DXF del cliente (INICIAL)

    Returns:
        EspacioCocina con contorno, paredes clasificadas y dimensiones
    """
    print(f"\n[ANALIZAR] Leyendo plano: {dxf_path}")
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    # 1. Detectar escala
    scale = _detectar_escala(doc)
    _scale_label = lambda s: 'metros->mm' if s == 1000 else 'cm->mm' if s == 10 else 'mm'
    print(f"[ANALIZAR] Escala detectada: x{scale:.0f} ({_scale_label(scale)})")

    # 2. Extraer segmentos
    segmentos, wall_segmentos = _extraer_segmentos(msp, scale)
    print(f"[ANALIZAR] Segmentos extraidos: {len(segmentos)} (>{MIN_WALL_LENGTH_MM}mm)")
    if wall_segmentos:
        print(f"[ANALIZAR] Segmentos de capas de pared: {len(wall_segmentos)}")

    # 2b. Detectar angulo dominante del edificio
    dominant_angle = _detectar_angulo_dominante(segmentos, wall_segmentos if wall_segmentos else None)
    print(f"[ANALIZAR] Angulo dominante: {dominant_angle:.1f}°")

    # 2c. Calcular centro de rotacion (centroide de segmentos)
    if segmentos:
        cx = sum((s.start[0] + s.end[0]) / 2 for s in segmentos) / len(segmentos)
        cy = sum((s.start[1] + s.end[1]) / 2 for s in segmentos) / len(segmentos)
    else:
        cx, cy = 0.0, 0.0
    rotation_center = (cx, cy)

    # 2d. Rotar segmentos para alinear con ejes (si el edificio esta rotado)
    if abs(dominant_angle) > 0.5:
        segmentos = _rotar_segmentos(segmentos, dominant_angle, cx, cy)
        print(f"[ANALIZAR] Segmentos rotados {dominant_angle:.1f}° para alinear con ejes")

    # 3. Filtrar paredes (cardinales — ahora si porque los segmentos estan alineados)
    paredes = _filtrar_paredes(segmentos)
    print(f"[ANALIZAR] Paredes cardinales: {len(paredes)}")

    # 3b. Retry con escalas alternativas si no hay paredes
    if not paredes:
        for alt_scale in [1000.0, 100.0, 10.0, 1.0]:
            if alt_scale == scale:
                continue
            alt_seg, _ = _extraer_segmentos(msp, alt_scale)
            if abs(dominant_angle) > 0.5:
                alt_seg = _rotar_segmentos(alt_seg, dominant_angle, cx, cy)
            alt_par = _filtrar_paredes(alt_seg)
            if len(alt_par) >= 4:
                scale = alt_scale
                segmentos = alt_seg
                paredes = alt_par
                print(f"[ANALIZAR] Retry con escala x{scale:.0f} ({_scale_label(scale)}): {len(paredes)} paredes encontradas")
                break

    if not paredes:
        print("[ANALIZAR] WARN: No se detectaron paredes, usando bbox de toda la geometria")
        bbox = _bounding_box_all_entities(msp, scale)
        width = bbox[2] - bbox[0]
        depth = bbox[3] - bbox[1]
        print(f"[ANALIZAR] BBox total: {width:.0f}mm x {depth:.0f}mm")
        paredes_synth = {
            "north": [SegmentoPared((bbox[0], bbox[3]), (bbox[2], bbox[3]), width, 0)],
            "south": [SegmentoPared((bbox[0], bbox[1]), (bbox[2], bbox[1]), width, 0)],
            "east":  [SegmentoPared((bbox[2], bbox[1]), (bbox[2], bbox[3]), depth, 90)],
            "west":  [SegmentoPared((bbox[0], bbox[1]), (bbox[0], bbox[3]), depth, 90)],
        }
        return EspacioCocina(
            boundary_rect=bbox,
            paredes=paredes_synth,
            width_mm=width,
            depth_mm=depth,
            unit_scale=scale,
            source_path=dxf_path,
            confidence="fallback",
            dominant_angle=0.0,
            rotation_center=((bbox[0]+bbox[2])/2, (bbox[1]+bbox[3])/2),
        )

    # 4. Detectar contorno (en espacio alineado con ejes)
    boundary, confidence = _detectar_contorno(paredes)

    if confidence == "fallback":
        boundary = _bounding_box_all_entities(msp, scale)

    min_x, min_y, max_x, max_y = boundary
    width = max_x - min_x
    depth = max_y - min_y

    # 4b. If boundary is very large, try to reduce it
    GIS_THRESHOLD = 500000     # 500m — likely GIS/UTM coordinates with multiple plan copies
    LARGE_THRESHOLD = 60000    # 60m — large but not GIS
    if width > LARGE_THRESHOLD or depth > LARGE_THRESHOLD:
        print(f"[ANALIZAR] Boundary too large ({width:.0f}mm x {depth:.0f}mm), applying filters...")

        # For GIS-scale files (>500m): use grid clustering to isolate one building copy
        if width > GIS_THRESHOLD or depth > GIS_THRESHOLD:
            # Quick scan for COCINA label to guide clustering
            cocina_center = None
            zonas_early = _extraer_zonas(msp, scale)
            for z in zonas_early:
                if z.nombre == "cocina":
                    if abs(dominant_angle) > 0.5:
                        cocina_center = _rotar_punto(
                            z.centro[0], z.centro[1],
                            math.radians(-dominant_angle), cx, cy,
                        )
                    else:
                        cocina_center = z.centro
                    break

            # Try with target center first, fallback to largest cluster
            clustered = _cluster_segmentos_grid(paredes, target_center=cocina_center)
            # If targeted cluster is too small (<20 segs), try without target
            if (not clustered or len(clustered) < 20) and cocina_center:
                clustered_alt = _cluster_segmentos_grid(paredes, target_center=None)
                if clustered_alt and len(clustered_alt) > (len(clustered) if clustered else 0):
                    clustered = clustered_alt

            if clustered and len(clustered) >= 4:
                boundary_c, confidence_c = _detectar_contorno(clustered)
                if confidence_c != "fallback":
                    wc = boundary_c[2] - boundary_c[0]
                    dc = boundary_c[3] - boundary_c[1]
                    if wc < width * 0.8 or dc < depth * 0.8:
                        boundary = boundary_c
                        confidence = confidence_c
                        paredes = clustered
                        min_x, min_y, max_x, max_y = boundary
                        width = max_x - min_x
                        depth = max_y - min_y
                        print(f"[ANALIZAR] Grid cluster: {width:.0f}mm x {depth:.0f}mm (conf={confidence})")

        # IQR outlier filter (for both GIS leftovers and large non-GIS)
        if width > LARGE_THRESHOLD or depth > LARGE_THRESHOLD:
            filtered_paredes = _filtrar_outlier_segmentos(paredes)
            if len(filtered_paredes) >= 4:
                boundary2, confidence2 = _detectar_contorno(filtered_paredes)
                if confidence2 != "fallback":
                    w2 = boundary2[2] - boundary2[0]
                    d2 = boundary2[3] - boundary2[1]
                    if w2 < width * 0.8 and d2 < depth * 0.8:
                        boundary = boundary2
                        confidence = confidence2
                        paredes = filtered_paredes
                        min_x, min_y, max_x, max_y = boundary
                        width = max_x - min_x
                        depth = max_y - min_y
                        print(f"[ANALIZAR] Outlier filter: {width:.0f}mm x {depth:.0f}mm (conf={confidence})")

    print(f"[ANALIZAR] Contorno: {width:.0f}mm x {depth:.0f}mm (confianza: {confidence})")

    # 5. Clasificar paredes (en espacio alineado con ejes)
    paredes_clasificadas = _clasificar_paredes(paredes, boundary)
    for side, walls in paredes_clasificadas.items():
        total_len = sum(w.length for w in walls)
        print(f"[ANALIZAR]   {side}: {len(walls)} segmentos, {total_len:.0f}mm total")

    # 6. Extraer zonas funcionales de etiquetas TEXT/MTEXT
    zonas = _extraer_zonas(msp, scale)
    if zonas:
        print(f"[ANALIZAR] Zonas funcionales detectadas: {len(zonas)}")
        for z in zonas:
            area_str = f" {z.area_m2:.1f}m²" if z.area_m2 else ""
            print(f"[ANALIZAR]   {z.nombre}: '{z.etiqueta}' ({z.centro[0]:.0f}, {z.centro[1]:.0f}){area_str}")

    # 7. Si hay zona "cocina", filtrar paredes a solo las de esa zona
    #    Try ALL kitchen zones and pick the one that yields the most walls.
    zonas_cocina = [z for z in zonas if z.nombre == "cocina"]
    best_cocina_result = None  # (total_walls, paredes_cocina, zona, radio_mm)

    for zona_cocina in zonas_cocina:
        # Skip labels that are clearly not room names (e.g. "Salida chimenea cocina")
        etiqueta_lower = zona_cocina.etiqueta.lower()
        if any(skip in etiqueta_lower for skip in ["salida", "chimenea", "campana", "puerta"]):
            continue

        # Rotar centro de zona al espacio axis-aligned (como los segmentos)
        if abs(dominant_angle) > 0.5:
            zc_rotado = _rotar_punto(
                zona_cocina.centro[0], zona_cocina.centro[1],
                math.radians(-dominant_angle), cx, cy,
            )
        else:
            zc_rotado = zona_cocina.centro

        # Radio de busqueda basado en area de la cocina
        if zona_cocina.area_m2:
            radio_mm = math.sqrt(zona_cocina.area_m2) * 1000 * 1.2
        else:
            radio_mm = 8000  # 8m por defecto

        # First try: filter from already-classified walls
        paredes_cocina = _filtrar_paredes_zona(paredes_clasificadas, zc_rotado, radio_mm)
        total_cocina = sum(len(segs) for segs in paredes_cocina.values())

        if total_cocina < 3:
            # Kitchen zone may be OUTSIDE the initial boundary → walls weren't classified.
            # Go back to ALL cardinal-filtered walls and find those near the kitchen center.
            zx, zy = zc_rotado
            # Try progressively larger radii; for very large plans (GIS), scale up aggressively
            radii = [radio_mm, radio_mm * 1.5, radio_mm * 3]
            if width > 100000 or depth > 100000:  # >100m boundary
                radii.extend([radio_mm * 5, min(width, depth) * 0.1])
            paredes_cerca = []
            for r in radii:
                paredes_cerca = [p for p in paredes
                                 if math.sqrt(((p.start[0]+p.end[0])/2 - zx)**2 +
                                              ((p.start[1]+p.end[1])/2 - zy)**2) < r]
                if len(paredes_cerca) >= 3:
                    radio_mm = r
                    break

            if len(paredes_cerca) >= 3:
                # Try to find a rectangle near the kitchen center with reasonable size
                kitchen_bbox = _buscar_rectangulo_cerca(
                    paredes_cerca, zc_rotado,
                    max_dim=50000,  # 50m max side
                )
                if kitchen_bbox is None:
                    kitchen_bbox = _bounding_box(paredes_cerca)

                paredes_cocina = _clasificar_paredes(paredes_cerca, kitchen_bbox)
                total_cocina = sum(len(segs) for segs in paredes_cocina.values())

        if total_cocina >= 3:
            if best_cocina_result is None or total_cocina > best_cocina_result[0]:
                best_cocina_result = (total_cocina, paredes_cocina, zona_cocina, radio_mm)

    if best_cocina_result:
        total_cocina, paredes_cocina, zona_cocina, radio_mm = best_cocina_result

        # 7a. Excluir paredes que estan mas cerca de zonas NO-cocina (aseo, almacen, comedor)
        #     que de la zona cocina. Esto evita incluir paredes de aseos/almacen en el boundary.
        zonas_excluir = [z for z in zonas if z.nombre in ("aseo", "almacen", "office")]
        if zonas_excluir:
            zc = zona_cocina.centro
            if abs(dominant_angle) > 0.5:
                zc = _rotar_punto(zc[0], zc[1], math.radians(-dominant_angle), cx, cy)

            centros_excluir = []
            for ze in zonas_excluir:
                ce = ze.centro
                if abs(dominant_angle) > 0.5:
                    ce = _rotar_punto(ce[0], ce[1], math.radians(-dominant_angle), cx, cy)
                centros_excluir.append((ce, ze.nombre))

            n_excluidos = 0
            for side in list(paredes_cocina.keys()):
                filtrados = []
                for s in paredes_cocina[side]:
                    mx = (s.start[0] + s.end[0]) / 2
                    my = (s.start[1] + s.end[1]) / 2
                    dist_cocina = math.sqrt((mx - zc[0]) ** 2 + (my - zc[1]) ** 2)
                    # Excluir si esta mas cerca de alguna zona no-cocina
                    excluir = False
                    for ce, nombre in centros_excluir:
                        dist_otra = math.sqrt((mx - ce[0]) ** 2 + (my - ce[1]) ** 2)
                        if dist_otra < dist_cocina * 0.95:  # excluir si esta mas cerca de zona no-cocina
                            excluir = True
                            break
                    if not excluir:
                        filtrados.append(s)
                    else:
                        n_excluidos += 1
                paredes_cocina[side] = filtrados

            if n_excluidos > 0:
                print(f"[ANALIZAR] Excluidos {n_excluidos} segmentos cercanos a zonas no-cocina ({', '.join(set(n for _, n in centros_excluir))})")
                total_cocina = sum(len(segs) for segs in paredes_cocina.values())

        # 7a-bis. Cross-constrain: clip horizontal walls (N/S) to vertical walls' (E/W)
        #         X range, and vice versa. Prevents long continuous walls from extending
        #         the boundary beyond the kitchen's actual extent.
        _tol_cc = 500  # 500mm tolerance for cross-constraining
        ew_xs = []
        for _side in ("east", "west"):
            for _s in paredes_cocina.get(_side, []):
                ew_xs.extend([_s.start[0], _s.end[0]])
        ns_ys = []
        for _side in ("north", "south"):
            for _s in paredes_cocina.get(_side, []):
                ns_ys.extend([_s.start[1], _s.end[1]])

        if ew_xs:
            cc_x_min = min(ew_xs) - _tol_cc
            cc_x_max = max(ew_xs) + _tol_cc
            for _side in ("north", "south"):
                _orig = paredes_cocina.get(_side, [])
                if not _orig:
                    continue
                _filt = [s for s in _orig
                         if cc_x_min <= (s.start[0] + s.end[0]) / 2 <= cc_x_max]
                if _filt:
                    _removed = len(_orig) - len(_filt)
                    if _removed > 0:
                        print(f"[ANALIZAR] Cross-constrain: {_side} {len(_orig)}->{len(_filt)} segs (clipped to E/W X range {cc_x_min:.0f}-{cc_x_max:.0f})")
                    paredes_cocina[_side] = _filt

        if ns_ys:
            cc_y_min = min(ns_ys) - _tol_cc
            cc_y_max = max(ns_ys) + _tol_cc
            for _side in ("east", "west"):
                _orig = paredes_cocina.get(_side, [])
                if not _orig:
                    continue
                _filt = [s for s in _orig
                         if cc_y_min <= (s.start[1] + s.end[1]) / 2 <= cc_y_max]
                if _filt:
                    _removed = len(_orig) - len(_filt)
                    if _removed > 0:
                        print(f"[ANALIZAR] Cross-constrain: {_side} {len(_orig)}->{len(_filt)} segs (clipped to N/S Y range {cc_y_min:.0f}-{cc_y_max:.0f})")
                    paredes_cocina[_side] = _filt

        paredes_clasificadas = paredes_cocina

        # Compute boundary using side-specific coordinates:
        # X extent from east/west walls, Y extent from north/south walls.
        # This avoids long horizontal walls stretching the X boundary.
        all_pts_x, all_pts_y = [], []
        side_xs = {"east": [], "west": []}
        side_ys = {"north": [], "south": []}
        for side_name, segs in paredes_cocina.items():
            for s in segs:
                all_pts_x.extend([s.start[0], s.end[0]])
                all_pts_y.extend([s.start[1], s.end[1]])
                if side_name in side_xs:
                    side_xs[side_name].extend([s.start[0], s.end[0]])
                if side_name in side_ys:
                    side_ys[side_name].extend([s.start[1], s.end[1]])
        if all_pts_x:
            # Use perpendicular walls to define extent when available
            bnd_min_x = min(side_xs["west"]) if side_xs["west"] else min(all_pts_x)
            bnd_max_x = max(side_xs["east"]) if side_xs["east"] else max(all_pts_x)
            bnd_min_y = min(side_ys["south"]) if side_ys["south"] else min(all_pts_y)
            bnd_max_y = max(side_ys["north"]) if side_ys["north"] else max(all_pts_y)
            # Fallback: if side-specific boundary is degenerate (single side missing),
            # use overall bbox for that dimension
            if bnd_max_x - bnd_min_x < 1000:  # <1m width is degenerate
                bnd_min_x, bnd_max_x = min(all_pts_x), max(all_pts_x)
            if bnd_max_y - bnd_min_y < 1000:
                bnd_min_y, bnd_max_y = min(all_pts_y), max(all_pts_y)
            boundary = (bnd_min_x, bnd_min_y, bnd_max_x, bnd_max_y)
            width = boundary[2] - boundary[0]
            depth = boundary[3] - boundary[1]
            confidence = "high"
        print(f"[ANALIZAR] Zona COCINA detectada: {width:.0f}mm x {depth:.0f}mm (radio={radio_mm:.0f}mm)")
        for side, walls in paredes_cocina.items():
            if walls:
                total_len = sum(w.length for w in walls)
                print(f"[ANALIZAR]   cocina-{side}: {len(walls)} segmentos, {total_len:.0f}mm")
    elif zonas_cocina:
        print(f"[ANALIZAR] WARN: Zona cocina detectada pero pocas paredes, usando edificio completo")

    # 7b. Si no hay zona cocina y el boundary es grande, buscar sub-rectangulo de
    #     tamanio razonable (heuristica para planos sin etiqueta "COCINA").
    MAX_KITCHEN_DIM = 20000  # 20m — cocina industrial rara vez supera esto
    if not best_cocina_result and (width > MAX_KITCHEN_DIM or depth > MAX_KITCHEN_DIM):
        print(f"[ANALIZAR] Sin zona cocina y boundary grande ({width/1000:.0f}m x {depth/1000:.0f}m), buscando sub-rectangulo...")
        # Buscar el rectangulo mas grande con lados <= 25m y buena cobertura de paredes
        centro_boundary = ((min_x + max_x) / 2, (min_y + max_y) / 2)
        sub_rect = _buscar_rectangulo_cerca(paredes, centro_boundary, max_dim=MAX_KITCHEN_DIM)
        if sub_rect:
            sw = sub_rect[2] - sub_rect[0]
            sd = sub_rect[3] - sub_rect[1]
            # Solo usar si es significativamente menor que el boundary actual
            if sw < width * 0.7 or sd < depth * 0.7:
                # Re-clasificar paredes dentro del sub-rectangulo
                paredes_sub = _clasificar_paredes(paredes, sub_rect)
                total_sub = sum(len(segs) for segs in paredes_sub.values())
                if total_sub >= 3:
                    boundary = sub_rect
                    width, depth = sw, sd
                    min_x, min_y, max_x, max_y = boundary
                    paredes_clasificadas = paredes_sub
                    confidence = "medium"
                    print(f"[ANALIZAR] Sub-rectangulo cocina: {sw:.0f}mm x {sd:.0f}mm")
                    for side, walls in paredes_sub.items():
                        if walls:
                            total_len = sum(w.length for w in walls)
                            print(f"[ANALIZAR]   sub-{side}: {len(walls)} segmentos, {total_len:.0f}mm")

    return EspacioCocina(
        boundary_rect=boundary,
        paredes=paredes_clasificadas,
        width_mm=width,
        depth_mm=depth,
        unit_scale=scale,
        source_path=dxf_path,
        confidence=confidence,
        dominant_angle=dominant_angle,
        rotation_center=rotation_center,
        zonas=zonas,
    )


# ─── CLI ─────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import os

    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        # Default: probar con Bodeguilla
        path = os.path.join("pruebas", "LA BODEGUILLA INICIAL.dxf")

    espacio = analizar_plano_cliente(path)
    print(f"\n--- RESULTADO ---")
    print(f"  Dimensiones: {espacio.width_mm:.0f}mm x {espacio.depth_mm:.0f}mm")
    print(f"  Area: {espacio.width_mm * espacio.depth_mm / 1e6:.1f}m²")
    print(f"  Escala: x{espacio.unit_scale:.0f}")
    print(f"  Confianza: {espacio.confidence}")
    print(f"  Angulo dominante: {espacio.dominant_angle:.1f}°")
    print(f"  Centro rotacion: ({espacio.rotation_center[0]:.0f}, {espacio.rotation_center[1]:.0f})")
