# Riesgos SEO

## URLs

Cambiar paths puede provocar perdida de trafico organico. La primera defensa es configurar WordPress con:

```txt
/post/%postname%/
```

El migrador compara `old_path` contra `new_path`, no dominio completo. Esto evita falsos positivos entre local y produccion.

## Trailing slash

Una diferencia solo por slash final se clasifica como `trailing_slash_only`. Debe revisarse, pero suele ser menos critica si WordPress y canonical quedan consistentes.

## Slugs

WordPress puede modificar slugs por caracteres invalidos, sanitizacion o duplicados. Esos casos se clasifican como `changed_by_wordpress` y deben revisarse en `url_report.csv`.

## Redirecciones

No conviene generar 100.000 redirecciones manuales en `.htaccess`. Si hacen falta muchas, usar una solucion por base de datos, plugin/API o reglas agrupadas.

## Canonical y sitemap

Despues de migrar una muestra, revisar:

- canonical final;
- sitemap;
- indexabilidad;
- status HTTP 200;
- ausencia de noindex accidental.

## Contenido

Contenido vacio, HTML roto o scripts removidos pueden afectar calidad SEO. Revisar warnings de CSV y reportes de contenido.

## Imagen destacada

Si una imagen falla y `CREATE_POST_IF_IMAGE_FAILS=true`, el post puede crearse sin featured image. Revisar `errors_report.csv` y `images_report.csv`.

## Categorias

Categorias mal mapeadas cambian taxonomia y navegacion interna. Ejecutar `verify-categories` antes de migrar.
