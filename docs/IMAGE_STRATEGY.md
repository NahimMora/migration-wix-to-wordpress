# Estrategia de imagenes

## Premisa

Muchas imagenes ya estan optimizadas y pesan menos de 50 KB. El problema principal no es el peso individual, sino:

- duplicacion;
- cantidad de archivos;
- inodes;
- tamanos intermedios de WordPress;
- backups;
- tiempo de importacion;
- trazabilidad.

## Flujo

```txt
validar -> deduplicar -> normalizar solo si hace falta -> subir -> reutilizar media ID
```

## Deduplicacion

Primero se revisa `source_image_url`. Si ya existe con `wp_media_id`, se reutiliza.

Si no existe:

1. descargar;
2. calcular hash original;
3. inspeccionar dimensiones y MIME;
4. normalizar solo si corresponde;
5. calcular hash final;
6. buscar media ya subida con ese hash;
7. subir solo si es unica.

## Normalizacion condicional

Se normaliza si:

- supera `MAX_IMAGE_WIDTH`;
- supera `MAX_IMAGE_FILESIZE_KB`;
- tiene MIME incompatible;
- esta corrupta;
- requiere correccion EXIF;
- `RECOMPRESS_ALL_IMAGES=true`.

Por defecto:

```env
NORMALIZE_ONLY_IF_NEEDED=true
RECOMPRESS_ALL_IMAGES=false
```

## WordPress

El plugin auxiliar elimina tamanos intermedios innecesarios como `medium_large`, `1536x1536` y `2048x2048`, pero deja tamanos normales para el tema.

## Fallos

Si falla la imagen:

- con `CREATE_POST_IF_IMAGE_FAILS=true`, el post puede crearse sin featured image y se registra error;
- con `false`, el post queda `failed`.
