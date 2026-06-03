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
python -m src.main check-encoding --file data/input/noticias_0001.csv
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

## Categorias

El CSV normalizado deja `category` como el primer ID de Wix encontrado en `categoryIds`. Para que `import-csv` pueda resolverlo sin caer en `DEFAULT_CATEGORY_ID`, `data/input/category_map.csv` debe incluir el ID de Wix:

```csv
wix_category,wix_category_id,wix_category_slug,wp_category_id,wp_category_name
Salta,0d80fe16-527a-4e1e-89e3-6a85d1dc22bd,salta,8,Salta
```

Tambien se puede generar un borrador desde el JSON de categorias exportado por Wix:

```bash
python -m src.main normalize-wix-categories --file data/input/wix_categories.json --output data/input/category_map_from_wix.csv --existing-map data/input/category_map.csv
```

El comando corrige mojibake en labels/slugs/descripciones, no escribe en WordPress y deja vacio `wp_category_id` cuando no puede inferirlo desde un mapa existente.

## Reporte

Genera:

```txt
data/output/normalize_wix_csv_report.csv
data/output/encoding_report.csv
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

## Encoding y mojibake

El normalizador prueba lectura en este orden:

1. `utf-8-sig`
2. `utf-8`
3. `cp1252`
4. `latin1`

Luego aplica correccion de mojibake en campos criticos:

- `title`
- `content`
- `excerpt`
- `slug`
- `old_url`
- `tags`
- `page_content`, solo si `richContent` es JSON parseable y puede reescribirse sin romperlo.

Corrige casos como `llegÃ³`, `detrÃ¡s`, `bÃºsqueda`, `OrÃ¡n`, `policÃ­a`, `afiliaciÃ³n`, `â€œ`, `â€™`, `â€“`, `â€”`, `â€¦` y `Â`.

Despues de normalizar, ejecutar:

```bash
python -m src.main analyze-csv --file data/input/wix_posts_normalized_0001.csv
```

Si quedan patrones sospechosos en columnas criticas, `analyze-csv` devuelve `possible_mojibake_detected` y `migration_recommended=false`.
