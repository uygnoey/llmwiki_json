# Consentimiento de compartición, reutilización y eliminación

- Versión del documento: v1.0-draft
- Fecha de entrada en vigor: {{EFFECTIVE_DATE}}

> Este es un borrador. Sométalo a revisión legal antes de lanzar el servicio.
> Este documento requiere un consentimiento específico en el momento del registro.

## 1. Qué cubre este documento

Este servicio es una herramienta para **integrar** conocimiento, no solo para
**referenciarlo**. Leer el documento de otra persona e incorporar su contenido al
propio es la forma normal de usarlo. Por eso «compartir» y «eliminar» funcionan aquí
de manera distinta a un servicio corriente de archivos compartidos. Comprenda esta
diferencia antes de dar su consentimiento.

## 2. Qué significa compartir

1. Un documento publicado en un **grupo público** puede ser leído por otros usuarios
   del Servicio.
2. Otros usuarios pueden enlazarlo y citarlo como fuente en su propio wiki.
3. Otros usuarios pueden leerlo y **escribir documentos nuevos propios** a partir de él.
4. Los documentos de un **grupo privado** solo pueden leerlos sus miembros, no pueden
   ser objeto de referencias externas y ni su existencia ni su título aparecen en
   ningún grafo externo.

## 3. Lo que no puede deshacerse — léalo, por favor

**Las frases que otro usuario haya escrito integrando su documento en su propio wiki
no desaparecen cuando usted elimina su documento o se da de baja.**

Esas frases son obra de esa persona y el Servicio no dispone de medio alguno para
recuperarlas. No prometemos una eliminación que presuponga tal recuperación. Si no
desea divulgar algo de forma irreversible, **manténgalo desde el principio en un grupo
privado o desactive la derivación en ese documento.**

## 4. Ajustes por documento

Cada documento lleva sus propios ajustes:

| Ajuste | Significado |
|---|---|
| `links` | Si otros documentos pueden enlazar a este |
| `citation` | Si otros documentos pueden citarlo como fuente |
| `derivation` | Si otros usuarios pueden integrar este contenido en sus documentos |
| `backlink` | Si los retroenlaces hacia este documento se muestran a otros usuarios |

Un ajuste por documento solo puede ser **más restrictivo que el de su grupo, nunca más
abierto.** Un documento de un grupo privado no puede hacerse público con su propio
ajuste.

## 5. Tratamiento de las fuentes originales (raw)

1. **Los archivos originales, como PDF y hojas de cálculo, no se suben al Servicio.**
   El cliente los bloquea en la ruta de carga.
2. Al compartir, el nombre del archivo original (`raw_ref`) y la instantánea del texto
   completo (`source_snapshot.text`) pueden eliminarse de la carga útil o sustituirse
   por un hash.
3. Ahora bien, **los extractos y resúmenes que usted transcribió al texto del wiki son
   el cuerpo del documento y, por tanto, se comparten.** No subir el archivo original
   es una cosa distinta de que no se compartan las frases extraídas de él.

## 6. Opciones al darse de baja

Al darse de baja elige una de estas opciones:

**A. Eliminación total**
- Se eliminan los documentos y bloques de los que usted es autor.
- En su lugar queda únicamente una **lápida (tombstone)** sin datos personales. Existe
  para que un enlace roto en el documento de otro usuario se lea como «documento
  eliminado» y no como «documento inexistente».
- En documentos coeditados solo se eliminan los bloques que usted escribió; el
  documento se mantiene.

**B. Transferencia de la propiedad y conservación**
- La propiedad pasa a su grupo y el contenido se conserva.
- La atribución de autoría puede sustituirse por un identificador anonimizado.

## 7. Qué permanece tras la eliminación

Aunque elija la eliminación total, permanecen:

1. Los **documentos derivados** que otros usuarios escribieron tras leer el suyo.
2. Las lápidas, que contienen solo el identificador del documento y la fecha de
   eliminación.
3. Los registros mínimos de tratamiento que estamos legalmente obligados a conservar.
4. Las cachés locales de otros usuarios y las copias ya distribuidas, que se eliminan
   progresivamente al sincronizarse, sin garantía de inmediatez.

## 8. Lista de consentimiento

En el registro debe consentir cada uno de los puntos siguientes:

- [ ] Entiendo que otros usuarios pueden leer y citar los documentos que publique en
      un grupo público.
- [ ] Entiendo que **las frases derivadas escritas por otros usuarios no desaparecen
      cuando me doy de baja.**
- [ ] Entiendo que los archivos originales no se suben, pero el contenido que yo
      transcriba al texto del wiki sí se comparte.
- [ ] No subiré el texto íntegro de obras de terceros ni información de seguridad.
