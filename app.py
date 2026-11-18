import os
import json
import html
import base64
import requests
import streamlit as st

# Cargar secretos de Streamlit en el entorno ANTES de importar monitor_secop
for key, value in st.secrets.items():
    if isinstance(value, str):
        os.environ[key] = value

from monitor_secop import consultar_secop, evaluar_con_gemini, enviar_telegram

st.set_page_config(page_title="Monitor SECOP II", page_icon="🏛️", layout="wide")

# --- AUTENTICACIÓN ---
APP_PASSWORD = st.secrets.get("APP_PASSWORD", os.getenv("APP_PASSWORD", "admin123"))

if "autenticado" not in st.session_state:
    st.session_state.autenticado = False

if not st.session_state.autenticado:
    st.title("🔐 Acceso al Panel SECOP II")
    pwd = st.text_input("Contraseña de acceso:", type="password")
    if st.button("Ingresar", type="primary"):
        if pwd == APP_PASSWORD:
            st.session_state.autenticado = True
            st.rerun()
        else:
            st.error("Contraseña incorrecta.")
    st.stop()

# --- CONEXIÓN CON GITHUB ---
GITHUB_TOKEN = st.secrets.get("GITHUB_TOKEN", os.getenv("GITHUB_TOKEN"))
GITHUB_REPO = st.secrets.get("GITHUB_REPO", os.getenv("GITHUB_REPO", "carbotekco-png/secop-gemini-bot"))
FILE_PATH = "empresas.json"

def cargar_empresas_github():
    if not GITHUB_TOKEN:
        with open(FILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f), None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{FILE_PATH}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    contenido = base64.b64decode(data["content"]).decode("utf-8")
    return json.loads(contenido), data["sha"]

def guardar_empresas_github(empresas_lista, sha):
    if not GITHUB_TOKEN:
        with open(FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(empresas_lista, f, indent=2, ensure_ascii=False)
        return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{FILE_PATH}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    contenido_b64 = base64.b64encode(json.dumps(empresas_lista, indent=2, ensure_ascii=False).encode("utf-8")).decode("utf-8")
    payload = {
        "message": "Actualización de criterios desde Panel Web Streamlit",
        "content": contenido_b64,
        "sha": sha
    }
    resp = requests.put(url, headers=headers, json=payload, timeout=15)
    resp.raise_for_status()

# --- INTERFAZ PRINCIPAL ---
st.title("🏛️ Panel de Control — Monitor Inteligente SECOP II")

try:
    empresas, file_sha = cargar_empresas_github()
except Exception as e:
    st.error(f"Error cargando empresas.json desde GitHub: {e}")
    st.stop()

tab_config, tab_buscar = st.tabs(["⚙️ Configurar Empresas y Criterios", "🔍 Ejecutar Búsqueda Manual"])

with tab_config:
    opciones = [f"{e['nombre']} ({e['id']})" for e in empresas] + ["➕ Crear nueva empresa"]
    seleccion = st.selectbox("Selecciona una empresa para editar:", opciones)

    es_nueva = seleccion == "➕ Crear nueva empresa"
    if es_nueva:
        emp_actual = {
            "id": "",
            "nombre": "",
            "activa": True,
            "presupuesto_min": 50000000,
            "presupuesto_max": 600000000,
            "dias_atras": 15,
            "score_minimo": 70,
            "codigos_unspsc": [],
            "palabras_clave": [],
            "perfil": ""
        }
        idx_actual = None
    else:
        idx_actual = opciones.index(seleccion)
        emp_actual = empresas[idx_actual]

    with st.form("form_empresa"):
        col1, col2, col3 = st.columns([2, 1, 1])
        nombre = col1.text_input("Nombre de la empresa", value=emp_actual.get("nombre", ""))
        emp_id = col2.text_input("ID corto (sin espacios)", value=emp_actual.get("id", ""), disabled=not es_nueva)
        activa = col3.checkbox("Empresa activa", value=emp_actual.get("activa", True))

        col4, col5, col6, col7 = st.columns(4)
        p_min = col4.number_input("Presupuesto Mínimo (COP)", min_value=0, step=5000000, value=int(emp_actual.get("presupuesto_min", 50000000)))
        p_max = col5.number_input("Presupuesto Máximo (COP)", min_value=0, step=10000000, value=int(emp_actual.get("presupuesto_max", 600000000)))
        dias_atras = col6.number_input("Ventana de búsqueda (días)", min_value=1, max_value=90, value=int(emp_actual.get("dias_atras", 15)))
        score_min = col7.slider("Afinidad mínima IA", min_value=50, max_value=95, value=int(emp_actual.get("score_minimo", 70)))

        codigos_str = st.text_input(
            "Códigos UNSPSC (separados por coma)",
            value=", ".join(emp_actual.get("codigos_unspsc", []))
        )
        palabras_str = st.text_input(
            "Palabras clave adicionales (separadas por coma)",
            value=", ".join(emp_actual.get("palabras_clave", []))
        )
        perfil = st.text_area(
            "Perfil técnico y comercial para evaluación con Gemini",
            value=emp_actual.get("perfil", ""),
            height=130
        )

        guardar = st.form_submit_button("💾 Guardar cambios en GitHub", type="primary")

        if guardar:
            if not nombre.strip() or not emp_id.strip():
                st.warning("Debes ingresar tanto el nombre como el ID de la empresa.")
            else:
                registro_actualizado = {
                    "id": emp_id.strip().lower().replace(" ", "_"),
                    "nombre": nombre.strip(),
                    "activa": activa,
                    "presupuesto_min": int(p_min),
                    "presupuesto_max": int(p_max),
                    "dias_atras": int(dias_atras),
                    "score_minimo": int(score_min),
                    "codigos_unspsc": [c.strip() for c in codigos_str.split(",") if c.strip()],
                    "palabras_clave": [k.strip() for k in palabras_str.split(",") if k.strip()],
                    "perfil": perfil.strip()
                }
                if es_nueva:
                    empresas.append(registro_actualizado)
                else:
                    empresas[idx_actual] = registro_actualizado

                try:
                    guardar_empresas_github(empresas, file_sha)
                    st.success("✅ Configuración guardada y sincronizada con GitHub correctamente.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error al guardar en GitHub: {e}")

with tab_buscar:
    st.subheader("Consulta inmediata en SECOP II")
    emp_nombres = [e["nombre"] for e in empresas if e.get("activa", True)]
    if not emp_nombres:
        st.warning("No hay empresas activas configuradas.")
    else:
        emp_elegida_nombre = st.selectbox("Empresa a consultar:", emp_nombres)
        enviar_tg = st.checkbox("Enviar también las oportunidades viables a Telegram", value=False)

        if st.button("🚀 Buscar y evaluar ahora", type="primary"):
            emp_obj = next(e for e in empresas if e["nombre"] == emp_elegida_nombre)
            with st.spinner("Consultando SECOP II y analizando con Gemini..."):
                candidatos = consultar_secop(emp_obj)
                st.info(f"Se encontraron {len(candidatos)} procesos candidatos en SECOP II.")

                viables = 0
                for p in candidatos:
                    analisis = evaluar_con_gemini(p, emp_obj)
                    if analisis.es_viable and analisis.score_afinidad >= emp_obj.get("score_minimo", 70):
                        viables += 1
                        referencia = p.get("referencia_del_proceso") or "Sin ref"
                        entidad = p.get("nombre_entidad") or "Desconocida"
                        titulo = p.get("nombre_del_procedimiento") or "Sin título"
                        fase = p.get("fase") or "Sin fase"
                        modalidad = p.get("modalidad_de_contratacion") or "Sin modalidad"
                        fecha_pub = (p.get("fecha_de_publicacion_del") or "Sin fecha")[:10]
                        fecha_cierre = (p.get("fecha_de_recepcion_de") or "Por definir")[:10]
                        url_p = (p.get("urlproceso") or {}).get("url", "https://community.secop.gov.co/")
                        cuantia = f"${float(p.get('precio_base') or 0):,.0f} COP" if float(p.get('precio_base') or 0) > 0 else "Por definir"

                        with st.expander(f"✅ [{analisis.score_afinidad}/100] {referencia} — {entidad}", expanded=True):
                            st.markdown(f"**Cuantía:** {cuantia} | **Fase:** {fase} | **Cierre:** {fecha_cierre}")
                            st.markdown(f"**Objeto:** {titulo}")
                            st.markdown(f"**Análisis IA:** {analisis.analisis_justificacion}")
                            if analisis.riesgos_o_requisitos_clave:
                                st.markdown("**Puntos críticos:** " + " • ".join(analisis.riesgos_o_requisitos_clave))
                            st.markdown(f"[Abrir proceso en SECOP II]({url_p})")

                        if enviar_tg:
                            riesgos_html = ""
                            if analisis.riesgos_o_requisitos_clave:
                                items = "\n".join(f"  • {html.escape(r)}" for r in analisis.riesgos_o_requisitos_clave)
                                riesgos_html = f"\n\n⚠️ <b>Puntos críticos a revisar:</b>\n{items}"

                            msg = (
                                f"🚨 <b>OPORTUNIDAD SECOP II ({analisis.score_afinidad}/100)</b>\n"
                                f"🏢 <b>Empresa:</b> {html.escape(emp_obj['nombre'])}\n"
                                f"📌 <b>Proceso:</b> <code>{html.escape(referencia)}</code>\n"
                                f"🏛 <b>Entidad:</b> {html.escape(entidad)}\n"
                                f"💰 <b>Cuantía:</b> {html.escape(cuantia)}\n"
                                f"📋 <b>Fase / Mod:</b> {html.escape(fase)} | {html.escape(modalidad)}\n"
                                f"📅 <b>Pub:</b> {fecha_pub} | <b>Cierre:</b> {fecha_cierre}\n\n"
                                f"📝 <b>Objeto:</b> {html.escape(titulo[:300])}\n\n"
                                f"💡 <b>Análisis IA:</b> {html.escape(analisis.analisis_justificacion)}"
                                f"{riesgos_html}\n\n"
                                f"🔗 <a href=\"{url_p}\">Ver proceso en SECOP II</a>"
                            )
                            enviar_telegram(msg)

                if viables == 0:
                    st.warning("Ningún proceso superó el puntaje mínimo de afinidad para esta empresa.")
