# Telegram eBook Bot

Bot de Telegram que busca libros de **dominio público** en Standard Ebooks,
Project Gutenberg (vía [Gutendex](https://gutendex.com)) e Internet Archive, y
manda el archivo directo en el chat en EPUB, Kindle o PDF (según lo que tenga
la fuente) — además de audiolibros de **LibriVox**, capítulo por capítulo. El
texto del bot está en inglés (público angloparlante).

Cuando el mismo libro aparece en más de una fuente, se muestra una sola vez —
la de mejor calidad (`Standard Ebooks > Gutenberg > Internet Archive`, ver
`SOURCE_PRIORITY` y `dedupe_by_title()` en `main.py`).

No aloja archivos: los botones de descarga apuntan siempre a la fuente original
(standardebooks.org / gutenberg.org / archive.org / archive.org vía LibriVox),
por lo que no hay problema de derechos de autor.

## Acceso pago (opcional)

El bot puede requerir un código de un solo uso antes de dejar buscar — pensado
para venderlo como producto (un ebook/guía que explica cómo activarlo). Hay
**dos funciones independientes**, cada una con sus propios códigos:

- `ebooks` — búsqueda de texto + `/popular`
- `audiobooks` — `/audiobook`

Activar con `REQUIRE_ACCESS_CODE=true` en el entorno (desactivado por defecto,
así el desarrollo local no pide código). Generar códigos con
`generate_codes(count, feature="ebooks"|"audiobooks")` desde un shell de
Python — no es un comando del bot, para que nadie se los pueda generar solo.
Ver `access.json` (no se sube a git) para el estado de códigos canjeados.

## Comandos

| Comando | Qué hace |
|---|---|
| `/start` | Mensaje de bienvenida (con botón de reset) |
| `/help` | Ayuda / soporte |
| `/popular` | Lista fija de 10 clásicos, descarga directa sin buscar (requiere código `ebooks`) |
| `/audiobook <título>` | Busca un audiolibro en LibriVox y lo entrega capítulo por capítulo (requiere código `audiobooks`, separado del de ebooks) |
| `/unlock <código>` | Canjea un código de acceso |
| `/reset` | Borra los últimos ~100 mensajes del chat y arranca de cero |

No hay comando `/search`: escribir el título del libro directo, sin comando,
dispara la búsqueda (requiere código `ebooks`).

## Puesta en marcha local

1. Creá el bot en Telegram hablando con [@BotFather](https://t.me/BotFather):
   `/newbot` → seguí los pasos → copiá el token (formato `123456789:ABC...`).
2. Instalá dependencias:

   ```bash
   pip install -r requirements.txt
   ```

3. Definí el token y corré el bot (modo polling, no necesita URL pública):

   ```bash
   # PowerShell
   $env:BOT_TOKEN = "tu_token_aca"
   python main.py
   ```

4. Buscá tu bot en Telegram, probá `/start`, y escribí `Pride and Prejudice`
   directo en el chat (sin comando) para probar la búsqueda.

## Deploy en Render (gratis, modo webhook)

1. Subí esta carpeta a un repo de GitHub.
2. En [Render](https://render.com) → **New Web Service** → conectá el repo.
   Render va a detectar `render.yaml` automáticamente.
3. Cargá las variables de entorno del servicio:
   - `BOT_TOKEN`: el token de @BotFather.
   - `WEBHOOK_URL`: la URL pública que Render te asigna, ej.
     `https://telegram-ebook-bot.onrender.com` (la sabés después del primer deploy;
     redeployá una vez que la tengas).
4. Listo — el bot queda corriendo 24/7 en el plan free de Render (con "sleep" tras
   inactividad, como cualquier free tier).

## Alcance legal

Solo se buscan y devuelven libros de dominio público / de descarga libre. La
búsqueda en Internet Archive excluye explícitamente los ítems marcados como
restringidos (`-access-restricted-item:true`), para no exponer libros de
préstamo digital controlado (copyright). No se buscan ni se entregan libros
con copyright vigente (bestsellers actuales, etc.) — eso excede el alcance
legal del bot.

## Nota de red (Windows / IPv6 roto)

Si corrés esto local y ves errores de conexión intermitentes hacia Gutenberg
o Archive.org, es probable que tu red tenga IPv6 mal configurado (anunciado
pero no enrutado). `main.py` ya fuerza resolución DNS solo IPv4 al arrancar
para evitar esto — no debería hacer falta tocar nada, pero si el problema
persiste, es la primera pista a revisar.

## Límite conocido — búsqueda de LibriVox

La API de LibriVox solo hace match por **prefijo literal** del título (no
"contiene"), y su catálogo a veces omite el artículo inicial — el título real
de "Sherlock Holmes" ahí es *"Adventures of Sherlock Holmes"*, sin el "The".
`search_librivox()` reintenta sacando un "The"/"A"/"An" inicial de la
búsqueda del usuario, pero si el título completo difiere más que eso, no lo
va a encontrar. Es una limitación real de la fuente, no un bug — LibriVox no
tiene una búsqueda de texto completo funcional (el parámetro `search` de su
API está roto: devuelve siempre los mismos resultados sin importar la
consulta, verificado directamente).

## Extender

- Agregar otra fuente: seguir el patrón de `search_gutenberg` / `search_archive`
  / `search_standard_ebooks` en `main.py` — cada una devuelve una lista de
  dicts con `source`, `title`, `author` y `formats` (lista de tuplas
  `(label, url, extensión)`), o `identifier` si el libro necesita resolución
  diferida (como Internet Archive).
- Agregar un mensaje promocional de tu propio producto en `WELCOME`/`ABOUT` si
  querés usar el bot como canal de marketing.
