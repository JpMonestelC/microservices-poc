"""
Processing Service
-------------------
Microservicio responsable de "procesar" una orden ya creada: simula la
validación de inventario y el cobro de pago para esa orden.

Punto clave de diseño: este servicio NO conoce nada sobre clientes ni sobre
cómo se crean las órdenes. Solo recibe los datos mínimos necesarios para
procesar (id de la orden, id de cliente, items, total) y responde con un
resultado (aprobado/rechazado). No llama a Customer Service ni a Order
Service, ni sabe que existen — es Order Service quien lo llama a él. Esto
demuestra bajo acoplamiento (loose coupling) e independencia de despliegue:
se podría reescribir este servicio en otro lenguaje/tecnología sin que los
otros dos se enteren, siempre que se respete el contrato REST.

Es también el servicio elegido para las dos demostraciones de arquitectura
más importantes del POC:

- Escalabilidad independiente: en docker-compose.yml este es el único
  servicio sin `container_name` fijo y sin puerto de host publicado
  (`expose` en vez de `ports`), justamente para poder levantar varias
  réplicas con `docker compose up --scale processing-service=3` sin
  conflictos de nombre ni de puerto.
- Diseño para el fallo / tolerancia a fallas: puede simular rechazos (monto
  muy alto, o el flag explícito `simulate_failure`), y si el contenedor se
  detiene por completo, es Order Service quien debe manejar esa ausencia
  (ver order-service/main.py, función `_send_to_processing`).
"""
import random
from datetime import datetime, timezone
from typing import Dict, List
from uuid import uuid4

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(
    title="Processing Service",
    description="Microservicio encargado de procesar (validar inventario y pago) órdenes.",
    version="1.0.0",
)

# CORS: permite que un cliente HTTP en el navegador (el dashboard, o Swagger
# UI de otro origen) pueda llamar a esta API directamente. Solo para fines de
# demo del POC; en producción se restringiría a una lista blanca de orígenes.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ProcessRequest(BaseModel):
    """Datos que Order Service envía para pedir el procesamiento de una orden.

    Nótese que viaja el `order_id` y el `customer_id` como simples strings de
    referencia (no objetos completos): Processing Service no necesita -ni
    debe- conocer el resto de los datos del cliente ni de la orden, solo lo
    mínimo indispensable para hacer su trabajo. Esto es intencional: es el
    contrato REST entre ambos servicios, y mantenerlo mínimo reduce el
    acoplamiento.
    """

    order_id: str
    customer_id: str
    items: List[str]
    total: float = Field(..., gt=0)
    simulate_failure: bool = Field(
        default=False,
        description="Si es true, fuerza un rechazo para fines de demostración.",
    )


class ProcessResult(BaseModel):
    """Resultado de procesar una orden: aprobada o rechazada, con motivo."""

    processing_id: str
    order_id: str
    status: str  # "approved" | "rejected"
    reason: str | None = None
    processed_at: datetime


# Almacenamiento propio de este servicio (in-memory, "database per service").
# Guarda cada resultado de procesamiento por su propio id (processing_id),
# no por order_id, ya que una misma orden podría reprocesarse más de una vez
# (ver /orders/{id}/retry-processing en Order Service) y cada intento generaría
# un ProcessResult distinto.
_processed: Dict[str, ProcessResult] = {}

# Regla de negocio simulada: montos mayores a este límite se rechazan.
# Sirve para poder demostrar en vivo un caso "rejected" de forma determinista
# (basta con pedir una orden de más de $5000), sin depender solo del azar.
MAX_APPROVED_AMOUNT = 5000.0


@app.get("/health", tags=["Health"])
def health_check():
    """Endpoint de salud. Lo consulta Order Service (vía el proxy del
    dashboard, /api/health/processing) para mostrar si este servicio está
    arriba, y también sirve como healthcheck manual del contenedor."""
    return {"status": "ok", "service": "processing-service"}


@app.post("/process", response_model=ProcessResult, status_code=status.HTTP_201_CREATED, tags=["Processing"])
def process_order(payload: ProcessRequest):
    """Simula la validación de inventario y el cobro de pago para una orden.

    Reglas de decisión, evaluadas en orden (la primera que aplica decide el
    resultado):

    1. `simulate_failure=true` en el payload -> rechazo forzado. Es el
       mecanismo que usa el botón "Simular rechazo" del dashboard para poder
       mostrar un caso "rejected" de forma controlada durante la demo.
    2. `total` mayor a `MAX_APPROVED_AMOUNT` -> rechazo por monto ("regla de
       negocio" simulada, como si fuera un límite de crédito).
    3. Orden sin items -> rechazo (no hay nada que procesar).
    4. Caso normal: se aprueba con ~95% de probabilidad (`random.random() >
       0.05`), simulando que en la vida real el inventario a veces no
       alcanza aunque el pedido sea válido.

    El resultado se guarda en `_processed` (historial propio de este
    servicio) y se devuelve a quien lo llamó — en este POC, siempre
    Order Service.
    """
    approved = True
    reason = None

    if payload.simulate_failure:
        approved = False
        reason = "Fallo simulado solicitado explícitamente (simulate_failure=true)."
    elif payload.total > MAX_APPROVED_AMOUNT:
        approved = False
        reason = f"Monto ${payload.total:.2f} supera el límite permitido (${MAX_APPROVED_AMOUNT:.2f})."
    elif not payload.items:
        approved = False
        reason = "La orden no contiene items para procesar."
    else:
        # Pequeña variabilidad para simular un sistema de inventario real,
        # donde incluso un pedido "válido" puede fallar ocasionalmente.
        approved = random.random() > 0.05  # ~95% de aprobación
        if not approved:
            reason = "Inventario insuficiente para uno o más items (simulado)."

    result = ProcessResult(
        processing_id=str(uuid4()),
        order_id=payload.order_id,
        status="approved" if approved else "rejected",
        reason=reason,
        processed_at=datetime.now(timezone.utc),
    )
    _processed[result.processing_id] = result
    return result


@app.get("/process/{processing_id}", response_model=ProcessResult, tags=["Processing"])
def get_processing_result(processing_id: str):
    """Consulta un resultado de procesamiento ya generado, por su id.
    Endpoint auxiliar de inspección (no lo usa el dashboard ni Order Service
    en el flujo normal); útil para depurar vía Swagger."""
    return _processed[processing_id]
