# wix-to-wordpress-migrator

Herramienta Python para preparar y ejecutar de forma controlada la migracion de aproximadamente 100.000 entradas de Wix a WordPress.org local usando WordPress REST API, SQLite y Application Passwords.

El proyecto esta pensado para HolaSalta, un medio digital donde la prioridad es conservar URLs, evitar duplicados, deduplicar imagenes y dejar trazabilidad antes de mover el WordPress local a Hostinger.

## Advertencia principal

Este proyecto no inicia ninguna migracion automaticamente.

Los comandos de inicializacion, analisis y dry-run no crean posts ni suben imagenes a WordPress. El comando `migrate` existe, pero no debe ejecutarse hasta completar auditoria previa, verificacion de WordPress, verificacion de categorias, dry-run y pruebas controladas. Ademas, las escrituras REST quedan bloqueadas por defecto con `ALLOW_WORDPRESS_WRITES=false`.

El estado inicial recomendado para WordPress es:

```env
DEFAULT_POST_STATUS=draft
```

## Que hace

- Analiza CSVs exportados desde Wix.
- Carga datos fuente a SQLite para idempotencia y reanudacion.
- Analiza URLs antiguas y compara paths para proteger SEO.
- Genera candidatos de redireccion sin escribir reglas masivas en `.htaccess`.
- Deduplica imagenes por URL y por hash.
- Normaliza imagenes solo si hace falta.
- Usa WordPress REST API con Application Passwords.
- Crea posts de a uno por request, aunque procese lotes.
- Genera reportes CSV y manifest JSON.
- Incluye plugin auxiliar para meta fields REST e imagenes intermedias.

## Que no hace todavia

- No migra al generarse.
- No publica por defecto.
- No usa plugins importadores.
- No usa WP-CLI.
- No crea 100.000 redirecciones automaticamente.
- No borra media huerfana automaticamente.
- No hace search-replace bruto sobre SQL.

## Requisitos

- Python 3.11 o superior recomendado.
- WordPress.org instalado en local.
- REST API habilitada.
- Application Password para un usuario con permisos de editor/administrador.
- Permalinks configurados como `/post/%postname%/`.
- Zona horaria de WordPress configurada para Argentina, por ejemplo `America/Argentina/Buenos_Aires`.

## Instalacion

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

En PowerShell tambien podes usar:

```powershell
Copy-Item .env.example .env
```

Edita `.env` con la URL local, usuario y Application Password. No subas `.env` a GitHub.

## Configuracion `.env`

Variables clave:

```env
WP_BASE_URL=http://localhost/wp
WP_USERNAME=admin
WP_APP_PASSWORD=replace_me
DEFAULT_POST_STATUS=publish
ALLOW_WORDPRESS_WRITES=false
CONFIRM_PUBLISH_MODE=false
BATCH_SIZE=100
CONTENT_FORMAT_MODE=richcontent_first
CONTENT_PARAGRAPH_HEURISTIC=true
MAX_PARAGRAPH_CHARS=700
MIN_SENTENCES_PER_PARAGRAPH=1
MAX_SENTENCES_PER_PARAGRAPH=3
PERMALINK_STRUCTURE=/post/%postname%/
NORMALIZE_IMAGES=true
NORMALIZE_ONLY_IF_NEEDED=true
RECOMPRESS_ALL_IMAGES=false
CREATE_POST_IF_IMAGE_FAILS=true
MAX_POST_FAILURE_RATE=0.02
MAX_IMAGE_FAILURE_RATE=0.10
STOP_ON_CRITICAL_URL_ERRORS=true
STOP_ON_UNMAPPED_CATEGORY=true
STOP_ON_UNMAPPED_AUTHOR=false
```

Las imagenes no se recomprimen en masa. Si una imagen esta bien, se sube tal cual. Solo se normaliza si supera limites, esta corrupta, tiene formato incompatible o requiere correccion.

## Application Password

En WordPress:

1. Ir a `Usuarios`.
2. Abrir el usuario que usara la migracion.
3. Buscar `Application Passwords`.
4. Crear una nueva clave.
5. Copiarla en `WP_APP_PASSWORD`.

No guardes esa clave en GitHub, capturas ni documentos compartidos.

## Plugin auxiliar

El plugin esta en:

```txt
wordpress-plugin/holasalta-migration-support/
```

Instalacion:

1. Copiar esa carpeta a `wp-content/plugins/`.
2. Activarla desde el panel de WordPress.
3. Verificar que los meta fields `_wix_id`, `_wix_old_url` y `_migration_batch` esten disponibles por REST:

```bash
python -m src.main verify-meta-support
python -m src.main test-meta-draft --dry-run
```

`verify-meta-support` solo hace requests GET. Si informa que el plugin no esta activo o que el post con `context=edit` solo devuelve `footnotes`, hay que copiar/activar este plugin en WordPress antes de crear nuevos posts de prueba.

Los posts creados antes de activar el plugin no quedan reparados automaticamente; para esos casos hay que recrear la prueba o hacer una reparacion controlada de meta aparte.

El plugin tambien reduce tamanos intermedios innecesarios como `medium_large`, `1536x1536` y `2048x2048`, sin eliminar todos los tamanos que WordPress o el tema pueden necesitar.

## Categorias

Editar:

```txt
data/input/category_map.csv
```

Formato:

```csv
wix_category,wix_category_id,wix_category_slug,wp_category_id,wp_category_name,wix_post_count,notes
Salta,0d80fe16-527a-4e1e-89e3-6a85d1dc22bd,salta,8,Salta,36959,
Policiales,e278b6f7-9daf-41e1-8498-7f1a812990cc,policiales,4,Policiales,30477,
```

El loader acepta el formato anterior y el nuevo. En el formato nuevo carga aliases por nombre, ID de Wix y slug, porque el CSV real normalizado guarda `category` como primer UUID de `categoryIds`.

Para generar un borrador desde el JSON de categorias exportado por Wix:

```bash
python -m src.main normalize-wix-categories --file data/input/wix_categories.json --output data/input/category_map_from_wix.csv --existing-map data/input/category_map.csv
```

Este comando no escribe en WordPress. Si una categoria no tiene `wp_category_id`, queda como pendiente, no se carga en SQLite y durante importacion se usara `DEFAULT_CATEGORY_ID` con warning.

## Autores

Editar:

```txt
data/input/author_map.csv
```

Formato de `data/input/author_map.csv`:

```csv
wix_member_id,wix_member_slug,wix_display_name,wp_user_id,wp_user_login,wp_display_name,is_default,notes
55a21023-...,holasalta,Equipo de HolaSalta!,ID_REAL_WP,equipo-holasalta,Equipo de HolaSalta,1,default_author
cab2b20b-...,nahimsalta,Fernando Nahim Mora,ID_REAL_WP,fernando-nahim-mora,Fernando Nahim Mora,0,
```

Primero listar usuarios WordPress por REST para obtener los IDs reales:

```bash
python -m src.main list-wordpress-users
```

Completar `wp_user_id` en `author_map.csv` con el ID numerico real (reemplazar `ID_REAL_WP`). Exactamente una fila debe tener `is_default=1`. Luego validar:

```bash
python -m src.main verify-authors
```

Si `wp_user_id` contiene placeholders como `ID_REAL_WP`, `verify-authors` devolvera `ok=false` y listara las filas invalidas.

El migrador extrae el autor Wix desde columnas `memberId`, `ownerId`, `authorId`, `createdBy`, `mostRecentContributorId`, `contactId`. Si no hay match, usa el autor default si `STOP_ON_UNMAPPED_AUTHOR=false`.

## Auditoria previa obligatoria

Ejecutar primero:

```bash
python -m src.main init-db
python -m src.main analyze-csv --file data/input/wix_posts_sample.csv
python -m src.main analyze-urls --file data/input/wix_posts_sample.csv
python -m src.main analyze-images --file data/input/wix_posts_sample.csv
python -m src.main verify-wordpress
python -m src.main verify-categories
python -m src.main verify-authors
python -m src.main verify-meta-support
python -m src.main dry-run --limit 10
```

Para el CSV real, reemplazar `wix_posts_sample.csv` por `wix_posts.csv`. Ese archivo esta ignorado por Git porque puede ser pesado o privado.

## Formato de contenido

El migrador formatea el cuerpo de cada noticia en parrafos HTML antes de crear el post. La estrategia con `CONTENT_FORMAT_MODE=richcontent_first`:

1. Intentar extraer parrafos reales del JSON `richContent` de Wix.
2. Si `richContent` devuelve 2 o mas parrafos, usarlos directamente.
3. Si devuelve 1 solo parrafo largo, aplicar la heuristica de oraciones para dividirlo (`richContent_split`).
4. Si `richContent` esta vacio o falla, aplicar la heuristica sobre `contentText` (`contentText_heuristic`).

La heuristica agrupa oraciones en parrafos de hasta `MAX_SENTENCES_PER_PARAGRAPH=3` oraciones o `MAX_PARAGRAPH_CHARS=700` caracteres, cortando antes de conectores narrativos como "Cerca de", "Segun", "Efectivos", "Ademas", etc.

Para verificar el formato antes de migrar:

```bash
python -m src.main preview-content-format --file data/input/noticias_0001.csv --limit 5
```

Muestra por cada post: titulo, extracto original, HTML generado, cantidad de parrafos y fuente usada (`richContent`, `richContent_split`, `contentText_heuristic` o `contentText`). Tambien genera `data/output/content_format_report.csv`.

## Dry-run

```bash
python -m src.main dry-run --limit 10
```

Hace:

- valida campos;
- normaliza slug;
- resuelve categoria y muestra `category_resolution` por post;
- resuelve autor y muestra `author_resolution` por post;
- predice cambios de URL por sanitizacion de slug en `url_prediction`;
- simula payload de post;
- simula plan de media;
- registra `dry_run_valid` si corresponde.

No sube imagenes, no crea posts y no modifica WordPress.

## Publicacion segura

Para publicar directamente:

```env
DEFAULT_POST_STATUS=publish
ALLOW_WORDPRESS_WRITES=true
CONFIRM_PUBLISH_MODE=true
```

Si `DEFAULT_POST_STATUS=publish` y `CONFIRM_PUBLISH_MODE=false`, `migrate` y `migrate-range` abortan con:

```txt
Publishing mode requires CONFIRM_PUBLISH_MODE=true
```

Para pruebas, usar:

```env
DEFAULT_POST_STATUS=draft
CONFIRM_PUBLISH_MODE=false
```

## Pruebas controladas

No empezar por 100.000 posts.

Plan recomendado:

```bash
python -m src.main migrate --limit 10
python -m src.main verify-import --limit 10
python -m src.main export-reports
```

Si todo esta correcto, repetir con 100, 1.000 y 10.000. Recien despues evaluar la corrida completa.

## Comandos disponibles

```bash
python -m src.main init-db
python -m src.main check-encoding --file data/input/noticias_0001.csv
python -m src.main preview-content-format --file data/input/noticias_0001.csv --limit 5
python -m src.main normalize-wix-csv --file data/input/noticias_0001.csv --output data/input/wix_posts_normalized_0001.csv
python -m src.main normalize-wix-categories --file data/input/wix_categories.json --output data/input/category_map_from_wix.csv --existing-map data/input/category_map.csv
python -m src.main analyze-csv --file data/input/wix_posts.csv
python -m src.main analyze-urls --file data/input/wix_posts.csv
python -m src.main analyze-images --file data/input/wix_posts.csv
python -m src.main test-image-download --url "https://static.wixstatic.com/media/..."
python -m src.main verify-wordpress
python -m src.main verify-categories
python -m src.main list-wordpress-users
python -m src.main verify-authors
python -m src.main verify-meta-support
python -m src.main test-meta-draft --dry-run
python -m src.main preflight-range --input-dir data/input --pattern "noticias_{num:04d}.csv" --start 1 --end 992 --sample-files 5
python -m src.main import-csv --file data/input/wix_posts.csv
python -m src.main dry-run --limit 10
python -m src.main migrate --limit 10
python -m src.main migrate --limit 100
python -m src.main migrate --batch-size 100
python -m src.main migrate-range --input-dir data/input --pattern "noticias_{num:04d}.csv" --start 1 --end 992 --run-id run_overnight_001
python -m src.main resume-run --run-id run_overnight_001
python -m src.main run-status --run-id run_overnight_001
python -m src.main export-run-report --run-id run_overnight_001
python -m src.main retry-failed
python -m src.main export-reports
python -m src.main scan-local-references
python -m src.main report-orphan-media
python -m src.main verify-import --limit 100
python -m src.main export-migration-manifest
python -m src.main scan-existing-wordpress --limit 100
python -m src.main cleanup-test-batch --batch test-001 --dry-run
```

`scan-existing-wordpress` solo lee posts existentes. `cleanup-test-batch` esta implementado como vista previa obligatoria con `--dry-run`; no borra posts ni media.

`preview-content-format`, `normalize-wix-csv`, `check-encoding`, `analyze-*`, `preflight-range`, `dry-run`, `verify-*`, `list-wordpress-users`, `run-status` y `export-run-report` no escriben en WordPress.

`migrate`, `migrate-range` y `retry-failed` son comandos de escritura y fallan de forma intencional mientras `ALLOW_WORDPRESS_WRITES=false`. Si publican posts, tambien exigen `CONFIRM_PUBLISH_MODE=true`.

`test-image-download` solo intenta descargar una imagen y validar el archivo local; no crea posts, no sube media y no usa WordPress REST API.

`normalize-wix-csv` adapta el CSV real exportado por Wix al schema canonico del migrador. Lee columnas como `id`, `contentText`, `richContent`, `firstPublishedDate`, `categoryIds`, `media`, `url.path`, `publicUrl` y `memberId`, y genera:

```txt
data/input/wix_posts_normalized_0001.csv
data/output/normalize_wix_csv_report.csv
data/output/encoding_report.csv
data/output/content_format_report.csv
```

El comando no escribe en WordPress. El CSV normalizado queda ignorado por Git porque puede contener datos reales. La salida canonica incluye `author_source_id`, `author_source_slug` y `author_resolution` para resolver autores despues contra `data/input/author_map.csv`.

`check-encoding` solo lee el archivo e informa encoding usado, patrones sospechosos y columnas afectadas. Si `analyze-csv` detecta mojibake en columnas criticas, devuelve `possible_mojibake_detected` y `migration_recommended=false`.

## Corrida nocturna

### Checklist obligatorio antes de migrate-range

Ejecutar en orden; todos deben pasar (o sus warnings ser aceptados) antes de encender escrituras:

```bash
# 1. Verificar conexion y permisos WordPress
python -m src.main verify-wordpress

# 2. Verificar que el plugin de meta esta activo y los campos funcionan
python -m src.main verify-meta-support

# 3. Verificar mapa de categorias
python -m src.main verify-categories

# 4. Verificar mapa de autores (wp_user_id debe ser numerico, no ID_REAL_WP)
python -m src.main verify-authors

# 5. Preflight sobre muestra de archivos: encoding, URLs, categorias, autores, estimacion de posts
python -m src.main preflight-range --input-dir data/input --pattern "noticias_{num:04d}.csv" --start 1 --end 992 --sample-files 5
```

Ninguno de estos comandos escribe en WordPress.

### Corrida reanudable

La corrida procesa `noticias_0001.csv` a `noticias_0992.csv` secuencialmente. Por cada archivo normaliza, analiza, importa a SQLite y migra. Guarda estado en SQLite y reportes en `data/output/runs/<run_id>/per_file/` y `global/`.

Si el proceso se interrumpe, correr el mismo comando o `resume-run` retoma desde donde quedo (los archivos con status `migrated` se saltean salvo `--force`).

```bash
python -m src.main migrate-range \
  --input-dir data/input \
  --pattern "noticias_{num:04d}.csv" \
  --start 1 --end 992 \
  --run-id run_overnight_001

# Reanudar si se interrumpe
python -m src.main resume-run --run-id run_overnight_001

# Ver estado de la corrida
python -m src.main run-status --run-id run_overnight_001

# Exportar reportes globales (posts, imagenes, errores, redirects, run_summary.json)
python -m src.main export-run-report --run-id run_overnight_001
```

`migrate-range` es comando de escritura. Falla si `ALLOW_WORDPRESS_WRITES=false`. Si `DEFAULT_POST_STATUS=publish`, falla tambien si `CONFIRM_PUBLISH_MODE=false`.

Al inicio de `migrate-range` se imprime un resumen de modo escritura con: write mode, post status, confirm publish, archivos esperados vs encontrados.

### Umbrales de seguridad nocturna

| Variable | Default | Efecto |
|---|---|---|
| `CREATE_POST_IF_IMAGE_FAILS=true` | true | Crea post aunque falle la imagen |
| `STOP_ON_UNMAPPED_CATEGORY=true` | true | Bloquea archivo si hay categorias sin mapeo |
| `STOP_ON_UNMAPPED_AUTHOR=false` | false | Usa default si autor sin mapeo (registra warning) |
| `MAX_POST_FAILURE_RATE=0.02` | 2% | Detiene corrida si posts fallan mas de 2% |
| `MAX_IMAGE_FAILURE_RATE=0.10` | 10% | Loguea warning fuerte si imagenes fallan mas de 10% |
| `STOP_ON_CRITICAL_URL_ERRORS=true` | true | Bloquea archivo si hay URLs invalidas |

## Reportes

Los reportes se generan en `data/output/` y estan ignorados por Git:

- `posts_report.csv`
- `images_report.csv`
- `url_report.csv`
- `pre_migration_url_risk_report.csv`
- `errors_report.csv`
- `redirect_candidates.csv`
- `content_format_report.csv`
- `internal_links_report.csv`
- `local_references_report.csv`
- `duplicate_posts_report.csv`
- `orphan_media_report.csv`
- `migration_manifest.json`

Los reportes de una corrida por rango quedan en `data/output/runs/<run_id>/global/`, incluyendo `run_summary.json`, `authors_report.csv`, `categories_report.csv` y `content_format_report.csv`.

## URLs y SEO

La estrategia principal es conservar `/post/%postname%/` para minimizar redirecciones.

El sistema compara paths, no dominios completos. Ignora diferencias entre `localhost` y produccion, y clasifica:

- `exact_match`
- `trailing_slash_only`
- `changed_by_wordpress`
- `path_structure_changed`
- `duplicate_slug_changed`
- `invalid_old_url`
- `missing_old_url`
- `error`

Si hay diferencias, genera `redirect_candidates.csv`. No conviene meter 100.000 redirecciones en `.htaccess`; si hacen falta, conviene una solucion por base de datos, plugin/API o reglas agrupadas.

## Imagenes

La estrategia es:

```txt
validar -> deduplicar -> normalizar solo si hace falta -> subir -> reutilizar media ID
```

No se recomprimen todas las imagenes. La deduplicacion se hace por URL y por hash para evitar subir la misma imagen varias veces.

## Local a Hostinger

Ver [docs/LOCAL_TO_HOSTINGER.md](docs/LOCAL_TO_HOSTINGER.md).

Resumen:

- WordPress vive en base MySQL + `wp-content`.
- Exportar base `.sql`.
- Copiar `wp-content/uploads`.
- Copiar tema hijo y plugins necesarios.
- Hacer search-replace compatible con datos serializados.
- Regenerar permalinks.
- Revisar reportes de URLs y referencias locales.

Antes de subir:

```bash
python -m src.main scan-local-references
python -m src.main export-migration-manifest
```

## GitHub y seguridad

El repositorio ignora:

- `.env`
- `db/*.sqlite`
- `data/images/*`
- `data/output/*`
- `logs/*`
- `*.sql`
- archivos comprimidos grandes
- credenciales y llaves

Comandos utiles:

```bash
git status
git log --oneline --decorate --max-count=10
git push origin main
```
