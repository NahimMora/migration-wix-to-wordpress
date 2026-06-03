# Plan de pruebas

## Criterios para avanzar

Avanzar de escala solo si:

- el CSV real de Wix fue normalizado con `normalize-wix-csv`;
- `verify-wordpress` pasa;
- `verify-categories` pasa o los warnings estan aceptados;
- `dry-run` no muestra errores criticos;
- URLs principales quedan `exact_match` o `trailing_slash_only`;
- no hay duplicados inesperados;
- errores de imagen estan explicados;
- no hay referencias locales antes de preparar Hostinger.

## Prueba 10

Si se parte de un export real de Wix:

```bash
python -m src.main normalize-wix-csv --file data/input/noticias_0001.csv --output data/input/wix_posts_normalized_0001.csv
python -m src.main analyze-csv --file data/input/wix_posts_normalized_0001.csv
python -m src.main analyze-urls --file data/input/wix_posts_normalized_0001.csv
```

Antes de migrar una muestra, probar 2 o 3 URLs reales de imagen:

```bash
python -m src.main test-image-download --url "https://static.wixstatic.com/media/..."
```

```bash
python -m src.main dry-run --limit 10
python -m src.main migrate --limit 10
python -m src.main verify-import --limit 10
python -m src.main export-reports
```

Revisar manualmente los 10 posts en WordPress.

## Prueba 100

```bash
python -m src.main migrate --limit 100
python -m src.main verify-import --limit 100
python -m src.main export-reports
```

Revisar categorias, imagenes destacadas, fechas, slugs y links internos.

## Prueba 1.000

Ejecutar despues de corregir problemas de 100. Medir tiempos, errores recurrentes y consumo de disco.

## Prueba 10.000

Validar que SQLite permite reanudar, que no duplica posts y que la deduplicacion de media baja subidas repetidas.

## Prueba completa

Solo despues de estabilizar reportes y revisar riesgos SEO.
