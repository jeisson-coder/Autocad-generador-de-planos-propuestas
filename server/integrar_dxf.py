"""
integrar_dxf.py -- Genera el DXF final con:
  - Plano del cliente (escalado a mm, limpio de cotas y ruido)
  - Catalogo de equipos organizado por zona a la derecha del plano
  - Tabla de especificaciones tipo Excel debajo del plano
  - Preview PNG
"""

import json
import math
import os
import re
from typing import Optional

import ezdxf
from analizar_plano import EspacioCocina
from posicionar_equipos import EquipoPosicionado


_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LIBRERIA_DXF = os.path.join(_BASE_DIR, "data", "libreria_bloques.dxf")
BLOQUE_MAP_JSON = os.path.join(_BASE_DIR, "data", "bloque_map.json")

_bloque_map: dict = {}
if os.path.exists(BLOQUE_MAP_JSON):
    with open(BLOQUE_MAP_JSON, encoding="utf-8") as _f:
        _bloque_map = json.load(_f)

COLORES_ZONA = {
    "coccion": 1,   # Rojo
    "frio":    4,   # Cyan
    "lavado":  5,   # Azul
    "horno":   30,  # Naranja
}


def _buscar_bloque(modelo: str) -> Optional[str]:
    """Busca el bloque CAD que corresponde a un modelo."""
    if not _bloque_map:
        return None

    def _valid(name: str) -> bool:
        return name in _bloque_map and _bloque_map[name]["width_mm"] > 50

    if _valid(modelo):
        return modelo

    # Normalizar: quitar /S, /M, sufijos POW, PRO, etc.
    normalizado = re.sub(r'/[A-Z0-9]+', '', modelo).strip()
    normalizado = re.sub(
        r'\s+(POW|PRO|POWER|PROFESSIONAL|BASIC|ELECTRIC).*$', '',
        normalizado, flags=re.IGNORECASE
    ).strip()

    for suffix in ["-P", "-PLANTA", ""]:
        if _valid(normalizado + suffix):
            return normalizado + suffix

    # Sin guiones intermedios: "BARG-71" -> "BARG71-P", etc.
    sin_guion = normalizado.replace("-", "")
    for suffix in ["-P", "-PLANTA", ""]:
        if _valid(sin_guion + suffix):
            return sin_guion + suffix

    # Prefijo parcial (e.g., "CG-720" matches "CG-720-P")
    partes = normalizado.rsplit("-", 1)
    if len(partes) > 1:
        prefix = partes[0]
        for bname, binfo in _bloque_map.items():
            if bname.startswith(prefix) and binfo["width_mm"] > 50:
                return bname

    return None


def generar_dxf_integrado(
    equipos_pos: list[EquipoPosicionado],
    espacio: EspacioCocina,
    filepath: str = "propuesta_integrada.dxf",
) -> str:
    """
    Genera un DXF con el plano del cliente + equipos posicionados.

    Args:
        equipos_pos: Equipos ya posicionados con coordenadas del cliente
        espacio: Espacio detectado (incluye source_path del DXF original)
        filepath: Ruta de salida del DXF

    Returns:
        Ruta absoluta del DXF generado
    """
    abs_path = os.path.abspath(filepath)

    print(f"\n[INTEGRAR] Cargando plano del cliente: {espacio.source_path}")
    doc = ezdxf.readfile(espacio.source_path)
    msp = doc.modelspace()

    scale = espacio.unit_scale
    if scale != 1.0:
        print(f"[INTEGRAR] Escalando geometria x{scale:.0f}")
        matrix = ezdxf.math.Matrix44.scale(scale, scale, scale)
        for entity in list(msp):
            try:
                entity.transform(matrix)
            except Exception:
                pass  # Algunos tipos no soportan transform

    # Eliminar cotas y textos del layer 0; preservar geometria estructural
    removed = 0
    for entity in list(msp):
        etype = entity.dxftype()
        layer = entity.dxf.get("layer", "")
        if etype in ("DIMENSION", "LEADER"):
            msp.delete_entity(entity)
            removed += 1
            continue
        if etype in ("MTEXT", "TEXT") and layer == "0":
            msp.delete_entity(entity)
            removed += 1
            continue
    if removed:
        print(f"[INTEGRAR] Eliminadas {removed} entidades de ruido (cotas, textos, leaders)")

    for zona, color in COLORES_ZONA.items():
        if zona not in doc.layers:
            doc.layers.add(zona, color=color)
    if "textos" not in doc.layers:
        doc.layers.add("textos", color=7)
    # Apagado por defecto: salida limpia igual que DXFs profesionales
    doc.layers.get("textos").off()
    if "bbox" not in doc.layers:
        doc.layers.add("bbox", color=8)
    doc.layers.get("bbox").off()
    try:
        doc.styles.add("EQUIPO", font="Arial")
    except Exception:
        pass

    bloques_disponibles: set[str] = set()
    bloques_por_equipo: dict[int, str] = {}

    if _bloque_map and os.path.exists(LIBRERIA_DXF):
        for i, ep in enumerate(equipos_pos):
            bname = _buscar_bloque(ep.modelo)
            if bname:
                bloques_por_equipo[i] = bname
                bloques_disponibles.add(bname)

        if bloques_disponibles:
            try:
                # Importer evita conflictos de handles que genera xref.Loader
                from ezdxf.addons import Importer
                libreria_doc = ezdxf.readfile(LIBRERIA_DXF)
                importer = Importer(libreria_doc, doc)
                for bname in bloques_disponibles:
                    try:
                        importer.import_block(bname)
                    except Exception as be:
                        print(f"[INTEGRAR] WARN: No se pudo importar bloque {bname}: {be}")
                        bloques_disponibles.discard(bname)
                importer.finalize()
                print(f"[INTEGRAR] Bloques CAD importados: {len(bloques_disponibles)}")
            except Exception as e:
                print(f"[INTEGRAR] WARN: No se pudo cargar libreria CAD: {e}")
                bloques_por_equipo.clear()
                bloques_disponibles.clear()
    else:
        print("[INTEGRAR] INFO: Sin libreria CAD -- usando rectangulos")

    eq_counter = 0
    for i, ep in enumerate(equipos_pos):
        w = ep.ancho_mm
        d = ep.fondo_mm
        rotation = ep.rotation

        # Usar esquinas pre-calculadas (soporta rotacion arbitraria)
        if ep.corners and len(ep.corners) == 4:
            pts = list(ep.corners) + [ep.corners[0]]
        elif ep.wall_side in ("north", "south"):
            pts = [(ep.x, ep.y), (ep.x+w, ep.y), (ep.x+w, ep.y+d), (ep.x, ep.y+d), (ep.x, ep.y)]
        else:
            pts = [(ep.x, ep.y), (ep.x+d, ep.y), (ep.x+d, ep.y+w), (ep.x, ep.y+w), (ep.x, ep.y)]

        layer = ep.zona if ep.zona in COLORES_ZONA else "coccion"

        bname = bloques_por_equipo.get(i)
        binfo = _bloque_map.get(bname) if bname else None
        usa_bloque = bname and bname in bloques_disponibles and binfo and binfo["width_mm"] > 50

        if usa_bloque:
            # extmin puede ser negativo (conexiones de gas detras de la pared):
            # el footprint visual excluye esa extension -> max(0, extmin)
            extmin = binfo.get("extmin", [0, 0])
            extmax = binfo.get("extmax", [0, 0])
            bw = extmax[0] - max(0.0, extmin[0])
            bd = extmax[1] - max(0.0, extmin[1])
            if bw < 50:
                bw = binfo["width_mm"]
            if bd < 50:
                bd = binfo["depth_mm"]
            scale_x = w / bw if bw > 0 else 1.0
            scale_y = d / bd if bd > 0 else 1.0

            # El punto de insercion depende de que esquina del footprint
            # coincide con el origen del bloque segun su rotacion:
            #   north (0deg)   -> corners[0] (inf-izq)
            #   south (180deg) -> corners[2] (sup-der)
            #   west  (90deg)  -> corners[1] (inf-der)
            #   east  (270deg) -> corners[3] (sup-izq)
            corner_idx = {"north": 0, "south": 2, "west": 1, "east": 3}.get(
                ep.wall_side, 0
            )
            if ep.corners and len(ep.corners) > corner_idx:
                ref_x, ref_y = ep.corners[corner_idx]
            else:
                ref_x, ref_y = ep.x, ep.y

            ex = binfo["extmin"][0] * scale_x
            ey = binfo["extmin"][1] * scale_y
            cos_a = math.cos(math.radians(rotation))
            sin_a = math.sin(math.radians(rotation))
            offset_x = ex * cos_a - ey * sin_a
            offset_y = ex * sin_a + ey * cos_a
            insert_x = ref_x - offset_x
            insert_y = ref_y - offset_y

            msp.add_blockref(bname, (insert_x, insert_y), dxfattribs={
                "layer": layer, "xscale": scale_x, "yscale": scale_y,
                "rotation": rotation,
            })

            eq_counter += 1
            print(f"  [{eq_counter:2d}] BLOQUE {bname:20s}  ({int(ref_x)},{int(ref_y)})  {int(w)}x{int(d)}mm  rot={rotation:.1f}  {ep.zona} pared={ep.wall_side}")
        else:
            msp.add_lwpolyline(pts, dxfattribs={"layer": layer})
            eq_counter += 1
            ref_x, ref_y = ep.corners[0] if ep.corners else (ep.x, ep.y)
            print(f"  [{eq_counter:2d}] RECT  {ep.modelo:25s}  ({int(ref_x)},{int(ref_y)})  {int(w)}x{int(d)}mm  rot={rotation:.1f}  {ep.zona} pared={ep.wall_side}")

        if ep.corners and len(ep.corners) == 4:
            center_x = sum(c[0] for c in ep.corners) / 4
            center_y = sum(c[1] for c in ep.corners) / 4
        else:
            center_x = (pts[0][0] + pts[2][0]) / 2
            center_y = (pts[0][1] + pts[2][1]) / 2

        txt_height = max(25, min(60, min(w, d) / 5))

        msp.add_text(
            ep.modelo,
            height=txt_height,
            rotation=rotation,
            dxfattribs={
                "layer": "textos", "style": "EQUIPO",
                "halign": ezdxf.const.CENTER, "valign": ezdxf.const.MIDDLE,
                "insert": (center_x, center_y), "align_point": (center_x, center_y),
            },
        )

    print(f"\n[INTEGRAR] DXF guardado: {abs_path}")
    doc.saveas(abs_path)

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

        min_x, min_y, max_x, max_y = espacio.boundary_rect
        ax.set_title(
            f"Propuesta Integrada -- {int(max_x - min_x)}mm x {int(max_y - min_y)}mm",
            color="white", fontsize=14, pad=10,
        )
        fig.patch.set_facecolor("#1a1a2e")
        fig.tight_layout()
        fig.savefig(png_path, dpi=150, facecolor=fig.get_facecolor())
        plt.close(fig)
        print(f"[INTEGRAR] Preview PNG: {png_path}")
    except Exception as e:
        print(f"[INTEGRAR] WARN: No se pudo generar PNG: {e}")

    return abs_path


# ─── Modo Catalogo: equipos organizados fuera del plano ──

NOMBRES_ZONA = {
    "coccion": "ZONA COCCION",
    "horno": "ZONA HORNO",
    "lavado": "ZONA LAVADO",
    "frio": "ZONA REFRIGERACION",
    "refrigeracion": "ZONA REFRIGERACION",
}

_ORDEN_ZONA = ["coccion", "horno", "lavado", "frio"]


def generar_dxf_catalogo(
    equipos: list,
    espacio: EspacioCocina,
    filepath: str = "propuesta_catalogo.dxf",
    margen_plano: float = 3000.0,
    gap_equipos: float = 500.0,
    gap_zonas: float = 1500.0,
    max_ancho_fila: float = 8000.0,
) -> str:
    """
    Genera un DXF con el plano del cliente + equipos organizados por zona
    a la derecha del plano, listos para que el profesional los arrastre.

    Args:
        equipos: Lista de EquipoPosicionado (solo se usan modelo, zona, ancho, fondo)
        espacio: Espacio detectado (incluye source_path y boundary_rect)
        filepath: Ruta de salida del DXF
        margen_plano: Espacio entre el plano y el catalogo (mm)
        gap_equipos: Espacio entre equipos dentro de una zona (mm)
        gap_zonas: Espacio vertical entre zonas (mm)
        max_ancho_fila: Ancho maximo de fila antes de saltar a la siguiente
    """
    abs_path = os.path.abspath(filepath)

    print(f"\n[CATALOGO] Cargando plano del cliente: {espacio.source_path}")
    doc = ezdxf.readfile(espacio.source_path)
    msp = doc.modelspace()

    scale = espacio.unit_scale
    if scale != 1.0:
        print(f"[CATALOGO] Escalando geometria x{scale:.0f}")
        matrix = ezdxf.math.Matrix44.scale(scale, scale, scale)
        for entity in list(msp):
            try:
                entity.transform(matrix)
            except Exception:
                pass

    removed = 0
    for entity in list(msp):
        etype = entity.dxftype()
        layer = entity.dxf.get("layer", "")
        if etype in ("DIMENSION", "LEADER"):
            msp.delete_entity(entity)
            removed += 1
        elif etype in ("MTEXT", "TEXT") and layer == "0":
            msp.delete_entity(entity)
            removed += 1
    if removed:
        print(f"[CATALOGO] Eliminadas {removed} entidades de ruido")

    for zona, color in COLORES_ZONA.items():
        if zona not in doc.layers:
            doc.layers.add(zona, color=color)
    if "catalogo" not in doc.layers:
        doc.layers.add("catalogo", color=7)
    if "catalogo_headers" not in doc.layers:
        doc.layers.add("catalogo_headers", color=2)  # Amarillo
    if "catalogo_specs" not in doc.layers:
        doc.layers.add("catalogo_specs", color=8)  # Gris
    try:
        doc.styles.add("CATALOGO", font="Arial")
    except Exception:
        pass

    bloques_necesarios: dict[str, str] = {}  # modelo -> bloque_name
    bloques_set: set[str] = set()

    if _bloque_map and os.path.exists(LIBRERIA_DXF):
        for ep in equipos:
            bname = _buscar_bloque(ep.modelo)
            if bname:
                bloques_necesarios[ep.modelo] = bname
                bloques_set.add(bname)

        if bloques_set:
            try:
                from ezdxf.addons import Importer
                libreria_doc = ezdxf.readfile(LIBRERIA_DXF)
                importer = Importer(libreria_doc, doc)
                for bname in list(bloques_set):
                    try:
                        importer.import_block(bname)
                    except Exception as be:
                        print(f"[CATALOGO] WARN: No se pudo importar bloque {bname}: {be}")
                        bloques_set.discard(bname)
                importer.finalize()
                print(f"[CATALOGO] Bloques importados: {len(bloques_set)}")
            except Exception as e:
                print(f"[CATALOGO] WARN: No se pudo cargar libreria: {e}")
                bloques_necesarios.clear()
                bloques_set.clear()

    bx1, by1, bx2, by2 = espacio.boundary_rect
    # Usar extents reales para no solapar con geometria del edificio fuera de boundary_rect
    try:
        from ezdxf import bbox as _bbox
        cache = _bbox.extents(msp)
        if cache.has_data:
            real_max_x = cache.extmax.x
            real_max_y = cache.extmax.y
        else:
            real_max_x = bx2
            real_max_y = by2
    except Exception:
        real_max_x = bx2
        real_max_y = by2

    cat_x_start = max(bx2, real_max_x) + margen_plano
    cat_y_start = max(by2, real_max_y)

    from collections import defaultdict
    equipos_por_zona: dict[str, list] = defaultdict(list)
    for ep in equipos:
        zona = ep.zona if ep.zona else "coccion"
        equipos_por_zona[zona].append(ep)

    zonas_ordenadas = []
    for z in _ORDEN_ZONA:
        if z in equipos_por_zona:
            zonas_ordenadas.append(z)
    for z in equipos_por_zona:
        if z not in zonas_ordenadas:
            zonas_ordenadas.append(z)

    cursor_y = cat_y_start
    eq_total = 0
    header_height = 300
    label_height = 150
    spec_height = 100

    for zona in zonas_ordenadas:
        items = equipos_por_zona[zona]
        zona_nombre = NOMBRES_ZONA.get(zona, f"ZONA {zona.upper()}")
        layer_zona = zona if zona in COLORES_ZONA else "coccion"
        color_zona = COLORES_ZONA.get(zona, 7)

        cursor_y -= header_height * 1.5
        msp.add_line(
            (cat_x_start, cursor_y + header_height * 0.3),
            (cat_x_start + max_ancho_fila, cursor_y + header_height * 0.3),
            dxfattribs={"layer": "catalogo_headers", "color": color_zona},
        )
        msp.add_text(
            zona_nombre,
            height=header_height,
            dxfattribs={
                "layer": "catalogo_headers",
                "style": "CATALOGO",
                "color": color_zona,
            },
        ).set_placement((cat_x_start, cursor_y))
        cursor_y -= header_height * 0.8

        cursor_x = cat_x_start
        fila_max_depth = 0

        for ep in items:
            w = ep.ancho_mm
            d = ep.fondo_mm
            bname = bloques_necesarios.get(ep.modelo)
            binfo = _bloque_map.get(bname) if bname else None
            usa_bloque = bname and bname in bloques_set and binfo and binfo["width_mm"] > 50

            if cursor_x + w > cat_x_start + max_ancho_fila and cursor_x > cat_x_start:
                cursor_y -= fila_max_depth + label_height + spec_height + gap_equipos
                cursor_x = cat_x_start
                fila_max_depth = 0

            eq_x = cursor_x
            eq_y = cursor_y - d - label_height - spec_height

            if usa_bloque:
                extmin = binfo.get("extmin", [0, 0])
                extmax = binfo.get("extmax", [0, 0])
                bw = extmax[0] - max(0.0, extmin[0])
                bd = extmax[1] - max(0.0, extmin[1])
                if bw < 50:
                    bw = binfo["width_mm"]
                if bd < 50:
                    bd = binfo["depth_mm"]
                sx = w / bw if bw > 0 else 1.0
                sy = d / bd if bd > 0 else 1.0

                # Compensar extmin para que la esquina visual quede en eq_x, eq_y
                insert_x = eq_x - extmin[0] * sx
                insert_y = eq_y - extmin[1] * sy

                msp.add_blockref(bname, (insert_x, insert_y), dxfattribs={
                    "layer": layer_zona,
                    "xscale": sx,
                    "yscale": sy,
                    "rotation": 0,
                })
            else:
                pts = [
                    (eq_x, eq_y), (eq_x + w, eq_y),
                    (eq_x + w, eq_y + d), (eq_x, eq_y + d),
                    (eq_x, eq_y),
                ]
                msp.add_lwpolyline(pts, dxfattribs={"layer": layer_zona})

            # Contorno siempre visible para referencia dimensional
            border_pts = [
                (eq_x, eq_y), (eq_x + w, eq_y),
                (eq_x + w, eq_y + d), (eq_x, eq_y + d),
                (eq_x, eq_y),
            ]
            msp.add_lwpolyline(border_pts, dxfattribs={
                "layer": "catalogo", "color": 8,
            })

            msp.add_text(
                ep.modelo,
                height=label_height,
                dxfattribs={
                    "layer": "catalogo",
                    "style": "CATALOGO",
                    "color": color_zona,
                },
            ).set_placement((eq_x, eq_y + d + spec_height * 0.3))

            spec_text = f"{int(w)}x{int(d)}mm"
            if hasattr(ep, "cantidad") and ep.cantidad > 1:
                spec_text += f"  x{ep.cantidad}"
            msp.add_text(
                spec_text,
                height=spec_height,
                dxfattribs={
                    "layer": "catalogo_specs",
                    "style": "CATALOGO",
                },
            ).set_placement((eq_x, eq_y - spec_height * 1.2))

            eq_total += 1
            print(f"  [{eq_total:2d}] {ep.modelo:25s}  {int(w)}x{int(d)}mm  zona={zona}")

            cursor_x += w + gap_equipos
            fila_max_depth = max(fila_max_depth, d)

        cursor_y -= fila_max_depth + label_height + spec_height + gap_zonas

    title_y = cat_y_start + header_height * 1.5
    msp.add_text(
        f"EQUIPOS PROPUESTOS ({eq_total} uds)",
        height=header_height * 1.3,
        dxfattribs={
            "layer": "catalogo_headers",
            "style": "CATALOGO",
            "color": 7,
        },
    ).set_placement((cat_x_start, title_y))

    msp.add_text(
        "Arrastrar cada equipo a su posicion definitiva en el plano",
        height=spec_height * 1.2,
        dxfattribs={
            "layer": "catalogo_specs",
            "style": "CATALOGO",
        },
    ).set_placement((cat_x_start, title_y - header_height * 0.9))

    print(f"\n[CATALOGO] {eq_total} equipos organizados en {len(zonas_ordenadas)} zonas")

    _dibujar_tabla_especificaciones(msp, doc, equipos, espacio, zonas_ordenadas, equipos_por_zona)

    print(f"[CATALOGO] DXF guardado: {abs_path}")
    doc.saveas(abs_path)

    png_path = abs_path.replace(".dxf", ".png")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from ezdxf.addons.drawing import RenderContext, Frontend
        from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

        fig, ax = plt.subplots(1, 1, figsize=(28, 14), dpi=150)
        ax.set_aspect("equal")
        ax.set_facecolor("#1a1a2e")

        ctx = RenderContext(doc)
        out = MatplotlibBackend(ax)
        Frontend(ctx, out).draw_layout(msp)

        kitchen_w = int(bx2 - bx1)
        kitchen_h = int(by2 - by1)
        ax.set_title(
            f"Plano + Catalogo de Equipos -- Cocina {kitchen_w}mm x {kitchen_h}mm -- {eq_total} equipos",
            color="white", fontsize=13, pad=10,
        )
        fig.patch.set_facecolor("#1a1a2e")
        fig.tight_layout()
        fig.savefig(png_path, dpi=150, facecolor=fig.get_facecolor())
        plt.close(fig)
        print(f"[CATALOGO] Preview PNG: {png_path}")
    except Exception as e:
        print(f"[CATALOGO] WARN: No se pudo generar PNG: {e}")

    return abs_path


def _dibujar_tabla_especificaciones(
    msp, doc, equipos: list, espacio, zonas_ordenadas: list, equipos_por_zona: dict,
):
    """
    Dibuja una tabla de especificaciones tipo Excel debajo del plano,
    con HATCH de celdas y MTEXT (similar al RTE ARGENTINO FINAL).
    """
    bx1, by1, bx2, by2 = espacio.boundary_rect

    if "tabla" not in doc.layers:
        doc.layers.add("tabla", color=7)
    if "tabla_headers" not in doc.layers:
        doc.layers.add("tabla_headers", color=1)

    # Respetar geometria que pueda existir por debajo de by1
    try:
        from ezdxf import bbox as _bbox
        cache = _bbox.extents(msp)
        real_min_y = cache.extmin.y if cache.has_data else by1
    except Exception:
        real_min_y = by1

    table_x = bx1
    table_y = min(by1, real_min_y) - 3000  # 3m de margen bajo el contenido existente

    ROW_H = 400
    HEADER_H = 500
    ZONE_H = 450
    TEXT_H = 120
    HEADER_TH = 150
    ZONE_TH = 170

    # Columnas: Pos | Descripcion | Uds | Marca | Dimensiones
    COL_WIDTHS = [700, 4500, 700, 1800, 1800]
    COL_HEADERS = ["POS.", "DESCRIPCION", "UDS", "MARCA", "DIMENSIONES"]
    TABLE_W = sum(COL_WIDTHS)

    COLOR_HEADER_BG = 5    # Azul
    COLOR_ZONE_BG = 8      # Gris
    COLOR_ROW_ALT = 254    # Gris muy claro
    COLOR_TEXT = 7
    COLOR_HEADER_TEXT = 7

    cursor_y = table_y

    cursor_y -= HEADER_H
    _hatch_rect(msp, table_x, cursor_y, TABLE_W, HEADER_H, COLOR_HEADER_BG)
    _rect_border(msp, table_x, cursor_y, TABLE_W, HEADER_H)
    msp.add_mtext(
        "LISTADO DE EQUIPAMIENTO",
        dxfattribs={
            "layer": "tabla_headers",
            "char_height": ZONE_TH,
            "color": COLOR_HEADER_TEXT,
            "insert": (table_x + TABLE_W / 2, cursor_y + HEADER_H / 2),
            "attachment_point": 5,  # MIDDLE_CENTER
        },
    )

    cursor_y -= HEADER_H
    _hatch_rect(msp, table_x, cursor_y, TABLE_W, HEADER_H, COLOR_HEADER_BG)
    x = table_x
    for cw, header in zip(COL_WIDTHS, COL_HEADERS):
        _rect_border(msp, x, cursor_y, cw, HEADER_H)
        msp.add_mtext(
            header,
            dxfattribs={
                "layer": "tabla_headers",
                "char_height": HEADER_TH,
                "color": COLOR_HEADER_TEXT,
                "insert": (x + cw / 2, cursor_y + HEADER_H / 2),
                "attachment_point": 5,
            },
        )
        x += cw

    num = 0
    for zona in zonas_ordenadas:
        items = equipos_por_zona[zona]
        zona_nombre = NOMBRES_ZONA.get(zona, f"ZONA {zona.upper()}")
        color_zona = COLORES_ZONA.get(zona, 7)

        cursor_y -= ZONE_H
        _hatch_rect(msp, table_x, cursor_y, TABLE_W, ZONE_H, COLOR_ZONE_BG)
        _rect_border(msp, table_x, cursor_y, TABLE_W, ZONE_H)
        msp.add_mtext(
            zona_nombre,
            dxfattribs={
                "layer": "tabla",
                "char_height": ZONE_TH,
                "color": color_zona,
                "insert": (table_x + 200, cursor_y + ZONE_H / 2),
                "attachment_point": 4,  # MIDDLE_LEFT
            },
        )

        for i, ep in enumerate(items):
            num += 1
            cursor_y -= ROW_H

            if i % 2 == 1:
                _hatch_rect(msp, table_x, cursor_y, TABLE_W, ROW_H, COLOR_ROW_ALT)

            modelo = getattr(ep, "modelo", str(ep))
            cant = getattr(ep, "cantidad", 1) or 1
            serie = getattr(ep, "serie", "") or ""
            w_mm = getattr(ep, "ancho_mm", 0) or 0
            d_mm = getattr(ep, "fondo_mm", 0) or 0

            marca = serie if serie else "--"
            dims = f"{int(w_mm)}x{int(d_mm)}mm" if w_mm and d_mm else "--"
            valores = [str(num), modelo, str(cant), marca, dims]

            x = table_x
            for j, (cw, val) in enumerate(zip(COL_WIDTHS, valores)):
                _rect_border(msp, x, cursor_y, cw, ROW_H)
                # Pos (0) y Uds (2) centrados; resto alineado a la izquierda
                if j in (0, 2):
                    attach = 5  # MIDDLE_CENTER
                    tx = x + cw / 2
                else:
                    attach = 4  # MIDDLE_LEFT
                    tx = x + 100

                msp.add_mtext(
                    val,
                    dxfattribs={
                        "layer": "tabla",
                        "char_height": TEXT_H,
                        "color": COLOR_TEXT,
                        "insert": (tx, cursor_y + ROW_H / 2),
                        "attachment_point": attach,
                    },
                )
                x += cw

    total_h = table_y - cursor_y
    _rect_border(msp, table_x, cursor_y, TABLE_W, total_h)

    print(f"[CATALOGO] Tabla de especificaciones: {num} filas en {len(zonas_ordenadas)} zonas")


def _hatch_rect(msp, x: float, y: float, w: float, h: float, color: int):
    """Dibuja un rectangulo relleno (HATCH SOLID) para celdas de tabla."""
    hatch = msp.add_hatch(color=color, dxfattribs={"layer": "tabla"})
    hatch.paths.add_polyline_path(
        [(x, y), (x + w, y), (x + w, y + h), (x, y + h)],
        is_closed=True,
    )


def _rect_border(msp, x: float, y: float, w: float, h: float):
    """Dibuja el borde de una celda de tabla."""
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
    msp.add_lwpolyline(pts, dxfattribs={"layer": "tabla", "color": 8})


def verificar_dxf_generado(
    filepath: str,
    equipos_pos: list[EquipoPosicionado],
    tolerancia_mm: float = 200.0,
) -> tuple[bool, list[str]]:
    """
    Re-lee el DXF generado y verifica que todos los equipos esten presentes.

    Busca INSERT y LWPOLYLINE en capas de equipos (coccion, frio, lavado, horno)
    y compara posiciones con los EquipoPosicionado.

    Returns:
        (all_ok, issues_list) donde issues_list contiene descripciones de problemas.
    """
    import math as _m

    if not os.path.exists(filepath):
        return False, [f"DXF no encontrado: {filepath}"]

    try:
        doc = ezdxf.readfile(filepath)
    except Exception as e:
        return False, [f"Error leyendo DXF: {e}"]

    msp = doc.modelspace()
    equipment_layers = {"coccion", "frio", "lavado", "horno"}

    dxf_entities: list[dict] = []
    for entity in msp:
        layer = entity.dxf.layer.lower() if hasattr(entity.dxf, "layer") else ""
        if layer not in equipment_layers:
            continue
        etype = entity.dxftype()
        if etype == "INSERT":
            pos = entity.dxf.insert
            ix, iy = pos.x, pos.y
            block_name = entity.dxf.name
            # bloque_map.json como fuente primaria de extmin (mas fiable que escanear geometria)
            bmap_info = _bloque_map.get(block_name)
            if bmap_info:
                try:
                    ext_min_x = bmap_info["extmin"][0]
                    ext_min_y = bmap_info["extmin"][1]
                    sx = entity.dxf.get("xscale", 1.0)
                    sy = entity.dxf.get("yscale", 1.0)
                    rot = _m.radians(entity.dxf.get("rotation", 0.0))
                    ex = ext_min_x * sx
                    ey = ext_min_y * sy
                    offset_x = ex * _m.cos(rot) - ey * _m.sin(rot)
                    offset_y = ex * _m.sin(rot) + ey * _m.cos(rot)
                    ix = pos.x + offset_x
                    iy = pos.y + offset_y
                except Exception:
                    pass
            elif block_name in doc.blocks:
                block_def = doc.blocks[block_name]
                try:
                    all_pts = []
                    for sub_e in block_def:
                        if sub_e.dxftype() == "LINE":
                            all_pts.extend([(sub_e.dxf.start.x, sub_e.dxf.start.y),
                                            (sub_e.dxf.end.x, sub_e.dxf.end.y)])
                        elif sub_e.dxftype() == "LWPOLYLINE":
                            all_pts.extend(sub_e.get_points("xy"))
                    if all_pts:
                        ext_min_x = min(p[0] for p in all_pts)
                        ext_min_y = min(p[1] for p in all_pts)
                        sx = entity.dxf.get("xscale", 1.0)
                        sy = entity.dxf.get("yscale", 1.0)
                        rot = _m.radians(entity.dxf.get("rotation", 0.0))
                        ex = ext_min_x * sx
                        ey = ext_min_y * sy
                        offset_x = ex * _m.cos(rot) - ey * _m.sin(rot)
                        offset_y = ex * _m.sin(rot) + ey * _m.cos(rot)
                        ix = pos.x + offset_x
                        iy = pos.y + offset_y
                except Exception:
                    pass
            dxf_entities.append({"x": ix, "y": iy, "type": "INSERT", "matched": False})
        elif etype == "LWPOLYLINE":
            pts = list(entity.get_points("xy"))
            if len(pts) >= 4:
                min_x = min(p[0] for p in pts)
                min_y = min(p[1] for p in pts)
                dxf_entities.append({"x": min_x, "y": min_y, "type": "POLY", "matched": False})

    issues: list[str] = []
    n_matched = 0

    for ep in equipos_pos:
        best_dist = float("inf")
        best_idx = -1
        # INSERT usa corner_idx como referencia; POLY usa esquina min -> probar todas
        ref_points = [(ep.x, ep.y)]
        if ep.corners:
            for cx, cy in ep.corners:
                if (cx, cy) != (ep.x, ep.y):
                    ref_points.append((cx, cy))
        for idx, ent in enumerate(dxf_entities):
            if ent["matched"]:
                continue
            for rx, ry in ref_points:
                dist = _m.sqrt((ent["x"] - rx) ** 2 + (ent["y"] - ry) ** 2)
                if dist < best_dist and dist < tolerancia_mm:
                    best_dist = dist
                    best_idx = idx
        if best_idx >= 0:
            dxf_entities[best_idx]["matched"] = True
            n_matched += 1
        else:
            issues.append(f"No encontrado en DXF: {ep.modelo} ({ep.x:.0f}, {ep.y:.0f})")

    all_ok = n_matched == len(equipos_pos)
    if all_ok:
        issues.append(f"OK: {n_matched}/{len(equipos_pos)} equipos verificados en DXF")

    return all_ok, issues
