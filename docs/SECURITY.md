# Diseño de seguridad

La seguridad de Effiwaste Suite se apoya en controles encadenados. Ningún control aislado —sesión, filtro SQL o prompt— se considera suficiente por sí mismo.

## Activos y fronteras de confianza

Los activos principales son las credenciales, PDFs de cada organización, fragmentos vectorizados, preguntas de usuarios y recomendaciones operativas. Las fronteras son:

- navegador ↔ aplicación web;
- aplicación ↔ PostgreSQL/Redis/volumen;
- aplicación/worker ↔ OpenAI;
- administrador ↔ contenido subido;
- documento no confiable ↔ instrucciones del modelo.

Se asume que TLS termina en un proxy de confianza, PostgreSQL y Redis no están expuestos a Internet, y solo los procesos web/worker pueden escribir el volumen de documentos.

## Controles implementados

| Riesgo | Control |
|---|---|
| Robo de contraseñas almacenadas | Argon2 mediante `pwdlib`; nunca se guarda la contraseña original. |
| Enumeración de cuentas | Mensaje genérico y verificación de un hash ficticio cuando el usuario no existe. |
| Fuerza bruta | Ventana móvil en Redis por pareja cuenta/IP y por IP global; claves HMAC sin correo o IP en claro. |
| Fijación de sesión | La sesión se limpia después del login y recibe un CSRF nuevo. |
| CSRF | Token aleatorio comprobado en login, logout y todas las mutaciones autenticadas. |
| Cookie robada o manipulada | Cookie firmada, `HttpOnly`, `SameSite=Lax`, caducidad acotada y `Secure` obligatorio en producción. |
| Host header / clickjacking / MIME sniffing | Hosts permitidos, CSP, `frame-ancestors 'none'`, `X-Frame-Options`, `nosniff` y políticas de permisos. |
| Acceso entre hoteles | Filtro de audiencia aplicado dentro de las consultas y repetido antes de servir el PDF. Un recurso ajeno responde 404. |
| Path traversal | Nombre físico generado como UUID y validado antes de construir una ruta. |
| PDF malicioso o abusivo | Límite durante streaming, firma, apertura con parser, límite de páginas y rechazo de cifrados. El worker repite la validación. |
| Adjuntos de soporte abusivos | Lista cerrada de extensiones, número y tamaño total limitados; se mantienen en memoria solo durante el envío y no se guardan en el portal. |
| XSS desde la IA | Markdown convertido y saneado con lista permitida; los extractos de fuentes se insertan con `textContent`. |
| Prompt injection documental | El prompt declara las fuentes como datos no confiables y prohíbe seguir sus instrucciones. La autorización ocurre antes del modelo. |
| Respuesta de IA inesperada | Esquemas Pydantic con longitudes, UUID y enumeraciones cerradas antes de persistir. |
| Fuga mediante errores | Excepciones completas en logs; respuestas públicas y estados del worker usan mensajes genéricos salvo errores de validación conocidos. |
| Sobrecarga de tareas | Ingesta fuera del web, prefetch 1, `acks_late` y límites temporal suave/duro. |
| Configuración insegura | El proceso se niega a arrancar como producción con valores de desarrollo. |

## Sesiones

Starlette firma el contenido de la cookie, pero no lo cifra. Por eso la sesión contiene solo `user_id` y el token CSRF, nunca contraseñas, permisos completos o documentos. Los permisos se vuelven a leer desde la base de datos en cada petición, de modo que desactivar una cuenta tiene efecto inmediato.

Las sesiones firmadas no ofrecen revocación central individual. Para invalidarlas todas se rota `SESSION_SECRET`; para revocación selectiva futura se recomienda almacenar identificadores de sesión en Redis.

## Aislamiento multiempresa

La política vive en `app/services/access.py` y tiene dos representaciones equivalentes:

- `document_access_filter` compone la condición SQL para listar y recuperar fragmentos;
- `can_access_document` comprueba una entidad ya cargada antes de verla o descargarla.

El administrador puede ver todos los ámbitos. Un usuario estándar solo recibe documentos globales, los de su cadena y los de su hotel. Las recomendaciones se consultan y mutan siempre con el `hotel_id` de la sesión.

## IA y privacidad

Para OCR visual, embeddings, respuestas y recomendaciones se envían a OpenAI las partes necesarias del documento. Las llamadas de Responses usan `store=False`. Esto no sustituye una evaluación contractual de protección de datos: antes de producción deben definirse base jurídica, región, categorías de documentos admitidas y condiciones de conservación del proveedor.

`QueryLog` conserva preguntas y respuestas completas para auditoría. Pueden contener información personal o comercial; cada despliegue debe establecer plazo de retención, acceso autorizado y mecanismo de borrado.

El centro de ayuda añade al correo de soporte el nombre y correo del usuario autenticado, junto con su hotel y cadena. El navegador no envía ni permite editar ese contexto: la aplicación lo obtiene de la sesión y la base de datos. El mensaje y sus adjuntos se entregan al buzón configurado mediante SMTP y no se persisten en el portal. La conservación posterior depende de la política del proveedor de correo y del buzón de soporte.

## Requisitos de producción

- TLS obligatorio de extremo a proxy y `COOKIE_SECURE=true`.
- `SESSION_SECRET` aleatorio de al menos 32 caracteres y fuera del repositorio.
- Contraseña administrativa única; datos demo desactivados.
- Credenciales distintas de los valores del compose de desarrollo.
- `ALLOWED_HOSTS` limitado a los dominios reales.
- PostgreSQL, Redis y el volumen en red privada con copias de seguridad cifradas.
- Secretos suministrados por el gestor de secretos del entorno, no por una imagen ni por Git.
- SMTP con TLS, credenciales dedicadas y remitente autorizado por el dominio; el buzón receptor debe aplicar su política de acceso y retención.
- Logs centralizados con acceso restringido, alertas para fallos repetidos de login/ingesta y sin cuerpos completos de documentos.
- Dependencias e imágenes revisadas y fijadas mediante el proceso de entrega de la organización.
- Migraciones de esquema versionadas y probadas antes de actualizar una base existente.

## Límites conocidos y decisiones conscientes

- Si Redis no está disponible, el limitador de login falla abierto para mantener accesible el servicio y deja una alerta en logs. En un entorno de alto riesgo debe cambiarse por fallo cerrado o protección equivalente en el proxy/WAF.
- No hay MFA, recuperación de contraseña ni revocación selectiva de sesiones. Son necesarias antes de usar cuentas privilegiadas en un entorno expuesto.
- El cifrado en reposo depende del disco/base de datos gestionados por infraestructura.
- La comprobación antivirus o sandbox de PDFs no forma parte de esta versión. El parser nunca ejecuta scripts PDF, pero un flujo corporativo puede añadir ClamAV o un servicio de análisis antes de encolar.
- La política de retención de `QueryLog`, PDFs y copias de seguridad debe definirse externamente.
- La protección contra prompt injection reduce riesgo, no lo elimina. Las respuestas deben seguir tratándose como asistencia con fuentes, no como autorización automática para actuar.

Estas limitaciones están documentadas para que una decisión de despliegue sea explícita y verificable, no implícita.
