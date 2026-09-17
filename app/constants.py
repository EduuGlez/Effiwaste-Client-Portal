"""Catálogos inmutables compartidos por las vistas web.

Mantener estos valores fuera de los routers evita duplicar reglas de negocio y
permite que plantillas, validadores y documentación usen las mismas etiquetas.
"""

DOCUMENT_CATEGORIES = {
    "monthly_report": "Informe mensual",
    "general": "Documento general",
    "procedure": "Procedimiento",
    "manual": "Manual",
}

LIBRARY_CATEGORY_LABELS = {
    "monthly_report": "Informes mensuales",
    "procedure": "Procedimientos",
    "manual": "Manuales",
    "general": "Documentos generales",
}

EXTERNAL_SERVICES = (
    {
        "name": "Effiwaste",
        "category": "Desperdicio alimentario",
        "description": "Mide, controla y analiza el desperdicio alimentario de tu operativa.",
        "url": "https://weight.effiwaste.es/#/login",
        "style": "weight",
    },
    {
        "name": "Effilabel",
        "category": "Etiquetado electrónico",
        "description": "Gestiona la información del buffet y actualiza tus etiquetas en tiempo real.",
        "url": "https://esl.effiwaste.es/#/login",
        "style": "label",
    },
    {
        "name": "EffiChef",
        "category": "Gestión de cocina",
        "description": "Centraliza inventarios, escandallos, producción y logística de cocina.",
        "url": "https://www.effichef.es/",
        "style": "chef",
    },
    {
        "name": "CircularChef",
        "category": "Recetas y sostenibilidad",
        "description": "Aprovecha los excedentes de cocina y reduce el desperdicio alimentario con recetas circulares.",
        "url": "https://circularchef.effichef.es/",
        "style": "chef",
    },
)
