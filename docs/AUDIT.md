# Auditoria critica previa

## Confirmacion operativa

El sistema no inicia migracion automaticamente. `init-db`, `analyze-*`, `verify-*` y `dry-run` no crean posts ni suben imagenes. `migrate` y `retry-failed` son comandos de escritura, pero quedan bloqueados por defecto con `ALLOW_WORDPRESS_WRITES=false`. `cleanup-test-batch` esta limitado a `--dry-run` en esta version.

## Arquitectura

- Python orquesta lectura de CSV, normalizacion, validacion y REST API.
- SQLite guarda estados, IDs Wix, IDs WordPress, URLs y errores.
- WordPress REST API crea posts y media.
- `verify-wordpress` prueba REST, autenticacion, posts y categorias sin escribir.
- El plugin auxiliar registra meta fields REST y reduce tamanos intermedios innecesarios.
- Los reportes quedan en `data/output/`.

## Riesgos principales

- URLs antiguas que no encajan con `/post/%postname%/`.
- Slugs que WordPress cambia por sanitizacion o duplicados.
- Duplicacion de posts si se reintenta sin control.
- Duplicacion de media si no se deduplica por URL y hash.
- Imagen subida correctamente y post fallado despues.
- Categorias inexistentes o mal mapeadas.
- HTML de Wix con scripts, estilos o contenido vacio.
- Referencias a `localhost`, `.local`, `127.0.0.1` o rutas Windows antes de subir a Hostinger.

## Mitigaciones

- SQLite con estados por post e imagen.
- Indices unicos parciales por `wix_id` y `old_url`.
- Deduplicacion de imagenes por URL y hash.
- `url_status` para cada post creado.
- `redirect_candidates.csv` solo como reporte.
- `orphan_media_report.csv` para media subida no usada.
- `scan-local-references` antes de pasar a produccion.
- `DEFAULT_POST_STATUS=draft` por defecto.
- `ALLOW_WORDPRESS_WRITES=false` por defecto.
- `csv_imports` registra importaciones de CSV para auditoria.

## Decisiones tecnicas

- No usar WP-CLI.
- No usar importadores masivos.
- Usar REST API con Application Passwords.
- Crear posts de a uno para registrar errores con precision.
- Procesar lotes desde el script, no desde la API.
- Conservar HTML de articulo de forma conservadora.
- No recomprimir todas las imagenes.

## Decisiones manuales pendientes

- Confirmar estructura real de URLs Wix.
- Confirmar categorias y IDs en WordPress.
- Confirmar zona horaria local.
- Confirmar si se permite crear posts sin imagen si falla media.
- Elegir herramienta de search-replace serializado para Hostinger.
- Decidir estrategia final de redirecciones si hay muchos cambios reales.

## Orden obligatorio

```bash
python -m src.main init-db
python -m src.main analyze-csv --file data/input/wix_posts.csv
python -m src.main analyze-urls --file data/input/wix_posts.csv
python -m src.main analyze-images --file data/input/wix_posts.csv
python -m src.main verify-wordpress
python -m src.main verify-categories
python -m src.main dry-run --limit 10
```
