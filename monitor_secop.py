import os
import sys
import json
import time
import html
from datetime import datetime, timedelta
import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import ServerError
from pydantic import BaseModel, Field

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATOS_GOV_TOKEN = os.getenv("DATOS_GOV_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

class EvaluacionLicitacion(BaseModel):
    es_viable: bool = Field(description="Indica si la empresa cumple con el objeto y alcance principal")
    score_afinidad: int = Field(description="Puntuación de afinidad de 0 a 100")
    analisis_justificacion: str = Field(description="Explicación concisa de por qué aplica o por qué se descarta")
    riesgos_o_requisitos_clave: list[str] = Field(description="Aspectos críticos a revisar en los pliegos o cotización")

client = genai.Client(api_key=GEMINI_API_KEY)

def enviar_telegram(mensaje_html):
    """Envía una notificación formateada en HTML a Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Credenciales de Telegram no configuradas; omitiendo envío.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error enviando mensaje a Telegram: {e}")

def cargar_empresas(ruta_archivo="empresas.json"):
    """Carga la configuración de las empresas desde el archivo JSON."""
    if not os.path.exists(ruta_archivo):
        print(f"Error: No se encontró el archivo '{ruta_archivo}'.")
        sys.exit(1)
    with open(ruta_archivo, "r", encoding="utf-8") as f:
        return json.load(f)

def consultar_secop(empresa):
    """Consulta procesos vigentes en SECOP II usando los criterios específicos de una empresa."""
    endpoint = "https://www.datos.gov.co/resource/p6dx-8zbt.json"
    
    p_min = empresa.get("presupuesto_min", 0)
    p_max = empresa.get("presupuesto_max", 999999999999)
    dias = empresa.get("dias_atras", 15)
    codigos_objetivo = empresa.get("codigos_unspsc", [])
    palabras_clave = empresa.get("palabras_clave", [])
    
    hoy_iso = datetime.now().strftime("%Y-%m-%dT00:00:00")
    fecha_limite = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00")
    
    condiciones_tema = []
    for c in codigos_objetivo:
        condiciones_tema.append(f"codigo_principal_de_categoria like '{c}%'")
        condiciones_tema.append(f"codigo_principal_de_categoria like 'V1.{c}%'")
    
    for kw in palabras_clave:
        kw_limpia = kw.lower().replace("'", "''")
        condiciones_tema.append(f"lower(descripci_n_del_procedimiento) like '%{kw_limpia}%'")
        condiciones_tema.append(f"lower(nombre_del_procedimiento) like '%{kw_limpia}%'")
    
    if not condiciones_tema:
        return []
        
    filtro_tema = " OR ".join(condiciones_tema)

    filtro_presupuesto = (
        f"((precio_base >= {p_min} AND precio_base <= {p_max}) "
        "OR precio_base = 0 OR precio_base IS NULL)"
    )

    condicion_etapa_temprana = (
        f"fecha_de_recepcion_de >= '{hoy_iso}' OR "
        "lower(fase) like '%planeaci%' OR "
        "lower(fase) like '%borrador%' OR "
        "lower(fase) like '%previo%' OR "
        "lower(estado_del_procedimiento) like '%borrador%' OR "
        "lower(estado_del_procedimiento) like '%planeaci%' OR "
        "lower(modalidad_de_contratacion) like '%solicitud de informaci%' OR "
        "lower(modalidad_de_contratacion) like '%estudio de mercado%' OR "
        "lower(modalidad_de_contratacion) like '%cotizaci%' OR "
        "lower(nombre_del_procedimiento) like '%estudio de mercado%' OR "
        "lower(nombre_del_procedimiento) like '%cotizaci%'"
    )

filtro_vigencia = (
        f"fecha_de_ultima_publicaci >= '{fecha_limite}' AND "
        "(modalidad_de_contratacion not like '%Directa%' OR modalidad_de_contratacion IS NULL) AND "
        "estado_del_procedimiento != 'Adjudicado' AND "
        "estado_del_procedimiento != 'Cancelado' AND "
        f"({condicion_etapa_temprana})"
    )

    params = {
        "$where": f"{filtro_vigencia} AND {filtro_presupuesto} AND ({filtro_tema})",
        "$order": "fecha_de_publicacion_del DESC",
        "$limit": 40
    }
    
    headers = {}
    if DATOS_GOV_TOKEN:
        headers["X-App-Token"] = DATOS_GOV_TOKEN

    try:
        response = requests.get(endpoint, params=params, headers=headers, timeout=20)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error consultando SECOP II: {e}")
        return []

def evaluar_con_gemini(proceso, empresa):
    """Evalúa un proceso frente al perfil y límites de la empresa indicada."""
    titulo = proceso.get('nombre_del_procedimiento') or 'Sin título'
    fecha_cierre = proceso.get('fecha_de_recepcion_de') or 'No definida (Etapa preliminar / Estudio previo)'
    p_min = empresa.get("presupuesto_min", 0)
    p_max = empresa.get("presupuesto_max", 0)
    
    try:
        valor_num = float(proceso.get('precio_base') or 0)
        cuantia_texto = f"${valor_num:,.0f} COP" if valor_num > 0 else "Por definir (Estudio previo / Cotización)"
    except ValueError:
        cuantia_texto = "Por definir"

    prompt = f"""
    Evalúa la siguiente oportunidad de contratación pública frente al perfil de la empresa:
    
    REGLAS IMPORTANTES DE EVALUACIÓN:
    1. Si el proceso está en estudios previos, planeación, borrador, solicitud de cotización o estudio de mercado, es una oportunidad ALTA de posicionamiento temprano: evalúala positivamente si el objeto técnico coincide con el perfil.
    2. El rango presupuestal objetivo de esta empresa es entre ${p_min:,.0f} y ${p_max:,.0f} COP (o cuantía aún por definir en etapa preliminar).
    3. Descarta si el objeto no corresponde al perfil técnico de la empresa o si es un reporte histórico de un contrato ya ejecutado en meses pasados.
    
    [PERFIL EMPRESA: {empresa.get('nombre')}]
    {empresa.get('perfil')}
    
    [DATOS DE LA LICITACIÓN]
    - Entidad: {proceso.get('nombre_entidad', 'N/A')}
    - Número de proceso: {proceso.get('referencia_del_proceso', 'N/A')}
    - Título del proceso: {titulo}
    - Objeto detallado: {proceso.get('descripci_n_del_procedimiento', 'N/A')}
    - Modalidad: {proceso.get('modalidad_de_contratacion', 'N/A')}
    - Fase / Estado: {proceso.get('fase', 'N/A')} / {proceso.get('estado_del_procedimiento', 'N/A')}
    - Fecha límite recepción de ofertas: {fecha_cierre}
    - Categoría UNSPSC: {proceso.get('codigo_principal_de_categoria', 'N/A')}
    - Cuantía base: {cuantia_texto}
    """

    modelos_candidatos = ["gemini-2.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash"]

    for modelo in modelos_candidatos:
        for _ in range(2):
            try:
                response = client.models.generate_content(
                    model=modelo,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=EvaluacionLicitacion,
                        temperature=0.2,
                    ),
                )
                return response.parsed
            except ServerError:
                time.sleep(2)
            except Exception:
                break

    return EvaluacionLicitacion(
        es_viable=False,
        score_afinidad=0,
        analisis_justificacion="No fue posible evaluar en este momento.",
        riesgos_o_requisitos_clave=[]
    )

def procesar_empresa(empresa):
    """Ejecuta el ciclo de búsqueda, análisis y alerta en Telegram para una empresa."""
    nombre = empresa.get("nombre", "Empresa sin nombre")
    score_min = empresa.get("score_minimo", 70)

    print(f"\n🏢 Analizando empresa: {nombre}...")
    procesos = consultar_secop(empresa)
    print(f"   Candidatos encontrados: {len(procesos)}")

    oportunidades_viables = []

    for p in procesos:
        fecha_pub = (p.get('fecha_de_publicacion_del') or 'Sin fecha')[:10]
        fecha_cierre = (p.get('fecha_de_recepcion_de') or 'Por definir')[:10]
        referencia = p.get('referencia_del_proceso') or 'Sin ref'
        titulo = p.get('nombre_del_procedimiento') or 'Sin título especificado'
        entidad = p.get('nombre_entidad') or 'Desconocida'
        modalidad = p.get('modalidad_de_contratacion') or 'Sin modalidad'
        fase = p.get('fase') or 'Sin fase'
        url_proceso = (p.get('urlproceso') or {}).get('url', 'https://community.secop.gov.co/')

        try:
            valor_num = float(p.get('precio_base') or 0)
            cuantia = f"${valor_num:,.0f} COP" if valor_num > 0 else "Por definir"
        except ValueError:
            cuantia = "Por definir"

        analisis = evaluar_con_gemini(p, empresa)

        if analisis.es_viable and analisis.score_afinidad >= score_min:
            print(f"   ✅ Viable [{analisis.score_afinidad}/100]: {referencia}")
            oportunidades_viables.append(p)

            # Formatear mensaje seguro en HTML para Telegram
            msg = (
                f"🚨 <b>OPORTUNIDAD SECOP II ({analisis.score_afinidad}/100)</b>\n"
                f"🏢 <b>Empresa:</b> {html.escape(nombre)}\n"
                f"📌 <b>Proceso:</b> <code>{html.escape(referencia)}</code>\n"
                f"🏛 <b>Entidad:</b> {html.escape(entidad)}\n"
                f"💰 <b>Cuantía:</b> {html.escape(cuantia)}\n"
                f"📋 <b>Fase / Mod:</b> {html.escape(fase)} | {html.escape(modalidad)}\n"
                f"📅 <b>Pub:</b> {fecha_pub} | <b>Cierre:</b> {fecha_cierre}\n\n"
                f"📝 <b>Objeto:</b> {html.escape(titulo[:300])}\n\n"
                f"💡 <b>Análisis IA:</b> {html.escape(analisis.analisis_justificacion)}\n\n"
                f"🔗 <a href=\"{url_proceso}\">Ver proceso en SECOP II</a>"
            )
            enviar_telegram(msg)
            time.sleep(1)

    return len(oportunidades_viables)

def ejecutar():
    empresas = cargar_empresas("empresas.json")
    filtro_id = sys.argv[1].lower() if len(sys.argv) > 1 else None

    empresas_a_procesar = [
        e for e in empresas
        if e.get("activa", True) and (filtro_id is None or e.get("id", "").lower() == filtro_id)
    ]

    total_viables = 0
    for empresa in empresas_a_procesar:
        total_viables += procesar_empresa(empresa)

    resumen = f"🏁 Ejecución finalizada. Total oportunidades enviadas a Telegram: {total_viables}"
    print(f"\n{resumen}")
    if total_viables == 0:
        enviar_telegram("🤖 <b>Monitor SECOP II:</b> Ejecución diaria completada sin novedades hoy.")

if __name__ == "__main__":
    ejecutar()
