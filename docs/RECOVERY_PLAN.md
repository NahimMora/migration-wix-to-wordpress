# Plan de recuperacion

## Se corta el script

Volver a ejecutar el mismo comando. SQLite conserva estados y evita duplicar posts creados.

## Falla WordPress

Si hay 401/403, revisar credenciales y Application Password. Si hay 404 en REST, revisar permalinks y REST API. Si hay 5xx, revisar PHP, memoria, logs del servidor y reintentar.

## Falla descarga de imagen

La imagen queda con `failed_download`. Revisar URL, conectividad y permisos. Luego ejecutar:

```bash
python -m src.main retry-failed
```

## Falla subida de media

La imagen queda con `failed_upload`. Revisar MIME, tamano maximo, permisos y limites de WordPress.

## Imagen subida y post fallado

El post puede quedar `image_uploaded_post_failed`. Ejecutar:

```bash
python -m src.main report-orphan-media
```

No borrar media automaticamente sin revisar.

## Hay posts duplicados

Revisar `duplicate_posts_report.csv`, `posts_report.csv`, `wix_id`, `old_url` y `desired_slug`. No continuar escala hasta entender el origen.

## Hay URLs cambiadas

Revisar `url_report.csv` y `redirect_candidates.csv`. Priorizar corregir estructura y slugs antes de aceptar redirecciones masivas.

## Hay referencias localhost

Ejecutar:

```bash
python -m src.main scan-local-references
```

Corregir antes de subir a Hostinger.
