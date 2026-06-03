# Normalizacion del CSV real de Wix

El export real de Wix puede venir con columnas distintas al schema del migrador:

```txt
id
title
slug
excerpt
contentText
richContent
firstPublishedDate
lastPublishedDate
categoryIds
media
url.path
publicUrl
hasImage
```

Antes de analizar o importar, convertirlo con:

```bash
python -m src.main normalize-wix-csv --file data/input/noticias_0001.csv --output data/input/wix_posts_normalized_0001.csv
```

Este comando no escribe en WordPress, no sube imagenes y no crea posts.

## Salida

Genera un CSV canonico:

```txt
wix_id,title,content,date,category,image_url,old_url,author,excerpt,slug,tags,page_content
```

Mapeo:

- `wix_id`: `id`
- `title`: `title`
- `content`: `contentText` convertido a HTML simple con parrafos
- `date`: `firstPublishedDate`
- `category`: primer valor de `categoryIds`
- `image_url`: `media.wixMedia.image.url`
- `old_url`: `publicUrl`
- `author`: vacio, salvo que exista columna `author`
- `excerpt`: `excerpt`
- `slug`: `slug`, sin modificar
- `tags`: `hashtags`, `tagIds` o `tags` si existen
- `page_content`: `richContent` crudo como referencia

## Reglas

- Si `contentText` esta vacio, intenta extraer texto desde `richContent`.
- Si `media` no tiene URL, deja `image_url` vacio y registra warning.
- Si `categoryIds` tiene varios valores, usa el primero y registra los demas.
- No modifica slugs.
- Preserva Unicode en `slug`, `old_url` y contenido.

## Reporte

Genera:

```txt
data/output/normalize_wix_csv_report.csv
```

Incluye resumen y warnings por fila:

- total filas;
- filas normalizadas;
- sin imagen;
- sin categoria;
- sin fecha;
- sin contenido;
- URLs invalidas;
- errores de JSON en `media`;
- errores de JSON en `categoryIds`.
