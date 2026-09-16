"""
Processing Service
-------------------
Microservicio responsable de "procesar" una orden ya creada: simula validación
de inventario y cobro de pago. No conoce nada sobre clientes ni sobre cómo se
crean las órdenes; solo recibe los datos necesarios para procesar y responde
con un resultado. Esto demuestra bajo acoplamiento e independencia de despliegue.

Para ilustrar el principio de "diseño para el fallo" y permitir la demo de
tolerancia a fallas, este servicio puede simular:
- Rechazo de pedidos con monto muy alto (regla de negocio simulada).
- Latencia/errores aleatorios opcionales vía el parámetro `simulate_failure`.
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

# CORS: permite que el dashboard HTML (abierto como archivo local o en otro
# puerto) pueda llamar a esta API directamente desde el navegador. Solo para
# fines de demo del POC; en producción se restringiría a orígenes conocidos.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ProcessRequest(BaseModel):
    order_id: str
    customer_id: str
    items: List[str]
    total: float = Field(..., gt=0)
    simulate_failure: bool = Field(
        default=False,
        description="Si es true, fuerza un rechazo para fines de demostración.",
    )


class ProcessResult(BaseModel):
    processing_id: str
    order_id: str
    status: str  # "approved" | "rejected"
    reason: str | None = None
    processed_at: datetime


_processed: Dict[str, ProcessResult] = {}

# Regla de negocio simulada: montos mayores a este límite se rechazan
MAX_APPROVED_AMOUNT = 5000.0


@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "ok", "service": "processing-service"}


@app.post("/process", response_model=ProcessResult, status_code=status.HTTP_201_CREATED, tags=["Processing"])
def process_order(payload: ProcessRequest):
    """Simula validación de inventario + cobro de pago para una orden."""
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
        # Pequeña variabilidad para simular un sistema de inventario real
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
    return _processed[processing_id]
