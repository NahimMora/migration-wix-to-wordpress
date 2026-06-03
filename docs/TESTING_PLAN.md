# Plan de pruebas

## Criterios para avanzar

Avanzar de escala solo si:

- `verify-wordpress` pasa;
- `verify-categories` pasa o los warnings estan aceptados;
- `dry-run` no muestra errores criticos;
- URLs principales quedan `exact_match` o `trailing_slash_only`;
- no hay duplicados inesperados;
- errores de imagen estan explicados;
- no hay referencias locales antes de preparar Hostinger.

## Prueba 10

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
