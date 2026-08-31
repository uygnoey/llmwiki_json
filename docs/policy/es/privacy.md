# Política de privacidad

- Versión del documento: v1.0-draft
- Fecha de entrada en vigor: {{EFFECTIVE_DATE}}
- Responsable del tratamiento: {{OPERATOR}}
- Contacto: {{CONTACT}}

> Este es un borrador. Sométalo a revisión legal antes de lanzar el servicio.

## 1. Datos que recogemos

| Categoría | Elementos | Base jurídica |
|---|---|---|
| Cuenta | Identificador, correo electrónico, nombre visible | Ejecución del contrato |
| Grupos | Pertenencia a grupos, rol, fecha de alta | Ejecución del contrato |
| Documentos | `authors`, `history`, `created`, `updated` de páginas y bloques | Ejecución del contrato |
| Acceso | Marca de tiempo, ruta de la petición, IP, agente de usuario | Interés legítimo (seguridad) |
| Invitaciones | Hash del token, caducidad, número de usos | Ejecución del contrato |

El cuerpo de los documentos lo escribe usted y no contiene datos personales salvo que
usted los incluya. No introduzca datos personales de terceros en el texto.

## 2. Finalidades

1. Prestar el Servicio: almacenamiento, indexación, búsqueda y visualización del grafo.
2. Evaluar permisos de grupo y aplicar el control de acceso.
3. Mostrar el historial de contribuciones y determinar qué eliminar al darse de baja.
4. Responder a incidencias, prevenir abusos e investigar eventos de seguridad.

## 3. Dónde se almacena y durante cuánto tiempo

1. Los documentos canónicos y los artefactos derivados se almacenan en almacenamiento
   de objetos ({{STORAGE_PROVIDER}}, región {{REGION}}).
2. Los registros de cuenta, grupo y permisos se guardan en una base de datos separada.
3. Conservación
   - Datos de cuenta: hasta la baja
   - Documentos: según la opción elegida al darse de baja
   - Registros de acceso: {{LOG_RETENTION_DAYS}} días
   - Lápidas (tombstones): indefinidamente (no contienen datos personales)

## 4. Comunicación de datos y encargados

1. No comunicamos datos a terceros salvo obligación legal.
2. Utilizamos los siguientes encargados de infraestructura: {{SUBPROCESSORS}}.
3. Que otros usuarios vean sus documentos no es una «comunicación a terceros», sino el
   **resultado de los ajustes de compartición que usted eligió**.

## 5. Baja de la cuenta

Al darse de baja eliminamos sin dilación indebida los datos de su cuenta. Para los
documentos, usted elige entre **eliminación total** o **transferencia de la propiedad
al grupo y conservación**. Incluso la eliminación total tiene límites técnicamente
irreversibles, recogidos en
[Consentimiento de compartición, reutilización y eliminación](sharing-and-deletion.md).

## 6. Sus derechos

Puede solicitar acceso, rectificación, supresión, limitación del tratamiento y
portabilidad. La portabilidad se entrega como descarga del JSON canónico completo.
Envíe su solicitud a {{CONTACT}}; respondemos en un plazo de {{RESPONSE_DAYS}} días.

## 7. Medidas de seguridad

1. Cifrado en tránsito (TLS) y en reposo.
2. URL prefirmadas con caducidad corta.
3. Tokens de invitación almacenados solo como hash; nunca se conserva el texto claro.
4. Reglas de aislamiento de los documentos de grupos privados aplicadas en la fase de
   compilación.
5. Las decisiones de permisos se toman en una base de datos de permisos dedicada, no
   en las ACL del almacenamiento.

## 8. Contacto

{{CONTACT}}
