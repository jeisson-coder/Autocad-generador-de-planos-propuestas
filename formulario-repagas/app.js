/* ═══════════════════════════════════════════════════════════
   Repagas — Formulario Cocinas Industriales (app.js)
   Lógica del wizard, validación y envío al webhook.
   ═══════════════════════════════════════════════════════════ */

// ─── Login (valida contra backend, credenciales en .env) ─

function checkLogin() {
  if (sessionStorage.getItem("repagas_auth") === "ok") {
    document.getElementById("loginScreen").style.display = "none";
    document.getElementById("appContainer").style.display = "";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  checkLogin();

  document.getElementById("btnLogin").addEventListener("click", doLogin);
  document.getElementById("loginPass").addEventListener("keydown", (e) => {
    if (e.key === "Enter") doLogin();
  });
  document.getElementById("loginUser").addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("loginPass").focus();
  });
});

async function doLogin() {
  const user = document.getElementById("loginUser").value.trim();
  const pass = document.getElementById("loginPass").value;
  const btn = document.getElementById("btnLogin");
  btn.disabled = true;
  btn.textContent = "Verificando...";
  try {
    const baseUrl = getBaseUrl();
    const resp = await fetch(baseUrl + "/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user, pass }),
    });
    if (resp.ok) {
      sessionStorage.setItem("repagas_auth", "ok");
      document.getElementById("loginScreen").style.display = "none";
      document.getElementById("appContainer").style.display = "";
      document.getElementById("loginError").style.display = "none";
    } else {
      document.getElementById("loginError").style.display = "";
      document.getElementById("loginPass").value = "";
      document.getElementById("loginPass").focus();
    }
  } catch (e) {
    document.getElementById("loginError").textContent = "No se pudo conectar al servidor";
    document.getElementById("loginError").style.display = "";
  } finally {
    btn.disabled = false;
    btn.textContent = "Entrar";
  }
}

// ─── App ─────────────────────────────────────────────────

const TOTAL_STEPS = 10;
let currentStep = 1;

// Catálogo de equipos cargado desde la API (se llena en fetchCatalogo)
let catalogoEquipos = null;

const stepNames = [
  "Proyecto",
  "Tecnica",
  "Energia",
  "Equipamiento",
  "Equipos Manual",
  "Gastronomia",
  "Lavado",
  "Refrigeracion",
  "Personal",
  "Enviar",
];

// ─── Helpers ────────────────────────────────────────────

function getVal(id) {
  const el = document.getElementById(id);
  return el ? el.value.trim() : "";
}

function getNum(id) {
  const v = getVal(id);
  return v ? parseFloat(v) : null;
}

function getRadio(name) {
  const el = document.querySelector(`input[name="${name}"]:checked`);
  return el ? el.value : null;
}

function getCheckedValues(name) {
  return Array.from(document.querySelectorAll(`input[name="${name}"]:checked`)).map((c) => c.value);
}

function splitCommas(str) {
  if (!str) return [];
  return str.split(",").map((s) => s.trim()).filter(Boolean);
}

function radioBool(name) {
  const v = getRadio(name);
  return v === "si" ? true : v === "no" ? false : null;
}

// ─── DOM refs ───────────────────────────────────────────

const form = document.getElementById("formulario");
const btnPrev = document.getElementById("btnPrev");
const btnNext = document.getElementById("btnNext");
const btnSubmit = document.getElementById("btnSubmit");
const progressFill = document.getElementById("progressFill");
const stepIndicatorsContainer = document.getElementById("stepIndicators");

// ─── Init step indicators ───────────────────────────────

function initIndicators() {
  stepIndicatorsContainer.innerHTML = "";
  stepNames.forEach((name, i) => {
    const el = document.createElement("span");
    el.className = "step-indicator" + (i === 0 ? " active" : "");
    el.textContent = name;
    el.dataset.step = i + 1;
    el.addEventListener("click", () => {
      const target = i + 1;
      // Allow jumping to any step (completed or current)
      showStep(target);
    });
    stepIndicatorsContainer.appendChild(el);
  });
}

initIndicators();

// ─── Navigation ─────────────────────────────────────────

function showStep(n) {
  document.querySelectorAll(".step").forEach((s) => s.classList.remove("active"));
  const target = document.querySelector(`.step[data-step="${n}"]`);
  if (target) target.classList.add("active");
  currentStep = n;

  btnPrev.style.display = n === 1 ? "none" : "";
  btnNext.style.display = n === TOTAL_STEPS ? "none" : "";
  btnSubmit.style.display = n === TOTAL_STEPS ? "" : "none";

  progressFill.style.width = Math.round((n / TOTAL_STEPS) * 100) + "%";

  document.querySelectorAll(".step-indicator").forEach((el) => {
    const s = parseInt(el.dataset.step);
    el.classList.remove("active", "completed");
    if (s === n) el.classList.add("active");
    else if (s < n) el.classList.add("completed");
  });

  if (n === TOTAL_STEPS) buildResumen();
  if (n === 5) ensureCatalogoLoaded();
  updateConditionalSections();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

btnNext.addEventListener("click", () => {
  if (validateStep(currentStep)) showStep(currentStep + 1);
});

btnPrev.addEventListener("click", () => {
  if (currentStep > 1) showStep(currentStep - 1);
});

// ─── Conditional visibility ─────────────────────────────

function setupConditionals() {
  // Renovación → retirar antigua
  document.querySelectorAll('input[name="tipo_proyecto"]').forEach((r) => {
    r.addEventListener("change", () => {
      document.getElementById("field_retirar").style.display =
        document.querySelector('input[name="tipo_proyecto"]:checked')?.value === "renovacion" ? "" : "none";
    });
  });

  // Tiene plano → upload
  document.querySelectorAll('input[name="tiene_plano"]').forEach((r) => {
    r.addEventListener("change", () => {
      document.getElementById("field_upload_plano").style.display =
        document.querySelector('input[name="tiene_plano"]:checked')?.value === "si" ? "" : "none";
    });
  });

  // Desniveles → detalle
  document.querySelectorAll('input[name="desniveles"]').forEach((r) => {
    r.addEventListener("change", () => {
      document.getElementById("field_desnivel_detalle").style.display =
        document.querySelector('input[name="desniveles"]:checked')?.value === "si" ? "" : "none";
    });
  });

  // Energía → tipo gas / eléctrico / caudal
  document.querySelectorAll('input[name="energia"]').forEach((r) => {
    r.addEventListener("change", () => {
      const val = document.querySelector('input[name="energia"]:checked')?.value;
      document.getElementById("field_tipo_gas").style.display = (val === "gas" || val === "mixto") ? "" : "none";
      document.getElementById("field_caudal_gas").style.display = (val === "gas" || val === "mixto") ? "" : "none";
      document.getElementById("field_tipo_electrico").style.display = (val === "electrico" || val === "mixto") ? "" : "none";
    });
  });

  // Formación → equipos
  document.querySelectorAll('input[name="formacion"]').forEach((r) => {
    r.addEventListener("change", () => {
      document.getElementById("field_equipos_formacion").style.display =
        document.querySelector('input[name="formacion"]:checked')?.value === "si" ? "" : "none";
    });
  });

  // File upload visual
  const fileInput = document.getElementById("archivo_plano");
  const fileZone = document.getElementById("fileUploadZone");
  const fileNameEl = document.getElementById("fileName");
  if (fileInput) {
    fileInput.addEventListener("change", () => {
      fileNameEl.textContent = fileInput.files[0] ? fileInput.files[0].name : "";
    });
    fileZone.addEventListener("dragover", (e) => { e.preventDefault(); fileZone.classList.add("dragover"); });
    fileZone.addEventListener("dragleave", () => fileZone.classList.remove("dragover"));
    fileZone.addEventListener("drop", () => fileZone.classList.remove("dragover"));
  }
}

setupConditionals();

function updateConditionalSections() {
  const needsLavado = document.querySelector('input[name="area_lavado"]')?.checked;
  const lavadoFields = document.getElementById("lavado_fields");
  const lavadoNoNecesita = document.getElementById("lavado_no_necesita");
  if (lavadoFields) lavadoFields.style.display = needsLavado ? "" : "none";
  if (lavadoNoNecesita) lavadoNoNecesita.style.display = needsLavado ? "none" : "";

  const needsRefri = document.querySelector('input[name="area_refrigeracion"]')?.checked;
  const refriFields = document.getElementById("refri_fields");
  const refriNoNecesita = document.getElementById("refri_no_necesita");
  if (refriFields) refriFields.style.display = needsRefri ? "" : "none";
  if (refriNoNecesita) refriNoNecesita.style.display = needsRefri ? "none" : "";
}

// ─── Dynamic Equipment Rows con catálogo ────────────────

function capitalize(s) {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function escapeAttr(s) {
  return String(s).replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

function formatTipo(tipo) {
  // "cocina_gas" → "Cocina gas", "fry_top_electrico" → "Fry top electrico"
  return tipo.replace(/_/g, " ").replace(/^\w/, c => c.toUpperCase());
}

function buildSelectHTML(zone, selectedModelo) {
  // Si no hay catálogo, fallback a input text
  if (!catalogoEquipos || !catalogoEquipos[zone] || !catalogoEquipos[zone].length) {
    return '<input type="text" class="equipo-nombre" placeholder="Nombre del equipo (ej: CG-760)" value="' + escapeAttr(selectedModelo) + '" />';
  }

  const equipos = catalogoEquipos[zone];

  // Agrupar por tipo
  const porTipo = {};
  equipos.forEach(eq => {
    const t = eq.tipo || "otro";
    if (!porTipo[t]) porTipo[t] = [];
    porTipo[t].push(eq);
  });

  let html = '<select class="equipo-nombre">';
  html += '<option value="">-- Seleccionar equipo --</option>';

  for (const [tipo, lista] of Object.entries(porTipo).sort((a, b) => a[0].localeCompare(b[0]))) {
    html += '<optgroup label="' + escapeAttr(formatTipo(tipo)) + '">';
    lista.forEach(eq => {
      const precio = eq.pvp_eur ? " - " + eq.pvp_eur.toLocaleString("es-ES") + " EUR" : "";
      const serie = eq.serie ? " [" + eq.serie + "]" : "";
      const label = eq.modelo + " (" + eq.ancho_mm + "x" + eq.fondo_mm + "mm)" + serie + precio;
      const selected = eq.modelo === selectedModelo ? " selected" : "";
      html += '<option value="' + escapeAttr(eq.modelo) + '"' + selected + '>' + escapeAttr(label) + '</option>';
    });
    html += '</optgroup>';
  }

  html += '</select>';
  return html;
}

function addEquipoRow(zone, nombre = "", cantidad = 1) {
  const list = document.getElementById("equipos" + capitalize(zone) + "List");
  if (!list) return;

  const row = document.createElement("div");
  row.className = "equipo-row";
  row.innerHTML =
    buildSelectHTML(zone, nombre) +
    '<input type="number" class="equipo-cantidad" min="1" value="' + cantidad + '" title="Cantidad" />' +
    '<button type="button" class="btn-remove-equipo" title="Eliminar">&times;</button>';

  row.querySelector(".btn-remove-equipo").addEventListener("click", () => row.remove());
  list.appendChild(row);
}

function getManualEquipos(zone) {
  const list = document.getElementById("equipos" + capitalize(zone) + "List");
  if (!list) return [];
  const rows = list.querySelectorAll(".equipo-row");
  const result = [];
  rows.forEach((row) => {
    const el = row.querySelector(".equipo-nombre");
    const nombre = el ? el.value.trim() : "";
    const cantidad = parseInt(row.querySelector(".equipo-cantidad").value) || 1;
    if (nombre) result.push({ nombre, cantidad });
  });
  return result;
}

// ─── Fetch catálogo desde API ───────────────────────────

let catalogoLoading = false;

const API_URL = "https://web-production-0a9ac.up.railway.app";

function getBaseUrl() {
  return API_URL;
}

function updateCatalogoStatus(msg, isError) {
  const el = document.getElementById("catalogoStatus");
  if (!el) return;
  el.textContent = msg;
  el.className = "catalogo-status" + (isError ? " catalogo-error" : " catalogo-ok");
  el.style.display = msg ? "" : "none";
}

async function fetchCatalogo() {
  if (catalogoLoading) return;
  catalogoLoading = true;
  updateCatalogoStatus("Cargando catalogo desde el servidor...", false);
  try {
    const resp = await fetch(getBaseUrl() + "/catalogo");
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    catalogoEquipos = await resp.json();
    const total = Object.values(catalogoEquipos).reduce((s, arr) => s + arr.length, 0);
    console.log("[Catalogo] Cargado:", Object.keys(catalogoEquipos).map(k => k + "=" + catalogoEquipos[k].length).join(", "));
    updateCatalogoStatus("Catalogo cargado: " + total + " equipos disponibles", false);
  } catch (err) {
    console.warn("[Catalogo] No se pudo cargar:", err.message);
    catalogoEquipos = null;
    updateCatalogoStatus("No se pudo conectar al servidor (" + getBaseUrl() + "). Asegurese de que el servidor este corriendo (python webhook_server.py). Puede escribir manualmente.", true);
  } finally {
    catalogoLoading = false;
  }
}

async function ensureCatalogoLoaded() {
  if (!catalogoEquipos) {
    await fetchCatalogo();
  }
}

// Cargar catálogo al iniciar (no bloqueante)
fetchCatalogo();

// Setup add buttons
document.querySelectorAll(".btn-add-equipo").forEach((btn) => {
  btn.addEventListener("click", () => {
    addEquipoRow(btn.dataset.zone);
  });
});

// ─── Validation ─────────────────────────────────────────

function validateStep(step) {
  document.querySelectorAll(`.step[data-step="${step}"] .field-error`).forEach((el) => el.classList.remove("field-error"));
  document.querySelectorAll(`.step[data-step="${step}"] .error-msg`).forEach((el) => el.remove());

  let valid = true;

  if (step === 1) {
    if (!getVal("tipo_negocio")) { markError("tipo_negocio", "Seleccione el tipo de negocio"); valid = false; }
    if (!getVal("comensales") || parseInt(getVal("comensales")) < 1) { markError("comensales", "Indique el número de comensales"); valid = false; }
  }
  if (step === 2) {
    if (!getRadio("tipo_proyecto")) { markRadioError("tipo_proyecto", "Seleccione tipo de proyecto"); valid = false; }
  }
  if (step === 3) {
    if (!getRadio("energia")) { markRadioError("energia", "Seleccione la fuente de energía"); valid = false; }
  }
  return valid;
}

function markError(fieldId, msg) {
  const el = document.getElementById(fieldId);
  const field = el.closest(".field");
  field.classList.add("field-error");
  const errEl = document.createElement("div");
  errEl.className = "error-msg";
  errEl.textContent = msg;
  field.appendChild(errEl);
}

function markRadioError(name, msg) {
  const radios = document.querySelectorAll(`input[name="${name}"]`);
  if (radios.length) {
    const field = radios[0].closest(".field");
    field.classList.add("field-error");
    const errEl = document.createElement("div");
    errEl.className = "error-msg";
    errEl.textContent = msg;
    field.appendChild(errEl);
  }
}

// ─── Build FormularioCliente JSON ───────────────────────

function buildFormularioJSON() {
  // Dimensiones accesos
  const accesos = {};
  const ap = getNum("acceso_principal");
  const ac = getNum("acceso_cocina");
  const aa = getNum("acceso_almacen");
  if (ap) accesos.puerta_principal_m = ap;
  if (ac) accesos.puerta_cocina_m = ac;
  if (aa) accesos.puerta_almacen_m = aa;

  return {
    proyecto: {
      nombre: getVal("nombre_proyecto") || null,
      tipo_negocio: getVal("tipo_negocio"),
      concepto: getVal("concepto") || null,
      comensales: parseInt(getVal("comensales")),
      superficie_m2: getNum("superficie_m2"),
      presupuesto_max: getNum("presupuesto_max"),
    },

    parte_tecnica: {
      tipo_proyecto: getRadio("tipo_proyecto") || "nuevo",
      retirar_cocina_antigua: getRadio("retirar_antigua") === "si",
      existe_plano_tecnico: getRadio("tiene_plano") === "si",
      altura_suelo_techo_m: getNum("altura_techo"),
      material_paredes: splitCommas(getVal("material_paredes")),
      material_suelo: getVal("material_suelo") || null,
      desniveles_suelo: {
        existe: getRadio("desniveles") === "si",
        detalle: getVal("desnivel_detalle") || null,
      },
      dimensiones_accesos: Object.keys(accesos).length ? accesos : null,
    },

    energia: {
      tipo_energia: getRadio("energia") || "gas",
      tipo_gas: getRadio("tipo_gas") || null,
      caudal_gas_disponible: getVal("caudal_gas") || null,
      tipo_electrico: getRadio("tipo_electrico") || null,
      potencia_contratada_kw: getNum("potencia_kw"),
    },

    necesidades_equipamiento: {
      coccion: splitCommas(getVal("equipos_coccion")),
      refrigeracion: splitCommas(getVal("equipos_refrigeracion")),
      lavado: splitCommas(getVal("equipos_lavado")),
      otros: splitCommas(getVal("equipos_otros")),
      preferencias_colocacion: getVal("preferencias_colocacion") || null,
      marcas_preferidas: splitCommas(getVal("marcas_preferidas")),
    },

    identidad_gastronomica: {
      identidad: getVal("identidad") || null,
      tipo_cocina: getVal("tipo_cocina") || null,
      estructura_menu: getCheckedValues("menu"),
      cantidad_platos: getNum("cantidad_platos"),
      ingredientes_frescos: getCheckedValues("fresco"),
      ingredientes_congelados: getCheckedValues("congelado"),
      cuarta_gama: splitCommas(getVal("cuarta_gama")),
      quinta_gama: splitCommas(getVal("quinta_gama")),
    },

    lavado: {
      platos: getNum("lavado_platos"),
      vasos: getNum("lavado_vasos"),
      copas: getNum("lavado_copas"),
      cubiertos: getNum("lavado_cubiertos"),
      tazas: getNum("lavado_tazas"),
      otros_utensilios: splitCommas(getVal("lavado_otros")),
      consideraciones: getCheckedValues("lavado_consid"),
    },

    refrigeracion: {
      primera_gama: {
        productos: splitCommas(getVal("g1_productos")),
        kg_aproximados: getNum("g1_kg"),
      },
      segunda_gama: {
        productos: splitCommas(getVal("g2_productos")),
        necesita_estanterias: getRadio("g2_estanterias") === "si",
      },
      tercera_gama: {
        productos: splitCommas(getVal("g3_productos")),
        kg_aproximados: getNum("g3_kg"),
      },
      cuarta_gama: {
        productos: splitCommas(getVal("g4_productos")),
        kg_aproximados: getNum("g4_kg"),
      },
      quinta_gama: {
        productos: splitCommas(getVal("g5_productos")),
      },
    },

    personal: {
      personas_en_cocina: getNum("personas_en_cocina"),
      roles: splitCommas(getVal("roles")),
    },

    escalabilidad: {
      puede_ampliar_carta: radioBool("ampliar_carta"),
      espacio_mas_equipamiento: radioBool("espacio_equipo"),
      instalacion_permite_mas_potencia: radioBool("mas_potencia"),
    },

    formacion: {
      requiere_formacion: getRadio("formacion") === "si",
      equipos_formacion: splitCommas(getVal("equipos_formacion")),
    },

    visita_fabrica: getRadio("visita_fabrica") === "si",

    equipos_manuales: {
      coccion: getManualEquipos("coccion"),
      refrigeracion: getManualEquipos("refrigeracion"),
      lavado: getManualEquipos("lavado"),
      horno: getManualEquipos("horno"),
    },
  };
}

// ─── Resumen ────────────────────────────────────────────

function buildResumen() {
  const data = buildFormularioJSON();
  const box = document.getElementById("resumen");

  function item(label, value) {
    if (value === null || value === undefined || value === "") return "";
    if (typeof value === "boolean") value = value ? "Sí" : "No";
    if (Array.isArray(value)) { value = value.length ? value.join(", ") : "—"; }
    return `<div class="resumen-item"><span class="resumen-label">${label}</span><span class="resumen-value">${value}</span></div>`;
  }

  let html = "";

  html += `<h4>Proyecto</h4>`;
  html += item("Nombre", data.proyecto.nombre);
  html += item("Tipo de negocio", data.proyecto.tipo_negocio);
  html += item("Concepto", data.proyecto.concepto);
  html += item("Comensales", data.proyecto.comensales);
  html += item("Superficie", data.proyecto.superficie_m2 ? data.proyecto.superficie_m2 + " m²" : null);
  html += item("Presupuesto", data.proyecto.presupuesto_max ? data.proyecto.presupuesto_max.toLocaleString("es-ES") + " EUR" : "Sin límite");

  html += `<h4>Parte Técnica</h4>`;
  html += item("Tipo proyecto", data.parte_tecnica.tipo_proyecto === "renovacion" ? "Renovación" : "Diseño desde cero");
  if (data.parte_tecnica.tipo_proyecto === "renovacion") html += item("Retirar antigua", data.parte_tecnica.retirar_cocina_antigua);
  html += item("Plano técnico", data.parte_tecnica.existe_plano_tecnico);
  const fileInput = document.getElementById("archivo_plano");
  if (fileInput?.files[0]) html += item("Archivo plano", fileInput.files[0].name);
  html += item("Altura techo", data.parte_tecnica.altura_suelo_techo_m ? data.parte_tecnica.altura_suelo_techo_m + " m" : null);
  html += item("Paredes", data.parte_tecnica.material_paredes);
  html += item("Suelo", data.parte_tecnica.material_suelo);
  if (data.parte_tecnica.desniveles_suelo.existe) html += item("Desniveles", data.parte_tecnica.desniveles_suelo.detalle || "Sí");
  if (data.parte_tecnica.dimensiones_accesos) {
    const acc = data.parte_tecnica.dimensiones_accesos;
    html += item("Accesos", Object.entries(acc).map(([k, v]) => `${k}: ${v}m`).join(", "));
  }

  html += `<h4>Energía</h4>`;
  html += item("Tipo", data.energia.tipo_energia);
  html += item("Gas", data.energia.tipo_gas);
  html += item("Caudal gas", data.energia.caudal_gas_disponible);
  html += item("Eléctrico", data.energia.tipo_electrico);
  html += item("Potencia", data.energia.potencia_contratada_kw ? data.energia.potencia_contratada_kw + " kW" : null);

  html += `<h4>Equipamiento</h4>`;
  html += item("Cocción", data.necesidades_equipamiento.coccion);
  html += item("Refrigeración", data.necesidades_equipamiento.refrigeracion);
  html += item("Lavado", data.necesidades_equipamiento.lavado);
  html += item("Otros", data.necesidades_equipamiento.otros);
  html += item("Colocación", data.necesidades_equipamiento.preferencias_colocacion);
  html += item("Marcas", data.necesidades_equipamiento.marcas_preferidas);

  // Equipos manuales
  const em = data.equipos_manuales || {};
  const allManual = [...(em.coccion || []), ...(em.refrigeracion || []), ...(em.lavado || []), ...(em.horno || [])];
  if (allManual.length) {
    html += `<h4>Equipos Manuales</h4>`;
    if (em.coccion?.length) html += item("Coccion", em.coccion.map(e => e.nombre + (e.cantidad > 1 ? " x" + e.cantidad : "")).join(", "));
    if (em.refrigeracion?.length) html += item("Refrigeracion", em.refrigeracion.map(e => e.nombre + (e.cantidad > 1 ? " x" + e.cantidad : "")).join(", "));
    if (em.lavado?.length) html += item("Lavado", em.lavado.map(e => e.nombre + (e.cantidad > 1 ? " x" + e.cantidad : "")).join(", "));
    if (em.horno?.length) html += item("Horno", em.horno.map(e => e.nombre + (e.cantidad > 1 ? " x" + e.cantidad : "")).join(", "));
  }

  html += `<h4>Identidad Gastronómica</h4>`;
  html += item("Identidad", data.identidad_gastronomica.identidad);
  html += item("Tipo cocina", data.identidad_gastronomica.tipo_cocina);
  html += item("Menú", data.identidad_gastronomica.estructura_menu);
  html += item("Nº platos", data.identidad_gastronomica.cantidad_platos);
  html += item("Frescos", data.identidad_gastronomica.ingredientes_frescos);
  html += item("Congelados", data.identidad_gastronomica.ingredientes_congelados);
  html += item("4ª gama", data.identidad_gastronomica.cuarta_gama);
  html += item("5ª gama", data.identidad_gastronomica.quinta_gama);

  html += `<h4>Lavado</h4>`;
  html += item("Platos", data.lavado.platos);
  html += item("Vasos", data.lavado.vasos);
  html += item("Copas", data.lavado.copas);
  html += item("Cubiertos", data.lavado.cubiertos);
  html += item("Tazas", data.lavado.tazas);
  html += item("Otros", data.lavado.otros_utensilios);
  html += item("Consideraciones", data.lavado.consideraciones);

  html += `<h4>Refrigeración</h4>`;
  const r = data.refrigeracion;
  if (r.primera_gama.productos.length) html += item("1ª gama", r.primera_gama.productos.join(", ") + (r.primera_gama.kg_aproximados ? ` (${r.primera_gama.kg_aproximados}kg)` : ""));
  if (r.segunda_gama.productos.length) html += item("2ª gama", r.segunda_gama.productos.join(", ") + (r.segunda_gama.necesita_estanterias ? " (estanterías: sí)" : ""));
  if (r.tercera_gama.productos.length) html += item("3ª gama", r.tercera_gama.productos.join(", ") + (r.tercera_gama.kg_aproximados ? ` (${r.tercera_gama.kg_aproximados}kg)` : ""));
  if (r.cuarta_gama.productos.length) html += item("4ª gama", r.cuarta_gama.productos.join(", ") + (r.cuarta_gama.kg_aproximados ? ` (${r.cuarta_gama.kg_aproximados}kg)` : ""));
  if (r.quinta_gama.productos.length) html += item("5ª gama", r.quinta_gama.productos.join(", "));

  html += `<h4>Personal y Escalabilidad</h4>`;
  html += item("Personas en cocina", data.personal.personas_en_cocina);
  html += item("Roles", data.personal.roles);
  html += item("Ampliar carta", data.escalabilidad.puede_ampliar_carta);
  html += item("Más equipamiento", data.escalabilidad.espacio_mas_equipamiento);
  html += item("Más potencia", data.escalabilidad.instalacion_permite_mas_potencia);
  html += item("Formación", data.formacion.requiere_formacion);
  if (data.formacion.requiere_formacion) html += item("Equipos formación", data.formacion.equipos_formacion);
  html += item("Visita fábrica", data.visita_fabrica);

  box.innerHTML = html;
}

// ─── Submit ─────────────────────────────────────────────

form.addEventListener("submit", async (e) => {
  e.preventDefault();

  const data = buildFormularioJSON();
  const baseUrl = getBaseUrl();
  const fileInput = document.getElementById("archivo_plano");
  const hasFile = fileInput?.files[0];

  document.getElementById("loadingModal").style.display = "flex";

  try {
    let response;

    if (hasFile) {
      const formData = new FormData();
      formData.append("formulario_json", JSON.stringify(data));
      formData.append("archivo", fileInput.files[0]);
      response = await fetch(baseUrl + "/generar-con-plano", { method: "POST", body: formData });
    } else {
      response = await fetch(baseUrl + "/generar", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
    }

    document.getElementById("loadingModal").style.display = "none";

    if (response.ok) {
      const blob = await response.blob();

      // Descargar el ZIP
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "propuesta.zip";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);

      // Extraer resultado.json del ZIP para mostrar comentario IA
      let resultado = null;
      try {
        const zip = await JSZip.loadAsync(blob);
        const jsonFile = zip.file("resultado.json");
        if (jsonFile) {
          resultado = JSON.parse(await jsonFile.async("string"));
        }
      } catch (e) {
        console.warn("No se pudo leer resultado.json del ZIP:", e);
      }

      showResult(true, "Propuesta generada correctamente. El archivo ZIP se ha descargado.", resultado);
    } else {
      const errText = await response.text();
      let errMsg;
      try { errMsg = JSON.parse(errText).detail || errText; } catch { errMsg = errText; }
      showResult(false, "Error del servidor: " + errMsg);
    }
  } catch (err) {
    document.getElementById("loadingModal").style.display = "none";
    showResult(false, "No se pudo conectar con el servidor.\nURL: " + baseUrl + "\n\nError: " + err.message);
  }
});

function showResult(success, message, resultado) {
  const modal = document.getElementById("resultModal");
  const content = document.getElementById("resultContent");
  const feedbackSection = document.getElementById("feedbackSection");
  const closeOnly = document.getElementById("resultCloseOnly");

  if (success && resultado) {
    // Exito con datos de la IA
    const equipos = resultado.total_equipos || 0;
    const pvp = resultado.total_pvp_eur || 0;
    const notas = resultado.notas_llm || "Propuesta generada correctamente.";
    const proyecto = resultado.proyecto || "";

    content.innerHTML = `
      <div class="result-success">&#10003;</div>
      <div class="result-msg"><strong>Propuesta generada${proyecto ? " para " + escapeHtml(proyecto) : ""}</strong></div>
      <div class="result-summary">
        <div class="result-stat">
          <div class="result-stat-value">${equipos}</div>
          <div class="result-stat-label">Equipos</div>
        </div>
        <div class="result-stat">
          <div class="result-stat-value">${pvp ? pvp.toLocaleString("es-ES") + " EUR" : "--"}</div>
          <div class="result-stat-label">Presupuesto est.</div>
        </div>
        <div class="result-stat">
          <div class="result-stat-value">${resultado.layout || "--"}</div>
          <div class="result-stat-label">Layout</div>
        </div>
      </div>
      <div class="ai-comment">
        <div class="ai-comment-label">Comentario de la IA</div>
        ${escapeHtml(notas)}
      </div>
    `;
    feedbackSection.style.display = "";
    closeOnly.style.display = "none";
  } else if (success) {
    // Exito sin datos
    content.innerHTML = `
      <div class="result-success">&#10003;</div>
      <div class="result-msg"><strong>Propuesta generada</strong></div>
      <pre>${escapeHtml(message)}</pre>
    `;
    feedbackSection.style.display = "";
    closeOnly.style.display = "none";
  } else {
    // Error
    content.innerHTML = `
      <div class="result-error">&#10007;</div>
      <div class="result-msg"><strong>Error</strong></div>
      <pre>${escapeHtml(message)}</pre>
    `;
    feedbackSection.style.display = "none";
    closeOnly.style.display = "";
  }

  // Reset feedback
  document.getElementById("feedbackText").value = "";
  document.getElementById("feedbackStatus").style.display = "none";

  modal.style.display = "flex";
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// ─── Feedback ────────────────────────────────────────────

document.getElementById("btnSendFeedback").addEventListener("click", async () => {
  const text = document.getElementById("feedbackText").value.trim();
  if (!text) return;

  const status = document.getElementById("feedbackStatus");
  const btn = document.getElementById("btnSendFeedback");
  btn.disabled = true;
  btn.textContent = "Aplicando cambios con IA...";
  status.innerHTML = "La IA esta procesando tu solicitud. Esto puede tardar unos segundos...";
  status.className = "feedback-status ok";
  status.style.display = "";

  try {
    const baseUrl = getBaseUrl();
    const resp = await fetch(baseUrl + "/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mensaje: text }),
    });

    if (resp.ok && resp.headers.get("content-type")?.includes("application/zip")) {
      // Recibimos un nuevo ZIP — descargar
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "propuesta_modificada.zip";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);

      // Extraer resultado.json del nuevo ZIP
      let nuevoResultado = null;
      try {
        const zip = await JSZip.loadAsync(blob);
        const jsonFile = zip.file("resultado.json");
        if (jsonFile) {
          nuevoResultado = JSON.parse(await jsonFile.async("string"));
        }
      } catch (e) { /* ignorar */ }

      // Actualizar el modal con la nueva info
      if (nuevoResultado) {
        const content = document.getElementById("resultContent");
        const equipos = nuevoResultado.total_equipos || 0;
        const pvp = nuevoResultado.total_pvp_eur || 0;
        const notas = nuevoResultado.notas_llm || "";
        const proyecto = nuevoResultado.proyecto || "";
        const fb = nuevoResultado.feedback_aplicado || text;

        content.innerHTML = `
          <div class="result-success">&#10003;</div>
          <div class="result-msg"><strong>Propuesta actualizada${proyecto ? " para " + escapeHtml(proyecto) : ""}</strong></div>
          <div class="result-summary">
            <div class="result-stat">
              <div class="result-stat-value">${equipos}</div>
              <div class="result-stat-label">Equipos</div>
            </div>
            <div class="result-stat">
              <div class="result-stat-value">${pvp ? pvp.toLocaleString("es-ES") + " EUR" : "--"}</div>
              <div class="result-stat-label">Presupuesto est.</div>
            </div>
            <div class="result-stat">
              <div class="result-stat-value">${nuevoResultado.layout || "--"}</div>
              <div class="result-stat-label">Layout</div>
            </div>
          </div>
          <div class="ai-comment">
            <div class="ai-comment-label">Cambio aplicado</div>
            ${escapeHtml(fb)}
          </div>
          ${notas ? `<div class="ai-comment"><div class="ai-comment-label">Comentario de la IA</div>${escapeHtml(notas)}</div>` : ""}
        `;
      }

      status.innerHTML = "<strong>Cambios aplicados.</strong> Se ha descargado el nuevo ZIP con la propuesta modificada.";
      status.className = "feedback-status ok";
      document.getElementById("feedbackText").value = "";
    } else {
      const errText = await resp.text();
      let errMsg;
      try { errMsg = JSON.parse(errText).detail || errText; } catch { errMsg = errText; }
      status.textContent = "Error: " + errMsg;
      status.className = "feedback-status err";
    }
  } catch (e) {
    status.textContent = "No se pudo conectar con el servidor: " + e.message;
    status.className = "feedback-status err";
  } finally {
    status.style.display = "";
    btn.disabled = false;
    btn.textContent = "Enviar solicitud";
  }
});

// ─── Import JSON ────────────────────────────────────────

document.getElementById("btnImportJson").addEventListener("click", () => {
  document.getElementById("importJsonFile").click();
});

document.getElementById("importJsonFile").addEventListener("change", (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = (ev) => {
    try {
      const data = JSON.parse(ev.target.result);
      fillFormFromJSON(data);
      document.getElementById("importStatus").textContent = "JSON cargado: " + file.name;
    } catch (err) {
      alert("Error al leer el JSON: " + err.message);
    }
  };
  reader.readAsText(file);
});

// ─── Import Excel ────────────────────────────────────────

document.getElementById("btnImportExcel").addEventListener("click", () => {
  document.getElementById("importExcelFile").click();
});

document.getElementById("importExcelFile").addEventListener("change", (e) => {
  const file = e.target.files[0];
  if (!file) return;
  if (typeof XLSX === "undefined") {
    alert("La libreria SheetJS no esta cargada. Verifica la conexion a internet.");
    return;
  }
  const reader = new FileReader();
  reader.onload = (ev) => {
    try {
      const data = new Uint8Array(ev.target.result);
      const workbook = XLSX.read(data, { type: "array" });
      const sheet = workbook.Sheets[workbook.SheetNames[0]];
      const rows = XLSX.utils.sheet_to_json(sheet, { header: 1, defval: null });
      const added = importEquiposFromExcelRows(rows);
      const total = Object.values(added).reduce((s, n) => s + n, 0);
      document.getElementById("importStatus").textContent =
        "Excel: " + total + " equipos cargados desde " + file.name;
      // Navegar al paso 5 si hay equipos
      if (total > 0 && currentStep < 5) {
        showStep(5);
      }
    } catch (err) {
      alert("Error al leer el Excel: " + err.message);
    }
  };
  reader.readAsArrayBuffer(file);
  // Reset so same file can be re-imported
  e.target.value = "";
});

// Mapa de palabras clave de zona Excel -> zona del formulario
const EXCEL_ZONA_MAP = [
  { key: "COCCION",      zone: "coccion"       },
  { key: "COCCIÓN",      zone: "coccion"       },
  { key: "LAVADO",       zone: "lavado"        },
  { key: "ALMACEN",      zone: "refrigeracion" },
  { key: "ALMACÉN",      zone: "refrigeracion" },
  { key: "FRIO",         zone: "refrigeracion" },
  { key: "FRÍO",         zone: "refrigeracion" },
  { key: "REFRIGERACION",zone: "refrigeracion" },
  { key: "REFRIGERACIÓN",zone: "refrigeracion" },
  { key: "HORNO",        zone: "horno"         },
  { key: "BARRA",        zone: null            }, // zona de barra: se omite
];

function importEquiposFromExcelRows(rows) {
  // Limpiar listas actuales
  ["coccion", "refrigeracion", "lavado", "horno"].forEach((zone) => {
    const list = document.getElementById("equipos" + capitalize(zone) + "List");
    if (list) list.innerHTML = "";
  });

  let currentZone = null;
  const added = { coccion: 0, refrigeracion: 0, lavado: 0, horno: 0 };

  for (const row of rows) {
    if (!Array.isArray(row)) continue;
    // Nombre en columna B (index 1), cantidad en columna C (index 2)
    const nameCell = row[1];
    const qtyCell  = row[2];
    if (nameCell == null) continue;

    const text = String(nameCell).trim();
    if (!text) continue;
    const textUp = text.toUpperCase();

    // Detectar cabecera de zona
    let isHeader = false;
    for (const { key, zone } of EXCEL_ZONA_MAP) {
      if (textUp.includes(key)) {
        currentZone = zone; // null = zona ignorada (barra, etc.)
        isHeader = true;
        break;
      }
    }
    if (isHeader) continue;
    if (currentZone === null) continue; // zona ignorada

    // Extraer nombre real (quitar prefijo tipo "1.01 ", "2.03 ")
    const codeMatch = text.match(/^\d+\.\d+\s+([\s\S]+)/);
    const equipName = codeMatch ? codeMatch[1].trim() : text;
    if (!equipName || equipName.toUpperCase() === "UNIDADES") continue;

    const qty = (typeof qtyCell === "number") ? Math.max(1, Math.round(qtyCell)) : 1;
    addEquipoRow(currentZone, equipName, qty);
    added[currentZone] = (added[currentZone] || 0) + 1;
  }

  console.log("[Excel] Equipos importados por zona:", added);
  return added;
}

function setVal(id, value) {
  const el = document.getElementById(id);
  if (el && value != null) el.value = value;
}

function setRadio(name, value) {
  if (value == null) return;
  // Map booleans to si/no
  if (value === true) value = "si";
  if (value === false) value = "no";
  const radio = document.querySelector(`input[name="${name}"][value="${value}"]`);
  if (radio) {
    radio.checked = true;
    radio.dispatchEvent(new Event("change", { bubbles: true }));
  }
}

function setCheckboxes(name, values) {
  if (!Array.isArray(values)) return;
  document.querySelectorAll(`input[name="${name}"]`).forEach((cb) => {
    cb.checked = values.includes(cb.value);
  });
}

function fillFormFromJSON(data) {
  // ─── 1. Proyecto ───
  const proy = data.proyecto || {};
  setVal("nombre_proyecto", proy.nombre);
  setVal("tipo_negocio", proy.tipo_negocio);
  setVal("concepto", proy.concepto);
  setVal("comensales", proy.comensales);
  setVal("superficie_m2", proy.superficie_m2);
  setVal("presupuesto_max", proy.presupuesto_max);

  // ─── 2. Parte Técnica ───
  const tec = data.parte_tecnica || {};
  setRadio("tipo_proyecto", tec.tipo_proyecto);
  setRadio("retirar_antigua", tec.retirar_cocina_antigua);
  setRadio("tiene_plano", tec.existe_plano_tecnico);
  setVal("altura_techo", tec.altura_suelo_techo_m);
  if (Array.isArray(tec.material_paredes)) setVal("material_paredes", tec.material_paredes.join(", "));
  setVal("material_suelo", tec.material_suelo);
  if (tec.desniveles_suelo) {
    setRadio("desniveles", tec.desniveles_suelo.existe);
    setVal("desnivel_detalle", tec.desniveles_suelo.detalle);
  }
  if (tec.dimensiones_accesos) {
    setVal("acceso_principal", tec.dimensiones_accesos.puerta_principal_m);
    setVal("acceso_cocina", tec.dimensiones_accesos.puerta_cocina_m);
    setVal("acceso_almacen", tec.dimensiones_accesos.puerta_almacen_m);
  }

  // ─── 3. Energía ───
  const ener = data.energia || {};
  setRadio("energia", ener.tipo_energia);
  setRadio("tipo_gas", ener.tipo_gas);
  setVal("caudal_gas", ener.caudal_gas_disponible);
  setRadio("tipo_electrico", ener.tipo_electrico);
  setVal("potencia_kw", ener.potencia_contratada_kw);

  // ─── 4. Equipamiento ───
  const neq = data.necesidades_equipamiento || {};
  if (Array.isArray(neq.coccion)) setVal("equipos_coccion", neq.coccion.join(", "));
  if (Array.isArray(neq.refrigeracion)) {
    setVal("equipos_refrigeracion", neq.refrigeracion.join(", "));
    // Activate refrigeracion checkbox
    const refriCb = document.querySelector('input[name="area_refrigeracion"]');
    if (refriCb && neq.refrigeracion.length) refriCb.checked = true;
  }
  if (Array.isArray(neq.lavado)) {
    setVal("equipos_lavado", neq.lavado.join(", "));
    // Activate lavado checkbox
    const lavCb = document.querySelector('input[name="area_lavado"]');
    if (lavCb && neq.lavado.length) lavCb.checked = true;
  }
  if (Array.isArray(neq.otros)) setVal("equipos_otros", neq.otros.join(", "));
  setVal("preferencias_colocacion", neq.preferencias_colocacion);
  if (Array.isArray(neq.marcas_preferidas)) setVal("marcas_preferidas", neq.marcas_preferidas.join(", "));

  // ─── 5. Identidad Gastronómica ───
  const ig = data.identidad_gastronomica || {};
  setVal("identidad", ig.identidad);
  setVal("tipo_cocina", ig.tipo_cocina);
  setCheckboxes("menu", ig.estructura_menu);
  setVal("cantidad_platos", ig.cantidad_platos);
  setCheckboxes("fresco", ig.ingredientes_frescos);
  setCheckboxes("congelado", ig.ingredientes_congelados);
  if (Array.isArray(ig.cuarta_gama)) setVal("cuarta_gama", ig.cuarta_gama.join(", "));
  if (Array.isArray(ig.quinta_gama)) setVal("quinta_gama", ig.quinta_gama.join(", "));

  // ─── 6. Lavado ───
  const lav = data.lavado || {};
  setVal("lavado_platos", lav.platos);
  setVal("lavado_vasos", lav.vasos);
  setVal("lavado_copas", lav.copas);
  setVal("lavado_cubiertos", lav.cubiertos);
  setVal("lavado_tazas", lav.tazas);
  if (Array.isArray(lav.otros_utensilios)) setVal("lavado_otros", lav.otros_utensilios.join(", "));
  setCheckboxes("lavado_consid", lav.consideraciones || []);

  // ─── 7. Refrigeración ───
  const ref = data.refrigeracion || {};
  if (ref.primera_gama) {
    if (Array.isArray(ref.primera_gama.productos)) setVal("g1_productos", ref.primera_gama.productos.join(", "));
    setVal("g1_kg", ref.primera_gama.kg_aproximados);
  }
  if (ref.segunda_gama) {
    if (Array.isArray(ref.segunda_gama.productos)) setVal("g2_productos", ref.segunda_gama.productos.join(", "));
    setRadio("g2_estanterias", ref.segunda_gama.necesita_estanterias);
  }
  if (ref.tercera_gama) {
    if (Array.isArray(ref.tercera_gama.productos)) setVal("g3_productos", ref.tercera_gama.productos.join(", "));
    setVal("g3_kg", ref.tercera_gama.kg_aproximados);
  }
  if (ref.cuarta_gama) {
    if (Array.isArray(ref.cuarta_gama.productos)) setVal("g4_productos", ref.cuarta_gama.productos.join(", "));
    setVal("g4_kg", ref.cuarta_gama.kg_aproximados);
  }
  if (ref.quinta_gama) {
    if (Array.isArray(ref.quinta_gama.productos)) setVal("g5_productos", ref.quinta_gama.productos.join(", "));
  }

  // ─── 8. Personal y Escalabilidad ───
  const per = data.personal || {};
  setVal("personas_en_cocina", per.personas_en_cocina);
  if (Array.isArray(per.roles)) setVal("roles", per.roles.join(", "));

  const esc = data.escalabilidad || {};
  setRadio("ampliar_carta", esc.puede_ampliar_carta);
  setRadio("espacio_equipo", esc.espacio_mas_equipamiento);
  setRadio("mas_potencia", esc.instalacion_permite_mas_potencia);

  const form_ = data.formacion || {};
  setRadio("formacion", form_.requiere_formacion);
  if (Array.isArray(form_.equipos_formacion)) setVal("equipos_formacion", form_.equipos_formacion.join(", "));

  setRadio("visita_fabrica", data.visita_fabrica);

  // ─── Equipos Manuales ───
  const eqm = data.equipos_manuales || {};
  ["coccion", "refrigeracion", "lavado", "horno"].forEach((zone) => {
    // Clear existing rows
    const list = document.getElementById("equipos" + capitalize(zone) + "List");
    if (list) list.innerHTML = "";
    // Add rows from JSON
    if (Array.isArray(eqm[zone])) {
      eqm[zone].forEach((eq) => addEquipoRow(zone, eq.nombre, eq.cantidad || 1));
    }
  });

  // Update conditional sections visibility
  updateConditionalSections();
}

// ─── Init ───────────────────────────────────────────────

showStep(1);
