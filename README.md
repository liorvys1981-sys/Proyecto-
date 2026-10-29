# YIMI — SaaS multi-tenant de agentes autónomos

API FastAPI inspirada en la organización de alto nivel de [ECO.IA](https://github.com/lio366/ECO.IA): agentes, API, configuración, base de datos, Docker, monitoreo y pruebas. Se adaptó la estructura; no se clonó ni copió el código fuente del otro repositorio.

## Estructura

```text
agents/       analítica, riesgos, recursos, monetización y orquestación
api/          rutas HTTP, autenticación, cobro y aprobación
app/          punto de entrada FastAPI y ciclo de vida supervisado
config/       tarifas y lectura de variables de entorno
database/     modelos y sesiones SQLAlchemy
docker/       Compose y proxy inverso
monitoring/   salud de agentes y configuración Prometheus
tests/        pruebas automatizadas
```

## Funcionalidad y límites

- `POST /v1/agent-runs`: ejecuta análisis y evaluación simulados; mide CPU y memoria del proceso por agente, y registra uso/costos por tenant en PostgreSQL.
- `GET /v1/agent-runs/{id}`: lectura limitada al tenant autenticado.
- `GET /v1/billing/preview`: muestra cargos pendientes, CPU por función, costos operativos estimados y margen estimado.
- `POST /v1/billing/checkout`: crea Checkout de Stripe **solo** si el cliente envía `consent: true`, confirma el importe exacto de la vista previa y Stripe está configurado. Se conserva la fecha de consentimiento. El cliente confirma el pago en Stripe; la API no cobra silenciosamente.
- `POST /v1/billing/webhook`: valida firma y verifica importe, moneda e ID de sesión antes de marcar una factura pagada. Los eventos repetidos son idempotentes.
- `/v1/admin/health`, `/v1/admin/agents`, `/v1/admin/metrics` y `/metrics`: salud, alertas por heartbeat vencido y métricas globales; requieren clave administrativa.
- `requires_approval: true` deja la acción en espera. Un administrador puede aprobar **solo la simulación**; aún no se ejecutan acciones externas.

El uso de CPU se mide por hilo; la memoria pico es una métrica global de proceso visible solo para administradores. El margen y los costos son estimaciones, no medición de energía ni garantía de beneficios. Los agentes son simulados; el proceso mantiene health checks pero no ejecuta tareas autónomas programadas en segundo plano, ni ofrece IA, trading o ejecución de tareas externas.

## Desarrollo local

Requiere Python 3.12 o posterior y PostgreSQL para persistencia compartida. SQLite se usa de forma predeterminada solo para desarrollo de un proceso.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TENANT_API_KEYS='{"empresa-demo":"clave-local"}'
export ADMIN_API_KEY='clave-admin-local'
uvicorn app.main:app --reload
```

Abre `http://localhost:8000/docs`. Genera claves con `python -c "import secrets; print(secrets.token_urlsafe(32))"`; no reutilices las claves de ejemplo.

## Docker con dos API y PostgreSQL persistente

```bash
cp .env.example .env
# Sustituye las claves de ejemplo; configura las credenciales de Stripe solo si quieres usar Checkout.
docker compose --env-file .env -f docker/docker-compose.yml up --build -d
docker compose --env-file .env -f docker/docker-compose.yml ps
```

El Compose inicia PostgreSQL con volumen, dos réplicas de API, Nginx, límites de CPU/memoria y políticas `restart: unless-stopped`. El volumen sobrevive a reinicios del contenedor, pero no sustituye copias de seguridad. La base de datos es una única instancia: alta disponibilidad real requiere PostgreSQL administrado/redundante, backups probados, TLS, secretos externos y un orquestador con health checks.

No uses las claves de `.env.example` en producción. `TENANT_API_KEYS` es un objeto JSON de ID de tenant a clave. `ADMIN_API_KEY` protege endpoints administrativos. No guardes `.env` ni secretos en el repositorio.

## Tarifas y medición

`PRICE_CENTS_PER_CPU_SECOND` y `COST_CENTS_PER_CPU_SECOND` son objetos JSON por función, por ejemplo `{"goal_analysis":2,"risk_assessment":2}`. Importes en centavos USD por segundo de CPU, redondeados hacia arriba al centavo. La vista previa acumula uso aún no facturado; los cargos menores al mínimo de Stripe se pueden acumular antes del Checkout.

Para cobro real, configura `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_SUCCESS_URL` y `STRIPE_CANCEL_URL`; registra en Stripe el webhook `/v1/billing/webhook` para `checkout.session.completed` y `checkout.session.expired`. Envía a `/v1/billing/checkout` el consentimiento y el importe exacto mostrado en la vista previa (por ejemplo, `{"consent":true,"approved_amount_cents":100}`); si el importe cambió, vuelve a mostrar la vista previa antes de confirmar. Prueba primero en modo de prueba de Stripe. No se guarda ningún dato de tarjeta.

## Operación y monitoreo

`GET /health` prueba conexión a la base. El proceso de API mantiene heartbeats; si un agente deja de reportar por más de 120 segundos, el endpoint administrativo lo marca como obsoleto. El orquestador nunca reintenta acciones externas porque todavía no las ejecuta. Los errores se registran en la respuesta del servicio, no se ocultan como éxitos.

`monitoring/prometheus.yml` muestra cómo consultar `/metrics`; configura el archivo `/etc/prometheus/secrets/admin-key` protegido con el mismo valor de `ADMIN_API_KEY`. No expongas directamente métricas administrativas a Internet.

`docker compose` aporta reinicios y dos procesos API, no garantiza un SLA 24/7: proxy y PostgreSQL local siguen siendo puntos únicos de fallo. La creación automática de tablas es una comodidad inicial; usa migraciones versionadas antes de actualizar esquemas en producción.

## Pruebas

```bash
python -m unittest discover -s tests -v
```
