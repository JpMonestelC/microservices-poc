"""
Customer Service
-----------------
Microservicio responsable exclusivamente del dominio "Cliente".
Expone una API REST (FastAPI) para crear y consultar clientes.

Principios de Microservices Architecture demostrados aquí:
- Responsabilidad única (bounded context "Customer").
- Persistencia propia y aislada (in-memory en este POC): ningún otro
  servicio accede directamente a estos datos, solo mediante esta API.
- Documentación autogenerada vía OpenAPI/Swagger (disponible en /docs).
"""
from datetime import datetime, timezone
from typing import Dict
from uuid import uuid4

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field

app = FastAPI(
    title="Customer Service",
    description="Microservicio encargado de la gestión de clientes.",
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


class CustomerCreate(BaseModel):
    name: str = Field(..., min_length=1, examples=["Jose Pablo Monestel"])
    email: EmailStr = Field(..., examples=["jose@example.com"])


class Customer(CustomerCreate):
    id: str
    created_at: datetime


# "Base de datos" propia del servicio (in-memory, exclusiva de Customer Service)
_customers: Dict[str, Customer] = {}


@app.get("/health", tags=["Health"])
def health_check():
    """Usado por Docker/orquestadores y por otros servicios para verificar disponibilidad."""
    return {"status": "ok", "service": "customer-service"}


@app.post("/customers", response_model=Customer, status_code=status.HTTP_201_CREATED, tags=["Customers"])
def create_customer(payload: CustomerCreate):
    customer = Customer(
        id=str(uuid4()),
        name=payload.name,
        email=payload.email,
        created_at=datetime.now(timezone.utc),
    )
    _customers[customer.id] = customer
    return customer


@app.get("/customers", response_model=list[Customer], tags=["Customers"])
def list_customers():
    return list(_customers.values())


@app.get("/customers/{customer_id}", response_model=Customer, tags=["Customers"])
def get_customer(customer_id: str):
    customer = _customers.get(customer_id)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return customer
