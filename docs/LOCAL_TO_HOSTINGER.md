# Plan local a Hostinger

## Que vive en local

Un WordPress migrado vive principalmente en:

- base de datos MySQL de WordPress;
- `wp-content/uploads`;
- tema hijo;
- plugins necesarios;
- plugin auxiliar de migracion si se quiere mantener meta REST.

No queda atrapado en la computadora: se puede mover copiando base y archivos.

## Exportar desde local

1. Exportar base MySQL como `.sql`.
2. Copiar `wp-content/uploads`.
3. Copiar tema hijo.
4. Copiar plugins necesarios.
5. Guardar version de WordPress y PHP usada.
6. Exportar reportes del migrador:

```bash
python -m src.main scan-local-references
python -m src.main export-migration-manifest
```

## Subir a Hostinger

1. Preparar hosting o WordPress limpio.
2. Subir `wp-content/uploads`.
3. Subir tema hijo.
4. Subir plugins necesarios.
5. Importar base MySQL.
6. Ajustar `wp-config.php` con credenciales de Hostinger.
7. Ejecutar search-replace seguro de URL local a produccion.
8. Regenerar permalinks desde `Ajustes -> Enlaces permanentes`.
9. Revisar home, categorias, posts, imagenes y sitemap.
10. Revisar reportes de URL antes de apuntar DNS.

## Search-replace

No hacer search-replace bruto si hay datos serializados. Alternativas sin WP-CLI:

- script PHP que respete serializacion;
- herramienta compatible con datos serializados;
- plugin de search-replace solo para ese paso si se acepta;
- script SQL solo si entiende y preserva serializacion.

## Referencias locales

Buscar:

- `localhost`;
- `.local`;
- `127.0.0.1`;
- rutas Windows tipo `C:\`.

Comando:

```bash
python -m src.main scan-local-references
```

Reporte:

```txt
data/output/local_references_report.csv
```

## Manifest

```bash
python -m src.main export-migration-manifest
```

Incluye:

- posts migrados;
- posts fallidos;
- imagenes unicas;
- imagenes reutilizadas;
- tamano local de imagenes;
- estados de URL;
- referencias locales encontradas;
- checklist para Hostinger.
