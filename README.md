# YIMI — SaaS multi-tenant con agente GPT-4o

Servicio FastAPI con cola persistente y un único proveedor de IA: **GPT-4o**. Los demás módulos realizan solo funciones locales de cola, medición, seguridad y cobro; no llaman a otros modelos ni tienen herramientas para operar sistemas externos.

La organización toma como referencia conceptual la estructura de [ECO.IA](https://github.com/lio366/ECO.IA), pero se implementó en este repositorio y no se clonó su código. No encontré un repositorio público inequívoco llamado “Yimi cloud free”; para adoptar uno concreto necesito su URL.

## Capacidades

- `POST /v1/agent-runs` encola una tarea durable y responde `202`. El worker la procesa usando una única llamada a `gpt-4o`, con salida limitada, sin herramientas y sin reintentos automáticos del SDK. Envía un `Idempotency-Key` único por solicitud para recibir el mismo run si el cliente reintenta el POST; reutilizarlo con otro contenido devuelve `409`.
- `GET /v1/agent-runs/{id}` consulta estado y resultado de una tarea del tenant autenticado: `queued`, `processing`, `completed`, `pending_approval` o `failed`.
- `GET /v1/agent-runs/{id}/decisions` devuelve el ledger de decisiones por agente, con versión de contrato y evidencia acotada, validado por tenant.
- `GET /v1/billing/preview` muestra tokens de entrada/salida, costo estimado del proveedor, precio de venta por nivel y margen estimado para consumos todavía no facturados.
- `POST /v1/billing/checkout` cobra mediante Stripe solo tras consentimiento explícito y confirmación del importe exacto mostrado. El cliente confirma el pago en Stripe; nunca hay un cargo silencioso.
- `/v1/billing/webhook` verifica firma, factura, tenant, sesión, importe, moneda y estado de pago; evita procesar el mismo evento más de una vez.
- Endpoints administrativos muestran salud de worker, cola, tasa de éxito y p95 de duración de ejecución sobre hasta las últimas 5.000 ejecuciones exitosas; requieren `X-Admin-Key`. Prometheus lee su credencial de un archivo protegido configurado en `monitoring/prometheus.yml`.
- `GET /v1/admin/orchestrator/report` ofrece un resumen ejecutivo de salud, cola, consumo estimado y recomendaciones basadas en evidencia local. Es de solo lectura y no hace llamadas GPT adicionales, cambios de infraestructura ni acciones de pago.
- Las operaciones que pidan aprobación quedan en espera. La aprobación es informativa: el agente no dispone de herramientas para ejecutar acciones reales.

## Arranque local

Requiere Python 3.12 o posterior, una API key de OpenAI y PostgreSQL para persistencia compartida. SQLite se utiliza por defecto para desarrollo de un solo proceso.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TENANT_API_KEYS='{"empresa-demo":"clave-del-tenant"}'
export ADMIN_API_KEY='clave-admin'
export OPENAI_API_KEY='clave-de-openai'
uvicorn app.main:app --reload
```

La cola solo procesa tareas si `OPENAI_API_KEY` está configurada; sin ella, los endpoints de tarea devuelven `503`. Documentación interactiva: `http://localhost:8000/docs`.

Ejemplo de uso:

```bash
curl -X POST http://localhost:8000/v1/agent-runs \
  -H 'Content-Type: application/json' \
  -H 'X-Tenant-ID: empresa-demo' \
  -H "Authorization: $AUTH_HEADER" \
  -d '{"goal":"Resume las métricas de este servicio"}'
```

`AUTH_HEADER` debe contener la credencial HTTP completa para el tenant. La respuesta contiene un ID; consulta `GET /v1/agent-runs/{id}` hasta que finalice.

## Docker: worker 24/7 supervisado

```bash
./scripts/setup.sh
# Edita .env: configura claves aleatorias, OpenAI y la contraseña de Grafana.
./scripts/start.sh
docker compose ps
```

Compose mantiene dos procesos API/worker con `restart: unless-stopped`, una cola PostgreSQL persistente y Nginx. Cada worker reclama trabajos mediante una actualización atómica para evitar que ambas réplicas procesen la misma tarea. Trabajos que quedan atascados se marcan fallidos y no se repiten automáticamente: así se evita una segunda llamada/costo potencial. Si la conexión se pierde después de que OpenAI haya procesado una petición, el proveedor podría haber cobrado aunque YIMI no recibiera el uso para registrarlo; revisa esos fallos antes de reenviar.

Esto da operación supervisada mientras el host, Docker, red, OpenAI y base de datos estén disponibles; **no garantiza un SLA ni disponibilidad real 24/7**. En esta configuración Nginx y PostgreSQL siguen siendo puntos únicos de fallo. Para producción usa base de datos administrada con alta disponibilidad, backups probados, TLS, secretos administrados y monitoreo externo. No guardes `.env` en Git.

Grafana queda disponible solo en `http://127.0.0.1:3000`, con contraseña inicial definida por `GRAFANA_ADMIN_PASSWORD`; el dashboard se aprovisiona desde los archivos versionados. Cambia la contraseña de ejemplo antes de arrancar. Para acceso remoto, usa un proxy con TLS y autenticación, no publiques el puerto de Grafana directamente.

## Costos de GPT-4o y precios de venta

Los valores iniciales configurables de costo del proveedor son **US$2.50/1M tokens de entrada** y **US$10/1M tokens de salida**. Verifica los precios vigentes de [OpenAI](https://platform.openai.com/docs/pricing) antes de desplegar; los valores no se actualizan automáticamente. Una API key de OpenAI factura el uso al propietario de esa cuenta.

`TOKEN_PRICE_TIERS_JSON` define el margen sobre el costo vigente según tokens acumulados por tenant en el mes. La configuración de ejemplo aplica 30% desde 0 tokens, 35% desde 100.000 y 50% desde 500.000. El nuevo nivel se aplica a usos futuros, no altera facturas ya creadas. `GET /v1/billing/preview` enseña costo y precio antes de facturar.

Las tarifas usan microdólares para acumular fracciones de centavo. Stripe factura el total redondeado al centavo y los importes inferiores al mínimo de Stripe se acumulan. Para confirmar un pago, envía el importe exacto que devolvió la vista previa:

```json
{"consent": true, "approved_amount_cents": 100}
```

El ejemplo corresponde a US$1.00; usa el valor real del preview, no copies el ejemplo. Configura `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_SUCCESS_URL` y `STRIPE_CANCEL_URL`; prueba primero en modo de test. YIMI no almacena datos de tarjetas.

Límites protectores configurables:

- `MAX_OUTPUT_TOKENS=800`: máximo de salida por llamada GPT-4o.
- `MAX_MONTHLY_TOKENS_PER_TENANT=1000000`: presupuesto preventivo mensual por tenant; cuenta tokens consumidos y reserva conservadoramente caracteres de prompts y salida máxima para tareas en cola.
- `MAX_QUEUED_RUNS_PER_TENANT=10`: limita trabajos pendientes o procesándose a la vez por tenant.

Los cálculos de costo y margen se basan en tarifas configuradas, no en la factura final del proveedor; no garantizan ganancias. El gasto de infraestructura (servidor, base de datos, Stripe, red) es aparte del costo de tokens.

## Orquestación multiagente, recuperación y monitoreo

Cada run ejecuta etapas locales en orden: planificación, política de seguridad, diagnóstico de base de datos/cola, una llamada GPT‑4o, medición de recursos, estimación de precio y señales analíticas. Las decisiones se guardan en `agent_decisions` junto al resultado. El orquestador agrega las seis respuestas de etapa mediante eventos `agent.decision` versionados en una outbox SQL transaccional; las entregas se confirman, reintentan hasta tres veces con backoff y pasan a `dead_letter` al agotarse los intentos o expirar repetidamente la concesión. `/v1/admin/orchestrator/report` muestra colaboraciones verificadas y el tamaño de la DLQ.

Los seis módulos sí ejecutan lógica acotada en el worker compartido; no son seis servicios independientes ni seis llamadas a modelos. El agente de seguridad aplica un clasificador por palabras clave como revisión adicional, no como motor exhaustivo de políticas. Recursos mide CPU del hilo y memoria máxima del proceso (esta última no es asignación exclusiva por tarea). Analytics registra caracteres/tokens y métricas operativas, no calidad semántica. El agente DevOps diagnostica y señala trabajos atascados; no modifica infraestructura. Ningún agente puede usar herramientas externas o aprobar operaciones privilegiadas.

Los prompts se envían a OpenAI para generar respuestas. No se envían herramientas ni permisos para realizar compras, trading, despliegues u operaciones externas. Un fallo del proveedor marca la tarea como `failed` sin reintento automático.

`GET /health` prueba la base de datos. `/v1/admin/health` informa heartbeats obsoletos después de 120 segundos; `/v1/admin/metrics` muestra tokens y costos estimados. Compose incluye Prometheus con retención persistente de 15 días y reglas para réplicas API caídas, heartbeats obsoletos y acumulación de la cola. Las métricas exponen también el total de tareas en espera y en proceso.

`scripts/start.sh` crea `monitoring/secrets/admin-key` con permisos restrictivos a partir de `ADMIN_API_KEY`. Usa para esa variable un valor aleatorio sin comillas ni espacios, igual que en `.env`; nunca guardes el archivo generado en Git. Prometheus no expone un puerto al host; las reglas disparan alertas visibles en su interfaz interna, pero **no envían notificaciones externas** hasta integrar y configurar un receptor de Alertmanager. Las métricas son agregados de la base de datos compartida, no una prueba independiente de disponibilidad del host.

Los scripts `scripts/setup.sh` y `scripts/start.sh` preparan directorios y arranque local; `scripts/backup.sh` genera un dump PostgreSQL en `backups/`. Un dump creado no demuestra que sea recuperable: prueba la restauración en una base aislada antes de depender de él. El catálogo `config/agents_config.yaml` distingue módulos existentes de agentes autónomos completos; sirve como inventario y no habilita capacidades runtime.

El informe del orquestador etiqueta con precisión los módulos especialistas como locales, observabilidad o guardrails; no afirma que sean workers autónomos separados. Los planes se presentan como recomendaciones, no se ejecutan. Los costos y precios son estimaciones registradas; el reporte no verifica pagos recibidos ni rentabilidad neta.

## Pruebas

```bash
python -m unittest discover -s tests -v
```

Las pruebas automatizadas verifican el pipeline de seis etapas con cliente GPT simulado, aislamiento tenant, persistencia de decisiones/outbox, coordinación de eventos, duplicados, reintentos, leases y DLQ. El uso del cliente simulado prueba la lógica de integración, no acredita conectividad real, exactitud de facturación OpenAI, Stripe Live, restauración de backups o comportamiento en producción. Ejecuta una prueba de proveedor real solo con credenciales de prueba y límites de gasto, y nunca incluyas secretos en commits o informes.
