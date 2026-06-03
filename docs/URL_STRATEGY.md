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

## Estados

- `exact_match`: path igual.
- `trailing_slash_only`: solo cambia slash final.
- `changed_by_wordpress`: WordPress cambio slug.
- `path_structure_changed`: cambio estructura.
- `invalid_old_url`: URL fuente invalida.
- `missing_old_url`: no hay URL fuente.
- `error`: no se pudo determinar.

## Redirecciones

El sistema genera:

```txt
data/output/redirect_candidates.csv
```

No aplica redirecciones automaticamente.
