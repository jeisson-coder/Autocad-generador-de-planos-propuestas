"""
Conversor DWG → DXF usando ODA File Converter.

Uso:
    from convertir_dwg import dwg_a_dxf
    dxf_path = dwg_a_dxf("plano_cliente.dwg")

    # O desde línea de comandos:
    python convertir_dwg.py plano_cliente.dwg
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

# Rutas conocidas de ODA File Converter en Windows
ODA_PATHS = [
    r"C:\Program Files\ODA\ODAFileConverter 27.1.0\ODAFileConverter.exe",
    r"C:\Program Files\ODA\ODAFileConverter\ODAFileConverter.exe",
    r"C:\Program Files (x86)\ODA\ODAFileConverter\ODAFileConverter.exe",
]


def _find_oda() -> str | None:
    """Encuentra el ejecutable de ODA File Converter."""
    for p in ODA_PATHS:
        if os.path.isfile(p):
            return p
    # Intentar con ezdxf addon
    try:
        from ezdxf.addons import odafc
        exe = getattr(odafc, "exe_path", None) or getattr(odafc, "_oda_fc_exe", None)
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return None


def dwg_a_dxf(dwg_path: str, output_dir: str | None = None, version: str = "ACAD2018") -> str:
    """
    Convierte un archivo DWG a DXF.

    Args:
        dwg_path: Ruta al archivo .dwg de entrada
        output_dir: Directorio de salida (default: mismo directorio que el DWG)
        version: Versión DXF de salida (ACAD2010, ACAD2013, ACAD2018)

    Returns:
        Ruta absoluta del archivo DXF generado

    Raises:
        FileNotFoundError: Si el DWG no existe o ODA no está instalado
        RuntimeError: Si la conversión falla
    """
    dwg_path = os.path.abspath(dwg_path)
    if not os.path.isfile(dwg_path):
        raise FileNotFoundError(f"Archivo DWG no encontrado: {dwg_path}")

    oda_exe = _find_oda()
    if not oda_exe:
        raise FileNotFoundError(
            "ODA File Converter no encontrado. "
            "Descárgalo gratis en: https://www.opendesign.com/guestfiles/oda_file_converter"
        )

    # ODA File Converter trabaja con directorios, no archivos individuales.
    # Copiamos el DWG a un directorio temporal y convertimos ahí.
    with tempfile.TemporaryDirectory(prefix="dwg2dxf_") as tmp_input:
        tmp_output = tempfile.mkdtemp(prefix="dwg2dxf_out_")
        try:
            # Copiar DWG al directorio temporal de entrada
            dwg_name = os.path.basename(dwg_path)
            shutil.copy2(dwg_path, os.path.join(tmp_input, dwg_name))

            # ODA args: input_dir output_dir version tipo_salida recurse audit
            # tipo_salida: 0=DWG, 1=DXF(ASCII), 2=DXF(binary)
            cmd = [
                oda_exe,
                tmp_input,      # Input folder
                tmp_output,     # Output folder
                version,        # Output version
                "DXF",          # Output type
                "0",            # Recurse: 0=no
                "1",            # Audit: 1=yes
            ]

            print(f"  Convirtiendo {dwg_name} → DXF ({version})...")
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
            )

            # ODA no siempre devuelve exit code correcto, verificar output
            dxf_name = Path(dwg_name).stem + ".dxf"
            dxf_tmp = os.path.join(tmp_output, dxf_name)

            if not os.path.isfile(dxf_tmp):
                stderr = result.stderr or result.stdout or "sin output"
                raise RuntimeError(f"ODA no generó el DXF. Output: {stderr}")

            # Mover al directorio de salida final
            if output_dir is None:
                output_dir = os.path.dirname(dwg_path)
            os.makedirs(output_dir, exist_ok=True)

            dxf_final = os.path.join(output_dir, dxf_name)
            shutil.move(dxf_tmp, dxf_final)

            size_kb = os.path.getsize(dxf_final) / 1024
            print(f"  DXF generado: {dxf_final} ({size_kb:.0f}KB)")
            return dxf_final

        finally:
            shutil.rmtree(tmp_output, ignore_errors=True)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Uso: python convertir_dwg.py <archivo.dwg> [directorio_salida]")
        sys.exit(1)

    dwg = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        dxf = dwg_a_dxf(dwg, output_dir=out)
        print(f"\nConversión exitosa: {dxf}")
    except Exception as e:
        print(f"\nERROR: {e}")
        sys.exit(1)
