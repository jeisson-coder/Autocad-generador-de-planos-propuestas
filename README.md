# RepagasConcept — Generador de Cocinas Industriales

Sistema de diseno automatizado de cocinas industriales para **RepagasConcept**. Recibe un formulario del cliente, genera una propuesta de equipamiento con IA, produce planos DXF y documentacion PDF profesional.

## Arquitectura

```
proyecto/
  formulario-repagas/    Frontend (HTML/CSS/JS) — desplegable en Netlify
  server/                Backend (Python/FastAPI) — desplegable en Railway/Render
    webhook_server.py    API REST principal
    generador_cocinas.py Orquestador: LLM + BD + generacion
    analizar_plano.py    Analisis de planos DXF del cliente
    posicionar_equipos.py Motor de posicionamiento de equipos
    integrar_dxf.py      Genera DXF catalogo con tabla de specs
    generar_pdf_propuesta.py  3 PDFs: propuesta, formulario, presupuesto
    rag_pipeline.py      Pipeline RAG (embeddings + Supabase pgvector)
    convertir_dwg.py     Conversion DWG a DXF
    assets/              Logo y tipografia Basier Square
    data/                Bloques CAD, patrones profesionales
  requirements.txt
  .env                   Variables de entorno (no se sube a git)
```

## Stack

| Capa | Tecnologia |
|---|---|
| Frontend | HTML + CSS + JS (vanilla), Basier Square font |
| Backend | Python 3.13, FastAPI, Uvicorn |
| LLM | Gemini 2.5 Pro (via OpenRouter o API directa) |
| Base de datos | Supabase (PostgreSQL + pgvector) |
| DXF | ezdxf (lectura, escritura, analisis geometrico) |
| PDF | fpdf2 con branding RepagasConcept |
| RAG | Gemini Embeddings + busqueda semantica pgvector |

## Setup

### 1. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 2. Configurar variables de entorno

Crear `.env` en la raiz:

```env
# LLM
OPENROUTER_API_KEY=sk-or-...
GEMINI_API_KEY=...

# Base de datos (Supabase)
SUPABASE_DB_HOST=...
SUPABASE_DB_PORT=5432
SUPABASE_DB_NAME=postgres
SUPABASE_DB_USER=postgres
SUPABASE_DB_PASSWORD=...

# RAG
DATA_ROOT=G:/.shortcut-targets-by-id/.../Repagas IA

# Login formulario
REPAGAS_LOGIN_USER=repagas
REPAGAS_LOGIN_PASS=concept2025
```

### 3. Iniciar el servidor

```bash
cd server
uvicorn webhook_server:app --reload --port 8000
```

### 4. Abrir el formulario

Abrir `formulario-repagas/index.html` en el navegador. El formulario se conecta al servidor en `http://localhost:8000`.

## Endpoints API

| Metodo | Ruta | Descripcion |
|---|---|---|
| `POST` | `/login` | Validar credenciales |
| `POST` | `/generar` | Generar propuesta (JSON) |
| `POST` | `/generar-con-plano` | Generar con plano DXF/DWG del cliente |
| `POST` | `/feedback` | Aplicar cambios con IA y regenerar |
| `GET` | `/catalogo` | Catalogo de equipos desde BD |

## Flujo de generacion

1. **Formulario** — el cliente completa el cuestionario (10 pasos)
2. **LLM** — Gemini analiza necesidades y propone equipamiento
3. **Resolucion** — cada equipo se resuelve contra la BD (modelos reales, precios, dimensiones)
4. **DXF** — plano del cliente + bloques CAD organizados por zona + tabla de especificaciones
5. **PDF** — propuesta de equipamiento, formulario del cliente, presupuesto con IVA
6. **ZIP** — se entrega todo empaquetado al usuario
7. **Feedback** — el usuario puede pedir cambios, la IA los aplica y regenera

## Deploy

- **Frontend**: Netlify (carpeta `formulario-repagas/`)
- **Backend**: Railway o Render (carpeta `server/` + `requirements.txt`)

## Branding

Paleta y tipografia del manual de marca RepagasConcept:
- Color principal: `#3943B7`
- Tipografia: Basier Square
- Tagline: *Proyectamos tu cocina*
