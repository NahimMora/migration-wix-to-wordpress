# Estrategia de URLs

## Objetivo

Conservar la mayor cantidad posible de URLs indexadas de Wix.

La estructura esperada es:

```txt
https://holasalta.com/post/slug
```

WordPress local debe usar:

```txt
/post/%postname%/
```

## Extraccion

Para cada `old_url`:

- `old_path`: path de la URL original.
- `desired_slug`: ultimo segmento del path o columna `slug` si existe.

Ejemplo:

```txt
old_url = https://holasalta.com/post/violento-robo-en-zona-sur
old_path = /post/violento-robo-en-zona-sur
desired_slug = violento-robo-en-zona-sur
```

## Comparacion

Se comparan paths:

- no se compara dominio;
- se ignora local vs produccion;
- se ignora http vs https;
- se detecta trailing slash;
- se detectan cambios reales de path o slug.
- antes de migrar, se predice si el slug sera sanitizado por quitar acentos.

Ejemplo de sanitizacion:

```txt
old_path = /post/la-violencia-llegó-al-futsal
expected_new_path = /post/la-violencia-llego-al-futsal/
status = slug_sanitized
reason = non_ascii_slug_sanitized
```

## Estados

- `exact_match`: path igual.
- `trailing_slash_only`: solo cambia slash final.
- `slug_sanitized`: `old_path` contiene caracteres no ASCII y el path esperado usa slug ASCII.
- `path_changed`: cambio previsto de path antes de migrar.
- `changed_by_wordpress`: WordPress cambio slug.
- `path_structure_changed`: cambio estructura.
- `invalid_old_url`: URL fuente invalida.
- `missing_old_url`: no hay URL fuente.
- `error`: no se pudo determinar.

## Redirecciones

El sistema genera:

```txt
data/output/pre_migration_url_risk_report.csv
data/output/redirect_candidates.csv
```

`pre_migration_url_risk_report.csv` se genera con `analyze-urls` y permite filtrar `needs_redirect=true` antes de ejecutar migraciones.

No aplica redirecciones automaticamente.
