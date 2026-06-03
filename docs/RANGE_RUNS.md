# Corridas por rango

## Preflight sin escrituras

Antes de una corrida grande:

```bash
python -m src.main verify-wordpress
python -m src.main verify-meta-support
python -m src.main verify-categories
python -m src.main verify-authors
python -m src.main preflight-range --input-dir data/input --pattern "noticias_{num:04d}.csv" --start 1 --end 992 --sample-files 5
```

`preflight-range` no escribe en WordPress. Revisa archivos faltantes, encoding, columnas, URLs, imagenes, categorias, autores, duplicados estimados, espacio en disco y configuracion peligrosa.

## Ejecucion reanudable

```bash
python -m src.main migrate-range --input-dir data/input --pattern "noticias_{num:04d}.csv" --start 1 --end 992 --run-id run_overnight_001
```

`migrate-range` escribe en WordPress. Si `DEFAULT_POST_STATUS=publish`, exige:

```env
ALLOW_WORDPRESS_WRITES=true
CONFIRM_PUBLISH_MODE=true
```

Si la PC se apaga o el proceso falla, reanudar con:

```bash
python -m src.main resume-run --run-id run_overnight_001
```

Los archivos ya marcados como `migrated` se omiten salvo que se use `--force` en `migrate-range`.

## Estado y reportes

```bash
python -m src.main run-status --run-id run_overnight_001
python -m src.main export-run-report --run-id run_overnight_001
```

Los reportes quedan en:

```txt
data/output/runs/<run_id>/per_file/
data/output/runs/<run_id>/global/
```

Reportes globales principales:

```txt
posts_report.csv
images_report.csv
errors_report.csv
url_report.csv
redirect_candidates.csv
authors_report.csv
categories_report.csv
content_format_report.csv
run_summary.json
```

## Umbrales

La corrida aplica estas variables:

```env
CREATE_POST_IF_IMAGE_FAILS=true
MAX_POST_FAILURE_RATE=0.02
MAX_IMAGE_FAILURE_RATE=0.10
STOP_ON_CRITICAL_URL_ERRORS=true
STOP_ON_UNMAPPED_CATEGORY=true
STOP_ON_UNMAPPED_AUTHOR=false
```

Con `CREATE_POST_IF_IMAGE_FAILS=true`, una imagen fallida no bloquea el post, pero queda registrada. Las categorias sin mapear bloquean el archivo si `STOP_ON_UNMAPPED_CATEGORY=true`. Los autores sin mapear usan default si `STOP_ON_UNMAPPED_AUTHOR=false`.
