# adr-index

Búsqueda en los ADRs del core (semana 20): pregunta en español → ranking léxico
determinista de secciones (vía `parser-io`) → respuesta LLM **con citas verificadas**.
La regla que lo define: **una cita que no existe en el índice invalida la respuesta** —
el bibliotecario no puede inventar fuentes.

## Demo

```bash
uv sync
uv run adr-index search ttl
# 0007-cache-ratelimit-contract.md · «2. Reglas» · líneas 44-55
# 0010-diff-engine-contract.md · «2. Reglas» · líneas 30-45

uv run adr-index ask "¿qué campos volátiles ignora diff-engine al comparar?"
# {
#   "answer": "VOLATILE_FIELDS: timestamps, ids y latencias; se ignoran por
#              defecto y se listan en el reporte…",
#   "citations": [{"file": "0010-diff-engine-contract.md", "section": "2. Reglas", …}],
#   "confidence": "alta"
# }
```

La respuesta exacta varía (es LLM); lo garantizado es el **formato validado**
(`answer-card-v1`) y que **cada cita exista en el índice** — si el modelo inventa un
archivo o sección, `ask` falla visible en vez de devolver la mentira.

## Cómo funciona

```
adr-index ask "…"
  ├─ secure-base: la pregunta se sanea antes de entrar al prompt
  ├─ índice léxico (parser-io): título vale 3, cuerpo cuenta ocurrencias — determinista
  ├─ LLM: respuesta JSON con citas de ENTRE las secciones indexadas
  └─ verificación: cada (archivo, sección) citado debe existir — o rechazo
```

- El ranking es **léxico y documentado como tal**: lo semántico (embeddings) es
  `vector-core` (C11), deuda abierta en el issue #2.
- El dataset **congela pregunta+contexto**: cuando los ADRs cambien, el replay del
  cassette no caduca — lo que evoluciona es el índice vivo, no el examen.
- Caché TTL + token bucket alrededor del proveedor (`cache-ratelimit`).

## Recicla de

| Módulo | Uso |
|---|---|
| `parser-io` | el índice nace de secciones con líneas reales de los ADRs |
| `llm-client` | la llamada de la respuesta (retry + reparación + span de 20 campos) |
| `schema-validate` | la respuesta valida contra `answer-card-v1` o falla |
| `secure-base` | la pregunta (que puede contener cualquier cosa) entra saneada |
| `test-kit` | dataset congelado + replay determinista (D4) |
| `cache-ratelimit` | caché TTL + token bucket por delante del proveedor |

## Limitaciones

- Léxico, no semántico: sin sinónimos ni paráfrasis (hasta `vector-core`, C11).
- Sin índice invertido ni persistencia: reindexa en cada arranque; con este corpus
  son milisegundos, cuando duela se invierte.
- La verificación de citas es por existencia, no por fidelidad: que la sección exista
  no garantiza que diga lo que el answer afirma — el lector sigue el enlace.

## Roadmap

- [x] Índice léxico + respuesta con citas verificadas (sem. 20)
- [ ] Ranking semántico cuando `vector-core` (C11) se siembre (issue #2)
- [ ] Servirlo por HTTP para la flota (con `web-api-base`)
- [ ] Verificación de fidelidad de cita: el body citado debe sostener el answer
