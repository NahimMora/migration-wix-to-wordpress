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
WP_BASE_URL=http://holasalta.local
WP_USERNAME=admin
WP_APP_PASSWORD=xxxx xxxx xxxx xxxx
DEFAULT_POST_STATUS=draft
ALLOW_WORDPRESS_WRITES=false
BATCH_SIZE=100
PERMALINK_STRUCTURE=/post/%postname%/
NORMALIZE_IMAGES=true
NORMALIZE_ONLY_IF_NEEDED=true
RECOMPRESS_ALL_IMAGES=false
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
3. Verificar que los meta fields `_wix_id`, `_wix_old_url` y `_migration_batch` esten disponibles por REST.

El plugin tambien reduce tamanos intermedios innecesarios como `medium_large`, `1536x1536` y `2048x2048`, sin eliminar todos los tamanos que WordPress o el tema pueden necesitar.

## Categorias

Editar:

```txt
data/input/category_map.csv
```

Formato:

```csv
wix_category,wp_category_id,wp_category_name
Policiales,4,Policiales
Deportes,5,Deportes
```

Si una categoria no esta mapeada, el sistema usa `DEFAULT_CATEGORY_ID`, registra warning y continua.

## Auditoria previa obligatoria

Ejecutar primero:

```bash
python -m src.main init-db
python -m src.main analyze-csv --file data/input/wix_posts_sample.csv
python -m src.main analyze-urls --file data/input/wix_posts_sample.csv
python -m src.main analyze-images --file data/input/wix_posts_sample.csv
python -m src.main verify-wordpress
python -m src.main verify-categories
python -m src.main dry-run --limit 10
```

Para el CSV real, reemplazar `wix_posts_sample.csv` por `wix_posts.csv`. Ese archivo esta ignorado por Git porque puede ser pesado o privado.

## Dry-run

```bash
python -m src.main dry-run --limit 10
```

Hace:

- valida campos;
- normaliza slug;
- resuelve categoria;
- simula payload de post;
- simula plan de media;
- registra `dry_run_valid` si corresponde.

No sube imagenes, no crea posts y no modifica WordPress.

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
python -m src.main analyze-csv --file data/input/wix_posts.csv
python -m src.main analyze-urls --file data/input/wix_posts.csv
python -m src.main analyze-images --file data/input/wix_posts.csv
python -m src.main verify-wordpress
python -m src.main verify-categories
python -m src.main import-csv --file data/input/wix_posts.csv
python -m src.main dry-run --limit 10
python -m src.main migrate --limit 10
python -m src.main migrate --limit 100
python -m src.main migrate --batch-size 100
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

`migrate` y `retry-failed` son comandos de escritura y fallan de forma intencional mientras `ALLOW_WORDPRESS_WRITES=false`.

## Reportes

Los reportes se generan en `data/output/` y estan ignorados por Git:

- `posts_report.csv`
- `images_report.csv`
- `url_report.csv`
- `errors_report.csv`
- `redirect_candidates.csv`
- `internal_links_report.csv`
- `local_references_report.csv`
- `duplicate_posts_report.csv`
- `orphan_media_report.csv`
- `migration_manifest.json`

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
